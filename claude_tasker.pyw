# -*- coding: utf-8 -*-
"""
Claude Tasker — schedule heavy Claude Code CLI prompts to run while you sleep.

The idea: your session (5h) limit resets on a rolling window. Instead of letting
a fresh window go unused overnight, queue heavy prompts that fire right when the
limit resets, so the new window gets used (and "recharges") while you sleep.

For each account it sets CLAUDE_CONFIG_DIR, runs `claude -p` headless, and logs
each run. Live limit info comes from the same OAuth endpoints Claude Code itself
uses, read straight from each account's local credentials — so it works on its
own with nothing else installed.

Optional: if you also run the Claude Usage Widget
(https://github.com/PanLipton/claude-usage-widget), point Settings at its folder
and Tasker will reuse the numbers it already polled instead of making its own
API calls.

Pure standard library: tkinter + urllib + subprocess + threading. No deps.
"""

import json
import os
import shutil
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
TASKS_PATH = os.path.join(HERE, "tasks.json")
SETTINGS_PATH = os.path.join(HERE, "settings.json")
LOGS_DIR = os.path.join(HERE, "logs")

# claude.exe is normally on PATH; this is only a last-resort fallback location.
FALLBACK_CLAUDE = os.path.join(os.path.expanduser("~"), ".local", "bin", "claude.exe")

# OAuth endpoints Claude Code itself uses (public client — no secrets here).
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
USER_AGENT = "claude-cli/2.0.0 (external, cli)"
REFRESH_SKEW_SEC = 120          # refresh a token this long before it expires
USAGE_POLL_DEFAULT = 180        # min seconds between direct usage fetches/account
WIDGET_FRESH_SEC = 600          # trust widget_usage.json if updated within this
_SSL_CTX = ssl.create_default_context()

# Windows process creation flags
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200

POLL_SECONDS = 3  # scheduler tick

# Palette (dark, matching the widget's mood)
C_BG = "#16161a"
C_PANEL = "#1b1b1f"
C_BORDER = "#2c2c33"
C_ROW = "#1f1f24"
C_ROW_ALT = "#1b1b20"
C_TEXT = "#e7e7ea"
C_MUTED = "#85858d"
C_FAINT = "#5c5c64"
C_ACCENT = "#58a6ff"
C_OK = "#3fb950"
C_WARN = "#d29922"
C_CRIT = "#f85149"
C_BTNBG = "#232329"
C_BTNBG_HOVER = "#30303a"
C_FIELD = "#202026"
FONT = "Segoe UI"
MONO = "Consolas"

ACCENTS = ["#58a6ff", "#bc8cff", "#56d4c4", "#f0a868"]

PERM_MODES = [
    ("Bypass all (autonomous)", "bypass"),
    ("Accept edits only", "acceptEdits"),
    ("Default (ask — not for unattended)", "default"),
]
PERM_LABEL = {v: k for k, v in PERM_MODES}
MODELS = ["(default)", "opus", "sonnet", "haiku"]
EFFORT = ["(default)", "low", "medium", "high", "xhigh", "max"]

SCHED_TYPES = [
    ("As soon as armed", "now"),
    ("At a specific time", "at"),
    ("When session limit resets", "session_reset"),
    ("After the previous task", "after_prev"),
]
SCHED_LABEL = {v: k for k, v in SCHED_TYPES}

# Only these task fields are persisted to tasks.json.
SERIAL_FIELDS = (
    "id", "name", "account", "cwd", "prompt", "perm", "model", "effort",
    "sched_type", "at", "reset_account", "offset_min",
    "repeat", "repeat_hours", "repeat_until",
    "status", "created_at", "started_at", "ended_at", "exit_code",
    "not_before", "log_file",
)


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------
def expand(p):
    return os.path.expandvars(os.path.expanduser(p or ""))


def atomic_write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def default_settings():
    return {
        "widget_dir": "",          # optional Claude Usage Widget folder
        "claude_bin": "claude",
        "max_concurrent": 1,
        "usage_poll_seconds": USAGE_POLL_DEFAULT,
        "default_account": "",
        "default_perm": "bypass",
        "default_model": "",
        "accounts": [],            # [{label, config_dir, email?}]
    }


def load_settings():
    s = default_settings()
    s.update(load_json(SETTINGS_PATH, {}))
    if not s.get("accounts"):
        s["accounts"] = discover_accounts()
    return s


def save_settings(s):
    atomic_write(SETTINGS_PATH, json.dumps(s, indent=2))


def discover_accounts():
    """Best-effort first-run seed: scan ~ for Claude config dirs.

    `.claude` -> 'claude', `.claude-accountN` -> 'claudeN'. Falls back to a
    single default account so the app is always usable out of the box.
    """
    home = os.path.expanduser("~")
    found = []
    try:
        for name in sorted(os.listdir(home)):
            if name == ".claude":
                found.append(("claude", os.path.join(home, name)))
            elif name.startswith(".claude-account"):
                suffix = name[len(".claude-account"):]
                label = "claude" + suffix if suffix else "claude"
                found.append((label, os.path.join(home, name)))
    except OSError:
        pass
    if not found:
        found = [("claude", os.path.join(home, ".claude"))]
    return [{"label": lbl, "config_dir": path} for lbl, path in found]


# -- OAuth usage fetching (standalone; mirrors the Claude Usage Widget) ------
def _http_json(url, method="GET", token=None, body=None, timeout=20):
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
        headers["anthropic-beta"] = "oauth-2025-04-20"
        headers["anthropic-version"] = "2023-06-01"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
        return json.loads(r.read().decode())


def read_credentials(config_dir):
    path = os.path.join(expand(config_dir), ".credentials.json")
    with open(path, "r", encoding="utf-8") as f:
        return path, json.load(f)


def refresh_token(cred_path, cred_data):
    """Refresh the OAuth token and write it back to .credentials.json."""
    oauth = cred_data["claudeAiOauth"]
    resp = _http_json(TOKEN_URL, method="POST", body={
        "grant_type": "refresh_token",
        "refresh_token": oauth["refreshToken"],
        "client_id": CLIENT_ID,
    })
    oauth["accessToken"] = resp["access_token"]
    oauth["refreshToken"] = resp.get("refresh_token", oauth["refreshToken"])
    oauth["expiresAt"] = int(time.time() * 1000) + int(resp.get("expires_in", 0)) * 1000
    atomic_write(cred_path, json.dumps(cred_data))
    return oauth["accessToken"]


def get_token(config_dir, force=False):
    cred_path, cred_data = read_credentials(config_dir)
    oauth = cred_data["claudeAiOauth"]
    expires_at = oauth.get("expiresAt", 0) / 1000.0
    if force or time.time() >= expires_at - REFRESH_SKEW_SEC:
        return refresh_token(cred_path, cred_data)
    return oauth["accessToken"]


def parse_reset(iso):
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def fetch_account_direct(account, prev=None):
    """Live usage for one account via the OAuth endpoints -> usage dict."""
    prev = prev or {}
    out = {"five": prev.get("five"), "seven": prev.get("seven"),
           "email": prev.get("email"), "error": None,
           "updated_at": prev.get("updated_at")}
    try:
        token = get_token(account["config_dir"])
        try:
            usage = _http_json(USAGE_URL, token=token)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                token = get_token(account["config_dir"], force=True)
                usage = _http_json(USAGE_URL, token=token)
            else:
                raise
        five = usage.get("five_hour") or {}
        seven = usage.get("seven_day") or {}
        out["five"] = {"util": float(five.get("utilization") or 0.0),
                       "reset": parse_reset(five.get("resets_at"))}
        out["seven"] = {"util": float(seven.get("utilization") or 0.0),
                        "reset": parse_reset(seven.get("resets_at"))}
        out["updated_at"] = time.time()
        if not out["email"]:
            try:
                prof = _http_json(PROFILE_URL, token=token)
                out["email"] = (prof.get("account") or {}).get("email")
            except Exception:
                pass
    except FileNotFoundError:
        out["error"] = "no credentials"
    except urllib.error.HTTPError as e:
        out["error"] = "rate limited" if e.code == 429 else "HTTP %s" % e.code
    except urllib.error.URLError:
        out["error"] = "offline"
    except Exception as e:
        out["error"] = type(e).__name__
    return out


def read_widget_usage(widget_dir):
    """config_dir(expanded) -> usage dict, from a fresh widget_usage.json.

    Returns {} when no widget folder is configured or the snapshot is stale.
    """
    if not widget_dir:
        return {}
    data = load_json(os.path.join(widget_dir, "widget_usage.json"), {})
    updated = data.get("updated_at")
    if not updated or (time.time() - updated) > WIDGET_FRESH_SEC:
        return {}
    out = {}
    for a in data.get("accounts", []):
        key = a.get("config_dir_expanded") or expand(a.get("config_dir", ""))
        out[os.path.normcase(key)] = {
            "five": a.get("five") or {},
            "seven": a.get("seven") or {},
            "email": a.get("email"),
            "error": a.get("error"),
            "updated_at": updated,
        }
    return out


# ---------------------------------------------------------------------------
# Time formatting
# ---------------------------------------------------------------------------
def fmt_countdown(epoch):
    if not epoch:
        return "--"
    secs = int(epoch - time.time())
    if secs <= 0:
        return "now"
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    if d > 0:
        return "%dd %02dh" % (d, h)
    if h > 0:
        return "%dh %02dm" % (h, m)
    return "%dm" % m


def fmt_clock(epoch):
    if not epoch:
        return "--"
    return datetime.fromtimestamp(epoch).strftime("%a %H:%M")


def parse_at(date_s, time_s):
    """Parse 'YYYY-MM-DD' + 'HH:MM' (local) into an epoch, or None."""
    try:
        dt = datetime.strptime(date_s.strip() + " " + time_s.strip(), "%Y-%m-%d %H:%M")
        return dt.timestamp()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Core engine: tasks + scheduler + runner
# ---------------------------------------------------------------------------
class Engine:
    def __init__(self):
        self.settings = load_settings()
        self.accounts = self.settings.get("accounts", [])
        self.tasks = self._load_tasks()
        self.usage = {}
        self.armed = False
        self.lock = threading.RLock()
        self.runtime = {}        # task id -> {"proc": Popen}
        self._usage_next = {}    # config_dir(norm) -> next direct-fetch epoch
        self._stop = threading.Event()
        os.makedirs(LOGS_DIR, exist_ok=True)
        save_settings(self.settings)  # persist first-run seeded accounts
        self.worker = threading.Thread(target=self._loop, daemon=True)
        self.worker.start()

    # -- accounts -----------------------------------------------------------
    def account_labels(self):
        return [a["label"] for a in self.accounts]

    def config_dir_for(self, label):
        for a in self.accounts:
            if a["label"] == label:
                return expand(a["config_dir"])
        return None

    def set_accounts(self, accounts):
        with self.lock:
            self.accounts = accounts
            self.settings["accounts"] = accounts
            self._usage_next = {}  # force a fresh fetch on the next tick
        save_settings(self.settings)

    # -- usage (widget snapshot if fresh, else direct OAuth) ----------------
    def refresh_usage(self):
        try:
            widget = read_widget_usage(self.settings.get("widget_dir"))
        except Exception:
            widget = {}
        poll = max(60, int(self.settings.get("usage_poll_seconds", USAGE_POLL_DEFAULT)))
        now = time.time()
        new_usage = dict(self.usage)
        for a in list(self.accounts):
            label = a.get("label")
            cdir = expand(a.get("config_dir", ""))
            key = os.path.normcase(cdir)
            if key in widget:                       # free — widget already polled
                new_usage[label] = widget[key]
                if widget[key].get("email") and not a.get("email"):
                    a["email"] = widget[key]["email"]
                continue
            if now < self._usage_next.get(key, 0) and label in new_usage:
                continue
            res = fetch_account_direct({"config_dir": cdir}, new_usage.get(label))
            new_usage[label] = res
            if res.get("email") and not a.get("email"):
                a["email"] = res["email"]
            self._usage_next[key] = now + poll
        self.usage = new_usage

    def session_reset_epoch(self, label):
        return (self.usage.get(label, {}).get("five") or {}).get("reset")

    # -- persistence --------------------------------------------------------
    def _load_tasks(self):
        raw = load_json(TASKS_PATH, [])
        tasks = []
        for t in raw if isinstance(raw, list) else []:
            # never resurrect a "running" status from a previous crash
            if t.get("status") == "running":
                t["status"] = "failed"
                t["exit_code"] = -1
            tasks.append(t)
        return tasks

    def save_tasks(self):
        with self.lock:
            data = [{k: t.get(k) for k in SERIAL_FIELDS} for t in self.tasks]
        atomic_write(TASKS_PATH, json.dumps(data, indent=2))

    # -- task CRUD ----------------------------------------------------------
    def add_task(self, task):
        with self.lock:
            self.tasks.append(task)
        self.save_tasks()

    def update_task(self, task):
        self.save_tasks()

    def delete_task(self, tid):
        self.stop_task(tid)
        with self.lock:
            self.tasks = [t for t in self.tasks if t["id"] != tid]
        self.save_tasks()

    def get_task(self, tid):
        with self.lock:
            for t in self.tasks:
                if t["id"] == tid:
                    return t
        return None

    def move_task(self, tid, delta):
        with self.lock:
            idx = next((i for i, t in enumerate(self.tasks) if t["id"] == tid), None)
            if idx is None:
                return
            j = idx + delta
            if 0 <= j < len(self.tasks):
                self.tasks[idx], self.tasks[j] = self.tasks[j], self.tasks[idx]
        self.save_tasks()

    def reset_task(self, tid):
        """Put a finished/failed task back into the pending queue."""
        t = self.get_task(tid)
        if not t or t.get("status") == "running":
            return
        with self.lock:
            t["status"] = "pending"
            t["started_at"] = None
            t["ended_at"] = None
            t["exit_code"] = None
            t["not_before"] = None
        self.save_tasks()

    # -- scheduling ---------------------------------------------------------
    def fire_time(self, task):
        """Epoch when this task becomes eligible, or None if not yet known."""
        st = task.get("sched_type", "now")
        nb = task.get("not_before") or 0
        offset = float(task.get("offset_min") or 0) * 60.0
        if st == "now":
            base = task.get("created_at", 0)
        elif st == "at":
            base = task.get("at")
        elif st == "session_reset":
            base = self.session_reset_epoch(task.get("reset_account"))
            if base:
                base = base + offset
        elif st == "after_prev":
            prev = self._prev_task(task)
            if prev is None:
                base = task.get("created_at", 0)  # nothing before it -> asap
            elif prev.get("status") in ("done", "failed") and prev.get("ended_at"):
                base = prev["ended_at"] + offset
            else:
                return None  # predecessor not finished yet
        else:
            base = task.get("created_at", 0)
        if base is None:
            return None
        return max(base, nb)

    def _prev_task(self, task):
        with self.lock:
            idx = next((i for i, t in enumerate(self.tasks)
                        if t["id"] == task["id"]), None)
            if idx is None or idx == 0:
                return None
            return self.tasks[idx - 1]

    def when_text(self, task):
        """Human summary of when a task will run, for the table."""
        status = task.get("status")
        if status == "running":
            return "running…"
        if status == "done":
            return "done · " + fmt_clock(task.get("ended_at"))
        if status == "failed":
            return "failed · rc=%s" % task.get("exit_code")
        ft = self.fire_time(task)
        if ft is None:
            return "waiting on previous"
        if not self.armed:
            return "%s (paused)" % fmt_clock(ft)
        delta = ft - time.time()
        if delta <= 0:
            return "starting…"
        return "in %s · %s" % (fmt_countdown(ft), fmt_clock(ft))

    # -- the scheduler loop -------------------------------------------------
    def _loop(self):
        last_usage = 0
        while not self._stop.is_set():
            if time.time() - last_usage > 30:
                self.refresh_usage()
                last_usage = time.time()
            if self.armed:
                self._tick()
            self._stop.wait(POLL_SECONDS)

    def _tick(self):
        now = time.time()
        running = sum(1 for t in self._snapshot() if t.get("status") == "running")
        max_conc = max(1, int(self.settings.get("max_concurrent", 1)))
        ready = []
        for t in self._snapshot():
            if t.get("status") not in ("pending", "scheduled"):
                continue
            ft = self.fire_time(t)
            if ft is not None and ft <= now:
                ready.append((ft, t))
        ready.sort(key=lambda x: x[0])
        for _, t in ready:
            if running >= max_conc:
                break
            self._start(t)
            running += 1

    def _snapshot(self):
        with self.lock:
            return list(self.tasks)

    # -- running a task -----------------------------------------------------
    def _start(self, task):
        with self.lock:
            if task.get("status") == "running":
                return
            task["status"] = "running"
            task["started_at"] = time.time()
            task["ended_at"] = None
            task["exit_code"] = None
        self.save_tasks()
        threading.Thread(target=self._run, args=(task,), daemon=True).start()

    def _run(self, task):
        log_path = os.path.join(LOGS_DIR, "%s.log" % task["id"])
        task["log_file"] = log_path
        cdir = self.config_dir_for(task.get("account"))
        cwd = expand(task.get("cwd"))
        bin_path = shutil.which(self.settings.get("claude_bin", "claude")) \
            or self.settings.get("claude_bin") or "claude"
        if not os.path.isabs(bin_path) and not shutil.which(bin_path):
            if os.path.exists(FALLBACK_CLAUDE):
                bin_path = FALLBACK_CLAUDE

        cmd = [bin_path, "-p", task.get("prompt", "")]
        perm = task.get("perm", "bypass")
        if perm == "bypass":
            cmd.append("--dangerously-skip-permissions")
        elif perm == "acceptEdits":
            cmd += ["--permission-mode", "acceptEdits"]
        model = (task.get("model") or "").strip()
        if model and model != "(default)":
            cmd += ["--model", model]
        effort = (task.get("effort") or "").strip()
        if effort and effort != "(default)":
            cmd += ["--effort", effort]

        env = os.environ.copy()
        if cdir:
            env["CLAUDE_CONFIG_DIR"] = cdir

        rc = -1
        try:
            with open(log_path, "w", encoding="utf-8", errors="replace") as logf:
                logf.write("=" * 70 + "\n")
                logf.write("Claude Tasker run\n")
                logf.write("Task     : %s\n" % task.get("name"))
                logf.write("Account  : %s  (CLAUDE_CONFIG_DIR=%s)\n" % (task.get("account"), cdir))
                logf.write("Dir      : %s\n" % cwd)
                logf.write("Perm     : %s   Model: %s   Effort: %s\n" % (
                    perm, model or "default", effort or "default"))
                logf.write("Started  : %s\n" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                logf.write("Command  : %s\n" % " ".join(
                    ('"%s"' % c if " " in c else c) for c in cmd[:2] + ["<prompt>"] + cmd[3:]))
                logf.write("-" * 70 + "\n\n")
                logf.flush()
                if not cwd or not os.path.isdir(cwd):
                    logf.write("ERROR: working directory does not exist: %r\n" % cwd)
                    raise FileNotFoundError(cwd)
                proc = subprocess.Popen(
                    cmd, cwd=cwd, env=env, stdout=logf, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
                )
                with self.lock:
                    self.runtime[task["id"]] = {"proc": proc}
                rc = proc.wait()
                logf.write("\n" + "-" * 70 + "\n")
                logf.write("Exit code: %s   Ended: %s\n" % (
                    rc, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        except Exception as e:
            try:
                with open(log_path, "a", encoding="utf-8", errors="replace") as logf:
                    logf.write("\n[runner] exception: %r\n" % e)
            except Exception:
                pass
            rc = -1
        finally:
            with self.lock:
                self.runtime.pop(task["id"], None)
                task["ended_at"] = time.time()
                task["exit_code"] = rc
                task["status"] = "done" if rc == 0 else "failed"
            self._reschedule_if_repeat(task)
            self.save_tasks()

    def _reschedule_if_repeat(self, task):
        if not task.get("repeat"):
            return
        # stop after repeat_until (HH:MM today/tomorrow) if given
        until = (task.get("repeat_until") or "").strip()
        if until:
            try:
                hh, mm = [int(x) for x in until.split(":")]
                now_dt = datetime.now()
                stop_dt = now_dt.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if stop_dt < now_dt - timedelta(hours=12):
                    stop_dt += timedelta(days=1)
                if time.time() >= stop_dt.timestamp():
                    return
            except Exception:
                pass
        hours = float(task.get("repeat_hours") or 0)
        with self.lock:
            task["status"] = "scheduled"
            task["not_before"] = time.time() + (hours * 3600 if hours > 0 else 0)
            if task.get("sched_type") == "at" and hours > 0 and task.get("at"):
                task["at"] = task["at"] + hours * 3600

    # -- stopping -----------------------------------------------------------
    def stop_task(self, tid):
        with self.lock:
            rt = self.runtime.get(tid)
        if not rt:
            return
        proc = rt.get("proc")
        if proc and proc.poll() is None:
            try:
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               creationflags=CREATE_NO_WINDOW,
                               capture_output=True)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def shutdown(self):
        self._stop.set()


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------
def hover_button(parent, text, command, fg=C_TEXT, bg=C_BTNBG, width=None):
    b = tk.Label(parent, text=text, bg=bg, fg=fg, font=(FONT, 9, "bold"),
                 padx=12, pady=6, cursor="hand2")
    if width:
        b.configure(width=width)
    b.bind("<Enter>", lambda e: b.configure(bg=C_BTNBG_HOVER))
    b.bind("<Leave>", lambda e: b.configure(bg=bg))
    b.bind("<Button-1>", lambda e: command())
    b._basebg = bg
    return b


def field_entry(parent, default="", width=40):
    wrap = tk.Frame(parent, bg=C_BORDER)
    ent = tk.Entry(wrap, bg=C_FIELD, fg=C_TEXT, insertbackground=C_TEXT,
                   relief="flat", font=(FONT, 10), width=width)
    ent.pack(fill="x", padx=1, pady=1, ipady=4, ipadx=4)
    if default:
        ent.insert(0, default)
    return wrap, ent


# ---------------------------------------------------------------------------
# Task editor dialog
# ---------------------------------------------------------------------------
class TaskDialog:
    def __init__(self, app, task=None):
        self.app = app
        self.engine = app.engine
        self.result = None
        self.task = task
        win = tk.Toplevel(app.root)
        self.win = win
        win.title("Edit task" if task else "New task")
        win.configure(bg=C_PANEL)
        win.transient(app.root)
        win.grab_set()
        win.geometry("640x740")
        win.minsize(560, 560)

        # Button bar is anchored to the bottom of the window FIRST, so the
        # expanding prompt box can never push Save/Cancel off-screen.
        self.btnbar = tk.Frame(win, bg=C_PANEL)
        self.btnbar.pack(side="bottom", fill="x", padx=16, pady=12)
        tk.Frame(win, bg=C_BORDER, height=1).pack(side="bottom", fill="x")

        body = tk.Frame(win, bg=C_PANEL)
        body.pack(fill="both", expand=True, padx=16, pady=(12, 8))

        self._label(body, "Name")
        _, self.e_name = field_entry(body, task.get("name", "") if task else "")
        _.pack(fill="x", pady=(2, 10))

        # account + perm + model row
        row = tk.Frame(body, bg=C_PANEL)
        row.pack(fill="x", pady=(0, 10))
        self._label(row, "Account").pack_forget() if False else None
        col1 = tk.Frame(row, bg=C_PANEL); col1.pack(side="left", fill="x", expand=True)
        col2 = tk.Frame(row, bg=C_PANEL); col2.pack(side="left", fill="x", expand=True, padx=(10, 0))
        col3 = tk.Frame(row, bg=C_PANEL); col3.pack(side="left", fill="x", expand=True, padx=(10, 0))
        self._label(col1, "Account")
        accs = self.engine.account_labels() or ["claude1"]
        self.v_acc = tk.StringVar(value=(task.get("account") if task else None)
                                  or self.engine.settings.get("default_account") or accs[0])
        self._option(col1, self.v_acc, accs).pack(fill="x", pady=(2, 0))
        self._label(col2, "Model")
        self.v_model = tk.StringVar(value=(task.get("model") if task else "") or "(default)")
        self._option(col2, self.v_model, MODELS).pack(fill="x", pady=(2, 0))
        self._label(col3, "Effort")
        self.v_effort = tk.StringVar(value=(task.get("effort") if task else "") or "(default)")
        self._option(col3, self.v_effort, EFFORT).pack(fill="x", pady=(2, 0))

        self._label(body, "Permission mode")
        self.v_perm = tk.StringVar(value=PERM_LABEL.get(
            (task.get("perm") if task else None)
            or self.engine.settings.get("default_perm", "bypass"), PERM_MODES[0][0]))
        self._option(body, self.v_perm, [p[0] for p in PERM_MODES]).pack(fill="x", pady=(2, 10))

        self._label(body, "Working directory")
        dirrow = tk.Frame(body, bg=C_PANEL)
        dirrow.pack(fill="x", pady=(2, 10))
        wrap, self.e_dir = field_entry(dirrow, task.get("cwd", "") if task else "", width=44)
        wrap.pack(side="left", fill="x", expand=True)
        hover_button(dirrow, "Browse", self._browse).pack(side="left", padx=(8, 0))

        self._label(body, "Prompt (the heavy work)")
        tw = tk.Frame(body, bg=C_BORDER)
        tw.pack(fill="both", expand=True, pady=(2, 10))
        self.t_prompt = tk.Text(tw, bg=C_FIELD, fg=C_TEXT, insertbackground=C_TEXT,
                                relief="flat", font=(FONT, 10), height=7, wrap="word")
        self.t_prompt.pack(fill="both", expand=True, padx=1, pady=1)
        if task:
            self.t_prompt.insert("1.0", task.get("prompt", ""))

        # schedule
        self._label(body, "When to run")
        self.v_sched = tk.StringVar(value=SCHED_LABEL.get(
            (task.get("sched_type") if task else None) or "session_reset"))
        om = self._option(body, self.v_sched, [s[0] for s in SCHED_TYPES])
        om.pack(fill="x", pady=(2, 6))
        self.v_sched.trace_add("write", lambda *a: self._render_sched())
        self.sched_frame = tk.Frame(body, bg=C_PANEL)
        self.sched_frame.pack(fill="x")

        # repeat
        rep = tk.Frame(body, bg=C_PANEL)
        rep.pack(fill="x", pady=(8, 4))
        self.v_repeat = tk.BooleanVar(value=bool(task.get("repeat")) if task else False)
        tk.Checkbutton(rep, text="Repeat every", variable=self.v_repeat,
                       bg=C_PANEL, fg=C_TEXT, selectcolor=C_FIELD, activebackground=C_PANEL,
                       activeforeground=C_TEXT, font=(FONT, 9)).pack(side="left")
        _, self.e_rep_h = field_entry(rep, str(task.get("repeat_hours", "5")) if task else "5", width=5)
        _.pack(side="left", padx=(4, 4))
        tk.Label(rep, text="hours, until", bg=C_PANEL, fg=C_MUTED, font=(FONT, 9)).pack(side="left")
        _, self.e_rep_until = field_entry(rep, (task.get("repeat_until", "") if task else ""), width=7)
        _.pack(side="left", padx=(4, 4))
        tk.Label(rep, text="HH:MM (blank = no stop)", bg=C_PANEL, fg=C_FAINT,
                 font=(FONT, 8)).pack(side="left")

        # buttons (anchored bottom bar created above)
        hover_button(self.btnbar, "Cancel", self._cancel, fg=C_MUTED).pack(side="right")
        hover_button(self.btnbar, "Save", self._save, fg=C_OK).pack(side="right", padx=(0, 8))

        self._render_sched()
        win.bind("<Escape>", lambda e: self._cancel())
        self.e_name.focus_set()

    # -- small widget factories --------------------------------------------
    def _label(self, parent, text):
        lbl = tk.Label(parent, text=text, bg=C_PANEL, fg=C_MUTED,
                       font=(FONT, 8, "bold"), anchor="w")
        lbl.pack(fill="x", anchor="w")
        return lbl

    def _option(self, parent, var, values):
        om = tk.OptionMenu(parent, var, *values)
        om.configure(bg=C_FIELD, fg=C_TEXT, activebackground=C_BTNBG_HOVER,
                     activeforeground=C_TEXT, relief="flat", highlightthickness=0,
                     font=(FONT, 10), anchor="w", cursor="hand2")
        om["menu"].configure(bg=C_PANEL, fg=C_TEXT, activebackground=C_BTNBG_HOVER,
                             activeforeground=C_TEXT, font=(FONT, 10))
        return om

    def _browse(self):
        d = filedialog.askdirectory(parent=self.win, title="Working directory")
        if d:
            d = d.replace("/", "\\")
            self.e_dir.delete(0, "end")
            self.e_dir.insert(0, d)

    def _sched_type(self):
        for label, val in SCHED_TYPES:
            if label == self.v_sched.get():
                return val
        return "now"

    def _render_sched(self):
        for w in self.sched_frame.winfo_children():
            w.destroy()
        st = self._sched_type()
        t = self.task or {}
        f = self.sched_frame
        if st == "at":
            now = datetime.now()
            default_dt = now.replace(hour=3, minute=0, second=0, microsecond=0)
            if default_dt < now:
                default_dt += timedelta(days=1)
            at = t.get("at")
            if at:
                dt = datetime.fromtimestamp(at)
                d_def, t_def = dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")
            else:
                d_def, t_def = default_dt.strftime("%Y-%m-%d"), default_dt.strftime("%H:%M")
            row = tk.Frame(f, bg=C_PANEL); row.pack(fill="x")
            tk.Label(row, text="Date", bg=C_PANEL, fg=C_MUTED, font=(FONT, 8, "bold")).pack(side="left")
            w1, self.e_date = field_entry(row, d_def, width=12); w1.pack(side="left", padx=(6, 14))
            tk.Label(row, text="Time", bg=C_PANEL, fg=C_MUTED, font=(FONT, 8, "bold")).pack(side="left")
            w2, self.e_time = field_entry(row, t_def, width=8); w2.pack(side="left", padx=(6, 0))
        elif st == "session_reset":
            row = tk.Frame(f, bg=C_PANEL); row.pack(fill="x")
            tk.Label(row, text="Account whose 5h reset to wait for:", bg=C_PANEL,
                     fg=C_MUTED, font=(FONT, 8, "bold")).pack(side="left")
            accs = self.engine.account_labels() or ["claude1"]
            self.v_reset_acc = tk.StringVar(value=t.get("reset_account") or self.v_acc.get())
            self._option(row, self.v_reset_acc, accs).pack(side="left", padx=(6, 0))
            self._offset_row(f, t.get("offset_min", 2), "minutes after reset")
            self._reset_hint(f)
        elif st == "after_prev":
            self._offset_row(f, t.get("offset_min", 0), "minutes after the previous task finishes")
        else:  # now
            tk.Label(f, text="Runs immediately once the app is armed.",
                     bg=C_PANEL, fg=C_FAINT, font=(FONT, 9)).pack(anchor="w")

    def _offset_row(self, parent, default, caption):
        row = tk.Frame(parent, bg=C_PANEL); row.pack(fill="x", pady=(6, 0))
        w, self.e_offset = field_entry(row, str(default), width=5)
        w.pack(side="left")
        tk.Label(row, text=caption, bg=C_PANEL, fg=C_MUTED, font=(FONT, 9)).pack(side="left", padx=(6, 0))

    def _reset_hint(self, parent):
        acc = self.v_acc.get()
        reset = self.engine.session_reset_epoch(acc)
        txt = ("Next reset for %s: %s (in %s)" % (acc, fmt_clock(reset), fmt_countdown(reset))
               if reset else "No live reset data yet (account may need /login).")
        tk.Label(parent, text=txt, bg=C_PANEL, fg=C_FAINT, font=(FONT, 8)).pack(anchor="w", pady=(6, 0))

    # -- save ---------------------------------------------------------------
    def _save(self):
        name = self.e_name.get().strip()
        prompt = self.t_prompt.get("1.0", "end").strip()
        cwd = self.e_dir.get().strip()
        if not name:
            messagebox.showwarning("Missing", "Please give the task a name.", parent=self.win)
            return
        if not prompt:
            messagebox.showwarning("Missing", "The prompt is empty.", parent=self.win)
            return
        if not cwd:
            messagebox.showwarning("Missing", "Choose a working directory.", parent=self.win)
            return
        perm = next((v for k, v in PERM_MODES if k == self.v_perm.get()), "bypass")
        st = self._sched_type()
        t = self.task or {
            "id": uuid.uuid4().hex[:12],
            "created_at": time.time(),
            "status": "pending",
        }
        t.update({
            "name": name,
            "account": self.v_acc.get(),
            "cwd": cwd,
            "prompt": prompt,
            "perm": perm,
            "model": "" if self.v_model.get() == "(default)" else self.v_model.get(),
            "effort": "" if self.v_effort.get() == "(default)" else self.v_effort.get(),
            "sched_type": st,
            "repeat": bool(self.v_repeat.get()),
            "repeat_hours": self._num(self.e_rep_h.get(), 5),
            "repeat_until": self.e_rep_until.get().strip(),
        })
        if st == "at":
            at = parse_at(self.e_date.get(), self.e_time.get())
            if at is None:
                messagebox.showwarning("Bad time", "Use date YYYY-MM-DD and time HH:MM.",
                                       parent=self.win)
                return
            t["at"] = at
        elif st == "session_reset":
            t["reset_account"] = self.v_reset_acc.get()
            t["offset_min"] = self._num(self.e_offset.get(), 2)
        elif st == "after_prev":
            t["offset_min"] = self._num(self.e_offset.get(), 0)
        # re-arm an edited finished task
        if t.get("status") in ("done", "failed"):
            t["status"] = "pending"
            t["not_before"] = None
        self.result = t
        if not self.task:
            self.engine.add_task(t)
        else:
            self.engine.update_task(t)
        self.win.destroy()

    def _num(self, s, default):
        try:
            return float(s)
        except Exception:
            return default

    def _cancel(self):
        self.win.destroy()


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------
class App:
    def __init__(self, root):
        self.root = root
        self.engine = Engine()
        root.title("Claude Tasker")
        root.configure(bg=C_BG)
        root.geometry("960x620")
        root.minsize(820, 520)

        self._init_style()
        self._build_header()
        self._build_usage_bar()
        self._build_table()
        self._build_footer()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._tick()

    # -- styling ------------------------------------------------------------
    def _init_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("Treeview", background=C_ROW, fieldbackground=C_ROW,
                        foreground=C_TEXT, rowheight=26, borderwidth=0,
                        font=(FONT, 9))
        style.configure("Treeview.Heading", background=C_PANEL, foreground=C_MUTED,
                        font=(FONT, 8, "bold"), relief="flat")
        style.map("Treeview", background=[("selected", "#2d3340")],
                  foreground=[("selected", C_TEXT)])
        style.map("Treeview.Heading", background=[("active", C_PANEL)])

    # -- header (title + arm toggle) ----------------------------------------
    def _build_header(self):
        bar = tk.Frame(self.root, bg=C_BG)
        bar.pack(fill="x", padx=14, pady=(12, 4))
        tk.Label(bar, text="Claude Tasker", bg=C_BG, fg=C_TEXT,
                 font=(FONT, 14, "bold")).pack(side="left")
        tk.Label(bar, text="  overnight prompt scheduler", bg=C_BG, fg=C_FAINT,
                 font=(FONT, 9)).pack(side="left")

        self.arm_btn = tk.Label(bar, text="", bg=C_BTNBG, fg=C_TEXT,
                                font=(FONT, 10, "bold"), padx=16, pady=7, cursor="hand2")
        self.arm_btn.pack(side="right")
        self.arm_btn.bind("<Button-1>", lambda e: self._toggle_arm())
        hover_button(bar, "Settings", self._open_settings, fg=C_MUTED).pack(side="right", padx=(0, 10))
        self._render_arm()

    def _render_arm(self):
        if self.engine.armed:
            self.arm_btn.configure(text="●  ARMED — tasks will fire", bg="#1f3a26", fg=C_OK)
        else:
            self.arm_btn.configure(text="‖  PAUSED — click to arm", bg=C_BTNBG, fg=C_WARN)

    def _toggle_arm(self):
        self.engine.armed = not self.engine.armed
        self._render_arm()

    # -- usage bar ----------------------------------------------------------
    def _build_usage_bar(self):
        self.usage_frame = tk.Frame(self.root, bg=C_PANEL, highlightbackground=C_BORDER,
                                    highlightthickness=1)
        self.usage_frame.pack(fill="x", padx=14, pady=(4, 8))
        self.usage_inner = tk.Frame(self.usage_frame, bg=C_PANEL)
        self.usage_inner.pack(fill="x", padx=10, pady=8)

    def _render_usage(self):
        for w in self.usage_inner.winfo_children():
            w.destroy()
        accs = self.engine.account_labels()
        if not accs:
            tk.Label(self.usage_inner, text="No accounts configured.", bg=C_PANEL,
                     fg=C_MUTED, font=(FONT, 9)).pack(side="left")
            return
        for i, label in enumerate(accs):
            u = self.engine.usage.get(label, {})
            five = u.get("five") or {}
            seven = u.get("seven") or {}
            accent = ACCENTS[i % len(ACCENTS)]
            col = tk.Frame(self.usage_inner, bg=C_PANEL)
            col.pack(side="left", padx=(0, 28))
            head = tk.Frame(col, bg=C_PANEL); head.pack(anchor="w")
            tk.Label(head, text="●", bg=C_PANEL, fg=accent, font=(FONT, 9)).pack(side="left")
            tk.Label(head, text=" %s" % label, bg=C_PANEL, fg=C_TEXT,
                     font=(FONT, 9, "bold")).pack(side="left")
            email = u.get("email") or ""
            if email:
                tk.Label(head, text="  %s" % email, bg=C_PANEL, fg=C_FAINT,
                         font=(FONT, 8)).pack(side="left")
            self._usage_metric(col, "session", five)
            self._usage_metric(col, "weekly", seven)

    def _usage_metric(self, parent, name, data):
        util = data.get("util")
        reset = data.get("reset")
        if util is None:
            txt = "%s: —" % name
            color = C_FAINT
        else:
            pct = int(round(util))
            color = C_CRIT if pct >= 85 else C_WARN if pct >= 60 else C_OK
            txt = "%s %d%% · resets %s" % (name, pct, fmt_countdown(reset))
        row = tk.Frame(parent, bg=C_PANEL); row.pack(anchor="w")
        tk.Label(row, text="  " + txt, bg=C_PANEL, fg=color, font=(FONT, 8)).pack(side="left")

    # -- task table ---------------------------------------------------------
    def _build_table(self):
        wrap = tk.Frame(self.root, bg=C_BG)
        wrap.pack(fill="both", expand=True, padx=14)

        cols = ("name", "account", "schedule", "when", "status")
        self.tree = ttk.Treeview(wrap, columns=cols, show="headings", selectmode="browse")
        for c, txt, w in (("name", "Task", 260), ("account", "Account", 90),
                          ("schedule", "Schedule", 170), ("when", "When", 200),
                          ("status", "Status", 90)):
            self.tree.heading(c, text=txt)
            self.tree.column(c, width=w, anchor="w")
        self.tree.column("status", anchor="center")
        self.tree.tag_configure("running", foreground=C_ACCENT)
        self.tree.tag_configure("done", foreground=C_OK)
        self.tree.tag_configure("failed", foreground=C_CRIT)
        self.tree.tag_configure("pending", foreground=C_TEXT)
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda e: self._edit_selected())

    # -- footer buttons -----------------------------------------------------
    def _build_footer(self):
        bar = tk.Frame(self.root, bg=C_BG)
        bar.pack(fill="x", padx=14, pady=10)
        hover_button(bar, "+ New task", self._new_task, fg=C_OK).pack(side="left")
        hover_button(bar, "Edit", self._edit_selected).pack(side="left", padx=(8, 0))
        hover_button(bar, "Duplicate", self._dup_selected).pack(side="left", padx=(8, 0))
        hover_button(bar, "Delete", self._del_selected, fg=C_CRIT).pack(side="left", padx=(8, 0))
        tk.Frame(bar, bg=C_BORDER, width=1, height=24).pack(side="left", padx=12)
        hover_button(bar, "▲", lambda: self._move(-1), width=2).pack(side="left")
        hover_button(bar, "▼", lambda: self._move(1), width=2).pack(side="left", padx=(4, 0))
        tk.Frame(bar, bg=C_BORDER, width=1, height=24).pack(side="left", padx=12)
        hover_button(bar, "Run now", self._run_now, fg=C_ACCENT).pack(side="left")
        hover_button(bar, "Stop", self._stop_selected, fg=C_WARN).pack(side="left", padx=(8, 0))
        hover_button(bar, "Re-queue", self._requeue_selected).pack(side="left", padx=(8, 0))
        hover_button(bar, "View log", self._view_log).pack(side="left", padx=(8, 0))

    # -- selection helpers --------------------------------------------------
    def _selected_id(self):
        sel = self.tree.selection()
        return sel[0] if sel else None

    def _new_task(self):
        TaskDialog(self)

    def _edit_selected(self):
        tid = self._selected_id()
        if not tid:
            return
        t = self.engine.get_task(tid)
        if t and t.get("status") == "running":
            messagebox.showinfo("Running", "Stop the task before editing it.", parent=self.root)
            return
        if t:
            TaskDialog(self, t)

    def _dup_selected(self):
        tid = self._selected_id()
        t = self.engine.get_task(tid)
        if not t:
            return
        clone = {k: t.get(k) for k in SERIAL_FIELDS}
        clone.update({"id": uuid.uuid4().hex[:12], "name": (t.get("name") or "") + " (copy)",
                      "status": "pending", "created_at": time.time(),
                      "started_at": None, "ended_at": None, "exit_code": None,
                      "not_before": None, "log_file": None})
        self.engine.add_task(clone)

    def _del_selected(self):
        tid = self._selected_id()
        t = self.engine.get_task(tid)
        if not t:
            return
        if messagebox.askyesno("Delete", "Delete task '%s'?" % t.get("name"), parent=self.root):
            self.engine.delete_task(tid)

    def _move(self, delta):
        tid = self._selected_id()
        if tid:
            self.engine.move_task(tid, delta)
            self.root.after(10, lambda: self.tree.selection_set(tid))

    def _run_now(self):
        tid = self._selected_id()
        t = self.engine.get_task(tid)
        if not t:
            return
        if t.get("status") == "running":
            return
        self.engine._start(t)

    def _stop_selected(self):
        tid = self._selected_id()
        if tid:
            self.engine.stop_task(tid)

    def _requeue_selected(self):
        tid = self._selected_id()
        if tid:
            self.engine.reset_task(tid)

    def _view_log(self):
        tid = self._selected_id()
        t = self.engine.get_task(tid)
        if not t:
            return
        path = t.get("log_file") or os.path.join(LOGS_DIR, "%s.log" % tid)
        if not os.path.exists(path):
            messagebox.showinfo("No log", "This task has not run yet.", parent=self.root)
            return
        LogViewer(self.root, path, t.get("name"))

    # -- settings -----------------------------------------------------------
    def _open_settings(self):
        SettingsDialog(self)

    # -- periodic refresh ---------------------------------------------------
    def _tick(self):
        self._render_usage()
        self._refresh_table()
        self.root.after(1000, self._tick)

    def _refresh_table(self):
        sel = self.tree.selection()
        existing = set(self.tree.get_children())
        order = []
        for t in self.engine._snapshot():
            tid = t["id"]
            order.append(tid)
            sched = self._sched_summary(t)
            vals = (t.get("name", ""), t.get("account", ""), sched,
                    self.engine.when_text(t), t.get("status", "pending"))
            tag = t.get("status", "pending")
            if tid in existing:
                self.tree.item(tid, values=vals, tags=(tag,))
            else:
                self.tree.insert("", "end", iid=tid, values=vals, tags=(tag,))
        for tid in existing:
            if tid not in order:
                self.tree.delete(tid)
        # keep ordering in sync with engine list
        for idx, tid in enumerate(order):
            self.tree.move(tid, "", idx)
        if sel:
            try:
                self.tree.selection_set(sel)
            except Exception:
                pass

    def _sched_summary(self, t):
        st = t.get("sched_type", "now")
        rep = ""
        if t.get("repeat"):
            rep = " ⟳%sh" % (t.get("repeat_hours") or "?")
        if st == "at":
            return fmt_clock(t.get("at")) + rep
        if st == "session_reset":
            return "%s reset +%sm%s" % (t.get("reset_account", ""), int(t.get("offset_min") or 0), rep)
        if st == "after_prev":
            return "after prev +%sm%s" % (int(t.get("offset_min") or 0), rep)
        return "asap" + rep

    def _on_close(self):
        running = [t for t in self.engine._snapshot() if t.get("status") == "running"]
        if running:
            if not messagebox.askyesno(
                    "Quit", "%d task(s) running. Quitting leaves them running in the "
                            "background (no GUI). Quit anyway?" % len(running),
                    parent=self.root):
                return
        self.engine.shutdown()
        self.root.destroy()


# ---------------------------------------------------------------------------
# Log viewer
# ---------------------------------------------------------------------------
class LogViewer:
    def __init__(self, parent, path, name):
        self.path = path
        win = tk.Toplevel(parent)
        self.win = win
        win.title("Log — %s" % (name or ""))
        win.configure(bg=C_PANEL)
        win.geometry("840x560")
        top = tk.Frame(win, bg=C_PANEL); top.pack(fill="x", padx=10, pady=8)
        tk.Label(top, text=path, bg=C_PANEL, fg=C_FAINT, font=(MONO, 8)).pack(side="left")
        self.v_follow = tk.BooleanVar(value=True)
        tk.Checkbutton(top, text="follow", variable=self.v_follow, bg=C_PANEL, fg=C_MUTED,
                       selectcolor=C_FIELD, activebackground=C_PANEL,
                       font=(FONT, 8)).pack(side="right")
        body = tk.Frame(win, bg=C_BORDER); body.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.txt = tk.Text(body, bg="#0d0d0f", fg="#d0d0d6", insertbackground=C_TEXT,
                           relief="flat", font=(MONO, 9), wrap="word")
        self.txt.pack(side="left", fill="both", expand=True, padx=1, pady=1)
        sb = ttk.Scrollbar(body, orient="vertical", command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self._size = -1
        self._refresh()

    def _refresh(self):
        try:
            if not self.win.winfo_exists():
                return
        except Exception:
            return
        try:
            sz = os.path.getsize(self.path)
            if sz != self._size:
                self._size = sz
                with open(self.path, "r", encoding="utf-8", errors="replace") as f:
                    data = f.read()
                self.txt.delete("1.0", "end")
                self.txt.insert("1.0", data)
                if self.v_follow.get():
                    self.txt.see("end")
        except Exception:
            pass
        self.win.after(1000, self._refresh)


# ---------------------------------------------------------------------------
# Settings dialog
# ---------------------------------------------------------------------------
class SettingsDialog:
    def __init__(self, app):
        self.app = app
        self.engine = app.engine
        self.accts = [dict(a) for a in self.engine.accounts]   # working copy
        win = tk.Toplevel(app.root)
        self.win = win
        win.title("Settings")
        win.configure(bg=C_PANEL)
        win.transient(app.root)
        win.grab_set()
        win.geometry("620x640")
        win.minsize(560, 520)

        # bottom button bar first so it can't be pushed off-screen
        bar = tk.Frame(win, bg=C_PANEL)
        bar.pack(side="bottom", fill="x", padx=16, pady=12)
        tk.Frame(win, bg=C_BORDER, height=1).pack(side="bottom", fill="x")
        hover_button(bar, "Cancel", win.destroy, fg=C_MUTED).pack(side="right")
        hover_button(bar, "Save", self._save, fg=C_OK).pack(side="right", padx=(0, 8))

        body = tk.Frame(win, bg=C_PANEL)
        body.pack(fill="both", expand=True, padx=16, pady=(14, 8))
        s = self.engine.settings

        def lab(parent, t):
            tk.Label(parent, text=t, bg=C_PANEL, fg=C_MUTED,
                     font=(FONT, 8, "bold"), anchor="w").pack(fill="x", anchor="w")

        # -- accounts -------------------------------------------------------
        lab(body, "Accounts  (label = the CLI name; dir holds that login's .credentials.json)")
        self.acc_frame = tk.Frame(body, bg=C_PANEL)
        self.acc_frame.pack(fill="x", pady=(2, 6))
        self._render_accounts()

        addrow = tk.Frame(body, bg=C_PANEL); addrow.pack(fill="x", pady=(0, 4))
        wl, self.e_add_label = field_entry(addrow, "", width=12)
        wl.pack(side="left")
        self.e_add_label.insert(0, "claude%d" % (len(self.accts) + 1))
        wd, self.e_add_dir = field_entry(addrow, "", width=30)
        wd.pack(side="left", fill="x", expand=True, padx=(6, 6))
        self.e_add_dir.insert(0, r"%%USERPROFILE%%\.claude-account%d" % (len(self.accts) + 1))
        hover_button(addrow, "…", lambda: self._browse_into(self.e_add_dir)).pack(side="left")
        hover_button(addrow, "Add", self._add_account, fg=C_OK).pack(side="left", padx=(6, 0))
        self.acc_err = tk.Label(body, text="", bg=C_PANEL, fg=C_CRIT, font=(FONT, 8), anchor="w")
        self.acc_err.pack(fill="x")

        tk.Frame(body, bg=C_BORDER, height=1).pack(fill="x", pady=(8, 10))

        # -- runtime --------------------------------------------------------
        lab(body, "Claude executable (name on PATH or full path)")
        w2, self.e_bin = field_entry(body, s.get("claude_bin", "claude"), width=40)
        w2.pack(fill="x", pady=(2, 10))

        rown = tk.Frame(body, bg=C_PANEL); rown.pack(fill="x", pady=(0, 10))
        c1 = tk.Frame(rown, bg=C_PANEL); c1.pack(side="left")
        c2 = tk.Frame(rown, bg=C_PANEL); c2.pack(side="left", padx=(24, 0))
        lab(c1, "Max tasks at once")
        w3, self.e_conc = field_entry(c1, str(s.get("max_concurrent", 1)), width=6)
        w3.pack(anchor="w", pady=(2, 0))
        lab(c2, "Usage poll (seconds)")
        w4, self.e_poll = field_entry(c2, str(s.get("usage_poll_seconds", USAGE_POLL_DEFAULT)), width=8)
        w4.pack(anchor="w", pady=(2, 0))

        # -- optional widget integration ------------------------------------
        lab(body, "Claude Usage Widget folder (optional — reuses its limit data)")
        row = tk.Frame(body, bg=C_PANEL); row.pack(fill="x", pady=(2, 4))
        w, self.e_widget = field_entry(row, s.get("widget_dir", ""), width=40)
        w.pack(side="left", fill="x", expand=True)
        hover_button(row, "Browse", lambda: self._browse_into(self.e_widget)).pack(side="left", padx=(8, 0))
        tk.Label(body, text="Leave blank to fetch limits directly from Anthropic's API.",
                 bg=C_PANEL, fg=C_FAINT, font=(FONT, 8), anchor="w").pack(fill="x")

    def _render_accounts(self):
        for w in self.acc_frame.winfo_children():
            w.destroy()
        if not self.accts:
            tk.Label(self.acc_frame, text="No accounts — add one below.", bg=C_PANEL,
                     fg=C_FAINT, font=(FONT, 9)).pack(anchor="w")
            return
        for i, a in enumerate(self.accts):
            accent = ACCENTS[i % len(ACCENTS)]
            row = tk.Frame(self.acc_frame, bg=C_ROW)
            row.pack(fill="x", pady=1)
            tk.Label(row, text="●", bg=C_ROW, fg=accent, font=(FONT, 9)).pack(side="left", padx=(8, 2))
            tk.Label(row, text=a.get("label", ""), bg=C_ROW, fg=C_TEXT,
                     font=(FONT, 9, "bold"), width=12, anchor="w").pack(side="left")
            tk.Label(row, text=a.get("config_dir", ""), bg=C_ROW, fg=C_MUTED,
                     font=(FONT, 8), anchor="w").pack(side="left", fill="x", expand=True, padx=(4, 4))
            rm = tk.Label(row, text="✕", bg=C_ROW, fg=C_BTNBG_HOVER if False else C_MUTED,
                          font=(FONT, 9), cursor="hand2", padx=8)
            rm.pack(side="right")
            rm.bind("<Enter>", lambda e, w=rm: w.configure(fg=C_CRIT))
            rm.bind("<Leave>", lambda e, w=rm: w.configure(fg=C_MUTED))
            rm.bind("<Button-1>", lambda e, idx=i: self._remove_account(idx))

    def _add_account(self):
        label = self.e_add_label.get().strip()
        cdir = self.e_add_dir.get().strip()
        if not label or not cdir:
            self.acc_err.configure(text="Both label and directory are required.")
            return
        if any(a.get("label") == label for a in self.accts):
            self.acc_err.configure(text="That label already exists.")
            return
        self.acc_err.configure(text="")
        self.accts.append({"label": label, "config_dir": cdir})
        n = len(self.accts) + 1
        self.e_add_label.delete(0, "end"); self.e_add_label.insert(0, "claude%d" % n)
        self.e_add_dir.delete(0, "end")
        self.e_add_dir.insert(0, r"%%USERPROFILE%%\.claude-account%d" % n)
        self._render_accounts()

    def _remove_account(self, idx):
        if 0 <= idx < len(self.accts):
            del self.accts[idx]
            self._render_accounts()

    def _browse_into(self, entry):
        d = filedialog.askdirectory(parent=self.win)
        if d:
            entry.delete(0, "end")
            entry.insert(0, d.replace("/", "\\"))

    def _save(self):
        s = self.engine.settings
        s["widget_dir"] = self.e_widget.get().strip()
        s["claude_bin"] = self.e_bin.get().strip() or "claude"
        try:
            s["max_concurrent"] = max(1, int(float(self.e_conc.get())))
        except Exception:
            s["max_concurrent"] = 1
        try:
            s["usage_poll_seconds"] = max(60, int(float(self.e_poll.get())))
        except Exception:
            s["usage_poll_seconds"] = USAGE_POLL_DEFAULT
        self.engine.set_accounts(self.accts)   # also persists settings
        self.win.destroy()


def _bind_select_all(root):
    """Make Ctrl+A select all in every Entry/Text (tk's default is 'go to
    line start'). Applied at the class level so it covers dialogs too."""
    def entry_all(e):
        e.widget.select_range(0, "end")
        e.widget.icursor("end")
        return "break"

    def text_all(e):
        e.widget.tag_add("sel", "1.0", "end-1c")
        e.widget.mark_set("insert", "end-1c")
        return "break"

    for key in ("<Control-a>", "<Control-A>"):
        root.bind_class("Entry", key, entry_all)
        root.bind_class("TEntry", key, entry_all)
        root.bind_class("Text", key, text_all)


def main():
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    _bind_select_all(root)
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()

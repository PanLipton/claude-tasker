# -*- coding: utf-8 -*-
"""Render preview screenshots of Claude Tasker's windows into docs/.

Runs the real UI with sample (anonymised) data — no network, no task launches —
captures each window with PrintWindow, and writes PNGs using a tiny pure-stdlib
encoder (zlib). Run:  python tools/generate_previews.py
"""
import ctypes
import importlib.util
import os
import struct
import time
import uuid
import zlib
from ctypes import wintypes
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "docs")
os.makedirs(DOCS, exist_ok=True)

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

# ---- load the app module (it's a .pyw) ------------------------------------
spec = importlib.util.spec_from_file_location("ct", os.path.join(ROOT, "claude_tasker.pyw"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

# ---- sample data (no real accounts, no network) ---------------------------
now = time.time()
SAMPLE_ACCOUNTS = [
    {"label": "claude1", "config_dir": r"%USERPROFILE%\.claude-account1"},
    {"label": "claude2", "config_dir": r"%USERPROFILE%\.claude-account2"},
]
SAMPLE_SETTINGS = dict(m.default_settings())
SAMPLE_SETTINGS["accounts"] = SAMPLE_ACCOUNTS
SAMPLE_USAGE = {
    "claude1": {"five": {"util": 12.0, "reset": now + 4 * 3600 + 600},
                "seven": {"util": 8.0, "reset": now + 5 * 86400},
                "email": "you@example.com", "error": None},
    "claude2": {"five": {"util": 78.0, "reset": now + 2 * 3600 + 240},
                "seven": {"util": 41.0, "reset": now + 3 * 86400},
                "email": "work@example.com", "error": None},
}

m.load_settings = lambda: dict(SAMPLE_SETTINGS)
m.save_settings = lambda s: None
m.fetch_account_direct = lambda *a, **k: {}
m.read_widget_usage = lambda *a, **k: {}


def task(name, account, sched, status, **extra):
    t = {k: None for k in m.SERIAL_FIELDS}
    t.update({"id": uuid.uuid4().hex[:12], "name": name, "account": account,
              "cwd": r"D:\Projects\my-service", "prompt": "", "perm": "bypass",
              "model": "", "effort": "", "sched_type": sched, "offset_min": 2,
              "repeat": False, "status": status, "created_at": now})
    t.update(extra)
    return t

tomorrow_3am = (datetime.now().replace(hour=3, minute=0, second=0, microsecond=0)
                + timedelta(days=1)).timestamp()
SAMPLE_TASKS = [
    task("Refactor auth module & add tests", "claude1", "session_reset", "running",
         reset_account="claude1", started_at=now - 1800),
    task("Nightly dependency upgrade sweep", "claude2", "at", "pending",
         at=tomorrow_3am, repeat=True, repeat_hours=5, repeat_until="08:00",
         prompt="Update all dependencies to latest minor versions, run the test "
                "suite, and fix any breakages. Commit on a new branch."),
    task("Generate API docs from docstrings", "claude1", "after_prev", "pending",
         offset_min=0),
    task("Migrate DB seed scripts", "claude2", "session_reset", "done",
         reset_account="claude2", ended_at=now - 600, exit_code=0),
    task("Stress-test the worker queue", "claude1", "now", "failed",
         ended_at=now - 200, exit_code=1),
]

# ---- screen capture via PrintWindow + pure-stdlib PNG ----------------------
user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
dwm = ctypes.windll.dwmapi
SRCCOPY = 0x00CC0020


class BMPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BMPINFO(ctypes.Structure):
    _fields_ = [("h", BMPINFOHEADER), ("cols", wintypes.DWORD * 3)]


def _png(path, bgra, W, H, cx, cy, cw, ch):
    cx = max(0, cx); cy = max(0, cy)
    cw = min(cw, W - cx); ch = min(ch, H - cy)
    out = bytearray()
    rb = W * 4
    for y in range(cy, cy + ch):
        out.append(0)
        base = y * rb + cx * 4
        ba = bytearray(bgra[base:base + cw * 4])
        rgb = bytearray(cw * 3)
        rgb[0::3] = ba[2::4]      # R
        rgb[1::3] = ba[1::4]      # G
        rgb[2::3] = ba[0::4]      # B
        out += rgb
    comp = zlib.compress(bytes(out), 9)

    def chunk(typ, data):
        return (struct.pack(">I", len(data)) + typ + data
                + struct.pack(">I", zlib.crc32(typ + data) & 0xffffffff))

    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", struct.pack(">IIBBBBB", cw, ch, 8, 2, 0, 0, 0)))
        f.write(chunk(b"IDAT", comp))
        f.write(chunk(b"IEND", b""))


def grab(win, name):
    win.update_idletasks()
    win.attributes("-topmost", True)
    win.lift()
    win.update()
    time.sleep(0.35)
    win.update()
    hwnd = user32.GetAncestor(win.winfo_id(), 2)  # GA_ROOT
    wr = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(wr))
    W, H = wr.right - wr.left, wr.bottom - wr.top
    hdc = user32.GetWindowDC(hwnd)
    memdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, W, H)
    gdi32.SelectObject(memdc, bmp)
    user32.PrintWindow(hwnd, memdc, 2)            # PW_RENDERFULLCONTENT
    bmi = BMPINFO()
    bmi.h.biSize = ctypes.sizeof(BMPINFOHEADER)
    bmi.h.biWidth = W
    bmi.h.biHeight = -H                           # top-down
    bmi.h.biPlanes = 1
    bmi.h.biBitCount = 32
    bmi.h.biCompression = 0
    buf = (ctypes.c_char * (W * H * 4))()
    gdi32.GetDIBits(memdc, bmp, 0, H, buf, ctypes.byref(bmi), 0)
    # crop the invisible Win10 resize border using the visible frame bounds
    eb = wintypes.RECT()
    cx = cy = 0
    cw, ch = W, H
    if dwm.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(eb), ctypes.sizeof(eb)) == 0:
        cx, cy = eb.left - wr.left, eb.top - wr.top
        cw, ch = eb.right - eb.left, eb.bottom - eb.top
    path = os.path.join(DOCS, name)
    _png(path, bytes(buf), W, H, cx, cy, cw, ch)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(memdc)
    user32.ReleaseDC(hwnd, hdc)
    print("wrote", path, "%dx%d" % (cw, ch))


# ---- build the UI and capture ---------------------------------------------
import tkinter as tk

root = tk.Tk()
m._bind_clipboard(root)
app = m.App(root)
app.engine._stop.set()                 # stop the scheduler thread
app.engine._start = lambda t: None     # belt-and-suspenders: never launch tasks
app.engine.usage = SAMPLE_USAGE
app.engine.tasks = SAMPLE_TASKS
app.engine.armed = True
app._render_arm()
app._sync_usage()
app._refresh_table()
app.tree.selection_set(SAMPLE_TASKS[1]["id"])
root.update_idletasks()
root.update()
grab(root, "main.png")

dlg = m.TaskDialog(app, SAMPLE_TASKS[1])
dlg.win.update_idletasks()
dlg.win.update()
grab(dlg.win, "new-task.png")
dlg.win.destroy()

sd = m.SettingsDialog(app)
sd.win.update_idletasks()
sd.win.update()
grab(sd.win, "settings.png")
sd.win.destroy()

root.destroy()
print("done")

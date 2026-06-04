# -*- coding: utf-8 -*-
"""Tests for the subprocess runner and engine lifecycle.

``_run`` is what actually launches the Claude CLI, so it is the part that most
needs to "just work when it has to": building the right command line, wiring the
per-account ``CLAUDE_CONFIG_DIR``, writing the log, recording the exit code and
re-queuing repeats. Every test mocks ``subprocess`` — nothing real is spawned.
"""
import os
import threading

import pytest


# ---------------------------------------------------------------------------
# A fake Popen: records the call and returns a chosen exit code.
# ---------------------------------------------------------------------------
class FakePopen:
    instances = []

    def __init__(self, cmd, cwd=None, env=None, stdout=None, stderr=None,
                 stdin=None, creationflags=0):
        self.cmd = cmd
        self.cwd = cwd
        self.env = env
        self.stdout = stdout
        self.creationflags = creationflags
        self.pid = 4242
        self._rc = FakePopen.next_rc
        self._polled = None
        FakePopen.instances.append(self)
        if stdout is not None:                 # mimic the CLI writing output
            stdout.write("hello from fake claude\n")

    def wait(self):
        return self._rc

    def poll(self):
        return self._polled


@pytest.fixture
def fake_popen(monkeypatch, ct_mod):
    FakePopen.instances = []
    FakePopen.next_rc = 0
    monkeypatch.setattr(ct_mod.subprocess, "Popen", FakePopen)
    # make the binary resolvable so the FALLBACK branch is not taken
    monkeypatch.setattr(ct_mod.shutil, "which", lambda b: r"C:\fake\claude.exe")
    return FakePopen


@pytest.fixture
def run_engine(engine, isolated_paths):
    """Engine wired with one account whose config dir is a real temp dir."""
    cdir = isolated_paths / "cfg"
    cdir.mkdir()
    engine.accounts = [{"label": "acct", "config_dir": str(cdir)}]
    engine._cdir = str(cdir)
    return engine


def _task(tmp_path, **over):
    cwd = tmp_path / "work"
    cwd.mkdir(exist_ok=True)
    t = {"id": "t1", "name": "Task One", "prompt": "do the thing",
         "account": "acct", "cwd": str(cwd), "perm": "bypass",
         "model": "(default)", "effort": "(default)", "repeat": False,
         "status": "running"}
    t.update(over)
    return t


# ---------------------------------------------------------------------------
# _run — happy path
# ---------------------------------------------------------------------------
def test_run_success_sets_done(run_engine, fake_popen, isolated_paths):
    t = _task(isolated_paths)
    fake_popen.next_rc = 0
    run_engine._run(t)
    assert t["status"] == "done"
    assert t["exit_code"] == 0
    assert t["ended_at"] is not None
    # runtime entry cleaned up
    assert "t1" not in run_engine.runtime


def test_run_nonzero_exit_sets_failed(run_engine, fake_popen, isolated_paths):
    t = _task(isolated_paths)
    fake_popen.next_rc = 1
    run_engine._run(t)
    assert t["status"] == "failed"
    assert t["exit_code"] == 1


def test_run_writes_log_with_header_and_output(run_engine, fake_popen,
                                               isolated_paths):
    t = _task(isolated_paths)
    run_engine._run(t)
    log = open(t["log_file"], encoding="utf-8").read()
    assert "Claude Tasker run" in log
    assert "Task     : Task One" in log
    assert "hello from fake claude" in log
    assert "Exit code: 0" in log
    # the prompt must not leak verbatim into the Command line (redacted)
    assert "<prompt>" in log
    assert "do the thing" not in log


# ---------------------------------------------------------------------------
# _run — command construction
# ---------------------------------------------------------------------------
def test_run_cmd_bypass_permissions(run_engine, fake_popen, isolated_paths):
    run_engine._run(_task(isolated_paths, perm="bypass"))
    cmd = fake_popen.instances[0].cmd
    assert "--dangerously-skip-permissions" in cmd
    assert cmd[1:3] == ["-p", "do the thing"]


def test_run_cmd_accept_edits(run_engine, fake_popen, isolated_paths):
    run_engine._run(_task(isolated_paths, perm="acceptEdits"))
    cmd = fake_popen.instances[0].cmd
    assert "--dangerously-skip-permissions" not in cmd
    assert cmd[cmd.index("--permission-mode") + 1] == "acceptEdits"


def test_run_cmd_includes_model_and_effort(run_engine, fake_popen,
                                           isolated_paths):
    run_engine._run(_task(isolated_paths, model="opus", effort="high"))
    cmd = fake_popen.instances[0].cmd
    assert cmd[cmd.index("--model") + 1] == "opus"
    assert cmd[cmd.index("--effort") + 1] == "high"


def test_run_cmd_omits_default_model_and_effort(run_engine, fake_popen,
                                                isolated_paths):
    run_engine._run(_task(isolated_paths, model="(default)", effort="(default)"))
    cmd = fake_popen.instances[0].cmd
    assert "--model" not in cmd
    assert "--effort" not in cmd


def test_run_sets_config_dir_env(run_engine, fake_popen, isolated_paths):
    run_engine._run(_task(isolated_paths))
    env = fake_popen.instances[0].env
    assert env["CLAUDE_CONFIG_DIR"] == run_engine._cdir


def test_run_uses_task_cwd(run_engine, fake_popen, isolated_paths):
    t = _task(isolated_paths)
    run_engine._run(t)
    assert fake_popen.instances[0].cwd == t["cwd"]


def test_run_uses_fallback_binary_when_not_on_path(run_engine, monkeypatch,
                                                   isolated_paths, ct_mod):
    FakePopen.instances = []
    FakePopen.next_rc = 0
    monkeypatch.setattr(ct_mod.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(ct_mod.shutil, "which", lambda b: None)  # not on PATH
    fallback = isolated_paths / "claude.exe"
    fallback.write_text("")                      # exists -> used as fallback
    monkeypatch.setattr(ct_mod, "FALLBACK_CLAUDE", str(fallback))
    run_engine._run(_task(isolated_paths))
    assert FakePopen.instances[0].cmd[0] == str(fallback)


# ---------------------------------------------------------------------------
# _run — error handling
# ---------------------------------------------------------------------------
def test_run_missing_cwd_fails_without_spawning(run_engine, fake_popen,
                                                isolated_paths):
    t = _task(isolated_paths, cwd=str(isolated_paths / "does-not-exist"))
    run_engine._run(t)
    assert t["status"] == "failed"
    assert t["exit_code"] == -1
    assert fake_popen.instances == []          # never reached Popen
    log = open(t["log_file"], encoding="utf-8").read()
    assert "working directory does not exist" in log


def test_run_popen_exception_is_caught(run_engine, monkeypatch, isolated_paths,
                                       ct_mod):
    def boom(*a, **k):
        raise OSError("spawn failed")
    monkeypatch.setattr(ct_mod.subprocess, "Popen", boom)
    monkeypatch.setattr(ct_mod.shutil, "which", lambda b: r"C:\fake\claude.exe")
    t = _task(isolated_paths)
    run_engine._run(t)                          # must not raise
    assert t["status"] == "failed"
    assert t["exit_code"] == -1
    log = open(t["log_file"], encoding="utf-8").read()
    assert "exception" in log


# ---------------------------------------------------------------------------
# _run — repeat re-queue integration
# ---------------------------------------------------------------------------
def test_run_repeat_requeues_task(run_engine, fake_popen, isolated_paths):
    t = _task(isolated_paths, repeat=True, repeat_hours=5, repeat_until="",
              sched_type="now")
    run_engine._run(t)
    assert t["status"] == "scheduled"           # re-queued, not left done
    assert t["not_before"] is not None


# ---------------------------------------------------------------------------
# _start
# ---------------------------------------------------------------------------
def test_start_marks_running_and_runs(run_engine, monkeypatch, isolated_paths):
    done = threading.Event()
    seen = {}

    def fake_run(task):
        seen["status_at_run"] = task["status"]
        done.set()
    monkeypatch.setattr(run_engine, "_run", fake_run)
    t = _task(isolated_paths, status="pending")
    run_engine._start(t)
    assert done.wait(2), "_run was never invoked by _start"
    assert t["status"] == "running"
    assert t["started_at"] is not None
    assert seen["status_at_run"] == "running"


def test_start_skips_already_running(run_engine, monkeypatch, isolated_paths):
    called = []
    monkeypatch.setattr(run_engine, "_run", lambda task: called.append(1))
    t = _task(isolated_paths, status="running")
    run_engine._start(t)
    assert called == []                         # no second launch


# ---------------------------------------------------------------------------
# stop_task
# ---------------------------------------------------------------------------
def test_stop_task_no_runtime_is_noop(run_engine):
    run_engine.stop_task("nope")                # must not raise


def test_stop_task_kills_live_process(run_engine, monkeypatch, ct_mod):
    calls = []

    class P:
        pid = 999
        def poll(self):
            return None                         # still running
    monkeypatch.setattr(ct_mod.subprocess, "run",
                        lambda *a, **k: calls.append((a, k)))
    run_engine.runtime["t1"] = {"proc": P()}
    run_engine.stop_task("t1")
    assert calls, "taskkill was not invoked"
    assert "taskkill" in calls[0][0][0]
    assert "999" in calls[0][0][0]


def test_stop_task_skips_finished_process(run_engine, monkeypatch, ct_mod):
    calls = []

    class P:
        pid = 1
        def poll(self):
            return 0                            # already exited
    monkeypatch.setattr(ct_mod.subprocess, "run",
                        lambda *a, **k: calls.append(1))
    run_engine.runtime["t1"] = {"proc": P()}
    run_engine.stop_task("t1")
    assert calls == []


def test_stop_task_falls_back_to_kill(run_engine, monkeypatch, ct_mod):
    killed = []

    class P:
        pid = 5
        def poll(self):
            return None
        def kill(self):
            killed.append(1)

    def boom(*a, **k):
        raise OSError("taskkill missing")
    monkeypatch.setattr(ct_mod.subprocess, "run", boom)
    run_engine.runtime["t1"] = {"proc": P()}
    run_engine.stop_task("t1")
    assert killed == [1]


# ---------------------------------------------------------------------------
# shutdown
# ---------------------------------------------------------------------------
def test_shutdown_sets_stop_event(run_engine):
    assert not run_engine._stop.is_set()
    run_engine.shutdown()
    assert run_engine._stop.is_set()

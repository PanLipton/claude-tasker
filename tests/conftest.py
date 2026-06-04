# -*- coding: utf-8 -*-
"""Shared pytest fixtures for the Claude Tasker test-suite.

The application lives in a single ``claude_tasker.pyw`` file, so we load it as
a module via importlib (the ``.pyw`` extension means a plain ``import`` won't
find it). Importing only runs module-level imports + constant definitions — no
Tk window, no network — so it is safe in a headless test run.

Every test gets its real on-disk paths (tasks.json / settings.json / logs)
redirected into a throw-away ``tmp_path`` by the autouse ``isolated_paths``
fixture, so the suite can never clobber the developer's real queue.
"""
import importlib.util
import os
import threading
import types

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PYW = os.path.join(ROOT, "claude_tasker.pyw")


def _load_module():
    spec = importlib.util.spec_from_file_location("claude_tasker_app", PYW)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Loaded once for the whole session; per-test isolation is done via monkeypatch.
ct = _load_module()


@pytest.fixture
def ct_mod():
    """The imported claude_tasker module under test."""
    return ct


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path, monkeypatch):
    """Point all persistence at a temp dir so tests never touch real files."""
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(ct, "TASKS_PATH", str(tmp_path / "tasks.json"))
    monkeypatch.setattr(ct, "SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(ct, "LOGS_DIR", str(logs))
    return tmp_path


@pytest.fixture
def clock(monkeypatch):
    """Freeze ``time.time()`` as seen by the module to a controllable value.

    Replaces the module's ``time`` reference with a shim that proxies the real
    ``time`` module but returns a fixed, settable value from ``time()``. Returns
    a dict whose ``["t"]`` key is the current fake epoch (assign to advance it).
    """
    real = ct.time
    state = {"t": 1_000_000.0}
    shim = types.SimpleNamespace()
    for name in dir(real):
        if not name.startswith("__"):
            setattr(shim, name, getattr(real, name))
    shim.time = lambda: state["t"]
    monkeypatch.setattr(ct, "time", shim)
    return state


@pytest.fixture
def make_engine():
    """Factory for a bare Engine instance (no worker thread, no disk/network).

    Bypasses ``Engine.__init__`` — which would start the scheduler thread and
    hit the network — and wires up just the attributes the methods need.
    """
    def _make(**overrides):
        e = object.__new__(ct.Engine)
        e.settings = {"max_concurrent": 1, "claude_bin": "claude",
                      "usage_poll_seconds": 180}
        e.accounts = []
        e.tasks = []
        e.usage = {}
        e.armed = False
        e.lock = threading.RLock()
        e.runtime = {}
        e._usage_next = {}
        e._stop = threading.Event()
        for k, v in overrides.items():
            setattr(e, k, v)
        return e
    return _make


@pytest.fixture
def engine(make_engine):
    return make_engine()

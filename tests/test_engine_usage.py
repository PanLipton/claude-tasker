# -*- coding: utf-8 -*-
"""Tests for Engine.refresh_usage orchestration and the worker loop.

The module-level ``fetch_account_direct`` / ``read_widget_usage`` are tested in
``test_oauth.py``; here we exercise how the Engine chooses between them, the
per-account throttle, email back-fill, and one iteration of ``_loop``.
"""
import threading


def _acct(label, cdir):
    return {"label": label, "config_dir": cdir}


# ---------------------------------------------------------------------------
# refresh_usage — widget snapshot wins (free, no direct fetch)
# ---------------------------------------------------------------------------
def test_refresh_uses_widget_when_present(engine, monkeypatch, ct_mod):
    import os
    cdir = "/cfg/a"
    key = os.path.normcase(cdir)
    monkeypatch.setattr(ct_mod, "read_widget_usage",
                        lambda d: {key: {"five": {"util": 10}, "email": "w@x"}})
    called = []
    monkeypatch.setattr(ct_mod, "fetch_account_direct",
                        lambda a, prev=None: called.append(1) or {})
    engine.accounts = [_acct("a", cdir)]
    engine.refresh_usage()
    assert engine.usage["a"] == {"five": {"util": 10}, "email": "w@x"}
    assert called == []                          # never hit the network
    # email back-filled onto the account record
    assert engine.accounts[0]["email"] == "w@x"


# ---------------------------------------------------------------------------
# refresh_usage — direct fetch when no widget data
# ---------------------------------------------------------------------------
def test_refresh_falls_back_to_direct(engine, monkeypatch, ct_mod):
    monkeypatch.setattr(ct_mod, "read_widget_usage", lambda d: {})
    monkeypatch.setattr(ct_mod, "fetch_account_direct",
                        lambda a, prev=None: {"five": {"util": 42},
                                              "email": "d@x"})
    engine.accounts = [_acct("a", "/cfg/a")]
    engine.refresh_usage()
    assert engine.usage["a"]["five"]["util"] == 42
    assert engine.accounts[0]["email"] == "d@x"


def test_refresh_throttles_repeated_direct_fetches(engine, monkeypatch,
                                                   ct_mod, clock):
    monkeypatch.setattr(ct_mod, "read_widget_usage", lambda d: {})
    n = {"calls": 0}

    def fake_direct(a, prev=None):
        n["calls"] += 1
        return {"five": {"util": n["calls"]}}
    monkeypatch.setattr(ct_mod, "fetch_account_direct", fake_direct)
    engine.settings["usage_poll_seconds"] = 180
    engine.accounts = [_acct("a", "/cfg/a")]

    engine.refresh_usage()                       # fetches (call 1)
    engine.refresh_usage()                       # throttled — still 1
    assert n["calls"] == 1
    clock["t"] += 200                            # past the poll window
    engine.refresh_usage()                       # fetches again (call 2)
    assert n["calls"] == 2


def test_refresh_poll_window_has_a_floor_of_60s(engine, monkeypatch, ct_mod,
                                                clock):
    monkeypatch.setattr(ct_mod, "read_widget_usage", lambda d: {})
    n = {"calls": 0}
    monkeypatch.setattr(ct_mod, "fetch_account_direct",
                        lambda a, prev=None: n.__setitem__("calls",
                                                           n["calls"] + 1) or {})
    engine.settings["usage_poll_seconds"] = 1    # below the 60s floor
    engine.accounts = [_acct("a", "/cfg/a")]
    engine.refresh_usage()
    clock["t"] += 30                             # 30s < 60s floor
    engine.refresh_usage()
    assert n["calls"] == 1                        # still throttled


def test_refresh_widget_error_is_swallowed(engine, monkeypatch, ct_mod):
    def boom(d):
        raise RuntimeError("bad widget file")
    monkeypatch.setattr(ct_mod, "read_widget_usage", boom)
    monkeypatch.setattr(ct_mod, "fetch_account_direct",
                        lambda a, prev=None: {"five": {"util": 7}})
    engine.accounts = [_acct("a", "/cfg/a")]
    engine.refresh_usage()                        # must not raise
    assert engine.usage["a"]["five"]["util"] == 7


# ---------------------------------------------------------------------------
# _loop — one iteration, then stop
# ---------------------------------------------------------------------------
def test_loop_refreshes_and_ticks_then_exits(engine, monkeypatch):
    refreshed, ticked = [], []
    monkeypatch.setattr(engine, "refresh_usage",
                        lambda: refreshed.append(1))

    def fake_tick():
        ticked.append(1)
        engine._stop.set()                        # stop after first tick
    monkeypatch.setattr(engine, "_tick", fake_tick)
    monkeypatch.setattr(engine._stop, "wait", lambda timeout=None: None)
    engine.armed = True
    engine._loop()
    assert refreshed == [1]
    assert ticked == [1]


def test_loop_skips_tick_when_not_armed(engine, monkeypatch):
    ticked = []
    monkeypatch.setattr(engine, "refresh_usage", lambda: engine._stop.set())
    monkeypatch.setattr(engine, "_tick", lambda: ticked.append(1))
    monkeypatch.setattr(engine._stop, "wait", lambda timeout=None: None)
    engine.armed = False
    engine._loop()
    assert ticked == []                           # disarmed → no ticks

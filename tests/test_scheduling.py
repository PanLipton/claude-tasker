# -*- coding: utf-8 -*-
"""Tests for the scheduler core: fire_time, latching, the tick, repeats.

This is the heart of the app — getting these wrong means tasks fire at the
wrong moment (or never), so they are covered thoroughly.
"""
from datetime import datetime, timedelta


# ---------------------------------------------------------------------------
# fire_time
# ---------------------------------------------------------------------------
def test_fire_time_now(engine):
    t = {"id": "a", "sched_type": "now", "created_at": 100}
    assert engine.fire_time(t) == 100


def test_fire_time_now_respects_not_before(engine):
    t = {"id": "a", "sched_type": "now", "created_at": 100, "not_before": 500}
    assert engine.fire_time(t) == 500


def test_fire_time_at(engine):
    t = {"id": "a", "sched_type": "at", "at": 12345, "created_at": 0}
    assert engine.fire_time(t) == 12345


def test_fire_time_at_missing_is_none(engine):
    t = {"id": "a", "sched_type": "at", "at": None, "created_at": 0}
    assert engine.fire_time(t) is None


def test_fire_time_session_reset_uses_latched_target(engine):
    t = {"id": "a", "sched_type": "session_reset", "reset_account": "c2",
         "offset_min": 5, "target_reset": 1000, "created_at": 0}
    # base = target_reset + offset(min)*60 = 1000 + 300
    assert engine.fire_time(t) == 1300


def test_fire_time_session_reset_falls_back_to_live(engine):
    engine.usage = {"c2": {"five": {"reset": 2000}}}
    t = {"id": "a", "sched_type": "session_reset", "reset_account": "c2",
         "offset_min": 5, "created_at": 0}
    assert engine.fire_time(t) == 2000 + 300


def test_fire_time_session_reset_no_data_is_none(engine):
    t = {"id": "a", "sched_type": "session_reset", "reset_account": "c2",
         "offset_min": 5, "created_at": 0}
    assert engine.fire_time(t) is None


def test_fire_time_after_prev_done(engine):
    prev = {"id": "p", "status": "done", "ended_at": 500}
    t = {"id": "a", "sched_type": "after_prev", "offset_min": 2, "created_at": 0}
    engine.tasks = [prev, t]
    assert engine.fire_time(t) == 500 + 120


def test_fire_time_after_prev_failed_still_counts(engine):
    prev = {"id": "p", "status": "failed", "ended_at": 700}
    t = {"id": "a", "sched_type": "after_prev", "offset_min": 0, "created_at": 0}
    engine.tasks = [prev, t]
    assert engine.fire_time(t) == 700


def test_fire_time_after_prev_pending_is_none(engine):
    prev = {"id": "p", "status": "pending"}
    t = {"id": "a", "sched_type": "after_prev", "offset_min": 0, "created_at": 0}
    engine.tasks = [prev, t]
    assert engine.fire_time(t) is None


def test_fire_time_after_prev_first_in_list_runs_asap(engine):
    t = {"id": "a", "sched_type": "after_prev", "offset_min": 0, "created_at": 42}
    engine.tasks = [t]
    assert engine.fire_time(t) == 42


# ---------------------------------------------------------------------------
# _prev_task / session_reset_epoch
# ---------------------------------------------------------------------------
def test_prev_task(engine):
    a = {"id": "a"}
    b = {"id": "b"}
    engine.tasks = [a, b]
    assert engine._prev_task(b) is a
    assert engine._prev_task(a) is None


def test_session_reset_epoch(engine):
    engine.usage = {"c1": {"five": {"reset": 9999}}}
    assert engine.session_reset_epoch("c1") == 9999
    assert engine.session_reset_epoch("missing") is None


# ---------------------------------------------------------------------------
# _latch_resets
# ---------------------------------------------------------------------------
def test_latch_resets_freezes_target(engine):
    engine.usage = {"c2": {"five": {"reset": 5555}}}
    t = {"id": "a", "sched_type": "session_reset", "reset_account": "c2",
         "status": "pending"}
    engine.tasks = [t]
    engine._latch_resets()
    assert t["target_reset"] == 5555


def test_latch_resets_does_not_overwrite_existing(engine):
    engine.usage = {"c2": {"five": {"reset": 5555}}}
    t = {"id": "a", "sched_type": "session_reset", "reset_account": "c2",
         "status": "pending", "target_reset": 1111}
    engine.tasks = [t]
    engine._latch_resets()
    assert t["target_reset"] == 1111


def test_latch_resets_ignores_other_sched_types(engine):
    engine.usage = {"c2": {"five": {"reset": 5555}}}
    t = {"id": "a", "sched_type": "now", "status": "pending"}
    engine.tasks = [t]
    engine._latch_resets()
    assert "target_reset" not in t


def test_latch_resets_ignores_running_tasks(engine):
    engine.usage = {"c2": {"five": {"reset": 5555}}}
    t = {"id": "a", "sched_type": "session_reset", "reset_account": "c2",
         "status": "running"}
    engine.tasks = [t]
    engine._latch_resets()
    assert t.get("target_reset") is None


def test_session_reset_fires_across_a_reset_boundary(engine, monkeypatch):
    """Regression for the core bug: the live ``resets_at`` always points at the
    *next* reset and jumps ~5h forward the instant a window resets. Latching a
    fixed target must let the task actually fire as the boundary passes."""
    now = 1_000_000.0
    state = {"now": now, "reset": now + 30}
    monkeypatch.setattr(engine, "session_reset_epoch",
                        lambda label: state["reset"])
    t = {"id": "a", "sched_type": "session_reset", "reset_account": "c2",
         "offset_min": 0, "status": "pending", "created_at": now,
         "not_before": None}
    engine.tasks = [t]

    fired = False
    for step in range(0, 600, 3):
        state["now"] = now + step
        if state["now"] >= state["reset"]:      # window reset -> API jumps +5h
            state["reset"] += 5 * 3600
        engine._latch_resets()
        ft = engine.fire_time(t)
        if ft is not None and ft <= state["now"]:
            fired = True
            break
    assert fired, "session_reset task never became ready across the boundary"


# ---------------------------------------------------------------------------
# _tick — readiness + concurrency
# ---------------------------------------------------------------------------
def _stub_start(engine):
    started = []

    def fake_start(task):
        started.append(task["id"])
        task["status"] = "running"

    engine._start = fake_start
    return started


def test_tick_starts_ready_task(engine, clock):
    started = _stub_start(engine)
    engine.tasks = [{"id": "a", "sched_type": "now", "status": "pending",
                     "created_at": clock["t"] - 10}]
    engine._tick()
    assert started == ["a"]


def test_tick_honours_max_concurrent(engine, clock):
    started = _stub_start(engine)
    engine.settings["max_concurrent"] = 1
    engine.tasks = [
        {"id": "a", "sched_type": "now", "status": "pending",
         "created_at": clock["t"] - 20},
        {"id": "b", "sched_type": "now", "status": "pending",
         "created_at": clock["t"] - 10},
    ]
    engine._tick()
    assert started == ["a"]            # only one slot used


def test_tick_starts_multiple_when_allowed(engine, clock):
    started = _stub_start(engine)
    engine.settings["max_concurrent"] = 2
    engine.tasks = [
        {"id": "a", "sched_type": "now", "status": "pending",
         "created_at": clock["t"] - 20},
        {"id": "b", "sched_type": "now", "status": "pending",
         "created_at": clock["t"] - 10},
    ]
    engine._tick()
    assert set(started) == {"a", "b"}


def test_tick_skips_future_tasks(engine, clock):
    started = _stub_start(engine)
    engine.tasks = [{"id": "a", "sched_type": "at", "status": "pending",
                     "at": clock["t"] + 10_000, "created_at": 0}]
    engine._tick()
    assert started == []


def test_tick_fires_earliest_first(engine, clock):
    started = _stub_start(engine)
    engine.settings["max_concurrent"] = 1
    # 'b' is eligible earlier than 'a' despite list order
    engine.tasks = [
        {"id": "a", "sched_type": "at", "status": "pending",
         "at": clock["t"] - 5, "created_at": 0},
        {"id": "b", "sched_type": "at", "status": "pending",
         "at": clock["t"] - 50, "created_at": 0},
    ]
    engine._tick()
    assert started == ["b"]


def test_tick_counts_already_running_against_limit(engine, clock):
    started = _stub_start(engine)
    engine.settings["max_concurrent"] = 1
    engine.tasks = [
        {"id": "run", "sched_type": "now", "status": "running",
         "created_at": clock["t"] - 100},
        {"id": "a", "sched_type": "now", "status": "pending",
         "created_at": clock["t"] - 10},
    ]
    engine._tick()
    assert started == []              # the slot is taken by the running task


# ---------------------------------------------------------------------------
# _reschedule_if_repeat
# ---------------------------------------------------------------------------
def test_reschedule_no_repeat_is_noop(engine):
    t = {"id": "a", "status": "done", "repeat": False}
    engine.tasks = [t]
    engine._reschedule_if_repeat(t)
    assert t["status"] == "done"


def test_reschedule_repeat_sets_scheduled(engine):
    t = {"id": "a", "status": "done", "repeat": True, "repeat_hours": 5,
         "repeat_until": "", "sched_type": "session_reset",
         "target_reset": 999}
    engine.tasks = [t]
    before = __import__("time").time()
    engine._reschedule_if_repeat(t)
    assert t["status"] == "scheduled"
    assert t["target_reset"] is None
    assert t["not_before"] >= before + 5 * 3600 - 2


def test_reschedule_at_advances_time(engine):
    base_at = 1_000_000.0
    t = {"id": "a", "status": "done", "repeat": True, "repeat_hours": 5,
         "repeat_until": "", "sched_type": "at", "at": base_at}
    engine.tasks = [t]
    engine._reschedule_if_repeat(t)
    assert t["at"] == base_at + 5 * 3600


def test_reschedule_stops_after_until_in_past(engine):
    # a repeat_until a couple of minutes ago should stop the repeat
    past = (datetime.now() - timedelta(minutes=2)).strftime("%H:%M")
    t = {"id": "a", "status": "done", "repeat": True, "repeat_hours": 5,
         "repeat_until": past, "sched_type": "now"}
    engine.tasks = [t]
    engine._reschedule_if_repeat(t)
    assert t["status"] == "done"          # not re-queued


def test_reschedule_continues_before_until(engine):
    future = (datetime.now() + timedelta(minutes=5)).strftime("%H:%M")
    t = {"id": "a", "status": "done", "repeat": True, "repeat_hours": 0,
         "repeat_until": future, "sched_type": "now"}
    engine.tasks = [t]
    engine._reschedule_if_repeat(t)
    assert t["status"] == "scheduled"


def test_reschedule_until_rolls_over_to_tomorrow(engine):
    # an "until" time well over 12h in the past is treated as tomorrow's window,
    # so the repeat continues rather than stopping immediately.
    rollover = (datetime.now() - timedelta(hours=13)).strftime("%H:%M")
    t = {"id": "a", "status": "done", "repeat": True, "repeat_hours": 0,
         "repeat_until": rollover, "sched_type": "now"}
    engine.tasks = [t]
    engine._reschedule_if_repeat(t)
    assert t["status"] == "scheduled"


def test_reschedule_until_malformed_is_ignored(engine):
    # a garbage repeat_until must not crash; the repeat just proceeds.
    t = {"id": "a", "status": "done", "repeat": True, "repeat_hours": 0,
         "repeat_until": "not-a-time", "sched_type": "now"}
    engine.tasks = [t]
    engine._reschedule_if_repeat(t)
    assert t["status"] == "scheduled"


# ---------------------------------------------------------------------------
# when_text
# ---------------------------------------------------------------------------
def test_when_text_running(engine):
    assert engine.when_text({"status": "running"}) == "running…"


def test_when_text_done(engine):
    out = engine.when_text({"status": "done", "ended_at": 1_000_000.0})
    assert out.startswith("done · ")


def test_when_text_failed(engine):
    assert engine.when_text({"status": "failed", "exit_code": 1}) == \
        "failed · rc=1"


def test_when_text_waiting_on_previous(engine):
    prev = {"id": "p", "status": "pending"}
    t = {"id": "a", "status": "pending", "sched_type": "after_prev",
         "offset_min": 0, "created_at": 0}
    engine.tasks = [prev, t]
    assert engine.when_text(t) == "waiting on previous"


def test_when_text_paused(engine, clock):
    engine.armed = False
    t = {"id": "a", "status": "pending", "sched_type": "now",
         "created_at": clock["t"] + 100}
    assert engine.when_text(t).endswith("(paused)")


def test_when_text_armed_future(engine, clock):
    engine.armed = True
    t = {"id": "a", "status": "pending", "sched_type": "now",
         "created_at": clock["t"] + 3600}
    out = engine.when_text(t)
    assert out.startswith("in ")


def test_when_text_armed_starting(engine, clock):
    engine.armed = True
    t = {"id": "a", "status": "pending", "sched_type": "now",
         "created_at": clock["t"] - 5}
    assert engine.when_text(t) == "starting…"


# ---------------------------------------------------------------------------
# App._sched_summary (pure — does not touch self)
# ---------------------------------------------------------------------------
def test_sched_summary_now(ct_mod):
    assert ct_mod.App._sched_summary(None, {"sched_type": "now"}) == "asap"


def test_sched_summary_session_reset_with_repeat(ct_mod):
    t = {"sched_type": "session_reset", "reset_account": "c2",
         "offset_min": 5, "repeat": True, "repeat_hours": 5}
    out = ct_mod.App._sched_summary(None, t)
    assert "c2 reset +5m" in out
    assert "⟳5h" in out


def test_sched_summary_after_prev(ct_mod):
    t = {"sched_type": "after_prev", "offset_min": 3}
    assert ct_mod.App._sched_summary(None, t) == "after prev +3m"

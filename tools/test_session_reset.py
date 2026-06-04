# -*- coding: utf-8 -*-
"""Repro/regression test for session_reset scheduling.

Simulates the real OAuth behaviour: `resets_at` is always the *upcoming* reset
(in the future), and the instant the window resets it jumps forward by ~5h.
Walks simulated time across a reset boundary and checks whether a
session_reset task ever becomes ready (fire_time <= now).
"""
import importlib.util
import os
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PYW = os.path.join(os.path.dirname(HERE), "claude_tasker.pyw")

spec = importlib.util.spec_from_file_location("ct", PYW)
ct = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ct)


def make_engine():
    e = object.__new__(ct.Engine)
    e.lock = threading.RLock()
    e.usage = {}
    e.settings = {}
    e.tasks = []
    return e


def run_sim(label):
    e = make_engine()
    now = time.time()
    task = {"id": "t1", "sched_type": "session_reset", "reset_account": "claude2",
            "offset_min": 5.0, "status": "pending", "created_at": now,
            "not_before": None}
    e.tasks = [task]

    reset = now + 30          # next reset is 30s away
    fired_at = None
    closest = None            # smallest (ft - now): how close we ever got
    for step in range(0, 600, 3):        # 10 min of 3s ticks
        sim_now = now + step
        if sim_now >= reset:             # window reset -> API advances by ~5h
            reset = reset + 5 * 3600
        e.usage = {"claude2": {"five": {"util": 10.0, "reset": reset}}}

        if hasattr(e, "_latch_resets"):  # fixed code freezes the target
            e._latch_resets()

        ft = e.fire_time(task)
        if ft is not None:
            gap = ft - sim_now
            closest = gap if closest is None else min(closest, gap)
            if ft <= sim_now:
                fired_at = step
                break

    if fired_at is not None:
        print(f"[{label}] FIRED at +{fired_at}s after arming  -> PASS")
        return True
    print(f"[{label}] NEVER fired over 10 min; closest it ever got: "
          f"fire_time was still {closest:.0f}s in the future -> FAIL")
    return False


if __name__ == "__main__":
    ok = run_sim("session_reset")
    raise SystemExit(0 if ok else 1)

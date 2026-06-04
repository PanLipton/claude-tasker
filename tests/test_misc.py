# -*- coding: utf-8 -*-
"""Edge cases: the HTTP helper, Engine bootstrap, and odd fire_time inputs."""
import json
import threading


# ---------------------------------------------------------------------------
# _http_json
# ---------------------------------------------------------------------------
class _FakeResp:
    def __init__(self, payload):
        self._payload = payload
    def read(self):
        return json.dumps(self._payload).encode()
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def test_http_json_parses_response(ct_mod, monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None, context=None):
        captured["url"] = req.full_url
        captured["headers"] = req.headers
        captured["method"] = req.get_method()
        return _FakeResp({"ok": True})
    monkeypatch.setattr(ct_mod.urllib.request, "urlopen", fake_urlopen)
    out = ct_mod._http_json("https://example/api", token="tok")
    assert out == {"ok": True}
    # token-auth headers are attached (urllib title-cases header keys)
    assert captured["headers"]["Authorization"] == "Bearer tok"
    assert captured["method"] == "GET"


def test_http_json_posts_body(ct_mod, monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None, context=None):
        captured["data"] = req.data
        captured["method"] = req.get_method()
        return _FakeResp({"got": "it"})
    monkeypatch.setattr(ct_mod.urllib.request, "urlopen", fake_urlopen)
    out = ct_mod._http_json("https://example/api", method="POST",
                            body={"a": 1})
    assert out == {"got": "it"}
    assert json.loads(captured["data"].decode()) == {"a": 1}
    assert captured["method"] == "POST"


# ---------------------------------------------------------------------------
# Engine.__init__ — bootstrap wiring (no real worker thread)
# ---------------------------------------------------------------------------
def test_engine_init_wires_state(ct_mod, monkeypatch, isolated_paths):
    started = {}

    class FakeThread:
        def __init__(self, target=None, daemon=None):
            started["target"] = target
            started["daemon"] = daemon
        def start(self):
            started["started"] = True
    monkeypatch.setattr(ct_mod.threading, "Thread", FakeThread)

    e = ct_mod.Engine()
    assert e.armed is False
    assert e.tasks == []
    assert e.runtime == {}
    assert started["started"] is True
    assert started["daemon"] is True
    # settings were persisted on first run
    import os
    assert os.path.exists(ct_mod.SETTINGS_PATH)


def test_engine_init_demotes_crashed_running_tasks(ct_mod, monkeypatch,
                                                   isolated_paths):
    # a task left "running" from a previous crash must come back as failed
    ct_mod.atomic_write(ct_mod.TASKS_PATH, json.dumps(
        [{"id": "x", "status": "running"}]))
    monkeypatch.setattr(ct_mod.threading, "Thread",
                        lambda target=None, daemon=None: type(
                            "T", (), {"start": lambda self: None})())
    e = ct_mod.Engine()
    assert e.tasks[0]["status"] == "failed"
    assert e.tasks[0]["exit_code"] == -1


# ---------------------------------------------------------------------------
# fire_time — unknown sched_type falls back to created_at
# ---------------------------------------------------------------------------
def test_fire_time_unknown_type_uses_created_at(engine):
    t = {"id": "a", "sched_type": "bogus", "created_at": 777}
    assert engine.fire_time(t) == 777

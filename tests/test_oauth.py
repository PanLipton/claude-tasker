# -*- coding: utf-8 -*-
"""Tests for OAuth credential handling and usage fetching.

All network access (``_http_json``) is monkeypatched — no real requests.
"""
import json
import time
import urllib.error

import pytest


def _write_creds(tmp_path, access="tokA", refresh="refA", expires_at_ms=None):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    if expires_at_ms is None:
        expires_at_ms = int((time.time() + 10_000) * 1000)
    (cfg / ".credentials.json").write_text(json.dumps({
        "claudeAiOauth": {
            "accessToken": access,
            "refreshToken": refresh,
            "expiresAt": expires_at_ms,
        }
    }), encoding="utf-8")
    return cfg


# -- read_credentials -------------------------------------------------------
def test_read_credentials(ct_mod, tmp_path):
    cfg = _write_creds(tmp_path)
    path, data = ct_mod.read_credentials(str(cfg))
    assert path.endswith(".credentials.json")
    assert data["claudeAiOauth"]["accessToken"] == "tokA"


# -- refresh_token ----------------------------------------------------------
def test_refresh_token_writes_back(ct_mod, tmp_path, monkeypatch):
    cfg = _write_creds(tmp_path, access="old", refresh="oldR")
    cred_path, cred_data = ct_mod.read_credentials(str(cfg))

    monkeypatch.setattr(ct_mod, "_http_json", lambda *a, **k: {
        "access_token": "newA", "refresh_token": "newR", "expires_in": 3600,
    })
    before = time.time()
    token = ct_mod.refresh_token(cred_path, cred_data)
    assert token == "newA"

    on_disk = json.loads((cfg / ".credentials.json").read_text(encoding="utf-8"))
    oauth = on_disk["claudeAiOauth"]
    assert oauth["accessToken"] == "newA"
    assert oauth["refreshToken"] == "newR"
    assert oauth["expiresAt"] >= int((before + 3600) * 1000)


def test_refresh_token_keeps_old_refresh_when_omitted(ct_mod, tmp_path, monkeypatch):
    cfg = _write_creds(tmp_path, refresh="keepme")
    cred_path, cred_data = ct_mod.read_credentials(str(cfg))
    monkeypatch.setattr(ct_mod, "_http_json", lambda *a, **k: {
        "access_token": "newA", "expires_in": 100,
    })
    ct_mod.refresh_token(cred_path, cred_data)
    assert cred_data["claudeAiOauth"]["refreshToken"] == "keepme"


# -- get_token --------------------------------------------------------------
def test_get_token_uses_cached_when_fresh(ct_mod, tmp_path, monkeypatch):
    cfg = _write_creds(tmp_path, access="cached",
                       expires_at_ms=int((time.time() + 10_000) * 1000))

    def fail(*a, **k):
        raise AssertionError("should not refresh a fresh token")

    monkeypatch.setattr(ct_mod, "_http_json", fail)
    assert ct_mod.get_token(str(cfg)) == "cached"


def test_get_token_refreshes_when_expired(ct_mod, tmp_path, monkeypatch):
    cfg = _write_creds(tmp_path, access="stale",
                       expires_at_ms=int((time.time() - 10) * 1000))
    monkeypatch.setattr(ct_mod, "_http_json", lambda *a, **k: {
        "access_token": "fresh", "expires_in": 3600,
    })
    assert ct_mod.get_token(str(cfg)) == "fresh"


def test_get_token_force_refreshes_even_when_fresh(ct_mod, tmp_path, monkeypatch):
    cfg = _write_creds(tmp_path, access="cached",
                       expires_at_ms=int((time.time() + 10_000) * 1000))
    monkeypatch.setattr(ct_mod, "_http_json", lambda *a, **k: {
        "access_token": "forced", "expires_in": 3600,
    })
    assert ct_mod.get_token(str(cfg), force=True) == "forced"


# -- fetch_account_direct ---------------------------------------------------
def _http_error(code):
    return urllib.error.HTTPError("u", code, "msg", None, None)


def test_fetch_account_direct_success(ct_mod, monkeypatch):
    monkeypatch.setattr(ct_mod, "get_token", lambda cd, force=False: "tok")

    def fake_http(url, **kw):
        if url == ct_mod.USAGE_URL:
            return {"five_hour": {"utilization": 42.5,
                                  "resets_at": "2026-06-05T10:00:00+00:00"},
                    "seven_day": {"utilization": 12.0,
                                  "resets_at": "2026-06-08T10:00:00+00:00"}}
        if url == ct_mod.PROFILE_URL:
            return {"account": {"email": "u@e.com"}}
        raise AssertionError("unexpected url %s" % url)

    monkeypatch.setattr(ct_mod, "_http_json", fake_http)
    out = ct_mod.fetch_account_direct({"config_dir": "x"})
    assert out["error"] is None
    assert out["five"]["util"] == 42.5
    assert out["seven"]["util"] == 12.0
    assert out["five"]["reset"] == ct_mod.parse_reset("2026-06-05T10:00:00+00:00")
    assert out["email"] == "u@e.com"
    assert out["updated_at"] is not None


def test_fetch_account_direct_retries_on_401(ct_mod, monkeypatch):
    monkeypatch.setattr(ct_mod, "get_token", lambda cd, force=False: "tok")
    calls = {"usage": 0}

    def fake_http(url, **kw):
        if url == ct_mod.USAGE_URL:
            calls["usage"] += 1
            if calls["usage"] == 1:
                raise _http_error(401)
            return {"five_hour": {"utilization": 5.0}, "seven_day": {}}
        if url == ct_mod.PROFILE_URL:
            return {"account": {"email": "e@e.com"}}
        raise AssertionError(url)

    monkeypatch.setattr(ct_mod, "_http_json", fake_http)
    out = ct_mod.fetch_account_direct({"config_dir": "x"})
    assert out["error"] is None
    assert out["five"]["util"] == 5.0
    assert calls["usage"] == 2          # retried once after forced refresh


def test_fetch_account_direct_rate_limited(ct_mod, monkeypatch):
    monkeypatch.setattr(ct_mod, "get_token", lambda cd, force=False: "tok")
    monkeypatch.setattr(ct_mod, "_http_json",
                        lambda url, **kw: (_ for _ in ()).throw(_http_error(429)))
    out = ct_mod.fetch_account_direct({"config_dir": "x"})
    assert out["error"] == "rate limited"


def test_fetch_account_direct_offline(ct_mod, monkeypatch):
    monkeypatch.setattr(ct_mod, "get_token", lambda cd, force=False: "tok")

    def boom(url, **kw):
        raise urllib.error.URLError("no route")

    monkeypatch.setattr(ct_mod, "_http_json", boom)
    out = ct_mod.fetch_account_direct({"config_dir": "x"})
    assert out["error"] == "offline"


def test_fetch_account_direct_no_credentials(ct_mod, monkeypatch):
    def boom(cd, force=False):
        raise FileNotFoundError("missing")

    monkeypatch.setattr(ct_mod, "get_token", boom)
    out = ct_mod.fetch_account_direct({"config_dir": "x"})
    assert out["error"] == "no credentials"


def test_fetch_account_direct_preserves_prev_on_error(ct_mod, monkeypatch):
    monkeypatch.setattr(ct_mod, "get_token", lambda cd, force=False: "tok")
    monkeypatch.setattr(ct_mod, "_http_json",
                        lambda url, **kw: (_ for _ in ()).throw(_http_error(429)))
    prev = {"five": {"util": 1.0}, "seven": {"util": 2.0}, "email": "p@p.com"}
    out = ct_mod.fetch_account_direct({"config_dir": "x"}, prev)
    assert out["five"] == {"util": 1.0}
    assert out["email"] == "p@p.com"


def test_fetch_account_direct_profile_error_keeps_usage(ct_mod, monkeypatch):
    # usage succeeds, but the profile lookup blows up -> email stays empty,
    # usage data is still returned with no error.
    monkeypatch.setattr(ct_mod, "get_token", lambda cd, force=False: "tok")

    def fake_http(url, **kw):
        if url == ct_mod.USAGE_URL:
            return {"five_hour": {"utilization": 9.0}, "seven_day": {}}
        raise RuntimeError("profile down")
    monkeypatch.setattr(ct_mod, "_http_json", fake_http)
    out = ct_mod.fetch_account_direct({"config_dir": "x"})
    assert out["error"] is None
    assert out["five"]["util"] == 9.0
    assert out["email"] is None


def test_fetch_account_direct_unexpected_error_is_named(ct_mod, monkeypatch):
    # an unexpected exception type is reported by class name, not crashed on.
    def boom(cd, force=False):
        raise ValueError("weird")
    monkeypatch.setattr(ct_mod, "get_token", boom)
    out = ct_mod.fetch_account_direct({"config_dir": "x"})
    assert out["error"] == "ValueError"


# -- read_widget_usage ------------------------------------------------------
def test_read_widget_usage_no_dir(ct_mod):
    assert ct_mod.read_widget_usage("") == {}
    assert ct_mod.read_widget_usage(None) == {}


def test_read_widget_usage_fresh(ct_mod, tmp_path):
    widget = tmp_path / "widget"
    widget.mkdir()
    (widget / "widget_usage.json").write_text(json.dumps({
        "updated_at": time.time(),
        "accounts": [{
            "config_dir_expanded": "C:\\Users\\me\\.claude",
            "five": {"util": 30.0}, "seven": {"util": 9.0},
            "email": "w@w.com", "error": None,
        }],
    }), encoding="utf-8")
    import os
    out = ct_mod.read_widget_usage(str(widget))
    key = os.path.normcase("C:\\Users\\me\\.claude")
    assert key in out
    assert out[key]["five"] == {"util": 30.0}
    assert out[key]["email"] == "w@w.com"


def test_read_widget_usage_stale_returns_empty(ct_mod, tmp_path):
    widget = tmp_path / "widget"
    widget.mkdir()
    (widget / "widget_usage.json").write_text(json.dumps({
        "updated_at": time.time() - (ct_mod.WIDGET_FRESH_SEC + 60),
        "accounts": [{"config_dir_expanded": "C:\\x"}],
    }), encoding="utf-8")
    assert ct_mod.read_widget_usage(str(widget)) == {}


def test_read_widget_usage_missing_file(ct_mod, tmp_path):
    widget = tmp_path / "widget"
    widget.mkdir()
    assert ct_mod.read_widget_usage(str(widget)) == {}

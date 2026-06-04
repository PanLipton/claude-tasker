# -*- coding: utf-8 -*-
"""Tests for the persistence / settings helpers."""
import json
import os

import pytest


# -- expand -----------------------------------------------------------------
def test_expand_none_and_empty(ct_mod):
    assert ct_mod.expand(None) == ""
    assert ct_mod.expand("") == ""


def test_expand_env_var(ct_mod, monkeypatch):
    monkeypatch.setenv("CT_TEST_VAR", "hello")
    out = ct_mod.expand("%CT_TEST_VAR%\\sub")
    assert out == "hello\\sub"


def test_expand_user(ct_mod):
    home = os.path.expanduser("~")
    out = ct_mod.expand("~\\thing")
    assert "~" not in out
    assert out.startswith(home)


# -- atomic_write -----------------------------------------------------------
def test_atomic_write_roundtrip(ct_mod, tmp_path):
    target = tmp_path / "out.txt"
    ct_mod.atomic_write(str(target), "héllo\nwörld")
    assert target.read_text(encoding="utf-8") == "héllo\nwörld"
    # the temporary side-file must not linger
    assert not (tmp_path / "out.txt.tmp").exists()


def test_atomic_write_overwrites(ct_mod, tmp_path):
    target = tmp_path / "out.txt"
    target.write_text("old", encoding="utf-8")
    ct_mod.atomic_write(str(target), "new")
    assert target.read_text(encoding="utf-8") == "new"


# -- load_json --------------------------------------------------------------
def test_load_json_valid(ct_mod, tmp_path):
    p = tmp_path / "data.json"
    p.write_text(json.dumps({"a": 1}), encoding="utf-8")
    assert ct_mod.load_json(str(p), {}) == {"a": 1}


def test_load_json_missing_returns_default(ct_mod, tmp_path):
    sentinel = {"default": True}
    assert ct_mod.load_json(str(tmp_path / "nope.json"), sentinel) is sentinel


def test_load_json_corrupt_returns_default(ct_mod, tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{ this is not json", encoding="utf-8")
    assert ct_mod.load_json(str(p), []) == []


# -- default_settings -------------------------------------------------------
def test_default_settings_shape(ct_mod):
    s = ct_mod.default_settings()
    for key in ("widget_dir", "claude_bin", "max_concurrent",
                "usage_poll_seconds", "default_account", "default_perm",
                "accounts"):
        assert key in s
    assert s["accounts"] == []
    assert s["claude_bin"] == "claude"
    assert s["max_concurrent"] == 1


# -- discover_accounts ------------------------------------------------------
def test_discover_accounts_finds_dot_claude_dirs(ct_mod, tmp_path, monkeypatch):
    home = tmp_path / "home"
    for name in (".claude", ".claude-account1", ".claude-account2",
                 "unrelated", ".bashrc_dir"):
        (home / name).mkdir(parents=True)
    monkeypatch.setattr(ct_mod.os.path, "expanduser", lambda p: str(home))

    accts = ct_mod.discover_accounts()
    labels = {a["label"] for a in accts}
    assert labels == {"claude", "claude1", "claude2"}
    by_label = {a["label"]: a["config_dir"] for a in accts}
    assert by_label["claude1"] == str(home / ".claude-account1")


def test_discover_accounts_fallback_when_none(ct_mod, tmp_path, monkeypatch):
    home = tmp_path / "empty_home"
    home.mkdir()
    monkeypatch.setattr(ct_mod.os.path, "expanduser", lambda p: str(home))
    accts = ct_mod.discover_accounts()
    assert accts == [{"label": "claude",
                      "config_dir": os.path.join(str(home), ".claude")}]


def test_discover_accounts_handles_unreadable_home(ct_mod, monkeypatch):
    monkeypatch.setattr(ct_mod.os.path, "expanduser", lambda p: "/no/such/home")

    def boom(_):
        raise OSError("denied")

    monkeypatch.setattr(ct_mod.os, "listdir", boom)
    # falls back gracefully instead of raising
    accts = ct_mod.discover_accounts()
    assert accts and accts[0]["label"] == "claude"


# -- save / load settings ---------------------------------------------------
def test_save_and_load_settings_roundtrip(ct_mod, monkeypatch):
    # avoid scanning the real home when the file has no accounts
    monkeypatch.setattr(ct_mod, "discover_accounts",
                        lambda: [{"label": "x", "config_dir": "C:\\x"}])
    s = ct_mod.default_settings()
    s["claude_bin"] = "C:\\custom\\claude.exe"
    s["accounts"] = [{"label": "acc", "config_dir": "C:\\acc"}]
    ct_mod.save_settings(s)

    loaded = ct_mod.load_settings()
    assert loaded["claude_bin"] == "C:\\custom\\claude.exe"
    assert loaded["accounts"] == [{"label": "acc", "config_dir": "C:\\acc"}]


def test_load_settings_seeds_accounts_when_absent(ct_mod, monkeypatch):
    seeded = [{"label": "seed", "config_dir": "C:\\seed"}]
    monkeypatch.setattr(ct_mod, "discover_accounts", lambda: seeded)
    # SETTINGS_PATH points at a non-existent temp file -> defaults + seed
    loaded = ct_mod.load_settings()
    assert loaded["accounts"] == seeded


def test_load_settings_merges_partial_file(ct_mod, monkeypatch):
    # a settings file that only overrides one key keeps defaults for the rest
    ct_mod.atomic_write(ct_mod.SETTINGS_PATH,
                        json.dumps({"max_concurrent": 4,
                                    "accounts": [{"label": "a",
                                                  "config_dir": "C:\\a"}]}))
    loaded = ct_mod.load_settings()
    assert loaded["max_concurrent"] == 4
    assert loaded["claude_bin"] == "claude"          # default preserved
    assert loaded["usage_poll_seconds"] == ct_mod.USAGE_POLL_DEFAULT

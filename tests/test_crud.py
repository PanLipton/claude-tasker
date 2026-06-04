# -*- coding: utf-8 -*-
"""Tests for task CRUD, persistence round-tripping and account wiring."""
import json


def _read_tasks(ct_mod):
    return json.loads(open(ct_mod.TASKS_PATH, encoding="utf-8").read())


# -- add / get / update -----------------------------------------------------
def test_add_task_persists(engine, ct_mod):
    engine.add_task({"id": "a", "name": "first"})
    assert engine.get_task("a")["name"] == "first"
    on_disk = _read_tasks(ct_mod)
    assert on_disk[0]["id"] == "a"
    assert on_disk[0]["name"] == "first"


def test_get_task_missing(engine):
    assert engine.get_task("nope") is None


def test_update_task_saves(engine, ct_mod):
    t = {"id": "a", "name": "before"}
    engine.add_task(t)
    t["name"] = "after"
    engine.update_task(t)
    assert _read_tasks(ct_mod)[0]["name"] == "after"


# -- delete -----------------------------------------------------------------
def test_delete_task(engine, ct_mod):
    engine.add_task({"id": "a", "name": "x"})
    engine.add_task({"id": "b", "name": "y"})
    engine.delete_task("a")
    assert engine.get_task("a") is None
    assert engine.get_task("b") is not None
    assert {t["id"] for t in _read_tasks(ct_mod)} == {"b"}


# -- move -------------------------------------------------------------------
def test_move_task(engine):
    engine.tasks = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    engine.move_task("c", -1)
    assert [t["id"] for t in engine.tasks] == ["a", "c", "b"]


def test_move_task_out_of_bounds_noop(engine):
    engine.tasks = [{"id": "a"}, {"id": "b"}]
    engine.move_task("a", -1)        # already at the top
    assert [t["id"] for t in engine.tasks] == ["a", "b"]
    engine.move_task("b", 1)         # already at the bottom
    assert [t["id"] for t in engine.tasks] == ["a", "b"]


def test_move_task_unknown_id_noop(engine):
    engine.tasks = [{"id": "a"}]
    engine.move_task("zzz", 1)
    assert [t["id"] for t in engine.tasks] == ["a"]


# -- reset_task -------------------------------------------------------------
def test_reset_task_requeues_finished(engine):
    t = {"id": "a", "status": "failed", "started_at": 1, "ended_at": 2,
         "exit_code": 1, "not_before": 5, "target_reset": 9}
    engine.tasks = [t]
    engine.reset_task("a")
    assert t["status"] == "pending"
    assert t["started_at"] is None
    assert t["ended_at"] is None
    assert t["exit_code"] is None
    assert t["not_before"] is None
    assert t["target_reset"] is None


def test_reset_task_ignores_running(engine):
    t = {"id": "a", "status": "running"}
    engine.tasks = [t]
    engine.reset_task("a")
    assert t["status"] == "running"


# -- _load_tasks ------------------------------------------------------------
def test_load_tasks_resurrects_running_as_failed(engine, ct_mod):
    ct_mod.atomic_write(ct_mod.TASKS_PATH, json.dumps([
        {"id": "a", "status": "running"},
        {"id": "b", "status": "done", "exit_code": 0},
    ]))
    tasks = engine._load_tasks()
    by_id = {t["id"]: t for t in tasks}
    assert by_id["a"]["status"] == "failed"
    assert by_id["a"]["exit_code"] == -1
    assert by_id["b"]["status"] == "done"      # untouched


def test_load_tasks_handles_non_list(engine, ct_mod):
    ct_mod.atomic_write(ct_mod.TASKS_PATH, json.dumps({"not": "a list"}))
    assert engine._load_tasks() == []


def test_load_tasks_missing_file(engine):
    # TASKS_PATH points at a non-existent temp file
    assert engine._load_tasks() == []


# -- save_tasks only serialises whitelisted fields --------------------------
def test_save_tasks_drops_runtime_only_fields(engine, ct_mod):
    engine.tasks = [{
        "id": "a", "name": "n", "status": "pending",
        "target_reset": 1234,          # runtime-only, must NOT be persisted
        "secret_runtime": "drop me",   # not in SERIAL_FIELDS
        "prompt": "hi",
    }]
    engine.save_tasks()
    saved = _read_tasks(ct_mod)[0]
    assert "target_reset" not in saved
    assert "secret_runtime" not in saved
    assert saved["prompt"] == "hi"
    # every persisted key is part of the documented schema
    assert set(saved).issubset(set(ct_mod.SERIAL_FIELDS))


# -- accounts ---------------------------------------------------------------
def test_account_labels(engine):
    engine.accounts = [{"label": "c1", "config_dir": "x"},
                       {"label": "c2", "config_dir": "y"}]
    assert engine.account_labels() == ["c1", "c2"]


def test_config_dir_for_expands(engine, monkeypatch):
    monkeypatch.setenv("CT_ACC_HOME", "C:\\base")
    engine.accounts = [{"label": "c1", "config_dir": "%CT_ACC_HOME%\\.claude"}]
    assert engine.config_dir_for("c1") == "C:\\base\\.claude"


def test_config_dir_for_unknown(engine):
    assert engine.config_dir_for("nope") is None


def test_set_accounts_persists_and_resets_poll(engine, ct_mod):
    engine._usage_next = {"stale": 123}
    new = [{"label": "c9", "config_dir": "C:\\c9"}]
    engine.set_accounts(new)
    assert engine.accounts == new
    assert engine.settings["accounts"] == new
    assert engine._usage_next == {}          # forced fresh fetch next tick
    saved = json.loads(open(ct_mod.SETTINGS_PATH, encoding="utf-8").read())
    assert saved["accounts"] == new

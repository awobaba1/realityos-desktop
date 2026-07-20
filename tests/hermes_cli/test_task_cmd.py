"""Smoke tests for the ``hermes task`` I/O adapter (ADR-V6-046 / A3).

Wires the CLI handler against a temp PTG store so the full path runs without a
tty: founder resolution → list → mark by #N / by name / by uuid. Pure-logic
coverage lives in ``test_adr_v6_046_r12_explicit.py``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hermes_cli import task_cmd
from plugins.memory.ptg.store import PTGStore


@pytest.fixture
def temp_store(tmp_path, monkeypatch):
    db_path = str(tmp_path / "ptg.db")
    monkeypatch.setattr(task_cmd, "load_ptg_config", lambda: {})
    monkeypatch.setattr(task_cmd, "resolve_db_path", lambda _cfg: db_path)
    s = PTGStore(db_path=db_path)
    s.ensure_founder("u1", "founder@realityos.local")
    # resolve_founder reads founder_user_id from ptg_meta when no config override.
    s._conn.execute(
        "INSERT OR REPLACE INTO ptg_meta(key, value) VALUES ('founder_user_id', ?)",
        ("u1",))
    yield s
    s.close()


def _seed_task(store, desc):
    return store.insert_meaning_event(
        user_id="u1", source_text="x", intent_class="Need_To_Do",
        task_description=desc, atom_kind="R2",
        confidence_base=0.8, relation_confidence=0.8)


def test_task_list_empty(temp_store, capsys):
    rc = task_cmd.cmd_task(SimpleNamespace(task_command="list"))
    assert rc == 0
    assert "没有待办" in capsys.readouterr().out


def test_task_list_shows_numbered(temp_store, capsys):
    _seed_task(temp_store, "交报告")
    _seed_task(temp_store, "回邮件")
    rc = task_cmd.cmd_task(SimpleNamespace(task_command="list"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "#1" in out and "#2" in out
    assert "交报告" in out or "回邮件" in out


def test_task_done_by_number(temp_store, capsys):
    _seed_task(temp_store, "做某事")
    rc = task_cmd.cmd_task(SimpleNamespace(task_command="done", ref="1", note=None))
    assert rc == 0
    out = capsys.readouterr().out
    assert "已完成" in out
    # promoted + dropped off open list
    assert len(temp_store.list_open_tasks("u1")) == 0


def test_task_done_by_name_fragment(temp_store, capsys):
    _seed_task(temp_store, "交季度报告")
    rc = task_cmd.cmd_task(SimpleNamespace(task_command="done", ref="季度报告", note="发了"))
    assert rc == 0
    assert len(temp_store.list_open_tasks("u1")) == 0
    # resolution_note landed in completion_note
    row = temp_store._conn.execute(
        "SELECT completion_note FROM meaning_events WHERE atom_kind='R12'").fetchone()
    import json
    assert json.loads(row[0])["resolution_note"] == "发了"


def test_task_failed_by_uuid(temp_store, capsys):
    tid = _seed_task(temp_store, "x")
    rc = task_cmd.cmd_task(SimpleNamespace(task_command="failed", ref=tid, note=None))
    assert rc == 0
    status = temp_store._conn.execute(
        "SELECT task_status FROM meaning_events WHERE id=?", (tid,)).fetchone()[0]
    assert status == "dismissed"


def test_task_not_found_returns_nonzero(temp_store, capsys):
    _seed_task(temp_store, "x")
    rc = task_cmd.cmd_task(
        SimpleNamespace(task_command="done", ref="不存在的任务", note=None))
    assert rc == 1
    assert "task list" in capsys.readouterr().out


def test_task_delayed_stays_open_overdue(temp_store, capsys):
    _seed_task(temp_store, "稍后做")
    rc = task_cmd.cmd_task(SimpleNamespace(task_command="delayed", ref="1", note=None))
    assert rc == 0
    rows = temp_store.list_open_tasks("u1")
    assert len(rows) == 1 and rows[0]["is_overdue"] == 1


def test_task_no_action_defaults_to_list(temp_store, capsys):
    _seed_task(temp_store, "x")
    rc = task_cmd.cmd_task(SimpleNamespace(task_command=None))
    assert rc == 0
    assert "#1" in capsys.readouterr().out

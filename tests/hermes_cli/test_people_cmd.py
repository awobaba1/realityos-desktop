"""Smoke tests for the ``hermes people`` I/O adapter (ADR-V6-048 / A5).

Wires the CLI handler against a temp PTG store so the full path runs without a
tty: founder resolution → list → resolve name → show profile. Pure-logic
coverage lives in ``test_adr_v6_048_people.py``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hermes_cli import people_cmd
from plugins.memory.ptg.store import PTGStore


@pytest.fixture
def temp_store(tmp_path, monkeypatch):
    db_path = str(tmp_path / "ptg.db")
    monkeypatch.setattr(people_cmd, "load_ptg_config", lambda: {})
    monkeypatch.setattr(people_cmd, "resolve_db_path", lambda _cfg: db_path)
    s = PTGStore(db_path=db_path)
    s.ensure_founder("u1", "founder@realityos.local")
    s._conn.execute(
        "INSERT OR REPLACE INTO ptg_meta(key, value) VALUES ('founder_user_id', ?)",
        ("u1",))
    yield s
    s.close()


def _seed_person(store, name="张三", aliases=None, mention_count=5):
    props = {"aliases": aliases} if aliases else {}
    eid = store.upsert_entity(
        user_id="u1", entity_name=name, entity_type="person", properties=props)
    if mention_count:
        store._conn.execute(
            "UPDATE entities SET mention_count=? WHERE id=?", (mention_count, eid))
        store._conn.commit()
    return eid


def test_people_list_empty(temp_store, capsys):
    rc = people_cmd.cmd_people(SimpleNamespace(people_command="list"))
    assert rc == 0
    assert "还没有" in capsys.readouterr().out


def test_people_list_shows_numbered(temp_store, capsys):
    _seed_person(temp_store, "甲", mention_count=2)
    _seed_person(temp_store, "乙", mention_count=9)
    rc = people_cmd.cmd_people(SimpleNamespace(people_command="list"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "#1" in out and "乙" in out  # highest mention_count first
    assert "提及 9 次" in out


def test_people_show_by_name(temp_store, capsys):
    eid = _seed_person(temp_store, "张三")
    temp_store.insert_identity_event(
        user_id="u1", source_text="x", person_name="张三", mention_context="开会聊项目",
        sentiment="positive", interaction_type="meeting",
        confidence_base=0.9, relation_confidence=0.9)
    rc = people_cmd.cmd_people(SimpleNamespace(people_command="show", ref="张三"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "人物画像" in out and "张三" in out
    assert "开会聊项目" in out  # recent context surfaced


def test_people_show_by_id(temp_store, capsys):
    eid = _seed_person(temp_store, "李四")
    rc = people_cmd.cmd_people(SimpleNamespace(people_command="show", ref=eid))
    assert rc == 0
    assert "李四" in capsys.readouterr().out


def test_people_show_not_found(temp_store, capsys):
    _seed_person(temp_store, "张三")
    rc = people_cmd.cmd_people(
        SimpleNamespace(people_command="show", ref="不存在的人"))
    assert rc == 1
    assert "找不到" in capsys.readouterr().out


def test_people_no_action_defaults_to_list(temp_store, capsys):
    _seed_person(temp_store, "张三")
    rc = people_cmd.cmd_people(SimpleNamespace(people_command=None))
    assert rc == 0
    assert "张三" in capsys.readouterr().out

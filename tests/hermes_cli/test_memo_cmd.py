"""Smoke tests for the ``hermes memo correct`` I/O adapter (ADR-V6-047 / A4).

Wires the CLI handler against a temp PTG store and a deterministic Atomizer
double (no real LLM) so the full path runs without a tty or network: founder
resolution → correct → re-extract → retire OLD. Pure-loop coverage lives in
``test_adr_v6_047_correction.py``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hermes_cli import memo_cmd
from plugins.memory.ptg.store import PTGStore


class _FakeAtomizer:
    """Deterministic Atomizer double: writes one corrected identity atom."""
    def __init__(self, store, user_id, new_person="张三"):
        self._s, self._u, self._new = store, user_id, new_person

    def atomize(self, *, memo_id, source_text, input_mode="text"):
        self._s.insert_identity_event(
            user_id=self._u, source_text=source_text,
            person_name=self._new, confidence_base=0.9,
            relation_confidence=0.9, memo_id=memo_id)
        return {"ok": True, "written": 1}


@pytest.fixture
def temp_store(tmp_path, monkeypatch):
    db_path = str(tmp_path / "ptg.db")
    monkeypatch.setattr(memo_cmd, "load_ptg_config", lambda: {})
    monkeypatch.setattr(memo_cmd, "resolve_db_path", lambda _cfg: db_path)
    s = PTGStore(db_path=db_path)
    s.ensure_founder("u1", "founder@realityos.local")
    s._conn.execute(
        "INSERT OR REPLACE INTO ptg_meta(key, value) VALUES ('founder_user_id', ?)",
        ("u1",))
    yield s
    s.close()


def _seed(store, text="和掌三开会", person="掌三"):
    mid = store.insert_memo(user_id="u1", source_text=text, input_mode="text")
    store.insert_identity_event(
        user_id="u1", source_text=text, person_name=person,
        confidence_base=0.9, relation_confidence=0.9, memo_id=mid)
    return mid


def test_memo_correct_happy(temp_store, capsys, monkeypatch):
    mid = _seed(temp_store)
    monkeypatch.setattr(
        memo_cmd, "_build_atomizer",
        lambda store, user_id, cfg: _FakeAtomizer(store, user_id, "张三"))
    rc = memo_cmd.cmd_memo(SimpleNamespace(
        memo_command="correct", memo_id=mid, text="和张三开会",
        expected_version=None))
    assert rc == 0
    out = capsys.readouterr().out
    assert "已纠正" in out or "重新提取" in out
    # OLD retired, NEW live
    live = [r["person_name"] for r in temp_store._conn.execute(
        "SELECT person_name FROM identity_events WHERE memo_id=? AND deleted_at IS NULL",
        (mid,))]
    assert live == ["张三"]


def test_memo_correct_not_found(temp_store, capsys, monkeypatch):
    monkeypatch.setattr(
        memo_cmd, "_build_atomizer",
        lambda store, user_id, cfg: _FakeAtomizer(store, user_id))
    rc = memo_cmd.cmd_memo(SimpleNamespace(
        memo_command="correct", memo_id="nope", text="x", expected_version=None))
    assert rc == 1
    assert "找不到" in capsys.readouterr().out


def test_memo_correct_version_conflict(temp_store, capsys, monkeypatch):
    mid = _seed(temp_store)
    monkeypatch.setattr(
        memo_cmd, "_build_atomizer",
        lambda store, user_id, cfg: _FakeAtomizer(store, user_id))
    rc = memo_cmd.cmd_memo(SimpleNamespace(
        memo_command="correct", memo_id=mid, text="新文", expected_version=99))
    assert rc == 1
    assert "版本冲突" in capsys.readouterr().out


def test_memo_correct_failure_keeps_old(temp_store, capsys, monkeypatch):
    """写后删: a failed re-extraction MUST NOT retire the old atoms."""
    mid = _seed(temp_store, person="旧人")

    class _Fail:
        def atomize(self, **kw):
            return {"ok": False, "written": 0}

    monkeypatch.setattr(
        memo_cmd, "_build_atomizer", lambda store, user_id, cfg: _Fail())
    rc = memo_cmd.cmd_memo(SimpleNamespace(
        memo_command="correct", memo_id=mid, text="新文", expected_version=None))
    assert rc == 1
    # OLD atom still live
    live = temp_store._conn.execute(
        "SELECT COUNT(*) FROM identity_events WHERE memo_id=? AND deleted_at IS NULL",
        (mid,)).fetchone()[0]
    assert live == 1


def test_memo_no_action_prints_usage(temp_store, capsys):
    rc = memo_cmd.cmd_memo(SimpleNamespace(memo_command=None))
    assert rc == 0
    assert "memo correct" in capsys.readouterr().out

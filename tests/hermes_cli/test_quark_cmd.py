"""Smoke tests for the ``hermes quark extract`` I/O adapter (ADR-V6-051 / B3).

Wires the CLI handler against a temp PTG store with a deterministic
extract_and_aggregate double (no real LLM) so the full path runs without a tty
or network: founder resolution → load memo → effective_text → extract → print.
Closed-loop coverage lives in ``test_adr_v6_049_quark.py``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hermes_cli import quark_cmd
from plugins.memory.ptg.store import PTGStore
import plugins.realityos_quark as quark_pkg


@pytest.fixture
def temp_store(tmp_path, monkeypatch):
    db_path = str(tmp_path / "ptg.db")
    monkeypatch.setattr(quark_cmd, "load_ptg_config", lambda: {})
    monkeypatch.setattr(quark_cmd, "resolve_db_path", lambda _cfg: db_path)
    s = PTGStore(db_path=db_path)
    s.ensure_founder("u1", "founder@realityos.local")
    s._conn.execute(
        "INSERT OR REPLACE INTO ptg_meta(key, value) VALUES ('founder_user_id', ?)",
        ("u1",))
    yield s
    s.close()


def _seed(store, text="和张三在厦门国贸开会"):
    return store.insert_memo(user_id="u1", source_text=text, input_mode="text")


def test_quark_extract_happy(temp_store, capsys, monkeypatch):
    mid = _seed(temp_store)
    seen = {}

    def _fake(store_, **kw):
        seen.update(kw)
        return {"ok": True, "extracted": 2, "aggregated": 2,
                "counts": {"written": 2, "by_kind": {"Identity": 1, "Meaning": 1},
                           "skipped": 0}}

    monkeypatch.setattr(quark_pkg, "extract_and_aggregate", _fake)
    rc = quark_cmd.cmd_quark(SimpleNamespace(
        quark_command="extract", memo_id=mid, user_id=None))
    assert rc == 0
    out = capsys.readouterr().out
    assert "抽出 2 个 quark" in out
    assert seen["capture_text"] == "和张三在厦门国贸开会"
    assert seen["source_text"] == "和张三在厦门国贸开会"


def test_quark_extract_not_found(temp_store, capsys, monkeypatch):
    monkeypatch.setattr(quark_pkg, "extract_and_aggregate",
                        lambda *a, **kw: pytest.fail("must not extract"))
    rc = quark_cmd.cmd_quark(SimpleNamespace(
        quark_command="extract", memo_id="nope", user_id=None))
    assert rc == 1
    assert "找不到" in capsys.readouterr().out


def test_quark_extract_empty_text(temp_store, capsys, monkeypatch):
    mid = _seed(temp_store, text="   ")  # whitespace-only
    monkeypatch.setattr(quark_pkg, "extract_and_aggregate",
                        lambda *a, **kw: pytest.fail("must not extract empty"))
    rc = quark_cmd.cmd_quark(SimpleNamespace(
        quark_command="extract", memo_id=mid, user_id=None))
    assert rc == 0
    assert "为空" in capsys.readouterr().out


def test_quark_extract_zero_quarks_is_not_failure(temp_store, capsys, monkeypatch):
    """0 quarks = honest empty (no I/M/F signal), exit 0 — not an error."""
    mid = _seed(temp_store)
    monkeypatch.setattr(quark_pkg, "extract_and_aggregate",
                        lambda *a, **kw: {"ok": False, "extracted": 0,
                                          "aggregated": 0, "counts": {}})
    rc = quark_cmd.cmd_quark(SimpleNamespace(
        quark_command="extract", memo_id=mid, user_id=None))
    assert rc == 0
    assert "未抽出" in capsys.readouterr().out


def test_quark_no_action_prints_usage(temp_store, capsys):
    rc = quark_cmd.cmd_quark(SimpleNamespace(quark_command=None))
    assert rc == 0
    assert "quark extract" in capsys.readouterr().out

"""Smoke tests for the ``hermes theory derive`` I/O adapter (ADR-V6-051 / B3).

Wires the CLI handler against a temp PTG store with a deterministic
derive_and_persist double (no real LLM) so the full path runs without a tty or
network: founder resolution → gather atoms/relations → derive → print.
Closed-loop coverage lives in ``test_adr_v6_050_theory.py``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hermes_cli import theory_cmd
from plugins.memory.ptg.store import PTGStore
import plugins.realityos_theory as theory_pkg


@pytest.fixture
def temp_store(tmp_path, monkeypatch):
    db_path = str(tmp_path / "ptg.db")
    monkeypatch.setattr(theory_cmd, "load_ptg_config", lambda: {})
    monkeypatch.setattr(theory_cmd, "resolve_db_path", lambda _cfg: db_path)
    s = PTGStore(db_path=db_path)
    s.ensure_founder("u1", "founder@realityos.local")
    s._conn.execute(
        "INSERT OR REPLACE INTO ptg_meta(key, value) VALUES ('founder_user_id', ?)",
        ("u1",))
    yield s
    s.close()


def _seed_atom(store):
    mid = store.insert_memo(user_id="u1", source_text="和张三开会", input_mode="text")
    store.insert_identity_event(
        user_id="u1", source_text="和张三开会", person_name="张三",
        confidence_base=0.9, relation_confidence=0.9, memo_id=mid)


def _mock_result():
    return {"ok": True, "derived": 12, "persisted": 12,
            "degraded_count": 4, "derivations": []}


def test_theory_derive_happy(temp_store, capsys, monkeypatch):
    _seed_atom(temp_store)
    seen = {}
    monkeypatch.setattr(
        theory_pkg, "derive_and_persist",
        lambda store_, **kw: seen.update(kw) or _mock_result())
    rc = theory_cmd.cmd_theory(SimpleNamespace(
        theory_command="derive", user_id=None, period_key="2026-07-20"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "推导 12 项" in out and "降级 4 项" in out
    assert seen["user_id"] == "u1"
    assert isinstance(seen.get("atoms"), list) and seen["atoms"]
    assert seen.get("period_key") == "2026-07-20"


def test_theory_derive_no_atoms(temp_store, capsys, monkeypatch):
    """Cold start: no atoms → friendly message, no LLM call."""
    monkeypatch.setattr(theory_pkg, "derive_and_persist",
                        lambda *a, **kw: pytest.fail("must not derive"))
    rc = theory_cmd.cmd_theory(SimpleNamespace(
        theory_command="derive", user_id=None, period_key=None))
    assert rc == 0
    assert "还没有原子数据" in capsys.readouterr().out


def test_theory_derive_zero_derived_is_dlq(temp_store, capsys, monkeypatch):
    """derive returned nothing (LLM fail/bad batch → DLQ): honest message."""
    _seed_atom(temp_store)
    monkeypatch.setattr(theory_pkg, "derive_and_persist",
                        lambda *a, **kw: {"ok": False, "derived": 0,
                                          "persisted": 0, "degraded_count": 0,
                                          "derivations": []})
    rc = theory_cmd.cmd_theory(SimpleNamespace(
        theory_command="derive", user_id=None, period_key=None))
    assert rc == 0
    assert "未产出" in capsys.readouterr().out


def test_theory_no_action_prints_usage(temp_store, capsys):
    rc = theory_cmd.cmd_theory(SimpleNamespace(theory_command=None))
    assert rc == 0
    assert "theory derive" in capsys.readouterr().out


# ── theory show (the B3 "UI" consumer — honest degradation render) ─────────


def _seed_theory(store, *, agg_type, name, score, degraded, basis="x", period="2026-07-20"):
    store.upsert_insight(
        user_id="u1", aggregation_type=agg_type,
        period_key=f"{period}|{name}", period_start=period, period_end=period,
        input_data="{}", result_data=__import__("json").dumps(
            {"kind": "PC" if agg_type == "constraint_state" else "FR",
             "name": name, "score": score, "rationale": "r",
             "basis": basis, "degraded": degraded}),
        confidence=0.25 if degraded else 0.5,
        data_sufficiency="partial" if degraded else "sufficient",
        generated_by="manual", schema_version="v1", expires_at="2026-07-21")


def test_theory_show_renders_degradation_honestly(temp_store, capsys):
    """Iron rule: a degraded dim renders as 数据不足 + basis, NOT its 0.0 score."""
    _seed_theory(temp_store, agg_type="constraint_state", name="Time",
                 score=0.66, degraded=False, basis="事件时间戳近似")
    _seed_theory(temp_store, agg_type="constraint_state", name="Energy",
                 score=0.0, degraded=True, basis="需 R1 fatigue+R10 sleep")
    rc = theory_cmd.cmd_theory(SimpleNamespace(
        theory_command="show", user_id=None, period_key="2026-07-20"))
    assert rc == 0
    out = capsys.readouterr().out
    # Non-degraded dim shows its score.
    assert "Time：0.66" in out
    # Degraded dim shows 「数据不足/降级」+ basis, NOT the 0.0 score.
    assert "Energy：数据不足/降级" in out
    assert "需 R1 fatigue+R10 sleep" in out
    assert "Energy：0.00" not in out  # the forced 0.0 must NOT read as a value
    assert "降级 1" in out


def test_theory_show_empty_state_not_pingwen(temp_store, capsys):
    """No rows ⇒ honest empty state, NEVER a fabricated 「平稳」."""
    rc = theory_cmd.cmd_theory(SimpleNamespace(
        theory_command="show", user_id=None, period_key="2026-07-20"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "尚无推导" in out
    assert "平稳" not in out


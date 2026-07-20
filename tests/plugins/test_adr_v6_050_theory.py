"""C4 regression: theory derivation + honest degradation (ADR-V6-050 / B2).

Locks the iron rule: every PC-dim derivation carries a machine-readable
``degraded`` flag + ``basis``, stamped deterministically by the engine (NOT the
LLM). Unsupported dims (Energy/Social/Environment) are forced degraded+score=0;
Cognition is degraded; only Time/Emotion/Execution are non-degraded. Plus the
C5/C6/C7 rails + persist-to-insight_aggregation. Mock LLM caller; no network.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from plugins.memory.ptg.phase2_contracts import (
    FR_DIMENSIONS, PC_CONSTRAINTS, PHASE2_CONTRACT_VERSION,
    TheoryDerivation, TheoryEngine)
from plugins.memory.ptg.store import PTGStore
from plugins.realityos_theory import derive_and_persist
from plugins.realityos_theory.engine import TheoryEngineImpl

USER = "u1"


def _resp(obj: dict):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(obj)))],
        model="mock-llm", provider="mock",
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=20))


def _full_llm_output():
    """A well-formed LLM output covering all 7 PC + 5 FR."""
    pcs = {d: {"score": 0.6, "rationale": f"{d} 推导"} for d in PC_CONSTRAINTS}
    frs = {d: {"score": 0.5, "rationale": f"{d} 推导"} for d in FR_DIMENSIONS}
    return {"PC": pcs, "FR": frs}


@pytest.fixture
def store(tmp_path):
    s = PTGStore(db_path=str(tmp_path / "ptg.db"))
    s.ensure_founder(USER, "founder@realityos.local")
    yield s
    s.close()


# ===========================================================================
# Contract v2
# ===========================================================================

class TestContractV2:
    def test_version_bumped(self):
        assert PHASE2_CONTRACT_VERSION == 2

    def test_theory_derivation_has_degradation_fields(self):
        d = TheoryDerivation(kind="PC", name="Time", score=0.5,
                             aggregation_type="constraint_state")
        assert hasattr(d, "basis") and hasattr(d, "degraded")
        assert d.degraded is False and d.basis == ""  # defaults


# ===========================================================================
# Engine + honest degradation
# ===========================================================================

class TestEngine:
    def test_satisfies_protocol(self, store):
        eng = TheoryEngineImpl(_DummyStore(), caller=lambda **kw: _resp(_full_llm_output()))
        assert isinstance(eng, TheoryEngine)

    def test_derive_full_set(self, store):
        eng = TheoryEngineImpl(store, caller=lambda **kw: _resp(_full_llm_output()))
        out = eng.derive(USER, [{"atom_kind": "R1"}], [])
        names = {(d.kind, d.name) for d in out}
        assert ("PC", "Time") in names and ("FR", "Career") in names
        assert len(out) == len(PC_CONSTRAINTS) + len(FR_DIMENSIONS)  # 7 + 5 = 12

    def test_unsupported_dims_degraded_and_zero(self, store):
        """Iron rule: Energy/Social/Environment have no text source → degraded
        + score forced 0.0 (the LLM's 0.6 guess is discarded)."""
        eng = TheoryEngineImpl(store, caller=lambda **kw: _resp(_full_llm_output()))
        out = {d.name: d for d in eng.derive(USER, [], []) if d.kind == "PC"}
        for dim in ("Energy", "Social", "Environment"):
            assert out[dim].degraded is True, f"{dim} must be degraded"
            assert out[dim].score == 0.0, f"{dim} score must be forced 0"
            assert out[dim].basis, f"{dim} must carry a basis"

    def test_cognition_degraded_keeps_score(self, store):
        eng = TheoryEngineImpl(store, caller=lambda **kw: _resp(_full_llm_output()))
        out = {d.name: d for d in eng.derive(USER, [], []) if d.kind == "PC"}
        cog = out["Cognition"]
        assert cog.degraded is True           # R8 has no continuous score
        assert cog.score == 0.6               # LLM score kept (severely degraded, not zeroed)
        assert cog.confidence == 0.25         # capped low

    def test_supported_dims_not_degraded(self, store):
        eng = TheoryEngineImpl(store, caller=lambda **kw: _resp(_full_llm_output()))
        out = {d.name: d for d in eng.derive(USER, [], []) if d.kind == "PC"}
        for dim in ("Time", "Emotion", "Execution"):
            assert out[dim].degraded is False
            assert out[dim].score == 0.6       # LLM score kept
            assert out[dim].confidence == 0.5

    def test_fr_not_degraded_but_approx(self, store):
        eng = TheoryEngineImpl(store, caller=lambda **kw: _resp(_full_llm_output()))
        out = {d.name: d for d in eng.derive(USER, [], []) if d.kind == "FR"}
        assert all(d.aggregation_type == "fr_snapshot" for d in out.values())
        assert all(d.degraded is False for d in out.values())
        assert all("近似" in d.basis for d in out.values())

    def test_invalid_json_dlq_and_empty(self, store):
        eng = TheoryEngineImpl(store, caller=lambda **kw: _resp("garbage"))
        assert eng.derive(USER, [], []) == []
        assert store._conn.execute(
            "SELECT COUNT(*) FROM dlq_messages WHERE source='theory' "
            "AND error_type='theory_schema_invalid'").fetchone()[0] == 1

    def test_llm_exception_dlq(self, store):
        def boom(**kw):
            raise TimeoutError("down")
        eng = TheoryEngineImpl(store, caller=boom)
        assert eng.derive(USER, [], []) == []
        assert store._conn.execute(
            "SELECT COUNT(*) FROM dlq_messages WHERE error_type='theory_error'"
        ).fetchone()[0] == 1

    def test_llm_log_recorded(self, store):
        eng = TheoryEngineImpl(store, caller=lambda **kw: _resp(_full_llm_output()))
        eng.derive(USER, [], [])
        log = store._conn.execute(
            "SELECT success, schema_valid FROM llm_call_logs ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        assert log["success"] == 1 and log["schema_valid"] == 1


# ===========================================================================
# Closed loop persist
# ===========================================================================

class TestDeriveAndPersist:
    def test_persists_to_insight_aggregation(self, store):
        r = derive_and_persist(
            store, user_id=USER, atoms=[{"atom_kind": "R1"}], relations=[],
            engine=TheoryEngineImpl(
                store, caller=lambda **kw: _resp(_full_llm_output())),
            period_key="2026-07-20")
        assert r["ok"] and r["derived"] == 12 and r["persisted"] == 12
        assert r["degraded_count"] == 4  # Energy + Cognition + Social + Environment
        # rows landed under both aggregation_types
        assert store._conn.execute(
            "SELECT COUNT(*) FROM insight_aggregation WHERE user_id=? "
            "AND aggregation_type='constraint_state'", (USER,)).fetchone()[0] == 7
        assert store._conn.execute(
            "SELECT COUNT(*) FROM insight_aggregation WHERE user_id=? "
            "AND aggregation_type='fr_snapshot'", (USER,)).fetchone()[0] == 5

    def test_degraded_row_carries_flag_in_result_data(self, store):
        derive_and_persist(
            store, user_id=USER, atoms=[], relations=[],
            engine=TheoryEngineImpl(
                store, caller=lambda **kw: _resp(_full_llm_output())),
            period_key="2026-07-20")
        row = store._conn.execute(
            "SELECT result_data, confidence FROM insight_aggregation "
            "WHERE aggregation_type='constraint_state' AND period_key LIKE '%|Energy'"
        ).fetchone()
        data = json.loads(row["result_data"])
        assert data["degraded"] is True and data["score"] == 0.0
        assert row["confidence"] == 0.25


class _DummyStore:
    def insert_dlq(self, **kw):
        pass

    def insert_llm_call_log(self, **kw):
        pass

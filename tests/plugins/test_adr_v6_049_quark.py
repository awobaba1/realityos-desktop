"""C4 regression: quark extraction + aggregation (ADR-V6-039 Batch1 / ADR-V6-049 B1).

Locks the closed loop: LLM → C5 QuarkRecord gate → C6 llm_call_log → C7 DLQ
on failure → aggregation into PRIMARY atoms. A mock caller stands in for the
LLM so the full path runs without a network (the same idiom as the atomizer
+ insights tests).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from plugins.memory.ptg.phase2_contracts import (
    PHASE2_QUARK_KINDS, QuarkExtractor, QuarkRecord)
from plugins.memory.ptg.store import PTGStore
from plugins.realityos_quark import extract_and_aggregate
from plugins.realityos_quark.aggregation import aggregate_quarks_to_atoms
from plugins.realityos_quark.extractor import QuarkExtractorImpl


USER = "u1"


def _resp(text: str):
    """OpenAI-shaped response double."""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        model="mock-llm", provider="mock",
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=20))


@pytest.fixture
def store(tmp_path):
    s = PTGStore(db_path=str(tmp_path / "ptg.db"))
    s.ensure_founder(USER, "founder@realityos.local")
    yield s
    s.close()


# ===========================================================================
# Protocol + extractor
# ===========================================================================

class TestExtractor:
    def test_satisfies_protocol(self):
        # runtime_checkable Protocol — structural match (has extract()).
        ext = QuarkExtractorImpl(_DummyStore(), caller=lambda **kw: _resp("[]"))
        assert isinstance(ext, QuarkExtractor)

    def test_extract_happy_three_kinds(self, store):
        ext = QuarkExtractorImpl(store, caller=lambda **kw: _resp(json.dumps([
            {"kind": "Identity", "value": "张三", "source_id": "m1",
             "occurrence_count": 1, "confidence": 0.9, "evidence": {"span": "和张三"}},
            {"kind": "Meaning", "value": "开会", "source_id": "m1",
             "occurrence_count": 1, "confidence": 0.7, "evidence": {}},
            {"kind": "Feeling", "value": "紧张", "source_id": "m1",
             "occurrence_count": 1, "confidence": 0.8, "evidence": {}},
        ])))
        ext.set_user_id(USER)
        recs = ext.extract([], "明天和张三开会，有点紧张")
        assert len(recs) == 3
        assert all(isinstance(r, QuarkRecord) for r in recs)
        assert {r.kind for r in recs} == {"Identity", "Meaning", "Feeling"}
        # C6: llm_call_log written, success + schema_valid
        log = store._conn.execute(
            "SELECT success, schema_valid FROM llm_call_logs "
            "ORDER BY created_at DESC LIMIT 1").fetchone()
        assert log and log["success"] == 1 and log["schema_valid"] == 1

    def test_extract_invalid_json_dlq_and_log(self, store):
        ext = QuarkExtractorImpl(store, caller=lambda **kw: _resp("not json"))
        ext.set_user_id(USER)
        recs = ext.extract([], "x")
        assert recs == []  # C5 failure → no records
        # C7: DLQ entry
        assert store._conn.execute(
            "SELECT COUNT(*) FROM dlq_messages WHERE source='quark' "
            "AND error_type='quark_schema_invalid'").fetchone()[0] == 1
        # C6: log marked schema_valid=0
        log = store._conn.execute(
            "SELECT schema_valid FROM llm_call_logs ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        assert log["schema_valid"] == 0

    def test_extract_llm_exception_dlq(self, store):
        def boom(**kw):
            raise TimeoutError("llm down")
        ext = QuarkExtractorImpl(store, caller=boom)
        ext.set_user_id(USER)
        recs = ext.extract([], "x")
        assert recs == []
        assert store._conn.execute(
            "SELECT COUNT(*) FROM dlq_messages WHERE error_type='quark_error'"
        ).fetchone()[0] == 1
        log = store._conn.execute(
            "SELECT success, error_type FROM llm_call_logs ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        assert log["success"] == 0 and log["error_type"] == "TimeoutError"

    def test_later_phase_kind_ignored(self, store):
        ext = QuarkExtractorImpl(store, caller=lambda **kw: _resp(json.dumps([
            {"kind": "Time", "value": "morning", "source_id": "m1",
             "occurrence_count": 1, "confidence": 0.5, "evidence": {}},
            {"kind": "Identity", "value": "张三", "source_id": "m1",
             "occurrence_count": 1, "confidence": 0.9, "evidence": {}},
        ])))
        ext.set_user_id(USER)
        recs = ext.extract([], "x")
        # Time is not in PHASE2_QUARK_KINDS → ignored; only Identity survives.
        assert len(recs) == 1 and recs[0].kind == "Identity"
        assert "Time" not in PHASE2_QUARK_KINDS

    def test_empty_capture_no_call(self, store):
        called = {"n": 0}

        def caller(**kw):
            called["n"] += 1
            return _resp("[]")
        ext = QuarkExtractorImpl(store, caller=caller)
        ext.set_user_id(USER)
        assert ext.extract([], "") == []
        assert called["n"] == 0  # short-circuit, no LLM call


# ===========================================================================
# Aggregation
# ===========================================================================

class TestAggregation:
    def _quarks(self):
        return [
            QuarkRecord(kind="Identity", value="张三", source_id="m1", confidence=0.9),
            QuarkRecord(kind="Meaning", value="开会", source_id="m1", confidence=0.7),
            QuarkRecord(kind="Feeling", value="紧张", source_id="m1", confidence=0.8),
        ]

    def test_writes_primary_atoms(self, store):
        counts = aggregate_quarks_to_atoms(
            store, self._quarks(), user_id=USER, source_text="x")
        assert counts["written"] == 3
        assert counts["Identity"] == 1 and counts["Meaning"] == 1 and counts["Feeling"] == 1
        # Identity → identity_events
        assert store._conn.execute(
            "SELECT person_name FROM identity_events WHERE memo_id='m1'").fetchone()[0] == "张三"
        # Meaning → meaning_events atom_kind R7 (Expression, not fabricated task)
        mrow = store._conn.execute(
            "SELECT atom_kind, intent_class, task_description FROM meaning_events "
            "WHERE memo_id='m1'").fetchone()
        assert mrow["atom_kind"] == "R7" and mrow["task_description"] == "开会"
        # Feeling → feeling_events atom_kind R9
        frow = store._conn.execute(
            "SELECT atom_kind, trigger_source FROM feeling_events WHERE memo_id='m1'").fetchone()
        assert frow["atom_kind"] == "R9"
        assert json.loads(frow["trigger_source"])["trigger"] == "紧张"

    def test_confidence_propagated(self, store):
        aggregate_quarks_to_atoms(
            store, [QuarkRecord(kind="Identity", value="李四",
                                source_id="m2", confidence=0.42)],
            user_id=USER, source_text="x")
        row = store._conn.execute(
            "SELECT confidence_base, relation_confidence FROM identity_events "
            "WHERE memo_id='m2'").fetchone()
        assert row["confidence_base"] == 0.42 and row["relation_confidence"] == 0.42

    def test_no_self_state_fabrication(self, store):
        """Honest boundary: Identity/Feeling quarks do NOT fabricate aux R1
        self-state atoms (that needs acoustic intensity — Phase 2.5)."""
        aggregate_quarks_to_atoms(
            store, [QuarkRecord(kind="Identity", value="张三", source_id="m1",
                                confidence=0.9)],
            user_id=USER, source_text="x")
        # No feeling_events with atom_kind R1 should have been created.
        assert store._conn.execute(
            "SELECT COUNT(*) FROM feeling_events WHERE atom_kind='R1'").fetchone()[0] == 0


# ===========================================================================
# Closed loop
# ===========================================================================

class TestExtractAndAggregate:
    def test_closed_loop(self, store):
        r = extract_and_aggregate(
            store, user_id=USER, capture_text="和张三开会，有点紧张",
            source_text="和张三开会，有点紧张",
            extractor=QuarkExtractorImpl(
                store, caller=lambda **kw: _resp(json.dumps([
                    {"kind": "Identity", "value": "张三", "source_id": "capture",
                     "occurrence_count": 1, "confidence": 0.9, "evidence": {}},
                    {"kind": "Feeling", "value": "紧张", "source_id": "capture",
                     "occurrence_count": 1, "confidence": 0.8, "evidence": {}},
                ]))))
        assert r["ok"] and r["extracted"] == 2 and r["aggregated"] == 2
        # atoms actually landed
        assert store._conn.execute(
            "SELECT COUNT(*) FROM identity_events WHERE user_id=?", (USER,)).fetchone()[0] >= 1

    def test_closed_loop_failure_is_honest(self, store):
        r = extract_and_aggregate(
            store, user_id=USER, capture_text="x",
            extractor=QuarkExtractorImpl(store, caller=lambda **kw: _resp("garbage")))
        assert r["ok"] is False and r["extracted"] == 0 and r["aggregated"] == 0


# small helper so the Protocol isinstance check has a real store-backed instance
class _DummyStore:
    def insert_dlq(self, **kw):
        pass

    def insert_llm_call_log(self, **kw):
        pass

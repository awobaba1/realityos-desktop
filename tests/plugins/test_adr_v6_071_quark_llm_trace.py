"""C4 regression: quark llm_call_id traceability (ADR-V6-071).

The fourth-round audit (C5/C6 维度) found a C6 断链 in the quark pipeline:
the extractor generated an ``llm_call_id`` for its LLM call (and logged it to
``llm_call_logs``) but ``extract()`` DISCARDED it (``_llm_id``). As a result
every quark-derived atom event landed with NULL ``llm_call_id`` —
untraceable to the LLM call that produced it (C6: every event MUST carry
llm_call_id). Compounding the fake-green, ``extract_and_aggregate``'s docstring
promised a ``llm_call_id`` return key that was never populated.

This module locks the closed loop: extract() exposes the id → aggregation
threads it into every PRIMARY atom event → extract_and_aggregate returns it.
"""

from __future__ import annotations

import inspect
import json
import re
from types import SimpleNamespace

import pytest

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


def _three_kind_resp():
    return _resp(json.dumps([
        {"kind": "Identity", "value": "张三", "source_id": "m1",
         "occurrence_count": 1, "confidence": 0.9, "evidence": {"span": "和张三"}},
        {"kind": "Meaning", "value": "开会", "source_id": "m1",
         "occurrence_count": 1, "confidence": 0.7, "evidence": {}},
        {"kind": "Feeling", "value": "紧张", "source_id": "m1",
         "occurrence_count": 1, "confidence": 0.8, "evidence": {}},
    ]))


@pytest.fixture
def store(tmp_path):
    s = PTGStore(db_path=str(tmp_path / "ptg.db"))
    s.ensure_founder(USER, "founder@realityos.local")
    yield s
    s.close()


def _latest_event_llm_id(store, table: str) -> str | None:
    with store._lock:
        row = store._conn.execute(
            f"SELECT llm_call_id FROM {table} "
            f"WHERE user_id=? ORDER BY created_at DESC LIMIT 1",
            (USER,)).fetchone()
    return None if row is None else row["llm_call_id"]


# ===========================================================================
# extract() exposes the llm_call_id (no longer discarded)
# ===========================================================================

class TestExtractExposesLlmCallId:
    def test_extract_sets_last_llm_call_id_matching_log(self, store):
        ext = QuarkExtractorImpl(store, caller=lambda **kw: _three_kind_resp())
        ext.set_user_id(USER)
        ext.extract([], "明天和张三开会，有点紧张")
        # The exposed id must match the llm_call_log row the same call wrote.
        assert ext._last_llm_call_id is not None
        log = store._conn.execute(
            "SELECT id FROM llm_call_logs ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        assert log and log["id"] == ext._last_llm_call_id

    def test_extract_empty_input_yields_none_not_stale(self, store):
        ext = QuarkExtractorImpl(store, caller=lambda **kw: _three_kind_resp())
        ext.set_user_id(USER)
        # No call made on empty input → None (honest, not a stale prior id).
        recs = ext.extract([], "")
        assert recs == []
        assert ext._last_llm_call_id is None

    def test_extract_no_longer_discards_llm_id(self):
        """Static guard (wrap-lucky-green lesson): the old discard binding
        ``records, _llm_id, _ok =`` must stay replaced by the expose-and-store
        pattern. Whitespace-normalized so a renamed regression can't slip past.
        (Checks the binding, not a bare substring — the comment legitimately
        references the old name.)"""
        src = inspect.getsource(QuarkExtractorImpl.extract)
        normalized = re.sub(r"\s+", " ", src).lower()
        assert "records, _llm_id, _ok =" not in normalized
        assert "self._last_llm_call_id = llm_id" in normalized


# ===========================================================================
# C6 traceability: aggregation threads llm_call_id into every atom event
# ===========================================================================

class TestAggregationThreadsLlmCallId:
    def test_three_event_tables_carry_llm_call_id(self, store):
        ext = QuarkExtractorImpl(store, caller=lambda **kw: _three_kind_resp())
        ext.set_user_id(USER)
        ext.extract([], "明天和张三开会，有点紧张")
        aggregate_quarks_to_atoms(
            store, ext.extract([], "明天和张三开会，有点紧张"),
            user_id=USER, source_text="明天和张三开会，有点紧张",
            llm_call_id=ext._last_llm_call_id)
        # Identity → identity_events, Meaning → meaning_events, Feeling → feeling_events
        for table in ("identity_events", "meaning_events", "feeling_events"):
            assert _latest_event_llm_id(store, table) == ext._last_llm_call_id, (
                f"{table} row missing llm_call_id (C6 断链)")

    def test_aggregation_default_llm_call_id_is_none(self, store):
        """Backward-compat: omitting llm_call_id writes NULL (pre-existing
        behaviour for non-quark callers). Pins the param default so the
        C6 fix doesn't accidentally require it everywhere."""
        from plugins.memory.ptg.phase2_contracts import QuarkRecord
        q = QuarkRecord(kind="Identity", value="李四", source_id="m2",
                        occurrence_count=1, confidence=0.9, evidence={})
        aggregate_quarks_to_atoms(store, [q], user_id=USER, source_text="李四")
        assert _latest_event_llm_id(store, "identity_events") is None


# ===========================================================================
# extract_and_aggregate returns llm_call_id (fulfils the docstring promise)
# ===========================================================================

class TestExtractAndAggregateReturnsLlmCallId:
    def test_return_dict_contains_llm_call_id(self, store):
        result = extract_and_aggregate(
            store, user_id=USER, capture_text="明天和张三开会，有点紧张",
            caller=lambda **kw: _three_kind_resp())
        assert "llm_call_id" in result  # the key the old docstring lied about
        assert result["llm_call_id"] is not None
        # And the events it wrote carry that same id (full C6 loop).
        assert result["aggregated"] >= 3
        for table in ("identity_events", "meaning_events", "feeling_events"):
            assert _latest_event_llm_id(store, table) == result["llm_call_id"]

    def test_empty_input_returns_none_llm_call_id(self, store):
        result = extract_and_aggregate(
            store, user_id=USER, capture_text="", caller=lambda **kw: _three_kind_resp())
        assert result["llm_call_id"] is None  # no call made → honest None
        assert result["ok"] is False

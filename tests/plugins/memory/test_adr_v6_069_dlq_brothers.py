"""C4 regression: C7 DLQ gap closure — the ADR-066 brothers (ADR-V6-069).

The third-round audit (R4) closed 5 log-yes/DLQ-no paths (ADR-V6-066) but
its sweep missed three direct brothers in the same files / same surfaces.
This module locks the closure the fourth-round audit surfaced:

  D1  ``PTGStore.adjust_atom_confidence``  (brother of D3 insert_feedback —
      same founder-calibration signal, ADR-V6-028 §11.4; same store.py)
  D2  ``PTGStore.mark_task_outcome``        (brother of D4 insert_tool_event —
      same task/event capture surface)
  D3  ``theory._persist_one``              (brother of D2 re_extract_memo —
      same outer DB-write backstop around an already-DLQ'd inner LLM call)

Every site now lands a DLQ row on failure (best-effort: except is OUTSIDE the
lock / transaction, so re-acquiring it for ``insert_dlq`` cannot deadlock),
and the fail-safe floor holds — if the DLQ write itself raises, the observer
still survives (the C7 "never breaks the loop" contract).
"""

from __future__ import annotations

import inspect
import re

import pytest

from plugins.memory.ptg.store import PTGStore

USER = "u1"


@pytest.fixture
def store(tmp_path):
    s = PTGStore(db_path=str(tmp_path / "ptg.db"))
    s.ensure_founder(USER, "founder@realityos.local")
    yield s
    s.close()


def _dlq_count(store, source: str) -> int:
    with store._lock:
        return store._conn.execute(
            "SELECT COUNT(*) FROM dlq_messages WHERE source=?", (source,)
        ).fetchone()[0]


def _raising(exc):
    """Return a callable that raises ``exc`` (ignores kwargs)."""
    def _fn(**kw):
        raise exc
    return _fn


# ===========================================================================
# D1 — adjust_atom_confidence: UPDATE failure → DLQ (brother of insert_feedback)
# ===========================================================================

class TestAdjustAtomConfidenceDlq:
    def test_update_failure_writes_dlq(self, store):
        # R2_Task lives in meaning_events; drop it so the UPDATE raises.
        with store._lock:
            store._conn.execute("DROP TABLE meaning_events")
        # Must not raise (C7); returns 0 on failure.
        count = store.adjust_atom_confidence(
            user_id=USER, atom_id="atom-1", atom_type="R2_Task",
            new_confidence=0.42, reason="founder_calibration")
        assert count == 0
        assert _dlq_count(store, "store.adjust_atom_confidence") == 1

    def test_unknown_atom_type_returns_zero_without_dlq(self, store):
        # Unknown type is a legitimate early-return (logged warning), NOT a
        # data-loss path — must NOT produce a DLQ row (pins the boundary so
        # the DLQ isn't fired on expected control flow).
        count = store.adjust_atom_confidence(
            user_id=USER, atom_id="atom-1", atom_type="R99_Bogus",
            new_confidence=0.5, reason="x")
        assert count == 0
        assert _dlq_count(store, "store.adjust_atom_confidence") == 0

    def test_dlq_write_failure_does_not_raise(self, store, monkeypatch):
        with store._lock:
            store._conn.execute("DROP TABLE meaning_events")
        monkeypatch.setattr(store, "insert_dlq", _raising(RuntimeError("conn dead")))
        # Must not raise — fail-safe warning backstop (C7).
        count = store.adjust_atom_confidence(
            user_id=USER, atom_id="a", atom_type="R2_Task", new_confidence=0.5)
        assert count == 0

    def test_except_block_has_dlq_source(self):
        """Static guard (wrap-lucky-green lesson): the fail-safe DLQ must
        stay wired. Whitespace-normalized so a line-wrapped regression can't
        slip past the assertion."""
        from plugins.memory.ptg import store as store_mod
        src = inspect.getsource(store_mod.PTGStore.adjust_atom_confidence)
        normalized = re.sub(r"\s+", " ", src).lower()
        assert "source=\"store.adjust_atom_confidence\"" in normalized
        assert "dlq write also failed" in normalized


# ===========================================================================
# D2 — mark_task_outcome: UPDATE failure → DLQ (brother of insert_tool_event)
# ===========================================================================

class TestMarkTaskOutcomeDlq:
    def test_update_failure_writes_dlq(self, store, monkeypatch):
        # Resolve a fake target so we reach the UPDATE, then drop the table
        # so the UPDATE inside the transaction raises.
        monkeypatch.setattr(
            store, "_resolve_task_ref",
            lambda uid, ref: ("atom-1", "some task"))
        with store._lock:
            store._conn.execute("DROP TABLE meaning_events")
        res = store.mark_task_outcome(user_id=USER, ref="#1", outcome="completed")
        assert res["ok"] is False
        assert _dlq_count(store, "store.mark_task_outcome") == 1

    def test_unresolved_ref_returns_false_without_dlq(self, store, monkeypatch):
        # Unresolved ref is a legitimate early-return (user-facing message),
        # NOT a data-loss path — must NOT produce a DLQ row.
        monkeypatch.setattr(store, "_resolve_task_ref", lambda uid, ref: None)
        res = store.mark_task_outcome(user_id=USER, ref="#99", outcome="completed")
        assert res["ok"] is False
        assert _dlq_count(store, "store.mark_task_outcome") == 0

    def test_dlq_write_failure_does_not_raise(self, store, monkeypatch):
        monkeypatch.setattr(
            store, "_resolve_task_ref",
            lambda uid, ref: ("atom-1", "t"))
        with store._lock:
            store._conn.execute("DROP TABLE meaning_events")
        monkeypatch.setattr(store, "insert_dlq", _raising(RuntimeError("conn dead")))
        # Must not raise — fail-safe warning backstop (C7).
        res = store.mark_task_outcome(user_id=USER, ref="#1", outcome="failed")
        assert res["ok"] is False

    def test_except_block_has_dlq_source(self):
        from plugins.memory.ptg import store as store_mod
        src = inspect.getsource(store_mod.PTGStore.mark_task_outcome)
        normalized = re.sub(r"\s+", " ", src).lower()
        assert "source=\"store.mark_task_outcome\"" in normalized
        assert "dlq write also failed" in normalized


# ===========================================================================
# D3 — theory._persist_one: persist failure → DLQ (brother of re_extract_memo)
# ===========================================================================

class _FakeDerivation:
    """Minimal stand-in for TheoryDerivation — only the attrs _persist_one reads."""
    kind = "PC"
    name = "focus_test"
    score = 0.5
    rationale = "rationale"
    basis = ["atom-1"]
    degraded = False
    aggregation_type = "theory_pc"
    confidence = 0.5


class TestTheoryPersistDlq:
    def test_persist_failure_writes_dlq(self, store, monkeypatch):
        from plugins.realityos_theory import _persist_one
        # Inner LLM call is already DLQ'd (engine._safe_dlq); the persist
        # write itself (upsert_insight) is the outer backstop D3 covers.
        monkeypatch.setattr(store, "upsert_insight",
                            _raising(RuntimeError("upsert exploded")))
        ok = _persist_one(store, user_id=USER, derivation=_FakeDerivation(),
                          period_key="2026-07-21")
        assert ok is False
        assert _dlq_count(store, "theory.persist_one") == 1

    def test_dlq_write_failure_does_not_raise(self, store, monkeypatch):
        from plugins.realityos_theory import _persist_one
        monkeypatch.setattr(store, "upsert_insight",
                            _raising(RuntimeError("upsert exploded")))
        monkeypatch.setattr(store, "insert_dlq", _raising(RuntimeError("conn dead")))
        # Must not raise — fail-safe warning backstop (C7).
        ok = _persist_one(store, user_id=USER, derivation=_FakeDerivation(),
                          period_key="2026-07-21")
        assert ok is False

    def test_except_block_has_dlq_source(self):
        import plugins.realityos_theory as theory_pkg
        src = inspect.getsource(theory_pkg._persist_one)
        normalized = re.sub(r"\s+", " ", src).lower()
        assert "source=\"theory.persist_one\"" in normalized
        assert "dlq write also failed" in normalized

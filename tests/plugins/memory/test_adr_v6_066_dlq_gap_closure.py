"""C4 regression: C7 DLQ gap closure (ADR-V6-066).

Locks the 5 R4 findings from the third-round audit — every former
log-yes/DLQ-no path now lands a DLQ row (D1-D4) or a DEBUG log (D5, a query
fallback rather than data loss). Also nails the fail-safe property at every
site: if the DLQ write itself raises (connection-dead), the observer still
survives — the C7 "never breaks the loop" contract holds.
"""

from __future__ import annotations

import inspect
import logging
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
# D3 / D4 — store-internal sinks: best-effort DLQ on failure
# ===========================================================================

class TestStoreInternalBestEffortDlq:
    def test_insert_feedback_failure_writes_dlq(self, store):
        with store._lock:
            store._conn.execute("DROP TABLE feedback")
        # Must not raise (C7); returns "" on failure.
        result = store.insert_feedback(
            user_id=USER, target_type="calibration_correct",
            target_id="atom-1", rating="thumbs_up")
        assert result == ""
        assert _dlq_count(store, "store.insert_feedback") == 1

    def test_insert_tool_event_failure_writes_dlq(self, store):
        with store._lock:
            store._conn.execute("DROP TABLE tool_events")
        # Must not raise (C7); returns the pre-generated event_id.
        eid = store.insert_tool_event(
            user_id=USER, tool_name="search", status="ok")
        assert eid  # event_id generated before the sink
        assert _dlq_count(store, "store.insert_tool_event") == 1

    def test_insert_feedback_dlq_write_failure_does_not_raise(self, store, monkeypatch):
        """If the store is SO unhealthy that even insert_dlq raises, the sink
        still swallows (observer never crashes — the C7 contract). This is the
        honest best-effort floor ADR-V6-066 D3 commits to."""
        with store._lock:
            store._conn.execute("DROP TABLE feedback")
        monkeypatch.setattr(store, "insert_dlq", _raising(RuntimeError("conn dead")))
        result = store.insert_feedback(
            user_id=USER, target_type="t", target_id="x", rating="thumbs_up")
        assert result == ""


# ===========================================================================
# D5 — _resolve_task_ref: exact-id except DEBUG-logs (not bare pass)
# ===========================================================================

class TestResolveTaskRefDebugLog:
    def test_exact_id_failure_logs_debug_and_falls_back(self, store, monkeypatch, caplog):
        # Force list_open_tasks to return [] (skip its own DB path) so we reach
        # the exact-id SELECT — then drop the table to make THAT raise.
        monkeypatch.setattr(store, "list_open_tasks", lambda uid: [])
        with store._lock:
            store._conn.execute("DROP TABLE meaning_events")
        with caplog.at_level(logging.DEBUG, logger="plugins.memory.ptg.store"):
            result = store._resolve_task_ref(USER, "nonexistent-ref")
        # Substring over [] → None; method never raised (legitimate fallback).
        assert result is None
        assert any("resolve_task_ref exact-id lookup failed" in r.message
                   for r in caplog.records if r.levelno == logging.DEBUG)

    def test_except_block_has_no_bare_pass(self):
        """Static guard (wrap-lucky-green lesson): the bare ``except: pass`` R4
        flagged must stay replaced by a DEBUG log. Whitespace-normalized so a
        line-wrapped regression can't slip past the assertion."""
        from plugins.memory.ptg import store as store_mod
        src = inspect.getsource(store_mod.PTGStore._resolve_task_ref)
        normalized = re.sub(r"\s+", " ", src).lower()
        assert "logger.debug(" in normalized
        assert "resolve_task_ref exact-id lookup failed" in normalized


# ===========================================================================
# D1 — provider.sync_turn: capture failure → DLQ (mirror _spawn_atomize)
# ===========================================================================

class TestSyncTurnDlq:
    def test_capture_failure_writes_dlq(self, tmp_path, monkeypatch):
        from plugins.memory.ptg.provider import PTGProvider
        p = PTGProvider(config={"db_path": str(tmp_path / "ptg.db")})
        p.initialize("sess-1", hermes_home=str(tmp_path), platform="cli",
                     agent_context="primary")
        try:
            monkeypatch.setattr(p._store, "insert_memo",
                                _raising(RuntimeError("insert_memo exploded")))
            # Must not raise (C7); capture failure swallowed + DLQ'd.
            p.sync_turn("hello user turn", "assistant reply")
            assert _dlq_count(p._store, "ptg.sync_turn") == 1
        finally:
            p.shutdown()

    def test_dlq_write_failure_does_not_raise(self, tmp_path, monkeypatch):
        from plugins.memory.ptg.provider import PTGProvider
        p = PTGProvider(config={"db_path": str(tmp_path / "ptg.db")})
        p.initialize("sess-1", hermes_home=str(tmp_path), platform="cli",
                     agent_context="primary")
        try:
            monkeypatch.setattr(p._store, "insert_memo", _raising(RuntimeError("boom")))
            monkeypatch.setattr(p._store, "insert_dlq",
                                _raising(RuntimeError("conn dead")))
            # Must not raise — fail-safe warning backstop (C7).
            p.sync_turn("hi", "bye")
        finally:
            p.shutdown()


# ===========================================================================
# D2 — correction.re_extract_memo: atomize uncaught → DLQ (mirror _spawn_atomize)
# ===========================================================================

class _FakeRaise:
    """Atomizer double whose atomize() raises an UNCAUGHT exception (the path
    D2 backstops — distinct from a clean not-ok return, which is expected)."""

    def atomize(self, **kw):
        raise RuntimeError("atomize blew up")


class TestReExtractMemoDlq:
    def test_atomize_uncaught_writes_dlq(self, store):
        from plugins.memory.ptg.correction import re_extract_memo
        mid = store.insert_memo(user_id=USER, source_text="seed text",
                                input_mode="text")
        res = re_extract_memo(
            store, _FakeRaise(), user_id=USER, memo_id=mid,
            corrected_text="corrected text")
        assert res["ok"] is False
        assert res["status"] == "atomize_error"
        assert _dlq_count(store, "correction.re_extract_memo") == 1

    def test_dlq_write_failure_does_not_raise(self, store, monkeypatch):
        from plugins.memory.ptg.correction import re_extract_memo
        mid = store.insert_memo(user_id=USER, source_text="seed text",
                                input_mode="text")
        monkeypatch.setattr(store, "insert_dlq", _raising(RuntimeError("conn dead")))
        # Must not raise — fail-safe warning backstop (C7).
        res = re_extract_memo(
            store, _FakeRaise(), user_id=USER, memo_id=mid,
            corrected_text="corrected text")
        assert res["ok"] is False

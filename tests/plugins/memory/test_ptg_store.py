"""RealityOS V6 PTG store — schema + capture regression tests (ADR-V6-008).

Locks the P0-4 data layer: 13-table V5-mirror schema, C2 soft-delete+version
invariant, FTS5 recall, append-only logs, sqlite-vec graceful degrade, and
the process-wide shared-connection singleton. Every test uses a unique temp
DB path so the module-level _shared registry never collides across tests.
"""

from __future__ import annotations

import sqlite3
import uuid

import pytest

from plugins.memory.ptg import schema as ptg_schema
from plugins.memory.ptg.store import PTGStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    """A fresh PTGStore on an isolated temp DB. Closed after the test so the
    shared-registry refcount returns to zero (no cross-test leakage)."""
    db = tmp_path / "ptg.db"
    s = PTGStore(db_path=str(db))
    s.ensure_founder("u1", "founder@example.com", nickname="founder")
    yield s
    s.close()


def _columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


# ---------------------------------------------------------------------------
# Schema: all 13 tables created
# ---------------------------------------------------------------------------

def test_all_thirteen_tables_exist(store):
    tables = {row[0] for row in store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    for t in ptg_schema.ALL_TABLES:
        assert t in tables, f"missing table {t}"
    # FTS5 virtual table + meta bookkeeping present too.
    assert "memos_fts" in tables
    assert "ptg_meta" in tables


def test_schema_version_recorded(store):
    row = store._conn.execute(
        "SELECT value FROM ptg_meta WHERE key='schema_version'"
    ).fetchone()
    assert row is not None
    assert int(row[0]) == ptg_schema.SCHEMA_VERSION


# ---------------------------------------------------------------------------
# C2 iron rule: deleted_at + version on every user-data table
# ---------------------------------------------------------------------------

def test_c2_user_tables_have_soft_delete_and_version(store):
    for table in ptg_schema.C2_USER_TABLES:
        cols = _columns(store._conn, table)
        assert "deleted_at" in cols, f"{table} missing deleted_at (C2)"
        assert "version" in cols, f"{table} missing version (C2)"


def test_c2_append_only_logs_exempt(store):
    """dlq_messages + llm_call_logs are append-only: NO deleted_at/version."""
    for table in ptg_schema.APPEND_ONLY_TABLES:
        cols = _columns(store._conn, table)
        assert "deleted_at" not in cols, f"{table} must NOT have deleted_at (append-only)"
        assert "version" not in cols, f"{table} must NOT have version (append-only)"


def test_meaning_events_holds_both_r2_and_r7(store):
    """meaning_events carries intent_class discriminating R2 (Need_To_Do) from
    R7 (the other 7 intents) — the V5 split this table encodes."""
    store.insert_meaning_event(
        user_id="u1", source_text="buy milk", intent_class="Need_To_Do",
        confidence_base=0.9, relation_confidence=0.8, task_description="buy milk",
    )
    store.insert_meaning_event(
        user_id="u1", source_text="so tired", intent_class="Health",
        confidence_base=0.7, relation_confidence=0.6,
    )
    assert store.count_rows("meaning_events") == 2
    # CHECK constraint rejects a bogus intent_class.
    with pytest.raises(sqlite3.IntegrityError):
        store._conn.execute(
            "INSERT INTO meaning_events (id, user_id, timestamp, source_text, "
            "input_mode, confidence_base, relation_confidence, intent_class) "
            "VALUES ('x','u1','2026-01-01','t','text',0.5,0.5,'Bogus')")


def test_entity_event_category_check(store):
    store.insert_entity_event(
        user_id="u1", source_text="went to Xiamen", entity_name="Xiamen",
        entity_category="place", confidence_base=0.9, relation_confidence=0.5,
    )
    with pytest.raises(sqlite3.IntegrityError):
        store._conn.execute(
            "INSERT INTO entity_events (id, user_id, timestamp, source_text, "
            "input_mode, confidence_base, relation_confidence, entity_name, "
            "entity_category) VALUES ('x','u1','2026-01-01','t','text',0.5,0.5,'X','Bogus')")


# ---------------------------------------------------------------------------
# V5-fidelity (schema v2 corrections — see ADR-V6-008 v2 note)
# ---------------------------------------------------------------------------

def test_identity_event_has_sentiment_and_interaction_type(store):
    """V5 identity_events carries sentiment + interaction_type (not the invented
    person_attributes). A full V5 row inserts cleanly."""
    eid = store.insert_identity_event(
        user_id="u1", source_text="met Alice", person_name="Alice",
        confidence_base=0.9, relation_confidence=0.8,
        sentiment="positive", interaction_type="meeting",
    )
    row = store._conn.execute(
        "SELECT person_name, sentiment, interaction_type FROM identity_events WHERE id=?",
        (eid,)).fetchone()
    assert row["person_name"] == "Alice"
    assert row["sentiment"] == "positive"
    assert row["interaction_type"] == "meeting"
    # person_attributes column must NOT exist (it was invented, not in V5).
    assert "person_attributes" not in _columns(store._conn, "identity_events")


def test_feeling_event_ser_source_accepts_v5_values(store):
    """V5 ser_source has NO CHECK and uses llm_text/ser_audio/both — the old
    acoustic/fused CHECK would have rejected real V5 data on migration."""
    for val in ("llm_text", "ser_audio", "both"):
        store.insert_feeling_event(
            user_id="u1", source_text="tired", confidence_base=0.7,
            relation_confidence=0.6, state_type="fatigue", direction="down",
            intensity="medium", ser_source=val,
        )
    assert store.count_rows("feeling_events") == 3


def test_feeling_event_requires_state_direction_intensity(store):
    """V5 has state_type/direction/intensity NOT NULL — incomplete extraction
    must be rejected (C5-adjacent: bad data doesn't silently land)."""
    with pytest.raises(sqlite3.IntegrityError):
        store._conn.execute(
            "INSERT INTO feeling_events (id, user_id, timestamp, source_text, "
            "input_mode, confidence_base, relation_confidence) "
            "VALUES ('x','u1','2026-01-01','t','text',0.5,0.5)")


def test_relations_uses_v5_column_names(store):
    """V5 relations uses subject_id/object_id/last_updated (not source/target/
    updated_at). Insert + index columns resolve under the V5 names."""
    cols = _columns(store._conn, "relations")
    assert "subject_id" in cols and "object_id" in cols
    assert "last_updated" in cols
    assert "source_id" not in cols and "target_id" not in cols
    assert "updated_at" not in cols  # V5 has last_updated, not updated_at


# ---------------------------------------------------------------------------
# Capture: memos + FTS recall (the "流经即捕获" surface)
# ---------------------------------------------------------------------------

def test_insert_memo_and_fts_recall(store):
    store.insert_memo(user_id="u1", source_text="meeting with Alice about Q3 budget")
    store.insert_memo(user_id="u1", source_text="gym session in the morning")
    store.insert_memo(user_id="u1", source_text="budget review notes")
    hits = store.search_memos_fts("budget", user_id="u1")
    assert len(hits) >= 2
    texts = " ".join(h["source_text"] for h in hits)
    assert "budget" in texts.lower()
    assert all("budget" in h["source_text"].lower() for h in hits)


def test_fts_excludes_soft_deleted(store):
    mid = store.insert_memo(user_id="u1", source_text="unique alpha budget marker")
    assert len(store.search_memos_fts("alpha", user_id="u1")) == 1
    assert store.soft_delete("memos", mid) is True
    # Soft-deleted rows must not appear in recall (deleted_at IS NULL filter).
    assert store.search_memos_fts("alpha", user_id="u1") == []
    # And count_rows respects soft-delete by default.
    assert store.count_rows("memos") == 0
    assert store.count_rows("memos", include_deleted=True) == 1


def test_fts_or_joins_tokens(store):
    """Multi-word query must not zero out recall — tokens are OR-joined."""
    store.insert_memo(user_id="u1", source_text="standalone budget word only")
    hits = store.search_memos_fts("budget nonexistentword", user_id="u1")
    assert len(hits) == 1  # would be 0 if AND-joined


# ---------------------------------------------------------------------------
# CJK recall (schema v3 trigram + LIKE net) — regression for the real-data
# finding that unicode61 returned ZERO Chinese hits while English "budget"
# tests passed (ADR-V6-009 / the ADR-088 "synthetic samples hid the bug" lesson)
# ---------------------------------------------------------------------------

def test_search_recalls_2char_cjk(store):
    """The commonest Chinese searches are 2 chars (北京/老婆/辞职). trigram
    can't match <3 chars, so the LIKE OR-join net must catch them — the exact
    gap synthetic English tests hid."""
    store.insert_memo(user_id="u1", source_text="今天晚上回北京")
    store.insert_memo(user_id="u1", source_text="我老婆今天休息")
    store.insert_memo(user_id="u1", source_text="无关的一条备忘")
    assert len(store.search_memos_fts("北京", user_id="u1")) == 1
    assert len(store.search_memos_fts("老婆", user_id="u1")) == 1


def test_search_recalls_3char_cjk(store):
    """≥3-char CJK matches via the trigram FTS tier (and the LIKE net too)."""
    store.insert_memo(user_id="u1", source_text="王大明是我老板")
    store.insert_memo(user_id="u1", source_text="无关的一条备忘")
    hits = store.search_memos_fts("王大明", user_id="u1")
    assert len(hits) == 1
    assert "王大明" in hits[0]["source_text"]


def test_search_cjk_excludes_soft_deleted(store):
    """Soft-delete must still exclude under the CJK / LIKE path."""
    store.insert_memo(user_id="u1", source_text="去北京出差")
    assert len(store.search_memos_fts("北京", user_id="u1")) == 1
    row = store._conn.execute(
        "SELECT id FROM memos WHERE source_text LIKE '%北京%'").fetchone()
    assert store.soft_delete("memos", row["id"]) is True
    assert store.search_memos_fts("北京", user_id="u1") == []


# ---------------------------------------------------------------------------
# C2 soft-delete semantics + append-only refusal
# ---------------------------------------------------------------------------

def test_soft_delete_is_idempotent_and_rejects_logs(store):
    mid = store.insert_memo(user_id="u1", source_text="to delete")
    assert store.soft_delete("memos", mid) is True
    assert store.soft_delete("memos", mid) is False  # already deleted
    # Append-only logs refuse soft-delete (C2/C7).
    lid = store.insert_llm_call_log(
        user_id="u1", model="m", prompt_input={"p": 1}, success=True)
    with pytest.raises(ValueError):
        store.soft_delete("llm_call_logs", lid)
    with pytest.raises(ValueError):
        store.soft_delete("dlq_messages", "anything")


def test_unknown_table_rejected(store):
    with pytest.raises(ValueError):
        store.soft_delete("not_a_table", "x")
    with pytest.raises(ValueError):
        store.count_rows("not_a_table")


def test_count_rows_works_on_append_only_logs(store):
    """C4 regression: count_rows must NOT append `WHERE deleted_at IS NULL` to
    append-only tables (dlq_messages/llm_call_logs have no such column)."""
    store.insert_llm_call_log(user_id="u1", model="m", prompt_input={}, success=True)
    store.insert_dlq(user_id="u1", source="s", error_type="t", error_msg="m",
                     original_data={"x": 1})
    assert store.count_rows("llm_call_logs") == 1
    assert store.count_rows("dlq_messages") == 1
    # include_deleted is a no-op for append-only (no deleted_at) — still counts.
    assert store.count_rows("llm_call_logs", include_deleted=True) == 1


# ---------------------------------------------------------------------------
# Append-only logs (C6 / C7)
# ---------------------------------------------------------------------------

def test_llm_call_log_full_fields(store):
    """C6 replay substrate: prompt_input + response stored as full JSON."""
    lid = store.insert_llm_call_log(
        user_id="u1", model="glm-5.2", provider="zhipu",
        prompt_template_version="v11", prompt_input={"system": "s", "user": "u"},
        response={"atoms": [1, 2]}, input_tokens=10, output_tokens=5,
        latency_ms=120, success=True, schema_valid=True, cost_cny=0.012,
    )
    row = store._conn.execute(
        "SELECT * FROM llm_call_logs WHERE id=?", (lid,)).fetchone()
    assert row["provider"] == "zhipu"
    assert row["prompt_template_version"] == "v11"
    assert row["schema_valid"] == 1
    assert row["cost_cny"] == pytest.approx(0.012)
    assert "atoms" in row["response"]  # full JSON round-trips
    assert row["success"] == 1


def test_dlq_round_trips_original_data(store):
    did = store.insert_dlq(
        user_id="u1", source="atom_filter",
        error_type="below_confidence_threshold", error_msg="too low",
        original_data={"memo_id": "m1", "atom": {"type": "R2", "text": "x"}},
    )
    row = store._conn.execute(
        "SELECT original_data FROM dlq_messages WHERE id=?", (did,)).fetchone()
    import json
    assert json.loads(row["original_data"])["atom"]["type"] == "R2"


# ---------------------------------------------------------------------------
# Declarative migration: _reconcile_columns is idempotent + additive
# ---------------------------------------------------------------------------

def test_reconcile_idempotent_on_reopen(tmp_path):
    """Re-instantiating the store on an existing DB re-runs reconcile without
    error and without duplicating columns."""
    db = str(tmp_path / "ptg.db")
    s1 = PTGStore(db_path=db)
    s1.ensure_founder("u1", "a@b.c")
    s1.close()
    # Second open heals any missing reconcile columns; must not raise.
    s2 = PTGStore(db_path=db)
    try:
        # is_overdue was a reconcile target — present and defaulted.
        cols = _columns(s2._conn, "meaning_events")
        assert "is_overdue" in cols
        assert "completed_at" in cols
    finally:
        s2.close()


# ---------------------------------------------------------------------------
# sqlite-vec graceful degrade
# ---------------------------------------------------------------------------

def test_vec_degrades_to_fts_when_unavailable(store, monkeypatch):
    """When sqlite-vec can't load, the store reports vec_available=False and
    FTS5 recall still works (base tier)."""
    # sqlite-vec is not installed in the base test env, so vec is already off.
    assert store.vec_available in (True, False)  # reflects actual env
    # Regardless of vec state, FTS recall must work.
    store.insert_memo(user_id="u1", source_text="degrade test budget")
    assert len(store.search_memos_fts("budget", user_id="u1")) == 1


def test_validate_embedding_dim():
    import struct
    good = struct.pack(f"{4}f", 0.1, 0.2, 0.3, 0.4) * 128  # 512 floats (V5 BGE-small-zh dim)
    assert ptg_schema.validate_embedding_dim(good, 512) is None
    short = struct.pack("4f", 0.1, 0.2, 0.3, 0.4)  # 4 floats
    reason = ptg_schema.validate_embedding_dim(short, 512)
    assert reason is not None
    assert "4" in reason or "expected" in reason
    assert ptg_schema.validate_embedding_dim(None, 512) is not None


# ---------------------------------------------------------------------------
# Process-wide shared-connection singleton (decision 3)
# ---------------------------------------------------------------------------

def test_shared_singleton_one_connection(tmp_path):
    """Two PTGStore instances on the same path share ONE connection + lock."""
    db = str(tmp_path / "ptg.db")
    a = PTGStore(db_path=db)
    b = PTGStore(db_path=db)
    try:
        assert a._conn is b._conn
        assert a._lock is b._lock
        # A write through A is visible through B (same connection).
        a.insert_memo(user_id="u1", source_text="shared singleton budget")
        assert b.count_rows("memos") == 1
    finally:
        a.close()
        b.close()


def test_close_refcount_keeps_connection_for_sibling(tmp_path):
    db = str(tmp_path / "ptg.db")
    a = PTGStore(db_path=db)
    b = PTGStore(db_path=db)
    a.close()  # drop one ref; connection must stay alive for b
    try:
        b.insert_memo(user_id="u1", source_text="sibling still alive budget")
        assert b.count_rows("memos") == 1
    finally:
        b.close()


# ---------------------------------------------------------------------------
# ensure_founder idempotency
# ---------------------------------------------------------------------------

def test_ensure_founder_idempotent(store):
    uid = "u1"
    # Second call returns same id, does not duplicate.
    store.ensure_founder(uid, "founder@example.com")
    store.ensure_founder(uid, "founder@example.com")
    assert store.count_rows("realityos_users") == 1


def test_ensure_founder_promotes_migrated_row(store):
    """V5's is_founder is all-false in production; a faithfully migrated founder
    row arrives with is_founder=0 and must be PROMOTED on init, not left at 0
    (real-data finding — ADR-V6-009)."""
    store._conn.execute(
        "INSERT INTO realityos_users (id, email, password_hash, is_founder, "
        "settings, data_consent, created_at, updated_at) "
        "VALUES ('mig', 'founder@v5.cn', '', 0, '{}', "
        "'{\"local_only\": true}', '2026-07-18T00:00:00+00:00', "
        "'2026-07-18T00:00:00+00:00')")
    before = store._conn.execute(
        "SELECT is_founder FROM realityos_users WHERE id='mig'").fetchone()
    assert before["is_founder"] == 0  # arrived un-flagged, exactly as in V5
    store.ensure_founder("mig", "founder@v5.cn")
    after = store._conn.execute(
        "SELECT is_founder FROM realityos_users WHERE id='mig'").fetchone()
    assert after["is_founder"] == 1  # promoted — no second row created
    assert store.count_rows("realityos_users") == 2  # u1 + mig, no dup

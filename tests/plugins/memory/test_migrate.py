"""V5 → V6 migration importer — regression tests with synthetic V5 JSONL.

The JSONL fixtures mimic exactly what the V5-side exporter writes: PG-native
types pre-serialized to JSON (UUID→str, TIMESTAMPTZ→iso8601 str, NUMERIC→float,
JSONB→dict/list, BOOLEAN→bool). The importer's converters are idempotent on
these already-converted values, so the same column maps handle live-PG and
JSONL-roundtripped rows.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from plugins.memory.ptg.migrate import import_dump, import_table
from plugins.memory.ptg.store import PTGStore

UID = "11111111-2222-3333-4444-555555555555"
MEMO_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _write_jsonl(path: Path, rows: list) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _v5_user():
    return {
        "id": UID, "email": "founder@example.com", "password_hash": "$2b$hash",
        "phone": None, "nickname": "founder", "avatar_url": None,
        "timezone": "Asia/Shanghai", "status": "active", "is_founder": True,
        "version": 1, "settings": {"theme": "dark"},
        "data_consent": {"local_only": True, "shareable": False},
        "last_active_at": "2026-07-17T10:00:00+00:00",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-07-17T10:00:00+00:00", "deleted_at": None,
    }


@pytest.fixture
def store(tmp_path):
    s = PTGStore(db_path=str(tmp_path / "ptg.db"))
    yield s
    s.close()


@pytest.fixture
def dump(tmp_path):
    d = tmp_path / "v5dump"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# users → realityos_users rename + type conversion
# ---------------------------------------------------------------------------

def test_import_users_renamed_and_converted(store, dump):
    _write_jsonl(dump / "users.jsonl", [_v5_user()])
    stats = import_table(store, "users", dump / "users.jsonl")
    assert stats == {"read": 1, "written": 1, "pk_conflict": 0, "violation": 0, "errors": 0}
    row = store._conn.execute("SELECT * FROM realityos_users").fetchone()
    assert row["id"] == UID
    assert row["is_founder"] == 1                       # bool → INTEGER
    assert json.loads(row["settings"])["theme"] == "dark"  # JSONB → JSON TEXT
    assert row["timezone"] == "Asia/Shanghai"


# ---------------------------------------------------------------------------
# memos: UUID/datetime/JSONB conversion
# ---------------------------------------------------------------------------

def test_import_memos_type_conversion(store, dump):
    _write_jsonl(dump / "users.jsonl", [_v5_user()])
    _write_jsonl(dump / "memos.jsonl", [{
        "id": MEMO_ID, "user_id": UID, "input_mode": "voice",
        "source_text": "今天和 Alice 开会讨论预算",
        "corrected_text": "今天和 Alice 开会讨论预算。", "audio_clip_id": None,
        "timestamp": "2026-07-17T09:30:00+00:00", "summary": "预算会议",
        "moderation_status": "clean", "version": 1,
        "location_context": {"lat": 24.48, "lng": 118.09, "place": "厦门"},
        "created_at": "2026-07-17T09:30:00+00:00", "deleted_at": None,
    }])
    import_dump(store, dump)
    row = store._conn.execute("SELECT * FROM memos WHERE id=?", (MEMO_ID,)).fetchone()
    assert row["source_text"] == "今天和 Alice 开会讨论预算"
    loc = json.loads(row["location_context"])
    assert loc["place"] == "厦门"
    assert row["input_mode"] == "voice"


# ---------------------------------------------------------------------------
# feeling_events: ser_audio (V5 value) accepted; NOT NULL fields present
# ---------------------------------------------------------------------------

def test_import_feeling_events_v5_ser_source(store, dump):
    _write_jsonl(dump / "users.jsonl", [_v5_user()])
    _write_jsonl(dump / "feeling_events.jsonl", [{
        "id": "f0000000-0000-0000-0000-000000000001", "user_id": UID,
        "memo_id": None, "timestamp": "2026-07-17T09:30:00+00:00",
        "source_text": "好累", "input_mode": "voice", "confidence_base": 0.82,
        "state_type": "fatigue", "direction": "down", "intensity": "high",
        "trigger_source": {"trigger": "workload"}, "emotion_vad": {"v": 0.2, "a": 0.3, "d": 0.4},
        "ser_source": "ser_audio", "relation_confidence": 0.75,
        "llm_call_id": None, "schema_version": "1.0", "version": 1,
        "created_at": "2026-07-17T09:30:00+00:00", "deleted_at": None,
    }])
    import_dump(store, dump)
    row = store._conn.execute("SELECT * FROM feeling_events").fetchone()
    assert row["ser_source"] == "ser_audio"   # would FAIL under the old acoustic/fused CHECK
    assert row["state_type"] == "fatigue"
    assert json.loads(row["emotion_vad"])["a"] == 0.3


# ---------------------------------------------------------------------------
# C4 regression (real-data 2026-07-18): V5 rows LACKING V6-added columns that
# carry a schema DEFAULT must import VIA THE DEFAULT — not trip NOT NULL by
# passing explicit NULL. Synthetic fixtures always set ser_source, hiding that
# the founder's real V5 feeling_events/entities rows have NO such column.
# ---------------------------------------------------------------------------

def test_import_v5_row_lacking_v6_defaulted_columns(store, dump):
    """Real V5 ``feeling_events`` has no ``ser_source``/``emotion_vad`` column;
    real V5 ``entities`` has no ``voiceprint_samples``/``voiceprint_confidence``
    column. The V6 schema declares DEFAULTs for these (``ser_source DEFAULT
    'llm_text'``, ``voiceprint_confidence DEFAULT 0.0``) precisely so such rows
    import. ``convert_row`` must OMIT the column when the V5 key is absent (so
    the INSERT does not name it and SQLite applies the DEFAULT) — NOT emit
    ``None``, which stores explicit NULL, defeats the DEFAULT, and trips NOT NULL
    (the bug surfaced by the 2026-06-14 real-dump validation: 14/14 feeling +
    165/165 entities rejected as ``ser_source``/``voiceprint_confidence`` NULL).
    """
    _write_jsonl(dump / "users.jsonl", [_v5_user()])
    # feeling_events row mirroring the real dump: NO ser_source / emotion_vad keys.
    _write_jsonl(dump / "feeling_events.jsonl", [{
        "id": "f0000000-0000-0000-0000-000000000010", "user_id": UID,
        "memo_id": None, "timestamp": "2026-07-17T09:30:00+00:00",
        "source_text": "好累", "input_mode": "text", "confidence_base": 0.9,
        "state_type": "fatigue", "direction": "down", "intensity": "high",
        "trigger_source": {"trigger": "workload"}, "relation_confidence": 0.75,
        "llm_call_id": None, "schema_version": "1.0", "version": 1,
        "created_at": "2026-07-17T09:30:00+00:00", "deleted_at": None,
    }])
    # entities row mirroring the real dump: NO voiceprint_* keys.
    _write_jsonl(dump / "entities.jsonl", [{
        "id": "e0000000-0000-0000-0000-000000000010", "user_id": UID,
        "entity_name": "Alice", "entity_name_normalized": "alice",
        "entity_type": "person", "properties": {}, "mention_count": 3,
        "first_seen_at": "2026-01-01T00:00:00+00:00",
        "last_seen_at": "2026-07-17T00:00:00+00:00",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-07-17T00:00:00+00:00", "deleted_at": None,
        "version": 1,
    }])
    report = import_dump(store, dump)
    fe = report["tables"]["feeling_events"]
    ent = report["tables"]["entities"]
    # Both import CLEANLY — zero violation, zero DLQ (the bug previously rejected
    # 100% of these rows as NOT NULL violations).
    assert fe["violation"] == 0 and fe["written"] == 1
    assert ent["violation"] == 0 and ent["written"] == 1
    assert store.count_rows("dlq_messages") == 0
    # The V6 DEFAULTs applied (column omitted from INSERT → DEFAULT, not NULL).
    fe_row = store._conn.execute(
        "SELECT ser_source, emotion_vad FROM feeling_events").fetchone()
    assert fe_row["ser_source"] == "llm_text"      # schema DEFAULT applied
    assert fe_row["emotion_vad"] is None            # nullable → NULL
    ent_row = store._conn.execute(
        "SELECT voiceprint_confidence, voiceprint_samples FROM entities").fetchone()
    assert ent_row["voiceprint_confidence"] == 0.0  # schema DEFAULT applied
    assert ent_row["voiceprint_samples"] is None    # nullable → NULL


# ---------------------------------------------------------------------------
# relations: V5 subject_id/object_id/last_updated names
# ---------------------------------------------------------------------------

def test_import_relations_v5_column_names(store, dump):
    _write_jsonl(dump / "users.jsonl", [_v5_user()])
    _write_jsonl(dump / "entities.jsonl", [
        {"id": "e0000000-0000-0000-0000-000000000001", "user_id": UID,
         "entity_name": "Alice", "entity_name_normalized": "alice",
         "entity_type": "person", "properties": {}, "mention_count": 3,
         "voiceprint_samples": None, "voiceprint_confidence": 0.0, "version": 1,
         "first_seen_at": "2026-01-01T00:00:00+00:00",
         "last_seen_at": "2026-07-17T00:00:00+00:00",
         "created_at": "2026-01-01T00:00:00+00:00",
         "updated_at": "2026-07-17T00:00:00+00:00", "deleted_at": None},
        {"id": "e0000000-0000-0000-0000-000000000002", "user_id": UID,
         "entity_name": "预算", "entity_name_normalized": "预算",
         "entity_type": "topic", "properties": {}, "mention_count": 2,
         "voiceprint_samples": None, "voiceprint_confidence": 0.0, "version": 1,
         "first_seen_at": "2026-01-01T00:00:00+00:00",
         "last_seen_at": "2026-07-17T00:00:00+00:00",
         "created_at": "2026-01-01T00:00:00+00:00",
         "updated_at": "2026-07-17T00:00:00+00:00", "deleted_at": None},
    ])
    _write_jsonl(dump / "relations.jsonl", [{
        "id": "r0000000-0000-0000-0000-000000000001", "user_id": UID,
        "subject_id": "e0000000-0000-0000-0000-000000000001",
        "object_id": "e0000000-0000-0000-0000-000000000002",
        "relation_type": "discusses", "value": None, "confidence": 0.66,
        "trend": {"dir": "up"}, "last_updated": "2026-07-17T00:00:00+00:00",
        "evidence_count": 2, "version": 1,
        "created_at": "2026-01-01T00:00:00+00:00", "deleted_at": None,
    }])
    import_dump(store, dump)
    row = store._conn.execute("SELECT * FROM relations").fetchone()
    assert row["subject_id"].startswith("e0000000")
    assert row["object_id"].startswith("e0000000")
    assert row["last_updated"].startswith("2026-07-17")
    assert json.loads(row["trend"])["dir"] == "up"


# ---------------------------------------------------------------------------
# Idempotency (INSERT OR IGNORE)
# ---------------------------------------------------------------------------

def test_import_is_idempotent(store, dump):
    _write_jsonl(dump / "users.jsonl", [_v5_user()])
    r1 = import_table(store, "users", dump / "users.jsonl")
    assert r1["written"] == 1
    r2 = import_table(store, "users", dump / "users.jsonl")
    assert r2["written"] == 0           # PK conflict → IGNORE
    assert r2["pk_conflict"] == 1       # classified as idempotent PK conflict (not silent skip)
    assert store.count_rows("realityos_users") == 1


# ---------------------------------------------------------------------------
# C7: INSERT OR IGNORE skips classified — idempotent PK conflict vs constraint
# violation. Constraint-violation rows are DLQ'd, never silently lost.
# ---------------------------------------------------------------------------

def test_import_classifies_pk_conflict_vs_constraint_violation(store, dump):
    """Regression (C7): a row INSERT OR IGNORE skips must NOT be silently lumped
    into a bare "skipped" count. It is either an idempotent PK conflict (legit,
    expected on re-runs) or a schema-constraint violation (NOT NULL / CHECK) —
    the latter is routed to the DLQ so user data is never silently lost.

    Previously ``skipped = read - written - errors`` conflated the two, so a
    V5 memo with e.g. ``location_context=NULL`` (violates the memos NOT NULL) or
    an out-of-set ``moderation_status`` (violates the CHECK) would vanish from
    the migration with no signal — indistinguishable from an idempotent re-run.

    Three memos: A valid (writes), B valid (writes, then PK-conflicts on
    re-import), C with ``moderation_status='pending'`` (violates the
    ``moderation_status IN ('clean','flagged')`` CHECK → violation + DLQ).
    """
    _write_jsonl(dump / "users.jsonl", [_v5_user()])
    _write_jsonl(dump / "memos.jsonl", [
        {"id": "aaaaaa00-0000-0000-0000-000000000001", "user_id": UID,
         "input_mode": "text", "source_text": "memo A valid",
         "corrected_text": None, "audio_clip_id": None,
         "timestamp": "2026-07-17T09:00:00+00:00", "summary": None,
         "moderation_status": "clean", "version": 1, "location_context": "{}",
         "created_at": "2026-07-17T09:00:00+00:00", "deleted_at": None},
        {"id": "aaaaaa00-0000-0000-0000-000000000002", "user_id": UID,
         "input_mode": "voice", "source_text": "memo B valid",
         "corrected_text": None, "audio_clip_id": None,
         "timestamp": "2026-07-17T09:10:00+00:00", "summary": None,
         "moderation_status": "clean", "version": 1, "location_context": "{}",
         "created_at": "2026-07-17T09:10:00+00:00", "deleted_at": None},
        {"id": "aaaaaa00-0000-0000-0000-000000000003", "user_id": UID,
         "input_mode": "text", "source_text": "memo C bad moderation",
         "corrected_text": None, "audio_clip_id": None,
         "timestamp": "2026-07-17T09:20:00+00:00", "summary": None,
         "moderation_status": "pending",  # violates CHECK (moderation_status IN ('clean','flagged'))
         "version": 1, "location_context": "{}",
         "created_at": "2026-07-17T09:20:00+00:00", "deleted_at": None},
    ])

    # First import: A + B write; C violates → violation=1 + DLQ.
    r1 = import_dump(store, dump, tables=["users", "memos"])
    m1 = r1["tables"]["memos"]
    assert m1["read"] == 3
    assert m1["written"] == 2          # A, B
    assert m1["pk_conflict"] == 0
    assert m1["violation"] == 1        # C
    assert m1["errors"] == 0
    # No silent loss: every read row is fully accounted for.
    assert m1["read"] == m1["written"] + m1["pk_conflict"] + m1["violation"] + m1["errors"]
    assert store.count_rows("memos") == 2   # C never inserted
    dlq1 = store._conn.execute(
        "SELECT error_type FROM dlq_messages WHERE source=?", ("v5_migrate:memos",)).fetchall()
    assert any(row["error_type"] == "constraint_violation" for row in dlq1)

    # Second import (re-run): A + B now idempotent PK conflicts; C still violates.
    r2 = import_dump(store, dump, tables=["users", "memos"])
    m2 = r2["tables"]["memos"]
    assert m2["written"] == 0
    assert m2["pk_conflict"] == 2      # A, B (idempotent, NOT data loss)
    assert m2["violation"] == 1        # C (still can't insert; DLQ'd again)
    assert m2["read"] == m2["written"] + m2["pk_conflict"] + m2["violation"] + m2["errors"]
    assert store.count_rows("memos") == 2   # unchanged — nothing silently dropped


# ---------------------------------------------------------------------------
# Missing files skipped; malformed lines → DLQ (C7)
# ---------------------------------------------------------------------------

def test_import_dump_skips_missing_tables(store, dump):
    _write_jsonl(dump / "users.jsonl", [_v5_user()])  # only users present
    report = import_dump(store, dump)
    assert report["tables"]["users"]["written"] == 1
    # Every other table file absent → read 0, no error.
    for t in ("memos", "relations", "llm_call_logs"):
        assert report["tables"][t]["read"] == 0


def test_malformed_line_goes_to_dlq(store, dump):
    # One good user + one garbage line.
    (dump / "users.jsonl").write_text(
        json.dumps(_v5_user()) + "\n" + "{not valid json\n", encoding="utf-8")
    stats = import_table(store, "users", dump / "users.jsonl")
    assert stats["read"] == 2
    assert stats["written"] == 1
    assert stats["errors"] == 1
    assert store.count_rows("dlq_messages") == 1     # C7: failure recorded, not silent
    dlq = store._conn.execute("SELECT source, original_data FROM dlq_messages").fetchone()
    assert dlq["source"] == "v5_migrate:users"
    assert json.loads(dlq["original_data"])["table"] == "users"


# ---------------------------------------------------------------------------
# append-only logs round-trip + audit row
# ---------------------------------------------------------------------------

def test_import_llm_call_logs_full_fields(store, dump):
    _write_jsonl(dump / "users.jsonl", [_v5_user()])
    _write_jsonl(dump / "llm_call_logs.jsonl", [{
        "id": "l0000000-0000-0000-0000-000000000001", "user_id": UID,
        "created_at": "2026-07-17T09:30:00+00:00", "model": "glm-5.2",
        "provider": "zhipu", "prompt_template_version": "v11",
        "prompt_input": {"system": "s", "user": "u"}, "input_tokens": 120,
        "response": {"atoms": [1, 2]}, "output_tokens": 45, "latency_ms": 800,
        "success": True, "schema_valid": True, "cost_cny": 0.012,
        "error_type": None, "error_msg": None,
    }])
    import_dump(store, dump)
    row = store._conn.execute("SELECT * FROM llm_call_logs").fetchone()
    assert row["model"] == "glm-5.2"
    assert row["success"] == 1 and row["schema_valid"] == 1
    assert row["cost_cny"] == pytest.approx(0.012)
    assert json.loads(row["prompt_input"])["system"] == "s"


def test_import_dump_writes_audit_row(store, dump):
    _write_jsonl(dump / "users.jsonl", [_v5_user()])
    report = import_dump(store, dump)
    row = store._conn.execute(
        "SELECT value FROM ptg_meta WHERE key=?", (report["audit_key"],)).fetchone()
    assert row is not None
    audit = json.loads(row["value"])
    assert audit["totals"]["written"] == 1
    assert "users" in audit["tables"]


# ---------------------------------------------------------------------------
# Batch boundary (more rows than batch size)
# ---------------------------------------------------------------------------

def test_import_handles_batch_boundary(store, dump):
    rows = []
    for i in range(1100):  # batch default 500 → 3 flushes
        u = _v5_user()
        u["id"] = f"{i:08d}-0000-0000-0000-000000000000"
        u["email"] = f"u{i}@example.com"
        rows.append(u)
    _write_jsonl(dump / "users.jsonl", rows)
    stats = import_table(store, "users", dump / "users.jsonl", batch=500)
    assert stats["read"] == 1100
    assert stats["written"] == 1100
    assert stats["errors"] == 0
    assert store.count_rows("realityos_users") == 1100

"""RealityOS V6 PTG SQLite schema — mirrors V5's 13 production tables.

See ``docs/adr/V6/ADR-V6-008.md`` for the schema-fidelity decision (Option A:
mirror V5's real tables, not the earlier-assumed 13 that included 4 phantom
tables). PG→SQLite type mapping per the digest cheat sheet:

    UUID ............ TEXT (36-char hyphenated; app generates uuid4)
    JSONB ........... TEXT (JSON-encoded; parsed in app)
    TIMESTAMPTZ ..... TEXT (ISO-8601, ideally with offset/Z)
    DATE ............ TEXT (YYYY-MM-DD)
    VARCHAR(n) ...... TEXT (SQLite ignores length; enum enforced via CHECK)
    BOOLEAN ......... INTEGER (0/1)
    NUMERIC(4,3)/Float REAL
    Vector(512) ..... vec0 virtual table (memos_vec) when sqlite-vec loads

C2 iron rule: every user-data table has ``deleted_at`` + ``version``. The two
append-only infrastructure logs (``dlq_messages``, ``llm_call_logs``) are the
only C2-exempt tables — V5 treats them the same way.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Bump when the DDL below changes structurally. _reconcile_columns heals rows
# on existing DBs; this version is recorded on the schema_version row of the
# ptg_meta table so a migration can detect a cross-version DB.
#
# v2 (2026-07-17): fidelity corrections to the "mirror V5" decision (REV-10),
# revealed by a full read of danao13/backend/app/models —
#   * identity_events: drop invented person_attributes; add V5's sentiment +
#     interaction_type (with their CHECKs).
#   * feeling_events: state_type/direction/intensity NOT NULL (V5); ser_source
#     has NO CHECK in V5 (values llm_text/ser_audio/both) — the previous
#     acoustic/fused CHECK would have rejected real V5 data.
#   * relations: V5 names are subject_id/object_id/last_updated (not
#     source_id/target_id/updated_at); evidence_count defaults to 1.
#   * meaning_events: add V5's nullable updated_at.
# No v1 production ptg.db exists (V6 unreleased), so additive cols are healed
# by _reconcile_columns on reopen; the relations column RENAME assumes a fresh
# DB (documented).
#
# v3 (2026-07-18): memos_fts tokenizer default(unicode61) → trigram. Found via
# REAL-data validation (ADR-V6-009): unicode61 splits on whitespace, and CJK
# has none, so Chinese recall was silently 0 — synthetic English tests ("budget")
# passed while the founder's actual 北京/<真实 3 字人名>/老婆 queries returned nothing
# (the ADR-088 "synthetic samples hid the bug" lesson, again). trigram gives
# substring recall for ≥3-char terms; search_memos_fts adds a LIKE OR-join
# safety-net for <3-char CJK (北京/老婆) that trigram can't match. Existing v2
# DBs are upgraded by _ensure_fts_trigram (drop+recreate+rebuild).
SCHEMA_VERSION = 3

# Common columns shared by the four R-atom event tables (identity/meaning/
# entity/feeling). Kept as a fragment so the four tables stay byte-for-byte
# consistent in their shared spine — a drift here is exactly the class of
# inconsistency that made ADR-088's R0 pollution bug possible.
_EVENT_SPINE = """
    id                  TEXT PRIMARY KEY,
    user_id             TEXT NOT NULL,
    memo_id             TEXT,
    timestamp           TEXT NOT NULL,
    source_text         TEXT NOT NULL,
    input_mode          TEXT NOT NULL CHECK (input_mode IN ('text','voice')),
    confidence_base     REAL NOT NULL CHECK (confidence_base BETWEEN 0 AND 1),
    relation_confidence REAL NOT NULL CHECK (relation_confidence BETWEEN 0 AND 1),
    llm_call_id         TEXT,
    schema_version      TEXT NOT NULL DEFAULT '1.0',
    version             INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deleted_at          TEXT
"""

_SCHEMA_SQL = """
-- ── realityos_users (V5: users) — single founder row in V6 desktop ──────
CREATE TABLE IF NOT EXISTS realityos_users (
    id              TEXT PRIMARY KEY,
    email           TEXT NOT NULL UNIQUE,
    password_hash   TEXT NOT NULL,
    phone           TEXT,
    nickname        TEXT,
    avatar_url      TEXT,
    timezone        TEXT NOT NULL DEFAULT 'Asia/Shanghai',
    status          TEXT NOT NULL DEFAULT 'active',
    is_founder      INTEGER NOT NULL DEFAULT 0,
    settings        TEXT NOT NULL DEFAULT '{}',
    data_consent    TEXT NOT NULL DEFAULT '{"local_only": true, "shareable": false}',
    last_active_at  TEXT,
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deleted_at      TEXT,
    version         INTEGER NOT NULL DEFAULT 1
);

-- ── memos (V5: memos) — raw captured turns / voice transcripts ──────────
CREATE TABLE IF NOT EXISTS memos (
    id                TEXT PRIMARY KEY,
    user_id           TEXT NOT NULL REFERENCES realityos_users(id),
    input_mode        TEXT NOT NULL CHECK (input_mode IN ('text','voice')),
    source_text       TEXT NOT NULL,
    corrected_text    TEXT,
    audio_clip_id     TEXT,
    timestamp         TEXT NOT NULL,
    summary           TEXT,
    moderation_status TEXT CHECK (moderation_status IN ('clean','flagged')),
    location_context  TEXT NOT NULL DEFAULT '{}',
    version           INTEGER NOT NULL DEFAULT 1,
    created_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deleted_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_memos_user_time ON memos(user_id, timestamp, id);
CREATE INDEX IF NOT EXISTS idx_memos_deleted   ON memos(deleted_at);

-- memos full-text index (base tier — always available, no extra deps). FTS5
-- over source_text + corrected_text so keyword recall works even without the
-- embeddings extra. tokenize='trigram' (schema v3): the default unicode61
-- tokenizer splits on whitespace, which CJK lacks — Chinese recall was 0 until
-- real-data validation caught it. trigram gives substring recall (incl. CJK)
-- for ≥3-char terms; the store unions a LIKE OR-join for <3-char CJK. External-
-- content table keyed off the implicit INTEGER rowid (memos.id is a TEXT UUID
-- and can't be the content_rowid). The three triggers keep the index in sync;
-- queries join memos_fts.rowid back to memos.rowid.
CREATE VIRTUAL TABLE IF NOT EXISTS memos_fts
    USING fts5(source_text, corrected_text, content=memos, content_rowid=rowid,
               tokenize='trigram');
CREATE TRIGGER IF NOT EXISTS memos_ai AFTER INSERT ON memos BEGIN
    INSERT INTO memos_fts(rowid, source_text, corrected_text)
        VALUES (new.rowid, new.source_text, new.corrected_text);
END;
CREATE TRIGGER IF NOT EXISTS memos_ad AFTER UPDATE ON memos BEGIN
    INSERT INTO memos_fts(memos_fts, rowid, source_text, corrected_text)
        VALUES ('delete', old.rowid, old.source_text, old.corrected_text);
    INSERT INTO memos_fts(rowid, source_text, corrected_text)
        VALUES (new.rowid, new.source_text, new.corrected_text);
END;
CREATE TRIGGER IF NOT EXISTS memos_ax AFTER DELETE ON memos BEGIN
    INSERT INTO memos_fts(memos_fts, rowid, source_text, corrected_text)
        VALUES ('delete', old.rowid, old.source_text, old.corrected_text);
END;

-- ── identity_events (R3 Person) ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS identity_events (""" + _EVENT_SPINE + """,
    person_name        TEXT NOT NULL,
    mention_context    TEXT,
    sentiment          TEXT CHECK (sentiment IN ('positive','neutral','negative')),
    interaction_type   TEXT CHECK (interaction_type IN ('meeting','communication','conflict','casual'))
);
CREATE INDEX IF NOT EXISTS idx_identity_user_time ON identity_events(user_id, timestamp, id);
CREATE INDEX IF NOT EXISTS idx_identity_deleted   ON identity_events(deleted_at);

-- ── meaning_events (R2 Task intent_class='Need_To_Do' + R7 Expression) ──
CREATE TABLE IF NOT EXISTS meaning_events (""" + _EVENT_SPINE + """,
    intent_class     TEXT NOT NULL CHECK (intent_class IN
        ('Need_To_Do','Complaint','Health','Help','Evaluation',
         'Conflict','Consumption','Other')),
    task_description TEXT,
    urgency          TEXT CHECK (urgency IN ('high','medium','low')),
    deadline         TEXT,
    task_status      TEXT NOT NULL DEFAULT 'pending'
        CHECK (task_status IN ('pending','in_progress','completed','dismissed')),
    topic_tags       TEXT,
    completed_at     TEXT,
    completion_note  TEXT,
    is_overdue       INTEGER NOT NULL DEFAULT 0,
    updated_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_meaning_user_time  ON meaning_events(user_id, timestamp, id);
CREATE INDEX IF NOT EXISTS idx_meaning_memo       ON meaning_events(memo_id);
CREATE INDEX IF NOT EXISTS idx_meaning_status     ON meaning_events(user_id, task_status);
CREATE INDEX IF NOT EXISTS idx_meaning_overdue    ON meaning_events(user_id, is_overdue, task_status);
CREATE INDEX IF NOT EXISTS idx_meaning_deleted    ON meaning_events(deleted_at);

-- ── entity_events (R0 Entity — places/orgs/terms; ADR-088) ──────────────
CREATE TABLE IF NOT EXISTS entity_events (""" + _EVENT_SPINE + """,
    entity_name     TEXT NOT NULL,
    entity_category TEXT NOT NULL
        CHECK (entity_category IN ('place','organization','term')),
    mention_context TEXT
);
CREATE INDEX IF NOT EXISTS idx_entity_user_time ON entity_events(user_id, timestamp, id);
CREATE INDEX IF NOT EXISTS idx_entity_memo      ON entity_events(memo_id);
CREATE INDEX IF NOT EXISTS idx_entity_deleted   ON entity_events(deleted_at);

-- ── feeling_events (R1 SelfState + emotion_vad; M2-F1 SER) ──────────────
-- state_type/direction/intensity are NOT NULL in V5 (LLM extraction always
-- assigns them); ser_source has NO CHECK in V5 (values llm_text/ser_audio/both).
CREATE TABLE IF NOT EXISTS feeling_events (""" + _EVENT_SPINE + """,
    state_type      TEXT NOT NULL CHECK (state_type IN ('stress','fatigue','energy','mood')),
    direction       TEXT NOT NULL CHECK (direction IN ('up','down','stable')),
    intensity       TEXT NOT NULL CHECK (intensity IN ('high','medium','low')),
    emotion_vad     TEXT,
    ser_source      TEXT NOT NULL DEFAULT 'llm_text',
    trigger_source  TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_feeling_user_time ON feeling_events(user_id, timestamp, id);
CREATE INDEX IF NOT EXISTS idx_feeling_deleted   ON feeling_events(deleted_at);

-- ── entities (PTG nodes — people are entity_type='person') ──────────────
CREATE TABLE IF NOT EXISTS entities (
    id                     TEXT PRIMARY KEY,
    user_id                TEXT NOT NULL REFERENCES realityos_users(id),
    entity_name            TEXT NOT NULL,
    entity_name_normalized TEXT NOT NULL,
    entity_type            TEXT NOT NULL
        CHECK (entity_type IN ('person','task','topic','context')),
    properties             TEXT NOT NULL DEFAULT '{}',
    mention_count          INTEGER NOT NULL DEFAULT 1,
    voiceprint_samples     TEXT,
    voiceprint_confidence  REAL NOT NULL DEFAULT 0.0,
    version                INTEGER NOT NULL DEFAULT 1,
    first_seen_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at             TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at             TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deleted_at             TEXT
);
CREATE INDEX IF NOT EXISTS idx_entities_user_norm   ON entities(user_id, entity_name_normalized);
CREATE INDEX IF NOT EXISTS idx_entities_user_type   ON entities(user_id, entity_type);
CREATE INDEX IF NOT EXISTS idx_entities_deleted     ON entities(deleted_at);

-- ── relations (PTG edges between entities) ──────────────────────────────
-- V5 column names: subject_id/object_id/last_updated (not source/target/
-- updated_at). evidence_count defaults to 1 in V5.
CREATE TABLE IF NOT EXISTS relations (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES realityos_users(id),
    subject_id      TEXT NOT NULL REFERENCES entities(id),
    object_id       TEXT NOT NULL REFERENCES entities(id),
    relation_type   TEXT NOT NULL,
    value           TEXT,
    confidence      REAL NOT NULL DEFAULT 0.5 CHECK (confidence BETWEEN 0 AND 1),
    trend           TEXT,
    last_updated    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    evidence_count  INTEGER NOT NULL DEFAULT 1,
    version         INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deleted_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_relations_subject ON relations(subject_id);
CREATE INDEX IF NOT EXISTS idx_relations_object ON relations(object_id);

-- ── task_suggestions (V-domain proactive; NOT the R2 task list) ─────────
CREATE TABLE IF NOT EXISTS task_suggestions (
    id                TEXT PRIMARY KEY,
    user_id           TEXT NOT NULL REFERENCES realityos_users(id),
    meaning_event_id  TEXT NOT NULL REFERENCES meaning_events(id),
    suggestion_type   TEXT NOT NULL CHECK (suggestion_type IN
        ('overdue_reminder','completion_prompt','deadline_approaching','stuck_task')),
    status            TEXT NOT NULL DEFAULT 'suggested'
        CHECK (status IN ('suggested','notified','accepted','dismissed','completed')),
    suggestion_text   TEXT NOT NULL,
    task_description  TEXT NOT NULL DEFAULT '',
    days_overdue      INTEGER NOT NULL DEFAULT 0,
    urgency           TEXT NOT NULL DEFAULT 'medium' CHECK (urgency IN ('high','medium','low')),
    notification_sent_at TEXT,
    user_responded_at TEXT,
    dismissal_reason  TEXT,
    confidence        REAL NOT NULL DEFAULT 0.5 CHECK (confidence BETWEEN 0 AND 1),
    llm_call_id       TEXT,
    version           INTEGER NOT NULL DEFAULT 1,
    created_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deleted_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_suggestion_user_status ON task_suggestions(user_id, status);
CREATE INDEX IF NOT EXISTS idx_suggestion_event       ON task_suggestions(meaning_event_id);

-- ── feedback (rating=thumbs_up|thumbs_down; 19 target_types) ────────────
CREATE TABLE IF NOT EXISTS feedback (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id   TEXT NOT NULL,
    rating      TEXT NOT NULL CHECK (rating IN ('thumbs_up','thumbs_down')),
    comment     TEXT,
    version     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deleted_at  TEXT
);
-- Plain UNIQUE (not partial) — ADR-083 F6: reviving a soft-deleted row
-- requires un-deleting the old row, else UNIQUE violation on re-submit.
CREATE UNIQUE INDEX IF NOT EXISTS uq_feedback_user_target
    ON feedback(user_id, target_type, target_id);

-- ── insight_aggregation (LLM insight cache with TTL via expires_at) ─────
CREATE TABLE IF NOT EXISTS insight_aggregation (
    id               TEXT PRIMARY KEY,
    user_id          TEXT NOT NULL REFERENCES realityos_users(id),
    aggregation_type TEXT NOT NULL,
    period_key       TEXT NOT NULL,
    period_start     TEXT NOT NULL,
    period_end       TEXT NOT NULL,
    input_data       TEXT,
    result_data      TEXT NOT NULL,
    confidence       REAL NOT NULL DEFAULT 0.0 CHECK (confidence BETWEEN 0 AND 1),
    data_days        INTEGER NOT NULL DEFAULT 0,
    data_sufficiency TEXT NOT NULL DEFAULT 'insufficient'
        CHECK (data_sufficiency IN ('sufficient','partial','insufficient')),
    generated_by     TEXT NOT NULL DEFAULT 'scheduled'
        CHECK (generated_by IN ('scheduled','manual','on_demand')),
    llm_call_id      TEXT,
    schema_version   TEXT NOT NULL DEFAULT '1.0',
    version          INTEGER NOT NULL DEFAULT 1,
    created_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at       TEXT NOT NULL,
    deleted_at       TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_insight_user_type_period
    ON insight_aggregation(user_id, aggregation_type, period_key);
CREATE INDEX IF NOT EXISTS idx_insight_expires ON insight_aggregation(expires_at);

-- ── dlq_messages (append-only; C7; absorbs V5 filtered_atoms) ───────────
-- C2-exempt: NO deleted_at, NO version (append-only infrastructure log).
CREATE TABLE IF NOT EXISTS dlq_messages (
    id            TEXT PRIMARY KEY,
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    user_id       TEXT NOT NULL,
    source        TEXT NOT NULL,
    error_type    TEXT NOT NULL,
    error_msg     TEXT NOT NULL,
    original_data TEXT NOT NULL,
    retry_count   INTEGER NOT NULL DEFAULT 0,
    resolved      INTEGER NOT NULL DEFAULT 0,
    resolved_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_dlq_resolved ON dlq_messages(resolved);
CREATE INDEX IF NOT EXISTS idx_dlq_source   ON dlq_messages(source);

-- ── llm_call_logs (append-only; C6 replay substrate; D1 field-aligned) ──
-- C2-exempt: NO deleted_at, NO version. Full prompt_input + response JSON.
CREATE TABLE IF NOT EXISTS llm_call_logs (
    id                      TEXT PRIMARY KEY,
    user_id                 TEXT NOT NULL,
    created_at              TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    model                   TEXT NOT NULL,
    provider                TEXT,
    prompt_template_version TEXT NOT NULL DEFAULT 'v1',
    prompt_input            TEXT NOT NULL,
    input_tokens            INTEGER,
    response                TEXT,
    output_tokens           INTEGER,
    latency_ms              INTEGER,
    success                 INTEGER NOT NULL DEFAULT 1,
    schema_valid            INTEGER,
    cost_cny                REAL,
    error_type              TEXT,
    error_msg               TEXT
);
CREATE INDEX IF NOT EXISTS idx_llmlog_created ON llm_call_logs(created_at);

-- ── ptg_meta (schema version bookkeeping; not a user-data table) ────────
CREATE TABLE IF NOT EXISTS ptg_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Tables subject to the C2 soft-delete + version invariant. dlq_messages and
# llm_call_logs are deliberately excluded (append-only logs). ptg_meta is
# internal bookkeeping. Used by the C2 audit test.
C2_USER_TABLES = (
    "realityos_users", "memos", "identity_events", "meaning_events",
    "entity_events", "feeling_events", "entities", "relations",
    "task_suggestions", "feedback", "insight_aggregation",
)
APPEND_ONLY_TABLES = ("dlq_messages", "llm_call_logs")
ALL_TABLES = C2_USER_TABLES + APPEND_ONLY_TABLES

# Declarative migration targets: table -> {column: SQLite type}. When the
# live DB is missing a column, _reconcile_columns ALTERs it in with a NULL or
# sensible default. This is the hermes-store analogue of Alembic — raw SQL,
# introspected via PRAGMA, additive only (never drops, C2).
#
# Keep this in lockstep with _SCHEMA_SQL. A new column added to the DDL MUST
# be added here too, or existing DBs won't get it on upgrade.
_RECONCILE_COLUMNS: Dict[str, Dict[str, str]] = {
    "memos": {"corrected_text": "TEXT", "moderation_status": "TEXT",
              "location_context": "TEXT DEFAULT '{}'"},
    "meaning_events": {"completed_at": "TEXT", "completion_note": "TEXT",
                       "is_overdue": "INTEGER NOT NULL DEFAULT 0",
                       "updated_at": "TEXT"},
    "identity_events": {"sentiment": "TEXT", "interaction_type": "TEXT"},
    "entity_events": {},  # ADR-088 table is new in V6; nothing to reconcile yet.
    "feeling_events": {"emotion_vad": "TEXT",
                       "ser_source": "TEXT NOT NULL DEFAULT 'llm_text'"},
    "llm_call_logs": {"cost_cny": "REAL", "schema_valid": "INTEGER",
                      "prompt_template_version": "TEXT NOT NULL DEFAULT 'v1'"},
    # NOTE: relations v1→v2 rename (source_id→subject_id, target_id→object_id,
    # updated_at→last_updated) is NOT additive and cannot be healed here. It
    # assumes a fresh DB — no v1 production ptg.db exists (V6 unreleased).
    # Add future additive migrations here.
}


def load_sqlite_vec(conn) -> bool:
    """Load the sqlite-vec extension into ``conn``.

    Returns True on success, False if sqlite-vec is unavailable or the Python
    build cannot load extensions. Failures are logged at DEBUG (not WARNING)
    because the base tier (FTS5) is fully functional without it — this is a
    graceful downgrade, not an error (ADR-V6-008 decision 4).
    """
    try:
        import sqlite_vec  # type: ignore[import-not-found]
    except ImportError:
        logger.debug("sqlite-vec not installed; PTG runs FTS5-only (base tier).")
        return False
    try:
        if not getattr(conn, "enable_load_extension", None):
            logger.debug("sqlite connection lacks enable_load_extension; FTS5-only.")
            return False
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return True
    except Exception as exc:  # noqa: BLE001 — extension load is environment-specific
        logger.debug("sqlite-vec load failed (%s); PTG runs FTS5-only.", exc)
        return False


def create_vec_table(conn, table: str = "memos_vec", dim: int = 1024) -> bool:
    """Create a ``vec0`` virtual table for memo embeddings.

    Returns True if created (sqlite-vec active), False if degraded to base
    tier. The dim is validated against every row inserted by the store via
    ``validate_embedding_dim`` so a model swap can't silently corrupt the index.
    """
    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {table} "
        f"USING vec0(embedding float[{int(dim)}])"
    )
    return True


def validate_embedding_dim(blob: bytes, expected_dim: int) -> Optional[str]:
    """Return None if ``blob`` is a valid float32 vector of ``expected_dim``,
    else a short reason string. Used as the C5-adjacent gate before inserting
    into memos_vec — a mismatched-dim insert goes to DLQ, never silently."""
    if blob is None:
        return "embedding is None"
    if len(blob) % 4 != 0:
        return f"byte length {len(blob)} not a multiple of 4 (float32)"
    dim = len(blob) // 4
    if dim != expected_dim:
        return f"dim {dim} != expected {expected_dim}"
    return None


def apply_schema(conn) -> None:
    """Create all 13 tables + indexes + triggers + FTS, then reconcile any
    columns missing on an existing DB. Idempotent."""
    conn.executescript(_SCHEMA_SQL)
    _reconcile_columns(conn)
    _ensure_fts_trigram(conn)
    # Record the schema version once (additive — never overwrite per C2/C6).
    conn.execute(
        "INSERT OR IGNORE INTO ptg_meta(key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )


def _ensure_fts_trigram(conn) -> None:
    """Upgrade a v2 memos_fts (unicode61) to v3 trigram; no-op when already trigram.

    A virtual table's tokenizer can't be ALTERed, so on a v2 DB we drop +
    recreate + rebuild from ``memos``. The sync triggers (created by the DDL
    above and still present) reference ``memos_fts`` by name and keep working
    once it's recreated. Idempotent and safe at init time (single-threaded).
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='memos_fts'"
    ).fetchone()
    if row is None:
        return  # _SCHEMA_SQL will create it fresh with trigram
    if "trigram" in (row[0] or ""):
        return  # already trigram
    conn.execute("DROP TABLE memos_fts")
    conn.execute(
        "CREATE VIRTUAL TABLE memos_fts USING fts5("
        "source_text, corrected_text, content=memos, content_rowid=rowid, "
        "tokenize='trigram')"
    )
    conn.execute(
        "INSERT INTO memos_fts(rowid, source_text, corrected_text) "
        "SELECT rowid, source_text, corrected_text FROM memos"
    )
    logger.info("PTG migrate: memos_fts upgraded unicode61 → trigram (CJK recall).")


def _reconcile_columns(conn) -> None:
    """Additively ALTER any missing columns declared in _RECONCILE_COLUMNS.

    Mirrors holographic's PRAGMA-table_info + ALTER TABLE pattern but
    table-driven. Never drops or renames (C2 nothing-lost)."""
    for table, cols in _RECONCILE_COLUMNS.items():
        try:
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        except Exception as exc:  # noqa: BLE001 — table may not exist yet on first run
            logger.debug("reconcile: %s not introspectable (%s)", table, exc)
            continue
        for col, decl in cols.items():
            if col not in existing:
                logger.info("PTG migrate: ALTER TABLE %s ADD COLUMN %s %s", table, col, decl)
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

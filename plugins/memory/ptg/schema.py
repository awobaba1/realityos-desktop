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
#
# v4 (2026-07-19): the §9#8 / §11.2 irreversible investment — the
# quality_metrics table that was skipped at Phase 1a is now built. Without it,
# every evaluation run overwrote a single v6_eval_report.json and the time-series
# quality history (precision/recall/cost/correction-rate/backtest-acc) was lost,
# making the §11.1 walk-forward backtest impossible and leaving every §8
# Phase-Gate KR without its "唯一证据来源". This is an additive table; existing
# v3 DBs get it via apply_schema's executescript (CREATE TABLE IF NOT EXISTS) —
# no _RECONCILE_COLUMNS entry needed for a brand-new table. Also adds the
# relations terminal-state columns (delta/completeness/consent_tag) the §9#1/#5
# irreversible investment demanded, so Phase 2 Quark/Theory won't force a PTG
# schema migration (relations._RECONCILE_COLUMNS heals existing DBs additively).
#
# v5 (2026-07-19): the §9#4 irreversible investment + §0.6 capture surface —
# the tool_events table. The ptg_capture plugin's post_tool_call hook was
# AUDIT-LOG ONLY (ADR-V6-008 decision 5); the "操作电脑" execution surface it
# guards had no DB sink, so every tool the agent ran on the user's behalf
# vanished instead of becoming a personal-timeline asset (流经即捕获 was only
# half-true — turns yes, tool executions no). tool_events is that sink. The
# §9#4 columns ``extracted_via`` (capture provenance — 'post_tool_call') and
# ``quark_evidence`` (JSON the Phase 2 quark extractor fills) are built NOW so
# Phase 2 derivation won't force a schema migration — the same reason relations
# got delta/completeness at v4. Additive; existing v4 DBs get it via the
# CREATE TABLE IF NOT EXISTS in apply_schema. C2 user-data table (soft-delete +
# version). tool_args/result_summary are size-capped at capture time (PIPL §6
# minimization — a web_fetch body is NOT stored whole).
SCHEMA_VERSION = 8  # v8 (ADR-V6-045): deletion_log audit table; v7 stale_at
                    # invalidation (pure UPDATE, append-only — C2). Additive via
                    # _RECONCILE_COLUMNS so existing v6 DBs heal on reopen.
                    # v6 (ADR-V6-016): atom_kind column on meaning/feeling events
                    # so R8/R9/R12 can be stored additively without touching the
                    # intent_class/state_type CHECK constraints (no table rebuild).

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
    updated_at       TEXT,
    -- ADR-V6-016: marks the real atom type behind a row. R7/R8/R12 all share
    -- meaning_events (intent_class CHECK can't be widened without a table
    -- rebuild), so atom_kind distinguishes them for recall/graph queries.
    atom_kind        TEXT NOT NULL DEFAULT 'R7'
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
    trigger_source  TEXT NOT NULL DEFAULT '{}',
    -- ADR-V6-016: marks the real atom type. R1/R9 share feeling_events
    -- (state_type CHECK can't be widened without a table rebuild), so
    -- atom_kind='R9' distinguishes co-occurrence emotion from self-state.
    atom_kind       TEXT NOT NULL DEFAULT 'R1'
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
    -- §9#1/#5 terminal-state columns (v4): the delta sub-fields container +
    -- completeness + consent_tag the §9 irreversible investment demanded, so
    -- Phase 2 Quark/Theory derivation + the §6 data constitution don't force a
    -- PTG schema migration. delta = JSON of derived deltas (interaction_count
    -- z-score etc., Phase 2 fills); completeness = 0..1 evidence sufficiency;
    -- consent_tag = data-constitution tag (NULL=new/local-only default,
    -- 'migrated'=V5 import per REV-9 H, 'shareable'/'restricted' etc.). Existing
    -- v3 DBs get these additively via _RECONCILE_COLUMNS (NULL — backfill is the
    -- V5 migration's job, not the schema's).
    delta           TEXT,
    completeness    REAL CHECK (completeness IS NULL OR completeness BETWEEN 0 AND 1),
    consent_tag     TEXT,
    version         INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deleted_at      TEXT,
    -- ADR-V6-044 (F4): staleness marker for derived edges (K_Correlation).
    -- NULL = current/active; a timestamp = the edge lost its current backing
    -- (recompute dropped it below the gate). Pure-UPDATE invalidation keeps
    -- value/delta/evidence for history (C2 append-only); the "current view"
    -- filters stale_at IS NULL.
    stale_at        TEXT
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

-- ── quality_metrics (§11.2 + §9#8 — Phase 1a irreversible investment) ─────
-- Time-series quality telemetry: atom precision/recall/f1, LLM cost, user
-- correction rate, backtest accuracy. THIS table is the sole evidence source
-- for every §8 Phase-Gate KR (§11.2 line 829) and the substrate for the §11.1
-- weekly walk-forward backtest. Built at Phase 1a so history accumulates from
-- day one — the §9#8 warning ("avoid retrofitting, which makes historical data
-- impossible to backtest") is exactly what skipping it at v2026.7.19 caused.
-- C2 user-data table (soft-delete + version); value has NO CHECK because
-- llm_cost is CNY (unbounded) while the ratio metrics are 0..1.
CREATE TABLE IF NOT EXISTS quality_metrics (
    id            TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL REFERENCES realityos_users(id),
    metric_date   TEXT NOT NULL,                 -- YYYY-MM-DD (the aggregation day)
    metric_type   TEXT NOT NULL CHECK (metric_type IN
                    ('atom_precision','atom_recall','atom_f1',
                     'llm_cost','correction_rate','backtest_acc')),
    atom_type     TEXT,                          -- R0/R1/R2/R3/R7 or NULL for overall
    value         REAL NOT NULL,
    sample_size   INTEGER NOT NULL DEFAULT 0,
    note          TEXT,
    version       INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deleted_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_qm_date_type ON quality_metrics(metric_date, metric_type, atom_type);
CREATE INDEX IF NOT EXISTS idx_qm_deleted   ON quality_metrics(deleted_at);

-- ── tool_events (§9#4 + §0.6 — tool-execution capture surface) ──────────────
-- The DB sink for the post_tool_call hook (the 操作电脑 capture surface). Until
-- v5 the hook was audit-log only (ADR-V6-008 decision 5); now every tool the
-- agent runs on the user's behalf becomes a searchable personal-timeline asset.
-- tool_args / result_summary are size-capped at capture time — a web_fetch body
-- is NOT stored whole (PIPL §6 minimization). extracted_via + quark_evidence
-- are the §9#4 provenance + derivation hooks (Phase 2 quark extractor fills the
-- latter); built now so Phase 2 won't force a migration.
CREATE TABLE IF NOT EXISTS tool_events (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES realityos_users(id),
    session_id      TEXT,
    tool_name       TEXT NOT NULL,
    tool_args       TEXT NOT NULL DEFAULT '{}',   -- JSON; size-capped (L3, may carry user content)
    result_summary  TEXT,                          -- JSON; truncated result (minimization)
    status          TEXT NOT NULL CHECK (status IN ('ok','error')),
    error_type      TEXT,
    error_msg       TEXT,
    duration_ms     INTEGER,
    extracted_via   TEXT NOT NULL DEFAULT 'post_tool_call',
    quark_evidence  TEXT NOT NULL DEFAULT '[]',    -- JSON array; Phase 2 quark extractor fills
    llm_call_id     TEXT,                          -- C6 linkage if the tool WAS an llm_* call
    captured_at     TEXT NOT NULL,
    version         INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deleted_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_tool_user_time ON tool_events(user_id, captured_at, id);
CREATE INDEX IF NOT EXISTS idx_tool_name      ON tool_events(user_id, tool_name);
CREATE INDEX IF NOT EXISTS idx_tool_deleted   ON tool_events(deleted_at);

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

-- ── deletion_log (append-only WORM; ADR-V6-045; C2 soft-delete audit) ──
-- Every soft-delete (single-row store.soft_delete + sovereignty cascade window)
-- writes one row here ATOMICALLY with the deleted_at update. C2-exempt: NO
-- deleted_at (append-only audit; purge_soft_deleted never touches it — purge
-- iterates C2_USER_TABLES only). Forensic purpose: deleted_at alone records
-- *when* a row retired; this records *who* (actor), *why* (reason), and *what*
-- (snapshot JSON of the row before retirement) — the R12 sovereignty audit
-- substrate and the anti-silent-cascade observability surface (C7).
CREATE TABLE IF NOT EXISTS deletion_log (
    id          TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    user_id     TEXT NOT NULL,
    table_name  TEXT NOT NULL,
    record_id   TEXT NOT NULL,
    actor       TEXT NOT NULL,   -- user | system | cascade | agent
    reason      TEXT NOT NULL DEFAULT '',
    snapshot    TEXT             -- JSON of the row before retirement (nullable)
);
CREATE INDEX IF NOT EXISTS idx_dellog_user_time ON deletion_log(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_dellog_table     ON deletion_log(table_name);

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
    "task_suggestions", "feedback", "insight_aggregation", "quality_metrics",
    "tool_events",
)
APPEND_ONLY_TABLES = ("dlq_messages", "llm_call_logs", "deletion_log")
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
                       "updated_at": "TEXT",
                       # ADR-V6-016: R8/R12 marker column (defaults to R7 for
                       # pre-existing rows).
                       "atom_kind": "TEXT NOT NULL DEFAULT 'R7'"},
    "identity_events": {"sentiment": "TEXT", "interaction_type": "TEXT"},
    "entity_events": {},  # ADR-088 table is new in V6; nothing to reconcile yet.
    "feeling_events": {"emotion_vad": "TEXT",
                       "ser_source": "TEXT NOT NULL DEFAULT 'llm_text'",
                       # trigger_source is in CREATE TABLE but was missing here
                       # — pre-v6 DBs lack the column and would fail the NOT
                       # NULL DEFAULT '{}' insert. Added with R9 (which writes
                       # trigger_source) to close the gap.
                       "trigger_source": "TEXT NOT NULL DEFAULT '{}'",
                       # ADR-V6-016: R9 marker column (defaults to R1).
                       "atom_kind": "TEXT NOT NULL DEFAULT 'R1'"},
    "llm_call_logs": {"cost_cny": "REAL", "schema_valid": "INTEGER",
                      "prompt_template_version": "TEXT NOT NULL DEFAULT 'v1'"},
    # relations v4 terminal-state columns (§9#1/#5) — additive on existing DBs.
    # v7 (ADR-V6-044): stale_at for K_Correlation invalidation.
    "relations": {"delta": "TEXT", "completeness": "REAL",
                  "consent_tag": "TEXT", "stale_at": "TEXT"},
    # quality_metrics v4 columns that legacy V5-era DBs (e.g. ~/.realityos/ptg.db
    # predating the desktop fork) lack: V5 created it with the old `date` column
    # and no user_id / C2 cols. CREATE TABLE IF NOT EXISTS no-ops on the existing
    # table, so these must be ALTERed in here — otherwise insert_quality_metric
    # column-mismatches and the run_eval --ptg-db bridge silently fails (C7
    # swallows the error), the "live quality_metrics 0 行" deadlock (ADR-V6-027).
    # The `date`→`metric_date` rename is backfilled by _backfill_legacy_data.
    "quality_metrics": {
        "user_id": "TEXT",                              # legacy rows lack it; inserts always supply
        "metric_date": "TEXT",                          # v4: renamed from `date`
        "version": "INTEGER NOT NULL DEFAULT 1",        # C2
        "deleted_at": "TEXT",                           # C2
        # created_at is NOT NULL DEFAULT CURRENT_TIMESTAMP in the fresh DDL, but
        # SQLite forbids ADD COLUMN with NOT NULL + a CURRENT_TIMESTAMP default;
        # reconcile it as nullable (legacy rows predate it → NULL is fine; every
        # insert supplies _now_iso() explicitly).
        "created_at": "TEXT",
    },
    # tool_events is new in v5 — created whole by _SCHEMA_SQL (§9#4 capture
    # surface sink). Listed for discoverability; nothing additive to reconcile.
    "tool_events": {},
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
    """Create all PTG tables + indexes + triggers + FTS, then reconcile any
    columns missing on an existing DB. Idempotent.

    Order matters: ``_reconcile_columns`` runs BEFORE ``executescript`` so that
    indexes referencing reconciled columns (``idx_qm_date_type`` on metric_date,
    ``idx_meaning_overdue`` on is_overdue) find the column present — otherwise
    executescript aborts mid-script on a legacy DB (leaving later tables like
    ptg_meta uncreated). Tables absent on a fresh DB are skipped by reconcile
    and created whole by executescript."""
    _reconcile_columns(conn)
    conn.executescript(_SCHEMA_SQL)
    _backfill_legacy_data(conn)
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

    Runs BEFORE ``executescript(_SCHEMA_SQL)`` so indexes that reference
    reconciled columns succeed on legacy DBs. Tables that don't exist yet
    (fresh DB, or not-yet-created) are skipped — executescript creates them
    whole with the full column set.

    Mirrors holographic's PRAGMA-table_info + ALTER TABLE pattern but
    table-driven. Never drops or renames (C2 nothing-lost)."""
    for table, cols in _RECONCILE_COLUMNS.items():
        try:
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        except Exception as exc:  # noqa: BLE001 — table may not exist yet on first run
            logger.debug("reconcile: %s not introspectable (%s)", table, exc)
            continue
        if not existing:
            continue  # table absent — executescript creates it fresh; nothing to ALTER
        for col, decl in cols.items():
            if col not in existing:
                try:
                    logger.info("PTG migrate: ALTER TABLE %s ADD COLUMN %s %s", table, col, decl)
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
                except Exception as exc:  # noqa: BLE001 — col somehow present/unsupported
                    logger.debug("reconcile: %s.%s add skipped (%s)", table, col, exc)


def _backfill_legacy_data(conn) -> None:
    """One-time idempotent data backfills for columns renamed/re-homed across
    schema versions. Runs after _reconcile_columns so the target columns exist.

    Each statement is guarded idempotent (only touches rows where the target is
    still NULL) and C2-safe (never deletes). A rename can't be expressed as ADD
    COLUMN, so this is the sole non-additive heal path — and it only copies
    forward, never destroys the source column (C2 nothing-lost)."""
    # v4 quality_metrics renamed `date` -> `metric_date`. Legacy V5-era DBs
    # (e.g. ~/.realityos/ptg.db predating the desktop fork) carry the old
    # `date`; reconcile ADDs `metric_date` empty for pre-existing rows and this
    # copies surviving rows forward. Skipped on fresh DBs (no `date` column).
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(quality_metrics)")}
    except Exception as exc:  # noqa: BLE001 — table may not exist yet on first run
        logger.debug("backfill: quality_metrics not introspectable (%s)", exc)
        return
    if "metric_date" in cols and "date" in cols:
        conn.execute(
            "UPDATE quality_metrics SET metric_date = date "
            "WHERE metric_date IS NULL AND date IS NOT NULL"
        )

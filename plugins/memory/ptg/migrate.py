"""V5 → V6 founder migration IMPORTER (JSONL → ptg.db).

One-time tooling (not runtime). Reads a V5 export directory of per-table JSONL
files (produced by the V5-side exporter; see ADR-V6-009), converts each row
PG→SQLite via ``converters.convert_row`` + a per-table column map, and bulk-
inserts into the V6 ``ptg.db`` via the shared PTGStore.

Design (ADR-V6-009):
  * **JSONL intermediate** — no Postgres driver shipped in the V6 desktop; the
    exporter (V5 env, asyncpg) writes JSONL, this importer (pure Python) reads
    it. The JSONL dump IS the permanently-backed-up artifact (D3).
  * **Idempotent** — ``INSERT OR IGNORE`` on PK conflict (first-wins). Re-runs
    add only new rows; never overwrites or deletes (C2). To re-import corrected
    data, drop the fresh ptg.db and re-run (the source of truth is V5 + JSONL).
  * **C2-safe** — no DELETE anywhere; append-only logs (dlq_messages,
    llm_call_logs) copied as-is.
  * **FK order** — imported in dependency order so children land after parents
    (SQLite FK enforcement is off during bulk load; order keeps it consistent
    for the day FK is enabled).
  * **Embeddings skipped** — memo_embeddings is a rebuildable derived cache
    (not one of the 13); V6 re-embeds in the extraction phase.
  * **location_log skipped** — D3 (founder PG dump permanently backed up).

Column maps are 1:1 except the ``users`` → ``realityos_users`` table rename and
the ``relations`` V5 names (subject_id/object_id/last_updated) — all of which
the v2 schema now mirrors exactly.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .converters import convert_row
from .store import PTGStore, _now_iso

logger = logging.getLogger(__name__)

# (v5_column, v6_column, converter_name). v5_column is also the JSONL key.
# v6_column matches the v2 schema exactly. Most are 1:1.
ColumnMap = List[Tuple[str, str, str]]

COLUMN_MAPS: Dict[str, ColumnMap] = {
    "users": [
        ("id", "id", "uuid"), ("email", "email", "text"),
        ("password_hash", "password_hash", "text"), ("phone", "phone", "text"),
        ("nickname", "nickname", "text"), ("avatar_url", "avatar_url", "text"),
        ("timezone", "timezone", "text"), ("status", "status", "text"),
        ("is_founder", "is_founder", "bool"), ("version", "version", "int"),
        ("settings", "settings", "json"), ("data_consent", "data_consent", "json"),
        ("last_active_at", "last_active_at", "iso8601"),
        ("created_at", "created_at", "iso8601"), ("updated_at", "updated_at", "iso8601"),
        ("deleted_at", "deleted_at", "iso8601"),
    ],
    "memos": [
        ("id", "id", "uuid"), ("user_id", "user_id", "uuid"),
        ("input_mode", "input_mode", "text"), ("source_text", "source_text", "text"),
        ("corrected_text", "corrected_text", "text"),
        ("audio_clip_id", "audio_clip_id", "uuid"),
        ("timestamp", "timestamp", "iso8601"), ("summary", "summary", "text"),
        ("moderation_status", "moderation_status", "text"),
        ("version", "version", "int"), ("location_context", "location_context", "json"),
        ("created_at", "created_at", "iso8601"), ("deleted_at", "deleted_at", "iso8601"),
    ],
    "identity_events": [
        ("id", "id", "uuid"), ("user_id", "user_id", "uuid"),
        ("memo_id", "memo_id", "uuid"), ("timestamp", "timestamp", "iso8601"),
        ("source_text", "source_text", "text"), ("input_mode", "input_mode", "text"),
        ("confidence_base", "confidence_base", "real"),
        ("person_name", "person_name", "text"), ("mention_context", "mention_context", "text"),
        ("sentiment", "sentiment", "text"), ("interaction_type", "interaction_type", "text"),
        ("relation_confidence", "relation_confidence", "real"),
        ("llm_call_id", "llm_call_id", "uuid"), ("schema_version", "schema_version", "text"),
        ("version", "version", "int"), ("created_at", "created_at", "iso8601"),
        ("deleted_at", "deleted_at", "iso8601"),
    ],
    "meaning_events": [
        ("id", "id", "uuid"), ("user_id", "user_id", "uuid"),
        ("memo_id", "memo_id", "uuid"), ("timestamp", "timestamp", "iso8601"),
        ("source_text", "source_text", "text"), ("input_mode", "input_mode", "text"),
        ("confidence_base", "confidence_base", "real"),
        ("intent_class", "intent_class", "text"),
        ("task_description", "task_description", "text"), ("urgency", "urgency", "text"),
        ("deadline", "deadline", "iso8601"), ("task_status", "task_status", "text"),
        ("topic_tags", "topic_tags", "json"), ("completed_at", "completed_at", "iso8601"),
        ("completion_note", "completion_note", "text"),
        ("is_overdue", "is_overdue", "bool"),
        ("relation_confidence", "relation_confidence", "real"),
        ("llm_call_id", "llm_call_id", "uuid"), ("schema_version", "schema_version", "text"),
        ("version", "version", "int"), ("created_at", "created_at", "iso8601"),
        ("updated_at", "updated_at", "iso8601"), ("deleted_at", "deleted_at", "iso8601"),
    ],
    "entity_events": [
        ("id", "id", "uuid"), ("user_id", "user_id", "uuid"),
        ("memo_id", "memo_id", "uuid"), ("timestamp", "timestamp", "iso8601"),
        ("source_text", "source_text", "text"), ("input_mode", "input_mode", "text"),
        ("confidence_base", "confidence_base", "real"),
        ("entity_name", "entity_name", "text"), ("entity_category", "entity_category", "text"),
        ("mention_context", "mention_context", "text"),
        ("relation_confidence", "relation_confidence", "real"),
        ("llm_call_id", "llm_call_id", "uuid"), ("schema_version", "schema_version", "text"),
        ("version", "version", "int"), ("created_at", "created_at", "iso8601"),
        ("deleted_at", "deleted_at", "iso8601"),
    ],
    "feeling_events": [
        ("id", "id", "uuid"), ("user_id", "user_id", "uuid"),
        ("memo_id", "memo_id", "uuid"), ("timestamp", "timestamp", "iso8601"),
        ("source_text", "source_text", "text"), ("input_mode", "input_mode", "text"),
        ("confidence_base", "confidence_base", "real"),
        ("state_type", "state_type", "text"), ("direction", "direction", "text"),
        ("intensity", "intensity", "text"),
        ("trigger_source", "trigger_source", "json"), ("emotion_vad", "emotion_vad", "json"),
        ("ser_source", "ser_source", "text"),
        ("relation_confidence", "relation_confidence", "real"),
        ("llm_call_id", "llm_call_id", "uuid"), ("schema_version", "schema_version", "text"),
        ("version", "version", "int"), ("created_at", "created_at", "iso8601"),
        ("deleted_at", "deleted_at", "iso8601"),
    ],
    "entities": [
        ("id", "id", "uuid"), ("user_id", "user_id", "uuid"),
        ("entity_name", "entity_name", "text"),
        ("entity_name_normalized", "entity_name_normalized", "text"),
        ("entity_type", "entity_type", "text"), ("properties", "properties", "json"),
        ("mention_count", "mention_count", "int"),
        ("voiceprint_samples", "voiceprint_samples", "json"),
        ("voiceprint_confidence", "voiceprint_confidence", "real"),
        ("version", "version", "int"), ("first_seen_at", "first_seen_at", "iso8601"),
        ("last_seen_at", "last_seen_at", "iso8601"), ("created_at", "created_at", "iso8601"),
        ("updated_at", "updated_at", "iso8601"), ("deleted_at", "deleted_at", "iso8601"),
    ],
    "relations": [
        ("id", "id", "uuid"), ("user_id", "user_id", "uuid"),
        ("subject_id", "subject_id", "uuid"), ("object_id", "object_id", "uuid"),
        ("relation_type", "relation_type", "text"), ("value", "value", "json"),
        ("confidence", "confidence", "real"), ("trend", "trend", "json"),
        ("last_updated", "last_updated", "iso8601"),
        ("evidence_count", "evidence_count", "int"), ("version", "version", "int"),
        ("created_at", "created_at", "iso8601"), ("deleted_at", "deleted_at", "iso8601"),
    ],
    "task_suggestions": [
        ("id", "id", "uuid"), ("user_id", "user_id", "uuid"),
        ("meaning_event_id", "meaning_event_id", "uuid"),
        ("suggestion_type", "suggestion_type", "text"), ("status", "status", "text"),
        ("suggestion_text", "suggestion_text", "text"),
        ("task_description", "task_description", "text"),
        ("days_overdue", "days_overdue", "int"), ("urgency", "urgency", "text"),
        ("notification_sent_at", "notification_sent_at", "iso8601"),
        ("user_responded_at", "user_responded_at", "iso8601"),
        ("dismissal_reason", "dismissal_reason", "text"),
        ("confidence", "confidence", "real"), ("llm_call_id", "llm_call_id", "uuid"),
        ("version", "version", "int"), ("created_at", "created_at", "iso8601"),
        ("updated_at", "updated_at", "iso8601"), ("deleted_at", "deleted_at", "iso8601"),
    ],
    "feedback": [
        ("id", "id", "uuid"), ("user_id", "user_id", "uuid"),
        ("target_type", "target_type", "text"), ("target_id", "target_id", "uuid"),
        ("rating", "rating", "text"), ("comment", "comment", "text"),
        ("version", "version", "int"), ("created_at", "created_at", "iso8601"),
        ("updated_at", "updated_at", "iso8601"), ("deleted_at", "deleted_at", "iso8601"),
    ],
    "insight_aggregation": [
        ("id", "id", "uuid"), ("user_id", "user_id", "uuid"),
        ("aggregation_type", "aggregation_type", "text"), ("period_key", "period_key", "text"),
        ("period_start", "period_start", "iso8601"), ("period_end", "period_end", "iso8601"),
        ("input_data", "input_data", "json"), ("result_data", "result_data", "json"),
        ("confidence", "confidence", "real"), ("data_days", "data_days", "int"),
        ("data_sufficiency", "data_sufficiency", "text"),
        ("generated_by", "generated_by", "text"), ("llm_call_id", "llm_call_id", "uuid"),
        ("schema_version", "schema_version", "text"), ("version", "version", "int"),
        ("created_at", "created_at", "iso8601"), ("expires_at", "expires_at", "iso8601"),
        ("deleted_at", "deleted_at", "iso8601"),
    ],
    "dlq_messages": [
        ("id", "id", "uuid"), ("created_at", "created_at", "iso8601"),
        ("user_id", "user_id", "uuid"), ("source", "source", "text"),
        ("error_type", "error_type", "text"), ("error_msg", "error_msg", "text"),
        ("original_data", "original_data", "json"), ("retry_count", "retry_count", "int"),
        ("resolved", "resolved", "bool"), ("resolved_at", "resolved_at", "iso8601"),
    ],
    "llm_call_logs": [
        ("id", "id", "uuid"), ("user_id", "user_id", "uuid"),
        ("created_at", "created_at", "iso8601"), ("model", "model", "text"),
        ("provider", "provider", "text"),
        ("prompt_template_version", "prompt_template_version", "text"),
        ("prompt_input", "prompt_input", "json"), ("input_tokens", "input_tokens", "int"),
        ("response", "response", "json"), ("output_tokens", "output_tokens", "int"),
        ("latency_ms", "latency_ms", "int"), ("success", "success", "bool"),
        ("schema_valid", "schema_valid", "bool"), ("cost_cny", "cost_cny", "real"),
        ("error_type", "error_type", "text"), ("error_msg", "error_msg", "text"),
    ],
}

# V5 table name → V6 table name (only the users rename; rest are identity).
TABLE_TARGET: Dict[str, str] = {"users": "realityos_users"}

# FK-safe import order: root user → memos → events → graph nodes → graph edges
# → suggestions/feedback/insights → infrastructure logs.
IMPORT_ORDER: List[str] = [
    "users", "memos", "identity_events", "meaning_events", "entity_events",
    "feeling_events", "entities", "relations", "task_suggestions", "feedback",
    "insight_aggregation", "dlq_messages", "llm_call_logs",
]


def _bulk_insert_ignore(store: PTGStore, table: str, rows: List[Dict[str, Any]]) -> int:
    """INSERT OR IGNORE a batch; return the number of rows actually added
    (after-before), so PK-conflict skips are correctly excluded from stats."""
    if not rows:
        return 0
    cols = list(rows[0].keys())
    placeholders = ", ".join("?" for _ in cols)
    sql = (f"INSERT OR IGNORE INTO {table} ({', '.join(cols)}) "
           f"VALUES ({placeholders})")
    payload = [tuple(r[c] for c in cols) for r in rows]
    with store._lock:
        before = store._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        store._conn.executemany(sql, payload)
        after = store._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    return after - before


def import_table(
    store: PTGStore,
    table: str,
    jsonl_path: "str | Path",
    *,
    batch: int = 500,
) -> Dict[str, int]:
    """Import one table's JSONL into ptg.db. Returns {read, written, skipped}.

    Missing file → {read:0, written:0, skipped:0} (not an error; lets
    import_dump skip tables the export didn't produce). Malformed JSON lines
    are counted as errors and logged (C7 — never silently swallowed); the row
    is written to dlq_messages if a user_id is resolvable.
    """
    target = TABLE_TARGET.get(table, table)
    colmap = COLUMN_MAPS[table]
    path = Path(jsonl_path)
    stats = {"read": 0, "written": 0, "skipped": 0, "errors": 0}
    if not path.exists():
        return stats

    buf: List[Dict[str, Any]] = []
    with open(path, encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            stats["read"] += 1
            try:
                v5_row = json.loads(line)
                buf.append(convert_row(v5_row, colmap))
            except Exception as exc:  # noqa: BLE001 — log + DLQ, keep going (C7)
                stats["errors"] += 1
                logger.warning("migrate %s:%d malformed: %s", table, line_no, exc)
                try:
                    store.insert_dlq(
                        user_id="founder", source=f"v5_migrate:{table}",
                        error_type="row_parse", error_msg=str(exc)[:500],
                        original_data={"table": table, "line": line_no, "raw": line[:2000]},
                    )
                except Exception:  # noqa: BLE001
                    pass
                continue
            if len(buf) >= batch:
                stats["written"] += _bulk_insert_ignore(store, target, buf)
                buf.clear()
    if buf:
        stats["written"] += _bulk_insert_ignore(store, target, buf)
    stats["skipped"] = stats["read"] - stats["written"] - stats["errors"]
    return stats


def import_dump(
    store: PTGStore,
    dump_dir: "str | Path",
    *,
    tables: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Import a full V5 JSONL dump directory into ptg.db.

    Iterates ``IMPORT_ORDER`` (or the given subset), reads ``<table>.jsonl``
    from ``dump_dir``, and records a migration-audit row in ptg_meta. Returns
    a report dict: per-table stats + totals + the audit key.
    """
    dump_dir = Path(dump_dir)
    order = tables or IMPORT_ORDER
    report: Dict[str, Any] = {"tables": {}, "totals": {"read": 0, "written": 0,
                              "skipped": 0, "errors": 0}}
    for table in order:
        if table not in COLUMN_MAPS:
            logger.warning("migrate: unknown table %s skipped", table)
            continue
        stats = import_table(store, table, dump_dir / f"{table}.jsonl")
        report["tables"][table] = stats
        for k in report["totals"]:
            report["totals"][k] += stats[k]
        if stats["read"]:
            logger.info("migrate %-20s read=%d written=%d skipped=%d errors=%d",
                        table, stats["read"], stats["written"], stats["skipped"],
                        stats["errors"])

    audit_key = f"v5_migration_{_now_iso()}"
    try:
        with store._lock:
            store._conn.execute(
                "INSERT OR REPLACE INTO ptg_meta(key, value) VALUES (?, ?)",
                (audit_key, json.dumps(report, ensure_ascii=False)),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("migrate: failed to write audit row: %s", exc)
    report["audit_key"] = audit_key
    return report

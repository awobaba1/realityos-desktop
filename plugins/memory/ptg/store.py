"""PTGStore — the RealityOS V6 personal-timeline-graph SQLite store.

Owns ``<hermes_home>/ptg.db``. Mirrors V5's 13 production tables (ADR-V6-008
decision 1). Process-wide shared-connection singleton (decision 3): every
PTGStore opened against the same db_path — the memory provider, the capture
plugin, the main agent, and every subagent — shares ONE sqlite connection and
ONE RLock, refcounted. This is the holographic ``MemoryStore._shared`` pattern
verbatim; it eliminates the multi-writer "database is locked" contention that
hits any process running several memory-provider instances against one file.

Phase 0 capture contract (decision 5):
  * ``sync_turn`` on the provider  →  ``insert_memo`` (the canonical
    "流经即捕获" surface: every user turn becomes a memo).
  * ``prefetch`` on the provider   →  ``search_memos_fts`` (base-tier recall).
  * ``post_tool_call`` / ``pre_gateway_dispatch`` hooks are wired in the
    capture plugin but only LOG in Phase 0; their semantic DB sink (tool-event
    table / outbound capture) is deferred to the extraction phase.

All write methods are C2-compliant (soft-delete + version; never hard DELETE)
except the two append-only logs. Every public method swallows+logs store
errors so a capture failure can NEVER break the agent loop (C7) — the capture
surface is observation-only.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from . import schema as _schema

logger = logging.getLogger(__name__)

# Default embedding dim for memos_vec. V5 uses BAAI/bge-small-zh-v1.5 (512-dim)
# via fastembed (confirmed: danao13 backend/app/core/config.py EMBEDDING_DIM=512,
# memo_embeddings.embedding Vector(512)). V6 keeps 512 so migrated V5 embeddings
# and fresh ones share one index. Validated on every insert via
# schema.validate_embedding_dim.
DEFAULT_EMBEDDING_DIM = 512


def _now_iso() -> str:
    """UTC now as ISO-8601 with +00:00 offset (V5 TIMESTAMPTZ analogue)."""
    return datetime.now(timezone.utc).isoformat()


def _uuid() -> str:
    return str(uuid.uuid4())


def _normalize_entity_name(name: str) -> str:
    """Stable lookup key for an entity name.

    Phase 1a: strip + collapse whitespace + lowercase Latin (CJK unaffected by
    ``.lower()``). Pinyin normalization (V5 ADR-052) is deferred — recorded in
    ADR-V6-011 决策6. Enough to dedupe ``"张三"`` vs ``" 张三 "`` today.
    """
    return " ".join(str(name).strip().split()).lower()


def load_ptg_config() -> dict:
    """Read the ``plugins.ptg`` section from ``$HERMES_HOME/config.yaml``.

    Shared by the memory provider AND the capture plugin so both resolve the
    SAME ``db_path`` and therefore hit the shared-connection singleton
    (ADR-V6-008 decision 2/3). Returns ``{}`` when the file/section is absent.
    """
    try:
        from hermes_constants import get_hermes_home
        from hermes_cli.config import cfg_get
        config_path = get_hermes_home() / "config.yaml"
        if not config_path.exists():
            return {}
        import yaml
        with open(config_path, encoding="utf-8-sig") as f:
            all_config = yaml.safe_load(f) or {}
        return cfg_get(all_config, "plugins", "ptg", default={}) or {}
    except Exception:
        return {}


def resolve_db_path(config: Optional[dict] = None) -> Optional[Path]:
    """Resolve the PTG db path from ``plugins.ptg.db_path``.

    Returns None when unset → ``PTGStore`` then uses its own default
    (``<HERMES_HOME>/ptg.db``). ``$HERMES_HOME`` / ``${HERMES_HOME}`` in a
    configured path are expanded via ``get_hermes_home()`` (honours the env
    override). Both plugins call this with the SAME config, so they open the
    SAME file → ONE shared connection.
    """
    cfg = config or {}
    raw = cfg.get("db_path")
    if not raw:
        return None
    from hermes_constants import get_hermes_home
    home = str(get_hermes_home())
    raw = raw.replace("$HERMES_HOME", home).replace("${HERMES_HOME}", home)
    return Path(raw).expanduser()


class PTGStore:
    """Process-wide shared SQLite store for the PTG (see class docstring)."""

    # --- Process-wide shared connection registry (copied from MemoryStore) --
    _shared: Dict[str, Dict[str, Any]] = {}
    _shared_guard = threading.Lock()

    def __init__(
        self,
        db_path: "str | Path | None" = None,
        *,
        embedding_dim: int = DEFAULT_EMBEDDING_DIM,
    ) -> None:
        if db_path is None:
            from hermes_constants import get_hermes_home
            db_path = str(get_hermes_home() / "ptg.db")
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.embedding_dim = int(embedding_dim)

        # resolve() so symlinked/relative paths to the same file share ONE
        # connection instead of silently reintroducing multi-writer contention.
        try:
            self._key = str(self.db_path.resolve())
        except OSError:
            self._key = str(self.db_path)

        with PTGStore._shared_guard:
            entry = PTGStore._shared.get(self._key)
            if entry is None:
                conn = sqlite3.connect(
                    self._key,
                    check_same_thread=False,
                    timeout=10.0,
                    # Autocommit: a write that raises mid-method can never leave
                    # a dangling transaction (and its write lock) open.
                    isolation_level=None,
                )
                conn.row_factory = sqlite3.Row
                entry = {"conn": conn, "lock": threading.RLock(), "refs": 0, "ready": False,
                         "vec": False}
                PTGStore._shared[self._key] = entry
            entry["refs"] += 1
            self._entry = entry
            self._conn = entry["conn"]
            self._lock = entry["lock"]

        with self._lock:
            if not self._entry["ready"]:
                self._init_db()
                self._entry["ready"] = True

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        """Create tables/indexes/triggers/FTS, reconcile columns, load vec."""
        from hermes_state import apply_wal_with_fallback
        apply_wal_with_fallback(self._conn, db_label="ptg.db (RealityOS V6)")
        _schema.apply_schema(self._conn)
        # Best-effort sqlite-vec; degrade to FTS5-only if unavailable.
        if _schema.load_sqlite_vec(self._conn):
            try:
                _schema.create_vec_table(self._conn, dim=self.embedding_dim)
                self._entry["vec"] = True
            except Exception as exc:  # noqa: BLE001 — vec setup is env-specific
                logger.debug("memos_vec create failed (%s); FTS5-only.", exc)
                self._entry["vec"] = False
        else:
            self._entry["vec"] = False
        if self._entry["vec"]:
            logger.info("PTG store ready (sqlite-vec active, dim=%d).", self.embedding_dim)
        else:
            logger.info("PTG store ready (FTS5-only base tier).")

    @property
    def vec_available(self) -> bool:
        return bool(self._entry.get("vec"))

    # ------------------------------------------------------------------
    # User bootstrap
    # ------------------------------------------------------------------

    def ensure_founder(self, user_id: str, email: str, *, nickname: str = "",
                       timezone: str = "Asia/Shanghai") -> str:
        """Ensure the founder row exists AND is marked is_founder=1. Returns id.

        V6 desktop is single-user; this row is the tenant root every captured
        memo/event references. Called at provider initialize(). Idempotent on
        insert; and if the row already exists (e.g. migrated from V5), it is
        PROMOTED to is_founder=1 — V5's ``is_founder`` column is all-false in
        production (never set), so a faithful migration brings the real founder
        in with is_founder=0 and this call corrects the flag (found via
        real-data validation, ADR-V6-009).
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT is_founder FROM realityos_users WHERE id = ?", (user_id,)
            ).fetchone()
            if row is None:
                self._conn.execute(
                    """INSERT INTO realityos_users
                       (id, email, password_hash, nickname, timezone, is_founder,
                        settings, data_consent, created_at, updated_at)
                       VALUES (?, ?, '', ?, ?, 1, '{}',
                               '{"local_only": true, "shareable": false}', ?, ?)""",
                    (user_id, email, nickname, timezone, _now_iso(), _now_iso()),
                )
            elif not row["is_founder"]:
                self._conn.execute(
                    "UPDATE realityos_users SET is_founder = 1, updated_at = ? "
                    "WHERE id = ?",
                    (_now_iso(), user_id),
                )
            return user_id

    # ------------------------------------------------------------------
    # Capture: memos (the canonical "流经即捕获" surface)
    # ------------------------------------------------------------------

    def insert_memo(
        self,
        *,
        user_id: str,
        source_text: str,
        input_mode: str = "text",
        corrected_text: Optional[str] = None,
        timestamp: Optional[str] = None,
        summary: Optional[str] = None,
        location_context: Optional[dict] = None,
        memo_id: Optional[str] = None,
    ) -> str:
        """Insert a captured turn as a memo. Returns the memo id.

        ``source_text`` is the user utterance / typed text (or a structured
        capture record). Soft-delete + version are set by the schema defaults.
        """
        memo_id = memo_id or _uuid()
        with self._lock:
            self._conn.execute(
                """INSERT INTO memos
                   (id, user_id, input_mode, source_text, corrected_text,
                    timestamp, summary, location_context, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    memo_id, user_id, input_mode, source_text, corrected_text,
                    timestamp or _now_iso(), summary,
                    json.dumps(location_context or {}, ensure_ascii=False),
                    _now_iso(),
                ),
            )
            return memo_id

    def search_memos_fts(
        self,
        query: str,
        *,
        user_id: Optional[str] = None,
        limit: int = 8,
    ) -> List[Dict[str, Any]]:
        """Base-tier keyword recall over memos (CJK-correct since schema v3).

        Returns dicts (id, source_text, summary, timestamp), excluding soft-
        deleted rows. Empty query → [].

        Two tiers, unioned (FTS hits first for ranking, LIKE hits appended):
          * **Ranked tier** — FTS5 trigram MATCH. Gives relevance ordering and
            substring recall for ≥3-char terms (incl. CJK, which the old
            unicode61 tokenizer couldn't match at all — found via real-data
            validation, ADR-V6-009).
          * **Recall safety-net** — a per-token ``LIKE`` OR-join. trigram can't
            match <3-char terms, but the commonest Chinese searches are 2 chars
            (北京/老婆/辞职), so LIKE guarantees they still hit. It also preserves
            multi-token OR semantics (``a b`` matches either word). Personal-
            scale scan; the semantic (vec0) tier does the heavy ranking later.
        """
        query = (query or "").strip()
        if not query:
            return []
        tokens = [t for t in query.split() if t]
        if not tokens:
            return []
        uid_clause = "AND m.user_id = ?" if user_id else ""
        uid_params: List[Any] = [user_id] if user_id else []
        with self._lock:
            # Ranked tier — best-effort; a malformed MATCH (FTS syntax in the
            # query) just yields no ranked rows, the LIKE net still catches all.
            fts_rows: List[Any] = []
            try:
                fts_rows = self._conn.execute(
                    f"""SELECT m.id, m.source_text, m.summary, m.timestamp
                        FROM memos m
                        JOIN memos_fts f ON f.rowid = m.rowid
                        WHERE memos_fts MATCH ? AND m.deleted_at IS NULL
                          {uid_clause}
                        ORDER BY f.rank LIMIT ?""",
                    [query, *uid_params, limit],
                ).fetchall()
            except Exception as exc:  # noqa: BLE001 — MATCH syntax varies by query
                logger.debug("PTG FTS MATCH skipped (%s); LIKE net only.", exc)
            seen = {r["id"] for r in fts_rows}
            # Recall safety-net — per-token LIKE OR-join (CJK + multi-token).
            or_clauses = " OR ".join(["m.source_text LIKE ?"] * len(tokens))
            like_params = [f"%{t}%" for t in tokens]
            extra = self._conn.execute(
                f"""SELECT m.id, m.source_text, m.summary, m.timestamp
                    FROM memos m
                    WHERE ({or_clauses}) AND m.deleted_at IS NULL {uid_clause}
                    ORDER BY m.timestamp DESC LIMIT ?""",
                [*like_params, *uid_params, limit],
            ).fetchall()
            merged = list(fts_rows) + [r for r in extra if r["id"] not in seen]
            return [dict(r) for r in merged][:limit]

    # ------------------------------------------------------------------
    # Capture: append-only logs (C6 / C7) — used by later phases + migration
    # ------------------------------------------------------------------

    def insert_llm_call_log(
        self,
        *,
        user_id: str,
        model: str,
        prompt_input: dict,
        response: Optional[dict] = None,
        provider: Optional[str] = None,
        prompt_template_version: str = "v1",
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
        latency_ms: Optional[int] = None,
        success: bool = True,
        schema_valid: Optional[bool] = None,
        cost_cny: Optional[float] = None,
        error_type: Optional[str] = None,
        error_msg: Optional[str] = None,
        log_id: Optional[str] = None,
    ) -> str:
        """C6 replay substrate — full prompt_input + response JSON."""
        log_id = log_id or _uuid()
        with self._lock:
            self._conn.execute(
                """INSERT INTO llm_call_logs
                   (id, user_id, created_at, model, provider,
                    prompt_template_version, prompt_input, input_tokens,
                    response, output_tokens, latency_ms, success, schema_valid,
                    cost_cny, error_type, error_msg)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    log_id, user_id, _now_iso(), model, provider,
                    prompt_template_version,
                    json.dumps(prompt_input, ensure_ascii=False), input_tokens,
                    json.dumps(response, ensure_ascii=False) if response is not None else None,
                    output_tokens, latency_ms, 1 if success else 0,
                    None if schema_valid is None else (1 if schema_valid else 0),
                    cost_cny, error_type, error_msg,
                ),
            )
            return log_id

    def insert_dlq(
        self,
        *,
        user_id: str,
        source: str,
        error_type: str,
        error_msg: str,
        original_data: dict,
    ) -> str:
        """C7 — every failure produces a DLQ entry, never silently dropped."""
        dlq_id = _uuid()
        with self._lock:
            self._conn.execute(
                """INSERT INTO dlq_messages
                   (id, created_at, user_id, source, error_type, error_msg,
                    original_data)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    dlq_id, _now_iso(), user_id, source, error_type, error_msg,
                    json.dumps(original_data, ensure_ascii=False),
                ),
            )
            return dlq_id

    # ------------------------------------------------------------------
    # Capture: R-atom event tables — used by the extraction phase (not Phase 0)
    # ------------------------------------------------------------------

    def _insert_event(self, table: str, *, user_id: str, source_text: str,
                      confidence_base: float, relation_confidence: float,
                      timestamp: Optional[str] = None, memo_id: Optional[str] = None,
                      input_mode: str = "text", llm_call_id: Optional[str] = None,
                      extra: Optional[dict] = None) -> str:
        """Shared insert for the four event tables (the _EVENT_SPINE columns
        + a per-table ``extra`` dict of additional columns)."""
        event_id = _uuid()
        cols = {
            "id": event_id, "user_id": user_id, "memo_id": memo_id,
            "timestamp": timestamp or _now_iso(), "source_text": source_text,
            "input_mode": input_mode, "confidence_base": confidence_base,
            "relation_confidence": relation_confidence, "llm_call_id": llm_call_id,
            "created_at": _now_iso(),
        }
        cols.update(extra or {})
        col_names = ", ".join(cols.keys())
        placeholders = ", ".join("?" for _ in cols)
        with self._lock:
            self._conn.execute(
                f"INSERT INTO {table} ({col_names}) VALUES ({placeholders})",
                tuple(cols.values()),
            )
        return event_id

    def insert_meaning_event(self, *, user_id: str, source_text: str, intent_class: str,
                             confidence_base: float, relation_confidence: float,
                             **kw) -> str:
        return self._insert_event(
            "meaning_events", user_id=user_id, source_text=source_text,
            confidence_base=confidence_base, relation_confidence=relation_confidence,
            extra={"intent_class": intent_class, **{
                k: kw[k] for k in
                ("task_description", "urgency", "deadline", "task_status",
                 "topic_tags", "completed_at", "completion_note", "is_overdue")
                if k in kw}},
            memo_id=kw.get("memo_id"), timestamp=kw.get("timestamp"),
            input_mode=kw.get("input_mode", "text"), llm_call_id=kw.get("llm_call_id"),
        )

    def insert_entity_event(self, *, user_id: str, source_text: str, entity_name: str,
                            entity_category: str, confidence_base: float,
                            relation_confidence: float, **kw) -> str:
        return self._insert_event(
            "entity_events", user_id=user_id, source_text=source_text,
            confidence_base=confidence_base, relation_confidence=relation_confidence,
            extra={"entity_name": entity_name, "entity_category": entity_category,
                   **{k: kw[k] for k in ("mention_context",) if k in kw}},
            memo_id=kw.get("memo_id"), timestamp=kw.get("timestamp"),
            input_mode=kw.get("input_mode", "text"), llm_call_id=kw.get("llm_call_id"),
        )

    def insert_identity_event(self, *, user_id: str, source_text: str, person_name: str,
                              confidence_base: float, relation_confidence: float, **kw) -> str:
        return self._insert_event(
            "identity_events", user_id=user_id, source_text=source_text,
            confidence_base=confidence_base, relation_confidence=relation_confidence,
            extra={"person_name": person_name,
                   **{k: kw[k] for k in ("mention_context", "sentiment", "interaction_type") if k in kw}},
            memo_id=kw.get("memo_id"), timestamp=kw.get("timestamp"),
            input_mode=kw.get("input_mode", "text"), llm_call_id=kw.get("llm_call_id"),
        )

    def insert_feeling_event(self, *, user_id: str, source_text: str,
                             confidence_base: float, relation_confidence: float, **kw) -> str:
        return self._insert_event(
            "feeling_events", user_id=user_id, source_text=source_text,
            confidence_base=confidence_base, relation_confidence=relation_confidence,
            extra={k: kw[k] for k in
                   ("state_type", "direction", "intensity", "emotion_vad",
                    "ser_source", "trigger_source") if k in kw},
            memo_id=kw.get("memo_id"), timestamp=kw.get("timestamp"),
            input_mode=kw.get("input_mode", "text"), llm_call_id=kw.get("llm_call_id"),
        )

    # ------------------------------------------------------------------
    # Entity/relation graph materialization (ADR-V6-011 决策6)
    # ------------------------------------------------------------------

    def upsert_entity(self, *, user_id: str, entity_name: str, entity_type: str,
                      properties: Optional[dict] = None) -> str:
        """Insert-or-bump a graph node. Idempotent on (user, normalized name).

        On re-mention: ``mention_count += 1``, ``last_seen_at``/``updated_at``
        touched, ``version`` bumped, and ``properties`` shallow-merged (new keys
        added, existing overwritten). Returns the entity id (stable across calls).
        """
        norm = _normalize_entity_name(entity_name)
        now = _now_iso()
        with self._lock:
            row = self._conn.execute(
                "SELECT id, properties FROM entities "
                "WHERE user_id = ? AND entity_name_normalized = ? "
                "AND deleted_at IS NULL",
                (user_id, norm),
            ).fetchone()
            if row is not None:
                eid = row["id"]
                if properties:
                    try:
                        base = json.loads(row["properties"] or "{}")
                    except Exception:  # noqa: BLE001 — never break a re-mention on bad JSON
                        base = {}
                    base.update(properties)
                    self._conn.execute(
                        "UPDATE entities SET mention_count = mention_count + 1, "
                        "last_seen_at = ?, updated_at = ?, version = version + 1, "
                        "properties = ? WHERE id = ?",
                        (now, now, json.dumps(base, ensure_ascii=False), eid),
                    )
                else:
                    self._conn.execute(
                        "UPDATE entities SET mention_count = mention_count + 1, "
                        "last_seen_at = ?, updated_at = ?, version = version + 1 "
                        "WHERE id = ?",
                        (now, now, eid),
                    )
                return eid
            eid = _uuid()
            self._conn.execute(
                "INSERT INTO entities (id, user_id, entity_name, entity_name_normalized, "
                "entity_type, properties, mention_count, first_seen_at, last_seen_at, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)",
                (eid, user_id, str(entity_name).strip(), norm, entity_type,
                 json.dumps(properties or {}, ensure_ascii=False),
                 now, now, now, now),
            )
            return eid

    def upsert_relation(self, *, user_id: str, subject_id: str, object_id: str,
                        relation_type: str, value: Optional[str] = None,
                        confidence: Optional[float] = None) -> str:
        """Insert-or-bump a graph edge. Idempotent on (user, subject, object, type).

        On re-evidence: ``evidence_count += 1``, ``last_updated`` touched,
        ``version`` bumped, confidence kept at the **max** of old/new (a later
        high-confidence mention raises the edge; a low one never dilutes it).
        Returns the relation id.
        """
        now = _now_iso()
        with self._lock:
            row = self._conn.execute(
                "SELECT id, confidence FROM relations "
                "WHERE user_id = ? AND subject_id = ? AND object_id = ? "
                "AND relation_type = ? AND deleted_at IS NULL",
                (user_id, subject_id, object_id, relation_type),
            ).fetchone()
            if row is not None:
                rid = row["id"]
                cur = row["confidence"] if row["confidence"] is not None else 0.0
                new = confidence if confidence is not None else cur
                self._conn.execute(
                    "UPDATE relations SET evidence_count = evidence_count + 1, "
                    "last_updated = ?, version = version + 1, confidence = ? "
                    "WHERE id = ?",
                    (now, max(cur, new), rid),
                )
                return rid
            rid = _uuid()
            self._conn.execute(
                "INSERT INTO relations (id, user_id, subject_id, object_id, "
                "relation_type, value, confidence, last_updated, evidence_count, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                (rid, user_id, subject_id, object_id, relation_type, value,
                 confidence if confidence is not None else 0.5, now, now),
            )
            return rid

    # ------------------------------------------------------------------
    # C2 soft delete — NEVER hard DELETE on user-data tables
    # ------------------------------------------------------------------

    def soft_delete(self, table: str, row_id: str) -> bool:
        """Set deleted_at on a row. Refuses append-only logs and unknown tables."""
        if table not in _schema.ALL_TABLES:
            raise ValueError(f"unknown PTG table: {table}")
        if table in _schema.APPEND_ONLY_TABLES:
            raise ValueError(f"{table} is append-only; cannot soft-delete (C2/C7)")
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE {table} SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL",
                (_now_iso(), row_id),
            )
            return cur.rowcount > 0

    def count_rows(self, table: str, *, include_deleted: bool = False) -> int:
        """Row count for tests / diagnostics. Respects soft-delete by default.

        Append-only logs (dlq_messages, llm_call_logs) have no ``deleted_at`` —
        for them the soft-delete filter is never applied (every row counts)."""
        if table not in _schema.ALL_TABLES:
            raise ValueError(f"unknown PTG table: {table}")
        has_soft_delete = table in _schema.C2_USER_TABLES
        if include_deleted or not has_soft_delete:
            clause = ""
        else:
            clause = "WHERE deleted_at IS NULL"
        with self._lock:
            row = self._conn.execute(f"SELECT COUNT(*) FROM {table} {clause}").fetchone()
            return int(row[0])

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Release this instance's ref on the shared connection. The connection
        closes only when the last PTGStore for this DB closes. Idempotent."""
        if getattr(self, "_entry", None) is None:
            return
        with PTGStore._shared_guard:
            entry = self._entry
            if entry is None:
                return
            entry["refs"] -= 1
            if entry["refs"] <= 0:
                try:
                    entry["conn"].close()
                finally:
                    PTGStore._shared.pop(self._key, None)
            self._entry = None

    def __enter__(self) -> "PTGStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

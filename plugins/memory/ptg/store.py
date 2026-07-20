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
import contextlib
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


def _from_unix_iso(now_ts: float) -> str:
    """Unix seconds → ISO-8601 UTC (ADR-V6-044 mark_k_correlation_stale stamp).
    Matches _now_iso()'s ``+00:00`` shape so stale_at / last_updated align."""
    try:
        return datetime.fromtimestamp(float(now_ts), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return _now_iso()


def _safe_json(s: Any, default: Any) -> Any:
    """Defensive JSON parse for text columns that hold serialized atom payloads
    (R8 completion_note / R9 emotion_vad+trigger_source). Returns ``default`` on
    None / malformed input rather than raising — recall must never break on a
    single bad row (C2: nothing lost, C7: no silent failure → the row still
    surfaces with its coarse fields)."""
    if not s:
        return default
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return default


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

    def correct_memo_source_text(
        self, *, user_id: str, memo_id: str, corrected_text: str,
        actor: str = "user", reason: Optional[str] = None,
        expected_version: Optional[int] = None,
    ) -> Dict[str, Any]:
        """§源文本纠正 — record the user's ASR/typo correction on a memo
        (ADR-V6-047 / A4). Pure-CRUD: it does NOT re-extract (the LLM call lives
        in ``correction.re_extract_memo``, never inside the store's lock).

        C2 (danao13 ADR-056): the original ``source_text`` is NEVER modified —
        only ``corrected_text`` + ``version`` bump + ``updated_at``. Optimistic
        concurrency via ``expected_version``: a mismatch → ``version_conflict``
        (caller surfaces it). No-op (returns ``unchanged``) when the corrected
        text equals the effective text (corrected_text ?? source_text) stripped.
        Never raises (C7) — returns ``{ok, status, memo_id, version, ...}``.
        """
        corrected_text = (corrected_text or "")
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT source_text, corrected_text, version, deleted_at "
                    "FROM memos WHERE id=? AND user_id=?",
                    (memo_id, user_id),
                ).fetchone()
                if row is None:
                    return {"ok": False, "status": "not_found", "memo_id": memo_id}
                if row["deleted_at"] is not None:
                    return {"ok": False, "status": "deleted", "memo_id": memo_id}
                if expected_version is not None and row["version"] != expected_version:
                    return {"ok": False, "status": "version_conflict",
                            "memo_id": memo_id, "version": row["version"]}
                effective = (row["corrected_text"] or row["source_text"] or "").strip()
                if effective == corrected_text.strip():
                    return {"ok": True, "status": "unchanged", "memo_id": memo_id,
                            "version": row["version"]}
                new_version = int(row["version"]) + 1
                # memos has no updated_at (V6 convention: version bump is the
                # mutation marker; the correction event is timestamped via the
                # paired deletion_log rows for retired atoms).
                self._conn.execute(
                    "UPDATE memos SET corrected_text=?, version=? "
                    "WHERE id=? AND user_id=?",
                    (corrected_text, new_version, memo_id, user_id),
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("correct_memo_source_text failed (%s): %s", memo_id, exc)
            return {"ok": False, "status": "error", "memo_id": memo_id,
                    "error": str(exc)}
        logger.info("correct_memo_source_text user=%s memo=%s v%s (%s/%s)",
                    user_id, memo_id, new_version, actor, reason or "user_correction")
        return {"ok": True, "status": "corrected", "memo_id": memo_id,
                "version": new_version, "old_text": effective,
                "new_text": corrected_text}

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
    # Quality telemetry — §11.2 / §9#8 time-series ground (Phase 1a)
    # ------------------------------------------------------------------

    def insert_quality_metric(
        self,
        *,
        user_id: str,
        metric_date: str,
        metric_type: str,
        value: float,
        atom_type: Optional[str] = None,
        sample_size: int = 0,
        note: Optional[str] = None,
    ) -> str:
        """Append one quality_metric row (§11.2). The §8 Phase-Gate KR evidence.

        ``metric_date`` is YYYY-MM-DD. ``metric_type`` is constrained by the
        table CHECK (atom_precision/recall/f1, llm_cost, correction_rate,
        backtest_acc). ``atom_type`` is R0/R1/R2/R3/R7 or None for overall.
        Multiple rows per (date, metric_type, atom_type) are ALLOWED — this is
        an append-only time series (one eval run → several rows), not a unique
        aggregate. Never raises (C7); a metrics-write failure is logged + swallowed
        so it can never break the eval/agent loop.
        """
        metric_id = _uuid()
        try:
            with self._lock:
                self._conn.execute(
                    """INSERT INTO quality_metrics
                       (id, user_id, metric_date, metric_type, atom_type,
                        value, sample_size, note, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (metric_id, user_id, metric_date, metric_type, atom_type,
                     float(value), int(sample_size), note, _now_iso()),
                )
        except Exception as exc:  # noqa: BLE001 — observation surface (C7)
            logger.warning("quality_metric insert failed (%s/%s): %s",
                           metric_type, atom_type, exc)
        return metric_id

    def insert_feedback(
        self, *, user_id: str, target_type: str, target_id: str,
        rating: str, comment: Optional[str] = None,
    ) -> str:
        """Append-or-revive one feedback row (§11.4 founder verdict sink).

        The feedback table carries a PLAIN unique index on
        (user_id, target_type, target_id) (not partial) — ADR-083 F6: re-submitting
        a verdict for the same target must revive the soft-deleted row, not INSERT
        a new one (else UNIQUE violation). So this upserts: an existing row is
        un-deleted + rating/comment updated + version bumped; a fresh (user,target)
        gets a new insert. Never raises (C7). ``rating`` MUST be thumbs_up /
        thumbs_down (table CHECK).

        ADR-V6-028 encodes the §11.4 3-way verdict in ``target_type``
        (calibration_correct / calibration_wrong / calibration_surprise) WITHOUT
        widening the rating CHECK — correct/surprise → thumbs_up, wrong →
        thumbs_down — so no migration is needed. The verdict itself is the
        target_type; the atom row id is target_id; ``comment`` carries the
        pre-adjust confidence snapshot + founder note (the audit trail for the
        paired confidence mutation in ``adjust_atom_confidence``).
        """
        now = _now_iso()
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT id FROM feedback "
                    "WHERE user_id=? AND target_type=? AND target_id=?",
                    (user_id, target_type, target_id),
                ).fetchone()
                if row is not None:
                    self._conn.execute(
                        "UPDATE feedback SET rating=?, comment=?, deleted_at=NULL, "
                        "version=version+1, updated_at=? WHERE id=?",
                        (rating, comment, now, row["id"]),
                    )
                    return row["id"]
                fb_id = _uuid()
                self._conn.execute(
                    "INSERT INTO feedback "
                    "(id, user_id, target_type, target_id, rating, comment, "
                    "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                    (fb_id, user_id, target_type, target_id, rating, comment, now, now),
                )
                return fb_id
        except Exception as exc:  # noqa: BLE001 — observation surface (C7)
            logger.warning("feedback insert failed (%s/%s): %s",
                           target_type, target_id, exc)
            return ""

    # Atom type → event-table row that holds its relation_confidence. The four
    # event tables share _EVENT_SPINE (incl. relation_confidence + deleted_at),
    # so lowering confidence is one UPDATE dispatched by atom type. R8/R9/R12
    # live in meaning/feeling behind atom_kind, but the row PK (atom_id) is
    # unique within its table — UPDATE-by-id needs no atom_kind predicate.
    _ATOM_TYPE_EVENT_TABLE = {
        "R3_Person": "identity_events",
        "R2_Task": "meaning_events",
        "R7_Expression": "meaning_events",
        "R8_Cognition": "meaning_events",
        "R12_Outcome": "meaning_events",
        "R0_Entity": "entity_events",
        "R1_SelfState": "feeling_events",
        "R9_Emotion": "feeling_events",
    }

    def adjust_atom_confidence(
        self, *, user_id: str, atom_type: str, atom_id: str,
        new_confidence: float, reason: Optional[str] = None,
    ) -> int:
        """§11.5 contract — the ONLY sanctioned human-mutates-confidence channel.

        Lowers (or raises) ``relation_confidence`` on the specific event-table row
        backing an atom. The founder's daily calibration verdict ("不准") routes
        here: a wrong atom's effective confidence drops, so future reads
        (recent_atoms / insights / weekly mirror) gate it out — WITHOUT deleting
        the row (C2 nothing-lost; the atom is demoted, not erased).

        ``atom_id`` is the row PK returned by ``recent_atoms`` as ``atom_id``
        (ADR-V6-028). ``new_confidence`` is clamped to [0, 1] (table CHECK).
        Returns the row count updated (0 = atom gone / soft-deleted / unknown
        type — logged, never raised; C7). ``reason`` is recorded in the log +
        the paired feedback row (callers write feedback first); this UPDATE
        mutates only the confidence column (the event tables share no
        ``updated_at`` outside meaning_events, so no timestamp column is touched).
        """
        table = self._ATOM_TYPE_EVENT_TABLE.get(atom_type)
        if table is None:
            logger.warning("adjust_atom_confidence: unknown atom_type %r", atom_type)
            return 0
        clamped = max(0.0, min(1.0, float(new_confidence)))
        try:
            with self._lock:
                cur = self._conn.execute(
                    f"UPDATE {table} SET relation_confidence=? "
                    f"WHERE id=? AND user_id=? AND deleted_at IS NULL",
                    (clamped, atom_id, user_id),
                )
                count = int(cur.rowcount or 0)
            if count == 0:
                logger.warning(
                    "adjust_atom_confidence: %s row %s not found for user %s (%s)",
                    atom_type, atom_id, user_id, reason or "no reason",
                )
            else:
                logger.info(
                    "adjust_atom_confidence: %s %s → %.3f (%s)",
                    atom_type, atom_id, clamped, reason or "founder_calibration",
                )
            return count
        except Exception as exc:  # noqa: BLE001 — observation surface (C7)
            logger.warning("adjust_atom_confidence failed (%s %s): %s",
                           atom_type, atom_id, exc)
            return 0

    # ------------------------------------------------------------------
    # R12 explicit outcome pathway (ADR-V6-046 / A3)
    # ------------------------------------------------------------------

    # outcome → (task_status, is_overdue). Mirrors atomizer.py's R12 write map
    # (atomizer.py:557-559) so the explicit path and the LLM path land the same
    # column shape. 'failed' has no enum value → 'dismissed' (closed-but-not-done);
    # the precise outcome survives in completion_note.
    _OUTCOME_STATUS = {
        "completed": ("completed", 0),
        "failed": ("dismissed", 0),
        "delayed": ("pending", 1),
    }

    def list_open_tasks(self, user_id: str, *, limit: int = 50) -> List[Dict[str, Any]]:
        """Open R2/R12 tasks for the tenant (pending / in_progress), newest first.

        The index surface for ``hermes task done #N`` — N is the 1-based position
        in THIS ordering (timestamp DESC, id), matching danao14's cross-function
        ``_INDEX_SQL`` contract (transitions.py:20-26). Pure SQL, never raises (C7).
        """
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT id, task_description, intent_class, task_status, "
                    "is_overdue, urgency, deadline, timestamp "
                    "FROM meaning_events "
                    "WHERE user_id=? AND atom_kind IN ('R2','R12') "
                    "AND task_status IN ('pending','in_progress') "
                    "AND deleted_at IS NULL "
                    "ORDER BY timestamp DESC, id LIMIT ?",
                    (user_id, limit),
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception:  # noqa: BLE001
            logger.warning("list_open_tasks read failed:", exc_info=True)
            return []

    def mark_task_outcome(
        self, *, user_id: str, ref: str, outcome: str,
        actor: str = "user", resolution_note: Optional[str] = None,
    ) -> Dict[str, Any]:
        """§6.x user-sovereign explicit task-outcome pathway (ADR-V6-046 / A3).

        The R12 explicit通路 the roadmap (ADR-V6-040 A3) calls out — until now the
        only way an R12 atom existed was the LLM choosing to extract one from chat.
        This lets the user (or, via ``actor='agent'``, the agent on the user's
        behalf) explicitly mark a task completed / failed / delayed.

        ``ref`` resolves the target row three ways (first hit wins):
          * ``#N`` / ``N`` (digit) → 1-based index into ``list_open_tasks``;
          * an exact ``meaning_events.id`` (uuid) → that row;
          * a task_description substring → the unique open task containing it
            (0 or >1 matches → ``resolved: false`` with a diagnostic message).

        The row is **promoted in place** to ``atom_kind='R12'`` (if it was an R2)
        rather than duplicated — danao14 inserts a new R12 row because its
        meaning_events has no R2 row to update; V6's atomizer usually already
        wrote the R2, so an UPDATE is correct (C2: version bump, never a second
        row). ``completion_note`` records actor/outcome/resolution_note/changed_at
        as the audit; ``version`` + ``updated_at`` make the mutation observable.
        Never raises (C7) — returns ``{ok, resolved, message, ...}``.
        """
        outcome = (outcome or "").strip().lower()
        if outcome not in self._OUTCOME_STATUS:
            return {"ok": False, "resolved": False,
                    "message": f"未知结果 {outcome!r}（用 completed/failed/delayed）"}
        task_status, is_overdue = self._OUTCOME_STATUS[outcome]

        target = self._resolve_task_ref(user_id, ref)
        if target is None:
            return {"ok": False, "resolved": False,
                    "message": f"没找到待办 {ref!r}（用 `hermes task list` 看编号）"}
        atom_id, task_desc = target

        now = _now_iso()
        note = {"outcome": outcome, "actor": actor,
                "resolution_note": resolution_note or "", "changed_at": now}
        completed_at = now if outcome == "completed" else None
        try:
            with self.transaction():
                cur = self._conn.execute(
                    "UPDATE meaning_events "
                    "SET task_status=?, is_overdue=?, atom_kind='R12', "
                    "completed_at=COALESCE(?, completed_at), "
                    "completion_note=?, version=version+1, updated_at=? "
                    "WHERE id=? AND user_id=? AND deleted_at IS NULL",
                    (task_status, is_overdue, completed_at,
                     json.dumps(note, ensure_ascii=False), now, atom_id, user_id),
                )
                if (cur.rowcount or 0) == 0:
                    return {"ok": False, "resolved": False,
                            "message": f"待办 {ref!r} 已不存在或已删除"}
        except Exception as exc:  # noqa: BLE001
            logger.warning("mark_task_outcome failed (%s %s): %s",
                           user_id, ref, exc)
            return {"ok": False, "resolved": False,
                    "message": "操作没成功，稍后再试。"}
        logger.info("mark_task_outcome user=%s atom=%s → %s (%s)",
                    user_id, atom_id, outcome, actor)
        return {"ok": True, "resolved": True, "atom_id": atom_id,
                "task_ref": task_desc or ref, "outcome": outcome,
                "task_status": task_status, "message": self._outcome_past(outcome, task_desc)}

    def _resolve_task_ref(
        self, user_id: str, ref: str,
    ) -> Optional[tuple]:
        """Return (atom_id, task_description) for ``ref``, or None.

        Resolution order: 1-based index (#N) into list_open_tasks → exact id →
        unique task_description substring. Never raises (C7)."""
        ref = (ref or "").strip().lstrip("#").strip()
        if not ref:
            return None
        open_rows = self.list_open_tasks(user_id)
        # 1) 1-based index
        if ref.isdigit():
            idx = int(ref) - 1
            if 0 <= idx < len(open_rows):
                r = open_rows[idx]
                return r["id"], r.get("task_description") or ""
            return None
        # 2) exact id (uuid) — open OR already-closed (allow re-marking)
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT id, task_description FROM meaning_events "
                    "WHERE id=? AND user_id=? AND deleted_at IS NULL",
                    (ref, user_id),
                ).fetchone()
            if row is not None:
                return row["id"], row["task_description"] or ""
        except Exception:  # noqa: BLE001
            pass
        # 3) unique substring over OPEN tasks (don't auto-resolve closed rows)
        hits = [r for r in open_rows
                if ref.lower() in (r.get("task_description") or "").lower()]
        if len(hits) == 1:
            return hits[0]["id"], hits[0].get("task_description") or ""
        return None

    @staticmethod
    def _outcome_past(outcome: str, task_desc: str) -> str:
        verb = {"completed": "已完成", "failed": "已记为未达成",
                "delayed": "已延期"}[outcome]
        return f"已记下「{task_desc}」{verb}。"

    def upsert_insight(
        self, *,
        user_id: str,
        aggregation_type: str,
        period_key: str,
        period_start: str,
        period_end: str,
        result_data: str,
        input_data: Optional[str] = None,
        confidence: float = 0.0,
        data_days: int = 0,
        data_sufficiency: str = "insufficient",
        generated_by: str = "scheduled",
        llm_call_id: Optional[str] = None,
        schema_version: str = "1.0",
        expires_at: str,
    ) -> str:
        """Upsert one insight_aggregation row (§4.4④ cache, ADR-V6-017).

        Used by the weekly mirror: one row per (user, aggregation_type,
        period_key) — regenerating a week replaces the cached mirror in place
        (ON CONFLICT DO UPDATE), which is correct cache semantics. The ATOMS
        that feed the mirror are immutable in the event tables (C2 preserved
        there); the mirror itself is a derived cache, not a user-data record.

        ``aggregation_type`` ∈ {'weekly_mirror', 'daily_report', ...}.
        ``data_sufficiency`` ∈ {'sufficient','partial','insufficient'} (the
        cold-start gate's verdict — 'insufficient' ⇒ the row holds a guidance
        placeholder, not an LLM mirror). Never raises (C7).
        """
        import uuid as _uuid_mod
        ins_id = str(_uuid_mod.uuid4())
        try:
            with self._lock:
                self._conn.execute(
                    """INSERT INTO insight_aggregation
                       (id, user_id, aggregation_type, period_key, period_start,
                        period_end, input_data, result_data, confidence,
                        data_days, data_sufficiency, generated_by, llm_call_id,
                        schema_version, version, created_at, expires_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(user_id, aggregation_type, period_key) DO UPDATE SET
                         result_data=excluded.result_data,
                         input_data=excluded.input_data,
                         confidence=excluded.confidence,
                         data_days=excluded.data_days,
                         data_sufficiency=excluded.data_sufficiency,
                         generated_by=excluded.generated_by,
                         llm_call_id=excluded.llm_call_id,
                         schema_version=excluded.schema_version,
                         version=insight_aggregation.version + 1,
                         created_at=excluded.created_at,
                         expires_at=excluded.expires_at""",
                    (ins_id, user_id, aggregation_type, period_key, period_start,
                     period_end, input_data, result_data, float(confidence),
                     int(data_days), data_sufficiency, generated_by, llm_call_id,
                     schema_version, 1, _now_iso(), expires_at),
                )
        except Exception as exc:  # noqa: BLE001 — observation surface (C7)
            logger.warning("insight upsert failed (%s/%s): %s",
                           aggregation_type, period_key, exc)
        return ins_id

    def get_insight(self, *, user_id: str, aggregation_type: str,
                    period_key: str) -> Optional[Dict[str, Any]]:
        """Read one cached insight row by (user, type, period_key), or None.

        The scheduler (ADR-V6-019) uses this to decide whether a period's report
        already exists (skip) before spending an LLM call, and the desktop UI
        read API (ADR-V6-020) uses it for cache-first reads. Respects soft-delete.
        Never raises (C7).
        """
        try:
            with self._lock:
                row = self._conn.execute(
                    """SELECT data_sufficiency, llm_call_id, version, created_at,
                              result_data, generated_by, period_start, period_end,
                              confidence, schema_version
                       FROM insight_aggregation
                       WHERE user_id = ? AND aggregation_type = ?
                         AND period_key = ? AND deleted_at IS NULL""",
                    (user_id, aggregation_type, period_key),
                ).fetchone()
        except Exception as exc:  # noqa: BLE001 — observation surface (C7)
            logger.warning("insight get failed (%s/%s): %s",
                           aggregation_type, period_key, exc)
            return None
        if row is None:
            return None
        return {
            "data_sufficiency": row["data_sufficiency"],
            "llm_call_id": row["llm_call_id"],
            "version": row["version"],
            "created_at": row["created_at"],
            "result_data": row["result_data"],
            "generated_by": row["generated_by"],
            "period_start": row["period_start"],
            "period_end": row["period_end"],
            "confidence": row["confidence"],
            # The prompt_version the cached report was generated under (stored in
            # the schema_version column). The scheduler (ADR-V6-026) compares it
            # to the service's current PROMPT_VERSION to detect a stale cache row
            # after a prompt bump and regenerate it — without this, an old-prompt
            # report is served until TTL expires.
            "schema_version": row["schema_version"],
        }

    def founder_user_id(self) -> Optional[str]:
        """Read the persisted founder user_id from ``ptg_meta``, or None.

        This is the stable single-tenant id ``PTGProvider._resolve_user_id``
        writes once (provider.py). Read-only — does NOT create one if absent
        (the provider owns creation during its init). Used by the desktop UI
        read API (ADR-V6-020) and the scheduler (ADR-V6-019) to scope reads to
        the founder tenant. Never raises (C7).
        """
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT value FROM ptg_meta WHERE key = 'founder_user_id'"
                ).fetchone()
        except Exception as exc:  # noqa: BLE001 — observation surface (C7)
            logger.warning("founder_user_id read failed: %s", exc)
            return None
        return row[0] if row is not None else None

    def recent_quality_metrics(
        self,
        *,
        user_id: str,
        metric_type: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Read the quality time-series (newest first) — backtest/dashboard feed.

        Optional ``metric_type`` filter. Respects soft-delete. Never raises (C7).
        """
        try:
            with self._lock:
                if metric_type:
                    rows = self._conn.execute(
                        """SELECT metric_date, metric_type, atom_type, value,
                                  sample_size, note
                           FROM quality_metrics
                           WHERE user_id = ? AND deleted_at IS NULL
                             AND metric_type = ?
                           ORDER BY metric_date DESC, created_at DESC LIMIT ?""",
                        (user_id, metric_type, int(limit)),
                    ).fetchall()
                else:
                    rows = self._conn.execute(
                        """SELECT metric_date, metric_type, atom_type, value,
                                  sample_size, note
                           FROM quality_metrics
                           WHERE user_id = ? AND deleted_at IS NULL
                           ORDER BY metric_date DESC, created_at DESC LIMIT ?""",
                        (user_id, int(limit)),
                    ).fetchall()
        except Exception as exc:  # noqa: BLE001 — read side, never break caller
            logger.debug("recent_quality_metrics failed: %s", exc)
            return []
        return [dict(r) for r in rows] if rows else []

    # ------------------------------------------------------------------
    # Capture: tool-execution surface (§9#4 + §0.6 — post_tool_call sink, v5)
    # ------------------------------------------------------------------

    def insert_tool_event(
        self,
        *,
        user_id: str,
        tool_name: str,
        status: str,
        tool_args: Optional[dict] = None,
        result_summary: Optional[dict] = None,
        session_id: Optional[str] = None,
        duration_ms: int = 0,
        error_type: Optional[str] = None,
        error_msg: Optional[str] = None,
        extracted_via: str = "post_tool_call",
        quark_evidence: Optional[list] = None,
        llm_call_id: Optional[str] = None,
        captured_at: Optional[str] = None,
    ) -> str:
        """Sink one tool-execution capture (§9#4 + §0.6). Never raises (C7).

        The DB sink for the ``post_tool_call`` hook — the 操作电脑 capture surface.
        ``tool_args`` / ``result_summary`` are expected already size-capped by
        the caller (``CaptureEvent.from_hook_kwargs``); the store trusts that
        gate and persists what it's handed. A write failure is logged + swallowed
        so an observer can never break the agent loop. Returns the row id.
        """
        event_id = _uuid()
        try:
            with self._lock:
                self._conn.execute(
                    """INSERT INTO tool_events
                       (id, user_id, session_id, tool_name, tool_args,
                        result_summary, status, error_type, error_msg,
                        duration_ms, extracted_via, quark_evidence, llm_call_id,
                        captured_at, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        event_id, user_id, session_id, tool_name,
                        json.dumps(tool_args or {}, ensure_ascii=False),
                        (json.dumps(result_summary, ensure_ascii=False)
                         if result_summary is not None else None),
                        status, error_type, error_msg, int(duration_ms or 0),
                        extracted_via,
                        json.dumps(quark_evidence or [], ensure_ascii=False),
                        llm_call_id, captured_at or _now_iso(), _now_iso(),
                    ),
                )
        except Exception as exc:  # noqa: BLE001 — observer surface (C7)
            logger.warning("tool_event insert failed (tool=%s): %s", tool_name, exc)
        return event_id

    def recent_tool_events(
        self,
        *,
        user_id: str,
        tool_name: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Read the tool-execution capture surface (newest first). Optional
        ``tool_name`` filter. Respects soft-delete. Never raises (C7)."""
        try:
            with self._lock:
                if tool_name:
                    rows = self._conn.execute(
                        """SELECT id, tool_name, status, tool_args, result_summary,
                                  error_type, duration_ms, extracted_via,
                                  quark_evidence, captured_at
                           FROM tool_events
                           WHERE user_id = ? AND deleted_at IS NULL
                             AND tool_name = ?
                           ORDER BY captured_at DESC LIMIT ?""",
                        (user_id, tool_name, int(limit)),
                    ).fetchall()
                else:
                    rows = self._conn.execute(
                        """SELECT id, tool_name, status, tool_args, result_summary,
                                  error_type, duration_ms, extracted_via,
                                  quark_evidence, captured_at
                           FROM tool_events
                           WHERE user_id = ? AND deleted_at IS NULL
                           ORDER BY captured_at DESC LIMIT ?""",
                        (user_id, int(limit)),
                    ).fetchall()
        except Exception as exc:  # noqa: BLE001 — read side, never break caller
            logger.debug("recent_tool_events failed: %s", exc)
            return []
        return [dict(r) for r in rows] if rows else []

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
                 "topic_tags", "completed_at", "completion_note", "is_overdue",
                 "atom_kind")
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
                    "ser_source", "trigger_source", "atom_kind") if k in kw},
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
                    # aliases accumulate across re-mentions (ADR-V6-013): a person
                    # called 老张 in memo 1 and 张总 in memo 2 should end up with
                    # aliases=[老张, 张总]. Union (dedup, order-preserving) instead of
                    # the shallow-overwrite every other key uses.
                    if "aliases" in properties:
                        merged = list(base.get("aliases") or [])
                        for a in properties["aliases"] or []:
                            if a and a not in merged:
                                merged.append(a)
                        base["aliases"] = merged
                        base.update({k: v for k, v in properties.items()
                                     if k != "aliases"})
                    else:
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

    def ensure_self_entity(self, user_id: str, *, name: str = "我") -> str:
        """Return the user's self entity id, creating the node if absent.

        ADR-V6-044 (F7): K-correlation edges need a real entities(id) FK for
        the subject (self); this finds the existing self-node
        (``properties.is_self``) regardless of its display name, or creates one
        with the default name. Idempotent. Mirrors the Atomizer's private
        ``_ensure_self_entity`` but is public so derived computations (K/quark/
        theory) all anchor on the SAME self node.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, properties FROM entities "
                "WHERE user_id = ? AND deleted_at IS NULL",
                (user_id,),
            ).fetchall()
            for r in rows:
                try:
                    props = json.loads(r["properties"]) if r["properties"] else {}
                except Exception:  # noqa: BLE001
                    props = {}
                if props.get("is_self"):
                    return r["id"]
        # No self node yet — create one (upsert_entity is idempotent on name).
        return self.upsert_entity(
            user_id=user_id, entity_name=name, entity_type="person",
            properties={"is_self": True})

    def upsert_relation(self, *, user_id: str, subject_id: str, object_id: str,
                        relation_type: str, value: Optional[str] = None,
                        confidence: Optional[float] = None,
                        delta: Optional[dict] = None) -> str:
        """Insert-or-bump a graph edge. Idempotent on (user, subject, object, type).

        On re-evidence: ``evidence_count += 1``, ``last_updated`` touched,
        ``version`` bumped, confidence kept at the **max** of old/new (a later
        high-confidence mention raises the edge; a low one never dilutes it),
        ``stale_at`` cleared (a re-evidenced edge is current again — ADR-V6-044
        K-correlation revival). ``delta`` (ADR-V6-044) — when supplied —
        overwrites the relations.delta JSON with the latest derived snapshot
        (history preserved in delta.evidence_event_ids + llm_call_logs). Returns
        the relation id.
        """
        now = _now_iso()
        delta_json = json.dumps(delta, ensure_ascii=False) if delta is not None else None
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
                # Build the SET clause; delta is optional (only written when
                # supplied — preserves a prior delta if the caller re-evidences
                # without one).
                sets = ("evidence_count = evidence_count + 1, "
                        "last_updated = ?, version = version + 1, "
                        "confidence = ?, stale_at = NULL")
                params: list = [now, max(cur, new)]
                if delta_json is not None:
                    sets += ", delta = ?"
                    params.append(delta_json)
                params.append(rid)
                self._conn.execute(
                    f"UPDATE relations SET {sets} WHERE id = ?", params)
                return rid
            rid = _uuid()
            cols = ("id, user_id, subject_id, object_id, relation_type, value, "
                    "confidence, last_updated, evidence_count, created_at")
            vals: list = [rid, user_id, subject_id, object_id, relation_type, value,
                          confidence if confidence is not None else 0.5, now, 1, now]
            if delta_json is not None:
                cols += ", delta"
                vals.append(delta_json)
            placeholders = ", ".join("?" for _ in vals)
            self._conn.execute(
                f"INSERT INTO relations ({cols}) VALUES ({placeholders})", vals)
            return rid

    @contextlib.contextmanager
    def transaction(self):
        """Atomic multi-statement block (ADR-V6-044 / F4).

        The shared connection is ``isolation_level=None`` (autocommit), so each
        ``execute`` normally commits on its own. K-correlation recompute must
        land "recompute + revive + invalidate" atomically — a mid-sequence
        failure would otherwise leave orphan edges (the pre-transaction bug
        danao14 fixed). Acquires the shared ``_lock`` for the whole block,
        issues ``BEGIN``, and ``COMMIT``s on clean exit / ``ROLLBACK``s + re-raises
        on any exception. Re-entrant by the same thread is NOT supported (a
        nested BEGIN would error); callers keep transaction() at the outermost
        scope of a unit of work.
        """
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield
            except Exception:
                try:
                    self._conn.execute("ROLLBACK")
                except Exception:  # noqa: BLE001 — rollback best-effort
                    pass
                raise
            else:
                self._conn.execute("COMMIT")

    def mark_k_correlation_stale(self, *, user_id: str, subject_id: str,
                                 keep_object_ids: set, now_ts: float) -> int:
        """Mark K_Correlation edges NOT in ``keep_object_ids`` as stale (ADR-V6-044).

        Pure UPDATE (C2 append-only — value/delta/evidence preserved for
        history); only ``stale_at`` is set, dropping the edge from the "current
        view" (which filters ``stale_at IS NULL``). ``now_ts`` (unix seconds)
        is the recompute anchor stamped into stale_at. Returns the count
        invalidated. Must run inside a ``transaction()`` (the caller groups it
        with the recompute so revive+invalidate land atomically).
        """
        ts = _from_unix_iso(now_ts)
        with self._lock:
            if keep_object_ids:
                placeholders = ", ".join("?" for _ in keep_object_ids)
                cur = self._conn.execute(
                    f"UPDATE relations SET stale_at = ? "
                    f"WHERE user_id = ? AND subject_id = ? "
                    f"AND relation_type = 'K_Correlation' AND deleted_at IS NULL "
                    f"AND stale_at IS NULL AND object_id NOT IN ({placeholders})",
                    (ts, user_id, subject_id, *keep_object_ids),
                )
            else:
                cur = self._conn.execute(
                    "UPDATE relations SET stale_at = ? "
                    "WHERE user_id = ? AND subject_id = ? "
                    "AND relation_type = 'K_Correlation' AND deleted_at IS NULL "
                    "AND stale_at IS NULL",
                    (ts, user_id, subject_id),
                )
            return cur.rowcount or 0

    # ------------------------------------------------------------------
    # Read / recall — the structured-data recall surface (ADR-V6-012)
    # ------------------------------------------------------------------
    # The Atomizer writes R-atoms into the four event tables + the entity/
    # relation graph. Until these read APIs existed, that structured data was
    # WRITE-ONLY — the user could never recall "what did I say about 张三" from
    # the atoms, only from raw memo text (search_memos_fts). These methods are
    # the read half of the brain: they power (a) the eval harness, which must
    # measure what ACTUALLY landed post C5-gate (not raw LLM output), and
    # (b) the prefetch renderer (§4.3A), which folds the graph into context.

    def recent_atoms(self, *, user_id: str, memo_id: Optional[str] = None,
                     limit: int = 500,
                     since: Optional[str] = None, until: Optional[str] = None,
                     ) -> List[Dict[str, Any]]:
        """Reconstruct R-atoms from the four event tables — what actually landed.

        Returns atom-dicts shaped for ``match_atom`` (eval) and for rendering.
        Phase 1b (ADR-V6-016) reconstructs all eight atoms: R3/R2/R7/R1/R0
        (Phase 1a) plus R8/R9/R12. meaning_events dispatch on the ``atom_kind``
        column (R2/R7/R8/R12); feeling_events dispatch on atom_kind (R1/R9).
        ``confidence`` prefers ``relation_confidence`` (the per-atom gate value)
        and falls back to ``confidence_base``. Ordered timestamp DESC. Respects
        soft-delete.

        ``memo_id`` narrows to one memo (the eval reconstructs per-sample).
        ``since``/``until`` (ISO-8601 strings) scope to a half-open time window
        ``[since, until)`` on each event table's ``timestamp`` — used by the
        weekly-mirror aggregator (ADR-V6-017) to read one week of atoms.
        """
        atoms: List[Dict[str, Any]] = []
        memo_clause = "AND memo_id = ?" if memo_id else ""
        memo_params: List[Any] = [memo_id] if memo_id else []
        window_clause = ""
        window_params: List[Any] = []
        if since is not None:
            window_clause += " AND timestamp >= ?"
            window_params.append(since)
        if until is not None:
            window_clause += " AND timestamp < ?"
            window_params.append(until)
        with self._lock:
            for r in self._conn.execute(
                f"""SELECT id, person_name, mention_context, sentiment, interaction_type,
                           confidence_base, relation_confidence, timestamp
                    FROM identity_events
                    WHERE user_id = ? AND deleted_at IS NULL {memo_clause}{window_clause}
                    ORDER BY timestamp DESC LIMIT ?""",
                [user_id, *memo_params, *window_params, limit],
            ).fetchall():
                atoms.append({
                    "atom_id": r["id"],
                    "type": "R3_Person", "person_name": r["person_name"],
                    "mention_context": r["mention_context"], "sentiment": r["sentiment"],
                    "interaction_type": r["interaction_type"],
                    "confidence": r["relation_confidence"]
                    if r["relation_confidence"] is not None else r["confidence_base"],
                    "_ts": r["timestamp"],
                })
            for r in self._conn.execute(
                f"""SELECT id, intent_class, task_description, urgency, deadline,
                           topic_tags, completion_note, atom_kind,
                           confidence_base, relation_confidence, timestamp
                    FROM meaning_events
                    WHERE user_id = ? AND deleted_at IS NULL {memo_clause}{window_clause}
                    ORDER BY timestamp DESC LIMIT ?""",
                [user_id, *memo_params, *window_params, limit],
            ).fetchall():
                conf = (r["relation_confidence"]
                        if r["relation_confidence"] is not None else r["confidence_base"])
                kind = r["atom_kind"] or "R7"
                if kind == "R8":
                    cn = _safe_json(r["completion_note"], {})
                    atoms.append({
                        "atom_id": r["id"],
                        "type": "R8_Cognition", "topic": r["task_description"],
                        "knowledge_tags": _safe_json(r["topic_tags"], []),
                        "engagement": cn.get("engagement", "medium"),
                        "is_question": bool(cn.get("is_question", False)),
                        "confidence": conf, "_ts": r["timestamp"],
                    })
                elif kind == "R12":
                    cn = _safe_json(r["completion_note"], {})
                    atoms.append({
                        "atom_id": r["id"],
                        "type": "R12_Outcome", "task_ref": r["task_description"],
                        "outcome": cn.get("outcome", "completed"),
                        "resolution_note": cn.get("resolution_note"),
                        "confidence": conf, "_ts": r["timestamp"],
                    })
                elif kind == "R2":
                    # R2_Task. Dispatch is purely on atom_kind (the source of
                    # truth — the Atomizer writes atom_kind='R2' for every task).
                    # intent_class is an R7 sub-classification only; a row that
                    # happens to carry Need_To_Do + atom_kind='R7' is an R7
                    # expression of intent, correctly reconstructed as R7 below.
                    atoms.append({
                        "atom_id": r["id"],
                        "type": "R2_Task", "task_description": r["task_description"],
                        "urgency": r["urgency"], "deadline": r["deadline"],
                        "confidence": conf, "_ts": r["timestamp"],
                    })
                else:
                    atoms.append({
                        "atom_id": r["id"],
                        "type": "R7_Expression", "intent_class": r["intent_class"],
                        "content_summary": r["task_description"],
                        "confidence": conf, "_ts": r["timestamp"],
                    })
            for r in self._conn.execute(
                f"""SELECT id, state_type, direction, intensity, emotion_vad,
                           trigger_source, atom_kind, confidence_base,
                           relation_confidence, timestamp
                    FROM feeling_events
                    WHERE user_id = ? AND deleted_at IS NULL {memo_clause}{window_clause}
                    ORDER BY timestamp DESC LIMIT ?""",
                [user_id, *memo_params, *window_params, limit],
            ).fetchall():
                conf = (r["relation_confidence"]
                        if r["relation_confidence"] is not None else r["confidence_base"])
                if (r["atom_kind"] or "R1") == "R9":
                    # R9_Emotion — the R9-specific fields were serialized into
                    # emotion_vad (label/valence/arousal) + trigger_source at write
                    # time; the CHECK-bound state_type/direction/intensity are the
                    # coarse projection (state_type='mood', direction←valence).
                    vad = _safe_json(r["emotion_vad"], {})
                    trg = _safe_json(r["trigger_source"], {})
                    atoms.append({
                        "atom_id": r["id"],
                        "type": "R9_Emotion",
                        "emotion_label": vad.get("label", "unknown"),
                        "valence": vad.get("valence", "neutral"),
                        "arousal": vad.get("arousal", "low"),
                        "trigger": trg.get("trigger"),
                        "intensity": r["intensity"],
                        "confidence": conf, "_ts": r["timestamp"],
                    })
                else:
                    atoms.append({
                        "atom_id": r["id"],
                        "type": "R1_SelfState", "state_type": r["state_type"],
                        "direction": r["direction"], "intensity": r["intensity"],
                        "confidence": conf, "_ts": r["timestamp"],
                    })
            for r in self._conn.execute(
                f"""SELECT id, entity_name, entity_category, mention_context,
                           confidence_base, relation_confidence, timestamp
                    FROM entity_events
                    WHERE user_id = ? AND deleted_at IS NULL {memo_clause}{window_clause}
                    ORDER BY timestamp DESC LIMIT ?""",
                [user_id, *memo_params, *window_params, limit],
            ).fetchall():
                atoms.append({
                    "atom_id": r["id"],
                    "type": "R0_Entity", "entity_name": r["entity_name"],
                    "entity_category": r["entity_category"],
                    "mention_context": r["mention_context"],
                    "confidence": (r["relation_confidence"]
                                   if r["relation_confidence"] is not None else r["confidence_base"]),
                    "_ts": r["timestamp"],
                })
        atoms.sort(key=lambda a: a.get("_ts") or "", reverse=True)
        return atoms[:limit]

    def memo_count(self, user_id: str, *, include_deleted: bool = False) -> int:
        """Count a tenant's captured memos (all-time, not windowed).

        The weekly-mirror cold-start gate (ADR-V6-017 §0.5③) reads this: a
        founder with < 15 memos gets a guidance placeholder, not a mirror that
        would say "你这周提了 0 次家人" and trigger an uninstall.
        """
        clause = "" if include_deleted else " AND deleted_at IS NULL"
        with self._lock:
            row = self._conn.execute(
                f"SELECT COUNT(*) FROM memos WHERE user_id = ?{clause}",
                [user_id],
            ).fetchone()
        return int(row[0]) if row else 0

    def user_created_at(self, user_id: str) -> Optional[str]:
        """The tenant's registration timestamp (realityos_users.created_at).

        The weekly-mirror cold-start gate reads this: registration < 14 days →
        placeholder (ADR-V6-017 §0.5③). None if the user row is absent.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT created_at FROM realityos_users WHERE id = ?",
                [user_id],
            ).fetchone()
        return row[0] if row is not None else None

    def search_entities(self, user_id: str, query: str, *,
                        limit: int = 10) -> List[Dict[str, Any]]:
        """Find graph nodes whose name matches any query token (CJK-correct LIKE).

        Mirrors search_memos_fts's per-token OR-join so 2-char Chinese names
        (张三/北京) hit. Returns dicts (id, entity_name, entity_type,
        mention_count). Ordered by mention_count DESC (most-mentioned first).
        Respects soft-delete.
        """
        query = (query or "").strip()
        if not query:
            return []
        tokens = [t for t in query.split() if t]
        if not tokens:
            return []
        or_clauses = " OR ".join(["entity_name LIKE ?"] * len(tokens))
        like_params = [f"%{t}%" for t in tokens]
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT id, entity_name, entity_type, mention_count
                    FROM entities
                    WHERE user_id = ? AND deleted_at IS NULL
                      AND ({or_clauses})
                    ORDER BY mention_count DESC, last_seen_at DESC LIMIT ?""",
                [user_id, *like_params, limit],
            ).fetchall()
        return [dict(r) for r in rows]

    def list_top_entities(self, user_id: str, *, limit: int = 100) -> List[Dict[str, Any]]:
        """Top entities by mention_count — the LLM entity-vocabulary source
        (ADR-V6-013, porting V5 ADR-049). Returns dicts (entity_name,
        entity_type, mention_count, aliases). Excludes soft-deleted nodes and
        the founder self-node (``properties.is_self``). Ordered mention_count
        DESC so the most-mentioned (most likely to recur / be ASR-garbled) surface
        first. Fetches a small over-fetch then filters self in Python (cheap, top-N).
        """
        with self._lock:
            rows = self._conn.execute(
                """SELECT entity_name, entity_type, mention_count, properties
                   FROM entities
                   WHERE user_id = ? AND deleted_at IS NULL
                   ORDER BY mention_count DESC, last_seen_at DESC
                   LIMIT ?""",
                [user_id, limit + 10],
            ).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            try:
                props = json.loads(r["properties"] or "{}")
            except (ValueError, TypeError):
                props = {}
            if props.get("is_self"):  # skip the implicit founder "我" node
                continue
            out.append({
                "entity_name": r["entity_name"],
                "entity_type": r["entity_type"],
                "mention_count": r["mention_count"],
                "aliases": list(props.get("aliases") or []),
            })
            if len(out) >= limit:
                break
        return out

    def relations_for_user(self, user_id: str, *, entity_id: Optional[str] = None,
                           limit: int = 50) -> List[Dict[str, Any]]:
        """Graph edges for a user, optionally narrowed to one entity's neighbourhood.

        Joins entity names so the renderer can format "subject → object" without
        a second round-trip. Returns dicts (relation_type, value, confidence,
        evidence_count, subject_name, subject_type, object_name, object_type).
        Ordered confidence DESC, evidence_count DESC. Respects soft-delete.
        """
        ent_clause = "AND (r.subject_id = ? OR r.object_id = ?)" if entity_id else ""
        ent_params: List[Any] = [entity_id, entity_id] if entity_id else []
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT r.subject_id, r.object_id, r.relation_type, r.value,
                           r.confidence, r.evidence_count,
                           s.entity_name AS subject_name, s.entity_type AS subject_type,
                           o.entity_name AS object_name, o.entity_type AS object_type
                    FROM relations r
                    JOIN entities s ON s.id = r.subject_id
                    JOIN entities o ON o.id = r.object_id
                    WHERE r.user_id = ? AND r.deleted_at IS NULL {ent_clause}
                    ORDER BY r.confidence DESC, r.evidence_count DESC LIMIT ?""",
                [user_id, *ent_params, limit],
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # M-domain people roster + profile (ADR-V6-048 / A5) — pure SQL, no LLM
    # ------------------------------------------------------------------

    def list_people(self, user_id: str, *, limit: int = 50) -> List[Dict[str, Any]]:
        """Person entities (``entity_type='person'``), self-node excluded.

        The M-domain roster for ``hermes people``. Ordered by ``mention_count``
        DESC then ``last_seen_at`` DESC so the most-relevant people surface
        first. Pure SQL — no LLM, no inference; the honest read-only view of
        who the founder has talked about and how often. Respects soft-delete.
        """
        with self._lock:
            rows = self._conn.execute(
                """SELECT id, entity_name, mention_count, first_seen_at, last_seen_at,
                          properties
                   FROM entities
                   WHERE user_id = ? AND deleted_at IS NULL AND entity_type = 'person'
                   ORDER BY mention_count DESC, last_seen_at DESC LIMIT ?""",
                [user_id, limit + 5],
            ).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            try:
                props = json.loads(r["properties"] or "{}")
            except (ValueError, TypeError):
                props = {}
            if props.get("is_self"):  # skip the implicit founder "我" node
                continue
            out.append({
                "entity_id": r["id"],
                "entity_name": r["entity_name"],
                "mention_count": r["mention_count"],
                "first_seen_at": r["first_seen_at"],
                "last_seen_at": r["last_seen_at"],
                "aliases": list(props.get("aliases") or []),
            })
            if len(out) >= limit:
                break
        return out

    def person_profile(
        self, user_id: str, entity_id: str, *, recent_limit: int = 10,
    ) -> Dict[str, Any]:
        """Full M-domain aggregation for one person (ADR-V6-048 / A5).

        Pure SQL — no LLM, no synthesis. Gathers five read-only facets so the
        founder can review ``who this person is to me`` from raw evidence:

        * ``header`` — name, mention_count, first/last seen, aliases, entity_id.
        * ``interaction_breakdown`` — identity_events counts by
          (interaction_type, sentiment) + ``total`` interactions.
        * ``recent_contexts`` — last ``recent_limit`` mention_context strings
          with timestamp + interaction_type + sentiment.
        * ``relations`` — graph neighbourhood (reuses ``relations_for_user``).
        * ``emotions`` — R9 feeling_events whose ``trigger_source.entity``
          resolves to this person: ``count`` + recent ``triggers``.

        Identity/feeling events are matched to the person by name (canonical
        + aliases, ``LOWER(TRIM(...))``) because those tables key on raw text,
        not entity_id. Returns ``{found: False, reason}`` on missing /
        soft-deleted / non-person. Never raises (C7).
        """
        # 1. header (also enforces entity_type='person').
        with self._lock:
            row = self._conn.execute(
                """SELECT id, entity_name, mention_count, first_seen_at, last_seen_at,
                          properties, entity_type
                   FROM entities
                   WHERE id = ? AND user_id = ? AND deleted_at IS NULL""",
                [entity_id, user_id],
            ).fetchone()
        if not row:
            return {"found": False, "entity_id": entity_id, "reason": "not_found"}
        if row["entity_type"] != "person":
            return {"found": False, "entity_id": entity_id,
                    "reason": f"not_a_person:{row['entity_type']}"}
        try:
            props = json.loads(row["properties"] or "{}")
        except (ValueError, TypeError):
            props = {}
        aliases = [str(a).strip() for a in (props.get("aliases") or []) if str(a).strip()]
        # Name variants for raw-text matching (identity/feeling events).
        variants = sorted({v for v in [row["entity_name"], *aliases] if v})

        profile: Dict[str, Any] = {
            "found": True,
            "entity_id": row["id"],
            "entity_name": row["entity_name"],
            "mention_count": row["mention_count"],
            "first_seen_at": row["first_seen_at"],
            "last_seen_at": row["last_seen_at"],
            "aliases": aliases,
        }

        # 2 + 3. interactions + recent contexts (identity_events by name match).
        like_ph = ", ".join(["?"] * len(variants)) if variants else "NULL"
        norm_variants = [v.strip().lower() for v in variants]
        with self._lock:
            if norm_variants:
                brk = self._conn.execute(
                    f"""SELECT interaction_type, sentiment, COUNT(*) AS n
                        FROM identity_events
                        WHERE user_id = ? AND deleted_at IS NULL
                          AND LOWER(TRIM(person_name)) IN ({like_ph})
                        GROUP BY interaction_type, sentiment""",
                    [user_id, *norm_variants],
                ).fetchall()
                ctx_rows = self._conn.execute(
                    f"""SELECT mention_context, timestamp, interaction_type, sentiment
                        FROM identity_events
                        WHERE user_id = ? AND deleted_at IS NULL
                          AND LOWER(TRIM(person_name)) IN ({like_ph})
                        ORDER BY timestamp DESC LIMIT ?""",
                    [user_id, *norm_variants, recent_limit],
                ).fetchall()
            else:
                brk, ctx_rows = [], []
        breakdown: Dict[str, Any] = {"by_type": {}, "by_sentiment": {}, "total": 0}
        for b in brk:
            itype = b["interaction_type"] or "unknown"
            sent = b["sentiment"] or "unknown"
            breakdown["by_type"][itype] = breakdown["by_type"].get(itype, 0) + b["n"]
            breakdown["by_sentiment"][sent] = breakdown["by_sentiment"].get(sent, 0) + b["n"]
            breakdown["total"] += b["n"]
        profile["interaction_breakdown"] = breakdown
        profile["recent_contexts"] = [
            {
                "context": c["mention_context"],
                "timestamp": c["timestamp"],
                "interaction_type": c["interaction_type"],
                "sentiment": c["sentiment"],
            }
            for c in ctx_rows
        ]

        # 4. relations (reuse the graph neighbourhood query).
        profile["relations"] = self.relations_for_user(
            user_id, entity_id=entity_id, limit=20)

        # 5. emotions — R9 atoms whose trigger_source.entity resolves here.
        # Fetch recent R9 rows for the user and filter by parsed entity (the
        # trigger_source JSON is small; bounded by the LIMIT window).
        triggers: List[Dict[str, Any]] = []
        with self._lock:
            r9_rows = self._conn.execute(
                """SELECT trigger_source, timestamp, state_type, intensity
                   FROM feeling_events
                   WHERE user_id = ? AND deleted_at IS NULL AND atom_kind = 'R9'
                   ORDER BY timestamp DESC LIMIT 200""",
                [user_id],
            ).fetchall()
        norm_set = {v.strip().lower() for v in variants}
        for r9 in r9_rows:
            try:
                ts = json.loads(r9["trigger_source"] or "{}")
            except (ValueError, TypeError):
                continue
            ent = str(ts.get("entity") or "").strip().lower()
            if ent and ent in norm_set:
                triggers.append({
                    "timestamp": r9["timestamp"],
                    "state_type": r9["state_type"],
                    "intensity": r9["intensity"],
                    "trigger": ts.get("trigger") or ts.get("situation") or "",
                })
        profile["emotions"] = {"count": len(triggers), "triggers": triggers[:recent_limit]}
        return profile

    # ------------------------------------------------------------------
    # C2 soft delete — NEVER hard DELETE on user-data tables
    # ------------------------------------------------------------------

    def soft_delete(
        self, table: str, row_id: str, *,
        actor: str = "system", reason: str = "", user_id: Optional[str] = None,
    ) -> bool:
        """Set ``deleted_at`` on a row AND write a ``deletion_log`` audit entry
        in one transaction (ADR-V6-045). Refuses append-only logs + unknown
        tables. ``actor`` ∈ {user, system, cascade, agent}; ``reason`` is free
        text; ``user_id`` scopes the audit row (defaults to the row's user_id
        when discoverable, else ''). The row's pre-deletion state is snapshotted
        into ``deletion_log.snapshot`` for forensic reconstruction.

        Atomicity is the point: if the audit INSERT fails, the deleted_at update
        rolls back too — a soft-delete without its audit row is the exact silent
        observability gap deletion_log exists to close (C7). Returns whether a
        row was newly retired (False if already soft-deleted or absent).
        """
        if table not in _schema.ALL_TABLES:
            raise ValueError(f"unknown PTG table: {table}")
        if table in _schema.APPEND_ONLY_TABLES:
            raise ValueError(f"{table} is append-only; cannot soft-delete (C2/C7)")
        uid_col = "id" if table == "realityos_users" else "user_id"
        with self.transaction():
            snap_row = self._conn.execute(
                f"SELECT * FROM {table} WHERE id = ? AND deleted_at IS NULL",
                (row_id,),
            ).fetchone()
            if snap_row is None:
                return False  # already retired or absent — nothing to audit
            snapshot = dict(snap_row)
            uid = user_id or snapshot.get(uid_col) or ""
            cur = self._conn.execute(
                f"UPDATE {table} SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL",
                (_now_iso(), row_id),
            )
            retired = cur.rowcount > 0
            if retired:
                self._conn.execute(
                    "INSERT INTO deletion_log"
                    "(id, user_id, table_name, record_id, actor, reason, snapshot) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (_uuid(), uid, table, row_id, actor, reason,
                     json.dumps(snapshot, ensure_ascii=False, default=str)),
                )
            return retired

    def log_deletion(
        self, *, user_id: str, table_name: str, record_id: str,
        actor: str, reason: str = "", snapshot: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Write a ``deletion_log`` audit row directly (ADR-V6-045).

        For paths that already performed their own bulk soft-delete UPDATE and
        only need the audit append (e.g. sovereignty ``_soft_delete_window``
        batches one log row per retired row inside its own transaction). Callers
        guarantee atomicity with their UPDATE; this is the append half.
        """
        with self._lock:
            self._conn.execute(
                "INSERT INTO deletion_log"
                "(id, user_id, table_name, record_id, actor, reason, snapshot) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (_uuid(), user_id, table_name, record_id, actor, reason,
                 json.dumps(snapshot, ensure_ascii=False, default=str)
                 if snapshot is not None else None),
            )

    def list_deletion_log(
        self, user_id: str, *, limit: int = 200, table_name: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Read the tenant's soft-delete audit trail (newest first), optionally
        filtered to one table. The R12 sovereignty audit surface — answers
        "what of mine was retired, by whom, when, and why?" Never raises (C7)."""
        try:
            with self._lock:
                if table_name is not None:
                    rows = self._conn.execute(
                        "SELECT id, created_at, table_name, record_id, actor, "
                        "reason, snapshot FROM deletion_log "
                        "WHERE user_id = ? AND table_name = ? "
                        "ORDER BY created_at DESC LIMIT ?",
                        (user_id, table_name, limit),
                    ).fetchall()
                else:
                    rows = self._conn.execute(
                        "SELECT id, created_at, table_name, record_id, actor, "
                        "reason, snapshot FROM deletion_log "
                        "WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
                        (user_id, limit),
                    ).fetchall()
            return [dict(r) for r in rows]
        except Exception:  # noqa: BLE001
            logger.warning("list_deletion_log read failed:", exc_info=True)
            return []

    # ------------------------------------------------------------------
    # Memo correction atom lifecycle (ADR-V6-047 / A4)
    # ------------------------------------------------------------------

    # The four atom event tables linked to a memo via _EVENT_SPINE.memo_id.
    _MEMO_ATOM_TABLES = (
        "identity_events", "meaning_events", "feeling_events", "entity_events",
    )

    def snapshot_memo_atom_ids(
        self, user_id: str, memo_id: str,
    ) -> Dict[str, List[str]]:
        """Snapshot the LIVE atom row ids for one memo across the 4 event tables
        (ADR-V6-047). The re-extract flow captures this BEFORE re-atomizing, then
        retires exactly these ids (by id, not by memo_id) — so the freshly-written
        atoms survive and the old ones don't. Pure read, never raises (C7).
        """
        out: Dict[str, List[str]] = {}
        try:
            with self._lock:
                for t in self._MEMO_ATOM_TABLES:
                    rows = self._conn.execute(
                        f"SELECT id FROM {t} "
                        f"WHERE user_id=? AND memo_id=? AND deleted_at IS NULL",
                        (user_id, memo_id),
                    ).fetchall()
                    out[t] = [r["id"] for r in rows]
        except Exception:  # noqa: BLE001
            logger.warning("snapshot_memo_atom_ids failed (%s):", memo_id, exc_info=True)
        return out

    def soft_delete_atom_ids(
        self, *, user_id: str, ids_by_table: Dict[str, List[str]],
        actor: str = "user", reason: str = "memo_corrected",
    ) -> int:
        """Retire specific atom rows (by exact id, per table) + one ``deletion_log``
        audit row each, in ONE transaction (ADR-V6-047 / A4 写后删's delete half).

        Unlike ``soft_delete(table, row_id)`` this retires a snapshot's worth of
        rows atomically — the re-extract flow uses it to retire the OLD atoms only
        after the corrected-text re-extraction succeeded (C2: old atoms soft-deleted
        with audit, never hard-removed; recoverable). Returns the count retired.
        Never raises (C7) — a tx failure rolls back and returns 0.
        """
        total = 0
        try:
            with self.transaction():
                now = _now_iso()
                for table, ids in ids_by_table.items():
                    if not ids or table not in self._MEMO_ATOM_TABLES:
                        continue
                    # snapshot each row before retiring (deletion_log.snapshot)
                    snaps = self._conn.execute(
                        f"SELECT * FROM {table} "
                        f"WHERE user_id=? AND id IN ({','.join('?'*len(ids))}) "
                        f"AND deleted_at IS NULL",
                        (user_id, *ids),
                    ).fetchall()
                    if not snaps:
                        continue
                    placeholders = ",".join("?" for _ in snaps)
                    ids_to_retire = [r["id"] for r in snaps]
                    self._conn.execute(
                        f"UPDATE {table} SET deleted_at=? "
                        f"WHERE id IN ({placeholders})",
                        (now, *ids_to_retire),
                    )
                    for r in snaps:
                        d = dict(r)
                        self._conn.execute(
                            "INSERT INTO deletion_log"
                            "(id, user_id, table_name, record_id, actor, reason, snapshot) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (_uuid(), user_id, table, d.get("id"), actor, reason,
                             json.dumps(d, ensure_ascii=False, default=str)),
                        )
                    total += len(ids_to_retire)
        except Exception:  # noqa: BLE001 — tx rolled back; report 0
            logger.warning("soft_delete_atom_ids tx failed: ", exc_info=True)
            return 0
        return total

    def invalidate_insights(self, user_id: str) -> int:
        """Force the tenant's cached insights to regenerate after a correction
        (ADR-V6-047). V6 has no Redis; ``insight_aggregation.expires_at`` is the
        local TTL surface — setting it to now makes every cached row expire
        immediately. Pure UPDATE, never raises (C7). Returns rows touched."""
        try:
            with self._lock:
                now = _now_iso()
                cur = self._conn.execute(
                    "UPDATE insight_aggregation SET expires_at=? "
                    "WHERE user_id=? AND (expires_at IS NULL OR expires_at > ?)",
                    (now, user_id, now),
                )
                return int(cur.rowcount or 0)
        except Exception:  # noqa: BLE001
            logger.warning("invalidate_insights failed:", exc_info=True)
            return 0

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

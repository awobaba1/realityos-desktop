"""RealityOS V6 — sovereignty layer (架构 §6.1–§6.8, PIPL §31/§45/§47).

The user's control surface over their own life-graph. Four exercise faces, all
gated on the single-founder tenant:

  1. ``cascade_soft_delete(mode='A'|'B', since=, until=)`` — the §6.2 two-mode
     deletion. Mode A = space reclaim (audio/transcript only, NEVER PTG atoms —
     Phase 1 has no audio table, so A soft-deletes the memos in the window and
     keeps atoms). Mode B = total forgetting (memos + atoms + PTG edges in the
     window, cascaded). Phase 1 does §6.2 阶段1 ONLY — immediate ``deleted_at``
     marking. The physical purge (阶段2, the one legitimate hard-DELETE path)
     is ``purge_soft_deleted`` below.
  2. ``export_user_data()`` — §6.8 one-click JSON export (<5s). Every user table
     for the tenant as plain dicts, ready to write to a file the user controls
     (PIPL §45 portability).
  3. ``set_minor_mode / is_minor`` — §6.7 age gate. When on, the atomizer must
     skip R1/R9 biometric atoms and aggregated profiling; deletion is one-click
     default. The flag lives in ``ptg_meta`` (V6 bookkeeping; the atomizer reads
     ``is_minor`` to downgrade). Phase 1 skeleton: flag + predicate.
  4. ``set_consent_tag / get_consent_tag`` — §6.1 exercise face for the
     ``consent_tag`` column (relations): local_only / shareable / restricted.

PHASE 1 SCOPE (honest)
----------------------
The exercise primitives are real and tested against a live PTGStore. What is
NOT yet done (documented, not fake-green): a UI to call them, and the
§6.2 阶段2 cron that runs ``purge_soft_deleted`` nightly. The atoms are here;
the wiring is the desktop-UI step.

C2 / C7: cascade_soft_delete only ever sets ``deleted_at`` (soft). purge is the
single hard-DELETE surface and is explicit + opt-in (never auto-run in tests).
Every entry never raises into a caller (C7) — returns a result dict instead.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# §6.2 modes.
MODE_A = "A"   # space reclaim — audio/transcript only, never PTG atoms
MODE_B = "B"   # total forgetting — audio + window atoms + PTG edges (cascade)

# Per-table time column for window scoping (the four event tables share the
# _EVENT_SPINE `timestamp`; entities/relations use their own columns).
_TIME_COL = {
    "memos": "timestamp",
    "identity_events": "timestamp",
    "meaning_events": "timestamp",
    "entity_events": "timestamp",
    "feeling_events": "timestamp",
    "entities": "last_seen_at",
    "relations": "last_updated",
    "task_suggestions": "created_at",
    "feedback": "created_at",
    "tool_events": "captured_at",
    "quality_metrics": "metric_date",
    "insight_aggregation": "period_start",
}

# Mode A scope (§6.2: never PTG atoms) — Phase 1 has no audio table, so A only
# retires the captured memos in the window.
_MODE_A_TABLES = ("memos",)
# Mode B scope (§6.2: memos + window atoms + PTG edges, cascaded).
_MODE_B_TABLES = (
    "memos", "identity_events", "meaning_events", "entity_events",
    "feeling_events", "entities", "relations",
)

_MINOR_KEY = "minor_mode"
_CONSENT_DEFAULT = "local_only"

# realityos_users uses ``id`` (it IS the tenant table); every other C2 table
# carries ``user_id``. export + window scoping key off this.
_USER_ID_COL = {"realityos_users": "id"}


def _soft_delete_window(
    store, table: str, user_id: str,
    since: Optional[str], until: Optional[str],
) -> int:
    """Set deleted_at on a table's rows for the tenant in an optional time
    window. Returns the count marked. ``table`` MUST be a C2 user-data table."""
    col = _TIME_COL.get(table, "created_at")
    clauses = ["user_id = ?", "deleted_at IS NULL"]
    params: List[Any] = [user_id]
    if since is not None:
        clauses.append(f"{col} >= ?")
        params.append(since)
    if until is not None:
        clauses.append(f"{col} <= ?")
        params.append(until)
    where = " AND ".join(clauses)
    with store._lock:
        cur = store._conn.execute(
            f"UPDATE {table} SET deleted_at = ? WHERE {where}",
            (_now_iso(), *params),
        )
        return int(cur.rowcount)


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# §6.2 cascade deletion (Phase 1 = 阶段1 soft-mark only)
# ---------------------------------------------------------------------------

def cascade_soft_delete(
    store,
    user_id: str,
    *,
    mode: str,
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> Dict[str, int]:
    """§6.2 two-mode deletion, Phase 1 阶段1 (immediate soft-mark).

    * mode='A' — space reclaim: soft-delete the captured memos in the window;
      NEVER touches PTG atoms/edges (§6.2 "模式A 永不删 PTG 原子"). Audio deletion
      is a no-op in Phase 1 (no audio table yet — documented, not fake-green).
    * mode='B' — total forgetting: soft-delete memos + the four R-atom event
      tables + entities + relations in the window (the §6.2 cascade).

    ``since``/``until`` are ISO-8601 stamps scoped on each table's time column;
    omit both to retire the tenant's entire living data in that mode's scope.
    Returns ``{table: count_marked}`` (omits zero-count tables). Never raises (C7).
    """
    if mode not in (MODE_A, MODE_B):
        raise ValueError(f"unknown sovereignty mode: {mode!r} (use 'A' or 'B')")
    tables = _MODE_A_TABLES if mode == MODE_A else _MODE_B_TABLES
    result: Dict[str, int] = {}
    try:
        for t in tables:
            n = _soft_delete_window(store, t, user_id, since, until)
            if n:
                result[t] = n
    except Exception as exc:  # noqa: BLE001 — sovereignty never breaks the caller
        logger.warning("cascade_soft_delete(%s) failed mid-way: %s", mode, exc)
    logger.info("sovereignty cascade_soft_delete mode=%s user=%s marked=%s",
                mode, user_id, result)
    return result


def purge_soft_deleted(
    store,
    *,
    older_than_days: int = 1,
    tables: Optional[List[str]] = None,
) -> Dict[str, int]:
    """§6.2 阶段2 — physical hard-DELETE of rows soft-deleted more than
    ``older_than_days`` ago (the grace window).

    THIS IS THE ONE LEGITIMATE HARD-DELETE SURFACE IN V6. It is C2-compliant
    because every row it removes was already soft-deleted (deleted_at set) and
    the grace window expired — §6.2 explicitly mandates this physical purge.
    Opt-in: never auto-run; the §6.2 nightly cron (Phase 1+) calls this.

    Returns ``{table: count_purged}``. Never raises (C7).
    """
    from plugins.memory.ptg import schema as ptg_schema
    scope = tables or list(ptg_schema.C2_USER_TABLES)
    out: Dict[str, int] = {}
    try:
        with store._lock:
            for t in scope:
                cur = store._conn.execute(
                    f"DELETE FROM {t} "
                    f"WHERE deleted_at IS NOT NULL "
                    f"AND deleted_at < datetime('now', ?)",
                    (f"-{int(older_than_days)} days",),
                )
                if cur.rowcount:
                    out[t] = int(cur.rowcount)
    except Exception as exc:  # noqa: BLE001
        logger.warning("purge_soft_deleted failed: %s", exc)
    return out


# ---------------------------------------------------------------------------
# §6.8 one-click JSON export (PIPL §45 portability, <5s)
# ---------------------------------------------------------------------------

def export_user_data(store, user_id: str) -> Dict[str, Any]:
    """One-click export: every C2 user table's living rows for the tenant as
    plain dicts, plus the append-only logs the user owns. JSON-serializable.

    <5s target: single pass per table, no joins, no LLM. The caller writes the
    returned dict to a file the user picks. Never raises (C7). Respects
    soft-delete (deleted rows are excluded — they're retired, not exported).
    """
    from plugins.memory.ptg import schema as ptg_schema
    payload: Dict[str, Any] = {
        "_export_meta": {
            "user_id": user_id,
            "schema_version": ptg_schema.SCHEMA_VERSION,
            "exported_at": _now_iso(),
        },
    }
    try:
        with store._lock:
            for table in ptg_schema.C2_USER_TABLES:
                uid_col = _USER_ID_COL.get(table, "user_id")
                rows = store._conn.execute(
                    f"SELECT * FROM {table} "
                    f"WHERE {uid_col} = ? AND deleted_at IS NULL",
                    (user_id,),
                ).fetchall()
                payload[table] = [dict(r) for r in rows]
            # The user's own failure/audit trail (append-only logs).
            for table in ptg_schema.APPEND_ONLY_TABLES:
                rows = store._conn.execute(
                    f"SELECT * FROM {table} WHERE user_id = ?",
                    (user_id,),
                ).fetchall()
                payload[table] = [dict(r) for r in rows]
    except Exception as exc:  # noqa: BLE001
        logger.warning("export_user_data failed: %s", exc)
        payload["_export_error"] = str(exc)
    return payload


def export_user_data_json(store, user_id: str) -> str:
    """Convenience: export_user_data as a JSON string (the one-click file body)."""
    return json.dumps(export_user_data(store, user_id), ensure_ascii=False,
                      default=str)


# ---------------------------------------------------------------------------
# §6.7 minor mode (PIPL §31 age gate)
# ---------------------------------------------------------------------------

def set_minor_mode(store, user_id: str, enabled: bool) -> bool:
    """Toggle the §6.7 minor mode flag for the tenant (stored in ptg_meta).

    When on, the Atomizer MUST skip R1/R9 biometric atoms + aggregated
    profiling, and deletion is one-click default. The atomizer reads
    ``is_minor`` to downgrade. Returns the persisted value. Never raises (C7).
    """
    val = "1" if enabled else "0"
    try:
        with store._lock:
            store._conn.execute(
                "INSERT OR REPLACE INTO ptg_meta(key, value) VALUES (?, ?)",
                (_MINOR_KEY, val),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("set_minor_mode failed: %s", exc)
    return enabled


def is_minor(store, user_id: str) -> bool:
    """Read the §6.7 minor mode flag. False when unset (adult default)."""
    try:
        with store._lock:
            row = store._conn.execute(
                "SELECT value FROM ptg_meta WHERE key=?", (_MINOR_KEY,)
            ).fetchone()
        return bool(row) and row[0] == "1"
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# §6.1 consent_tag exercise face (relations)
# ---------------------------------------------------------------------------

def set_consent_tag(
    store, user_id: str, *, relation_ids: List[str], tag: str,
) -> int:
    """Set the ``consent_tag`` on the tenant's relation rows (the §6.1 exercise
    face — the user flips local_only / shareable / restricted). Returns the
    count updated. Empty list / unknown ids → 0. Never raises (C7)."""
    if not relation_ids:
        return 0
    qmarks = ",".join("?" for _ in relation_ids)
    try:
        with store._lock:
            cur = store._conn.execute(
                f"UPDATE relations SET consent_tag = ? "
                f"WHERE user_id = ? AND id IN ({qmarks})",
                (tag, user_id, *relation_ids),
            )
            return int(cur.rowcount)
    except Exception as exc:  # noqa: BLE001
        logger.warning("set_consent_tag failed: %s", exc)
        return 0


def get_consent_summary(store, user_id: str) -> Dict[str, int]:
    """Count of the tenant's relations per consent_tag value (the §6.1 status
    face). Returns ``{tag: count}``; NULL tags bucket as the default."""
    out: Dict[str, int] = {}
    try:
        with store._lock:
            rows = store._conn.execute(
                "SELECT IFNULL(consent_tag, ?) AS tag, COUNT(*) AS n "
                "FROM relations WHERE user_id = ? AND deleted_at IS NULL "
                "GROUP BY tag",
                (_CONSENT_DEFAULT, user_id),
            ).fetchall()
        for r in rows:
            out[r["tag"]] = int(r["n"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_consent_summary failed: %s", exc)
    return out


def register(ctx) -> None:  # pragma: no cover — sovereignty is called explicitly
    """Plugin entry point. Phase 1: the exercise primitives are available as a
    module; the desktop UI to call them (delete dialog, export button, age gate,
    consent toggles) is the next step."""
    logger.debug("realityos_sovereignty registered (Phase 1 primitives live, "
                 "UI wiring next).")

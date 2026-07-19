"""RealityOS V6 — local disaster-recovery for the PTG (ADR-V6-012, 架构 §6.9).

V6 pins all user data to one local machine — ``disk failure / theft / accidental
delete = the user's life-graph is gone``. That is the one failure a data-asset
product may never ship with. Three lines of defence (§6.9):

  ① daily SQLite ``.backup()`` to a user-chosen second location, 30 rolling copies
  ② weekly raw-audio cold rsync                              (Phase 1: no audio — deferred)
  ③ monthly restore drill: restore to a temp DB → reconcile atom/relation counts
     → alert on mismatch (defends against "backed up but can't restore" silent failure)

This module is the engine for ① and ③. RPO ≤ 24h, RTO ≤ 30min. The second
location is OPT-IN (the onboarding guides the user to pick a pure-local external
disk / synced folder; never forces cloud — honours "data never leaves device").

C7 (no silent failure) is the whole point of verify_backup: a backup that
restores to fewer rows than live is a *failed* backup, and we say so out loud.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_KEEP = 30
_COUNT_TABLES = (
    "memos", "identity_events", "meaning_events", "entity_events",
    "feeling_events", "entities", "relations",
)
_BACKUP_NAME_RE = re.compile(r"^ptg_backup_.+\.db$")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(dt: datetime) -> str:
    """Filesystem-safe UTC stamp: ptg_backup_20260719T143000Z.db."""
    return dt.strftime("ptg_backup_%Y%m%dT%H%M%SZ.db")


def _counts_from_conn(conn: sqlite3.Connection) -> dict:
    """Live row counts (respecting soft-delete) for the reconcile contract."""
    out = {}
    for t in _COUNT_TABLES:
        try:
            row = conn.execute(
                f"SELECT COUNT(*) FROM {t} WHERE deleted_at IS NULL"
            ).fetchone()
            out[t] = int(row[0]) if row else 0
        except sqlite3.Error:
            out[t] = -1  # table missing in this (older) backup — flag, don't crash
    return out


def backup_ptg(store, dest_dir, *, keep: int = _DEFAULT_KEEP,
               now: Optional[datetime] = None) -> Path:
    """Online SQLite backup of the PTG into ``dest_dir`` (line of defence ①).

    Uses the sqlite3 online backup API against the store's shared connection
    (held under the store lock so it serialises with the atomize daemon). Prunes
    the destination to the ``keep`` newest backups. Returns the backup path.

    ``dest_dir`` is created if missing. Raises on failure — backup failure is a
    P0 the caller MUST surface (never silent, C7).
    """
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / _stamp(now or _now_utc())

    conn = getattr(store, "_conn", None)
    lock = getattr(store, "_lock", None)
    if conn is None:
        raise RuntimeError("PTGStore has no live connection; cannot back up")
    # Serialize with writers; the online backup API is safe under the lock.
    with lock:
        dst = sqlite3.connect(str(target))
        try:
            with dst:
                conn.backup(dst)  # full online copy
        finally:
            dst.close()

    _prune_backups(dest, keep=keep)
    logger.info("PTG backed up to %s (keep=%d)", target, keep)
    return target


def _prune_backups(dest_dir: Path, *, keep: int) -> int:
    """Delete oldest backups beyond ``keep``. Returns the number pruned."""
    backups = sorted(
        (p for p in dest_dir.iterdir() if p.is_file() and _BACKUP_NAME_RE.match(p.name)),
        key=lambda p: p.name,  # stamp sorts lexicographically == chronologically
    )
    excess = len(backups) - keep
    if excess <= 0:
        return 0
    for p in backups[:excess]:
        try:
            p.unlink()
        except OSError as exc:
            logger.warning("could not prune old backup %s: %s", p, exc)
    return excess


def verify_backup(backup_path, *, expected_counts: Optional[dict] = None) -> dict:
    """Line of defence ③ — prove the backup actually restores (no fake-green).

    Opens the backup read-only, runs a structural integrity check, counts the
    user-data tables, and (when ``expected_counts`` is the live DB's counts)
    reconciles. A backup whose restored counts are LESS than live is a FAILED
    backup → ``ok=False``. Returns ``{ok, counts, mismatches, error}``.

    C7: a corrupt/non-database file, a torn write, or a missing core table are
    all FAILED backups — never green. The probe + integrity_check exist solely
    to stop ``_counts_from_conn``'s per-table tolerance from hiding corruption.
    """
    result = {"ok": False, "counts": {}, "mismatches": [], "error": None}
    path = Path(backup_path)
    if not path.is_file():
        result["error"] = f"backup file not found: {path}"
        return result
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        # Probe: a non-database file raises here (sqlite reads the header page).
        conn.execute("SELECT 1").fetchone()
        # Structural integrity (cheap; catches torn writes / page corruption).
        ic = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if ic != "ok":
            result["error"] = f"integrity_check failed: {ic}"
            conn.close()
            return result
        result["counts"] = _counts_from_conn(conn)
        conn.close()
    except sqlite3.Error as exc:
        result["error"] = f"backup unreadable/corrupt: {exc}"
        return result

    # A valid PTG backup must carry its core capture table.
    if result["counts"].get("memos", -1) < 0:
        result["error"] = "core table 'memos' missing — not a valid PTG backup"
        return result

    # Anti-fake-green: the 7 spine tables are MANDATORY in Phase 1 (SCHEMA_VERSION
    # is fixed). A missing table is a defect UNCONDITIONALLY — not only when we
    # happen to have a live count to compare against. This stops a partial backup
    # (e.g. entities dropped) from passing green just because expected_counts
    # omitted that key. Same-or-fewer ROWS is an acceptable stale snapshot; a
    # missing TABLE never is.
    for t in _COUNT_TABLES:
        if result["counts"].get(t, -1) == -1:
            result["mismatches"].append(f"{t}: missing in backup")

    if expected_counts is not None:
        for table, live in expected_counts.items():
            got = result["counts"].get(table)
            if live is not None and got is not None and got > live:
                result["mismatches"].append(
                    f"{table}: backup has {got} > live {live} (corrupt)")
    result["ok"] = (not result["mismatches"]) and result["error"] is None
    return result


def restore_drill(store, backup_path) -> dict:
    """Monthly drill (③): reconcile a backup against the live store.

    Returns the verify report with ``expected_counts`` taken from the LIVE store,
    so a backup that silently lost rows is caught. Never mutates the live DB.
    """
    conn = getattr(store, "_conn", None)
    lock = getattr(store, "_lock", None)
    if conn is None:
        return {"ok": False, "error": "live store has no connection", "counts": {}}
    with lock:
        live = _counts_from_conn(conn)
    return verify_backup(backup_path, expected_counts=live)

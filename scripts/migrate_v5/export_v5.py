"""V5 → V6 founder migration EXPORTER (PostgreSQL → JSONL).

Runs ONCE, in an environment with ``asyncpg`` + network access to the V5
Postgres database. Produces one ``<table>.jsonl`` per table in an output
directory; the V6 importer (``plugins/memory/ptg/migrate.py``) ingests that
directory. The JSONL dump is the permanently-backed-up artifact (D3).

Why JSONL (not pg_dump SQL or a live-PG read from V6)?
  * V6 desktop ships NO Postgres driver — JSONL keeps the desktop dependency-
    free (ADR-V6-009).
  * JSONL is human-inspectable, diffable, and resumable; pg_dump SQL is fragile
    to parse and PG-version-specific.
  * The exporter serializes PG-native types to JSON-native values (UUID→str,
    datetime→iso8601, Decimal→float) so the importer's converters handle them
    idempotently.

Tables exported (the 13 V5 real tables, FK-safe order). ``location_log`` is
intentionally excluded (D3 — founder PG dump permanently backed up).
``memo_embeddings`` is excluded (rebuildable derived cache; V6 re-embeds).

Usage:
    python export_v5.py --dsn "$DATABASE_URL_ADMIN" --out ./v5dump
    # optional: --tables users,memos  (comma-list subset)

Requires: ``pip install asyncpg`` (not a V6 runtime dependency; migration-only).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import sys
import uuid
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, List

logger = logging.getLogger("v5_export")

# FK-safe export order (matches the importer's IMPORT_ORDER).
TABLES: List[str] = [
    "users", "memos", "identity_events", "meaning_events", "entity_events",
    "feeling_events", "entities", "relations", "task_suggestions", "feedback",
    "insight_aggregation", "dlq_messages", "llm_call_logs",
]


def serialize_pg_value(v: Any) -> Any:
    """Convert one PG-native cell to a JSON-serializable value.

    Mirrors the importer's converters in reverse: every output here is handled
    idempotically by the matching converter on the V6 side. Unknown types fall
    back to ``str(v)`` so nothing is ever lost or silently dropped (C2/C7)."""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float, str)):
        return v
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=None)  # naive → importer stamps UTC; keep as-is
        return v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (bytes, bytearray)):
        return base64.b64encode(bytes(v)).decode("ascii")
    if isinstance(v, (list, tuple)):
        return [serialize_pg_value(x) for x in v]
    if isinstance(v, dict):
        return {str(k): serialize_pg_value(val) for k, val in v.items()}
    # JSONB may arrive as a JSON string (asyncpg without codecs) — normalize.
    if isinstance(v, str):
        return v
    return str(v)  # last resort — never drop


def row_to_json(row: Iterable[Any], columns: List[str]) -> dict:
    """Map an asyncpg Record (or tuple) + column list to a JSON-ready dict."""
    return {col: serialize_pg_value(val) for col, val in zip(columns, row)}


async def export_table(conn, table: str, out_path: Path, batch: int = 1000) -> dict:
    """SELECT * FROM <table> ORDER BY id, write JSONL. Returns row count."""
    cur = await conn.cursor(f"SELECT * FROM {table} ORDER BY id")
    columns = [d.name for d in cur.get_attributes()]
    written = 0
    with open(out_path, "w", encoding="utf-8") as f:
        while True:
            rows = await cur.fetch(batch)
            if not rows:
                break
            for r in rows:
                f.write(json.dumps(row_to_json(r, columns), ensure_ascii=False) + "\n")
                written += 1
    logger.info("export %-20s rows=%d → %s", table, written, out_path.name)
    return {"table": table, "rows": written}


async def export_all(dsn: str, out_dir: Path, tables: List[str]) -> dict:
    """Connect to V5 PG and export each table to ``<out_dir>/<table>.jsonl``."""
    import asyncpg  # lazy — not a V6 runtime dep
    out_dir.mkdir(parents=True, exist_ok=True)
    report = {"tables": {}, "dsn_db": dsn.rsplit("/", 1)[-1]}
    conn = await asyncpg.connect(dsn)
    try:
        for table in tables:
            try:
                stats = await export_table(conn, table, out_dir / f"{table}.jsonl")
                report["tables"][table] = stats["rows"]
            except Exception as exc:  # noqa: BLE001 — one table failing shouldn't abort all
                logger.error("export %s FAILED: %s", table, exc)
                report["tables"][table] = f"ERROR: {exc}"
    finally:
        await conn.close()
    manifest = out_dir / "manifest.json"
    manifest.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("export manifest → %s", manifest)
    return report


def main(argv: List[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Export V5 PostgreSQL → JSONL for V6 migration.")
    p.add_argument("--dsn", default=None,
                   help="V5 admin DSN (default: $DATABASE_URL_ADMIN)")
    p.add_argument("--out", default="./v5dump", help="Output directory.")
    p.add_argument("--tables", default=None,
                   help="Comma-separated subset (default: all 13).")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    dsn = args.dsn or __import__("os").environ.get("DATABASE_URL_ADMIN")
    if not dsn:
        print("error: --dsn or $DATABASE_URL_ADMIN required", file=sys.stderr)
        return 2
    tables = args.tables.split(",") if args.tables else TABLES
    unknown = [t for t in tables if t not in TABLES]
    if unknown:
        print(f"error: unknown table(s): {unknown}; valid: {TABLES}", file=sys.stderr)
        return 2

    report = asyncio.run(export_all(dsn, Path(args.out), tables))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

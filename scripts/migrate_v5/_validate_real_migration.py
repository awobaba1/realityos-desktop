"""ONE-OFF verification: run the V5→V6 importer against the founder's REAL dump.

Validates ``plugins/memory/ptg/migrate.py`` (the P0-5 deliverable) end-to-end on
real heterogeneous production data (C3 / production-reality-is-truth, and the
ADR-088 lesson: synthetic samples once hid a 41% pollution bug). All 72 unit
tests use synthetic fixtures; this run uses the founder's actual 4-year data.

Pipeline (all local, no Postgres/Docker/asyncpg):
  1. ``_parse_pgdump_copy.py`` turns the V5 pg_dump into JSONL (exporter format).
  2. Pre-flight: every mapped V6 column must exist in the created schema (a
     missing column would crash the batch flush; catch it cleanly first).
  3. ``import_dump`` runs the REAL importer unchanged into a throwaway ptg.db.
  4. Reconcile per-table read/written/skipped/errors. On a fresh DB the invariant
     is ``skipped == 0 and errors == 0 and written == read`` for ALL 13 tables —
     any non-zero skip/error is a real-data edge case the synthetic tests missed.
  5. Spot-check real content (founder memos, CJK FTS recall, a relation, an
     llm_call_log replay record) and print a verdict.

This is a verification harness, not a shipped migration path. The throwaway
ptg.db is deleted on each run (the founder's real V6 asset is built on their own
machine at deploy time per ADR-V6-009).

Usage (from repo root):
    uv run --extra dev python scripts/migrate_v5/_validate_real_migration.py \
        <path/to/realityos_YYYYMMDD_030000.sql.gz>
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

# Make the repo root importable (plugins.* + the sibling parser/exporter).
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "migrate_v5"))

import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "_parse_pgdump_copy", _REPO_ROOT / "scripts" / "migrate_v5" / "_parse_pgdump_copy.py")
_parse = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_parse)

from plugins.memory.ptg.migrate import (  # noqa: E402
    COLUMN_MAPS, IMPORT_ORDER, TABLE_TARGET, import_dump,
)
from plugins.memory.ptg.provider import PTGProvider  # noqa: E402
from plugins.memory.ptg.store import PTGStore  # noqa: E402

logger = logging.getLogger("v5_validate")


def _table_columns(conn, table: str) -> set:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def preflight_columns(store: PTGStore) -> list:
    """Return a list of (table, v6_col) for mapped columns ABSENT from the schema.

    A non-empty list means the import would crash on that table's batch flush
    (INSERT references a non-existent column)."""
    missing = []
    for table, colmap in COLUMN_MAPS.items():
        target = TABLE_TARGET.get(table, table)
        cols = _table_columns(store._conn, target)
        for _v5, v6, _conv in colmap:
            if v6 not in cols:
                missing.append((table, v6))
    return missing


def main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="Validate V5→V6 migration on real dump.")
    p.add_argument("dump", help="Path to the V5 pg_dump (.sql or .sql.gz).")
    p.add_argument("--work", default=str(_REPO_ROOT / ".migration-validate"),
                   help="Working dir for JSONL + throwaway ptg.db.")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    dump = Path(args.dump)
    if not dump.exists():
        print(f"error: dump not found: {dump}", file=sys.stderr)
        return 2

    # 1. Parse dump → JSONL (exporter-equivalent format).
    print(f"\n[1/5] Parsing {dump.name} → JSONL …")
    parsed = _parse.parse_dump(dump, work, IMPORT_ORDER)
    print(f"      parsed rows: {parsed}")

    # 2. Fresh throwaway store + pre-flight column existence.
    print("\n[2/5] Pre-flight: mapped columns exist in schema?")
    db = work / "ptg.validation.db"
    if db.exists():
        db.unlink()
    store = PTGStore(db_path=str(db))
    missing = preflight_columns(store)
    if missing:
        print(f"      ❌ MISSING {len(missing)} columns: {missing}")
        store.close()
        return 1
    print("      ✓ every mapped V6 column exists in the created schema.")

    # 3. Run the REAL importer.
    print("\n[3/5] Importing JSONL → ptg.db (real importer) …")
    report = import_dump(store, work, tables=IMPORT_ORDER)

    # 4. Reconcile.
    print("\n[4/5] Reconcile (fresh-DB invariant: skipped=0, errors=0, written=read):")
    fail = False
    for table in IMPORT_ORDER:
        s = report["tables"][table]
        ok = (s["errors"] == 0 and s["skipped"] == 0 and
              (s["written"] == s["read"] or s["read"] == 0))
        flag = "✓" if ok else "❌"
        if not ok:
            fail = True
        print(f"      {flag} {table:20s} read={s['read']:4d} written={s['written']:4d} "
              f"skipped={s['skipped']:4d} errors={s['errors']:4d}")
    t = report["totals"]
    print(f"      {'─'*60}")
    print(f"        TOTAL              read={t['read']:4d} written={t['written']:4d} "
          f"skipped={t['skipped']:4d} errors={t['errors']:4d}")

    # 5. Spot-checks on real content.
    print("\n[5/5] Spot-checks on real founder data:")
    conn = store._conn
    n_users = conn.execute("SELECT COUNT(*) FROM realityos_users").fetchone()[0]
    n_memos = conn.execute("SELECT COUNT(*) FROM memos").fetchone()[0]
    # The real founder = the user who actually authored memos (V5 also carried
    # test/smoke users). is_founder is all-false in V5, so the migrated founder
    # arrives un-flagged; ensure_founder must PROMOTE them (Finding B).
    founder = conn.execute(
        "SELECT u.id, u.email, u.nickname, u.is_founder, "
        "(SELECT COUNT(*) FROM memos m WHERE m.user_id=u.id) AS memo_n "
        "FROM realityos_users u ORDER BY memo_n DESC LIMIT 1").fetchone()
    print(f"      users={n_users}  memos={n_memos}")
    print(f"      real founder (most memos): {dict(founder)} "
          f"→ migrated is_founder={founder['is_founder']}")
    store.ensure_founder(founder["id"], founder["email"] or "",
                         nickname=founder["nickname"] or "")
    promoted = conn.execute(
        "SELECT is_founder FROM realityos_users WHERE id=?",
        (founder["id"],)).fetchone()
    pb = "✓ promoted to 1" if promoted["is_founder"] else "❌ still 0"
    print(f"      after ensure_founder: is_founder={promoted['is_founder']} ({pb})")

    # CJK FTS recall — the base-tier capture surface must work on real text.
    # Generic CJK terms (2-char) that appear in the real data — person-name
    # queries are redacted from this committed artifact (covered instead by the
    # 3-char unit test). Substitute real terms when re-running on a fresh dump.
    for q in ("北京", "辞职"):
        hits = store.search_memos_fts(q, limit=3)
        snippet = hits[0]["source_text"][:30] if hits else "(none)"
        print(f"      FTS '{q}': {len(hits)} hit(s) — e.g. {snippet}")

    # A relation + an llm_call_log (C6 replay substrate) round-tripped intact.
    rel = conn.execute(
        "SELECT relation_type, confidence FROM relations LIMIT 1").fetchone()
    llm = conn.execute(
        "SELECT model, provider, schema_valid FROM llm_call_logs LIMIT 1").fetchone()
    print(f"      relation sample: {dict(rel) if rel else None}")
    print(f"      llm_call_log sample: {dict(llm) if llm else None}")

    # Integrity: confirm one founder memo's CJK text survived byte-faithfully.
    sample = conn.execute(
        "SELECT source_text FROM memos WHERE source_text LIKE '%北京%' LIMIT 1"
    ).fetchone()
    print(f"      CJK fidelity (北京 memo): "
          f"{sample['source_text'][:40] if sample else '(not found)'}")

    # 6. RUNTIME GATE — the real "does V6 work" check: after migrating the
    # founder's data, the PTG provider must initialise on the populated DB,
    # RECALL migrated memos (prefetch), and CAPTURE a new turn (sync_turn).
    # Distribution-independent (tests the agent data layer, not the packaging).
    print("\n[6/6] Runtime gate (PTGProvider on the migrated DB):")
    provider = PTGProvider(config={
        "db_path": str(db),
        "founder_user_id": founder["id"],
        "founder_email": founder["email"] or "",
        "founder_nickname": founder["nickname"] or "",
    })
    provider.initialize("runtime-gate", agent_context="primary")
    rt_ok = provider._store is not None
    recall = provider.prefetch("北京")
    rt_recall = "北京" in recall and "今天晚上" in recall
    provider.sync_turn("运行时冒烟：再记一条关于北京出差的备忘", "好的，已记下。")
    recall2 = provider.prefetch("北京")
    rt_capture = "运行时冒烟" in recall2
    blk = provider.system_prompt_block()
    rt_status = "captured memo" in blk
    provider.shutdown()
    print(f"      provider initialised: {'✓' if rt_ok else '❌'}")
    print(f"      prefetch('北京') recalls migrated memo: {'✓' if rt_recall else '❌'}"
          f"\n        → {recall.splitlines()[-1][:50] if recall else '(empty)'}")
    print(f"      sync_turn captures new memo + recallable: {'✓' if rt_capture else '❌'}")
    print(f"      system_prompt_block reports status: {'✓' if rt_status else '❌'}")
    if not (rt_ok and rt_recall and rt_capture and rt_status):
        fail = True

    store.close()
    print(f"\nVERDICT: {'✅ PASS — importer accepted all real data, 0 skip/0 error' if not fail else '❌ FAIL — see skipped/errors above'}")
    print(f"(throwaway db left at {db} for inspection; delete anytime.)")
    return 0 if not fail else 1


if __name__ == "__main__":
    raise SystemExit(main())

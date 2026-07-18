"""ONE-OFF verification harness: parse a V5 ``pg_dump`` (COPY blocks) → JSONL.

NOT the canonical V5→V6 migration path. ADR-V6-009 specifies a live-Postgres
export (``export_v5.py`` + asyncpg) as the canonical path, and rejects pg_dump
SQL parsing as fragile/PG-version-specific for the GENERAL migration.

This parser exists ONLY to validate the importer
(``plugins/memory/ptg/migrate.py``) against the founder's REAL production data
when no live Postgres is reachable from the dev machine — only a ``pg_dump`` is.
It therefore targets exactly that one, well-formed input: a default pg_dump with
COPY-FROM-stdin blocks. It emits the SAME ``<table>.jsonl`` + ``manifest.json``
format as ``export_v5.py``, so the importer is exercised UNCHANGED (the JSONL is
the contract surface; the importer never knows which side produced it).

COPY-format handling (libpq default):
  * field separator = tab; every data row is exactly one physical line (real
    newlines inside a field are escaped as ``\\n``, so they never break reads).
  * ``\\N`` (whole field) = NULL → Python None.
  * ``\\t \\n \\r \\b \\v \\f \\\\`` decode to the control char / backslash;
    ``\\NNN`` octal; unknown escape → the literal char.

PG text values are passed to the importer's converters AS STRINGS — which every
converter already accepts (converters.py), so no type decoding is duplicated
here. The one cosmetic divergence from the asyncpg path: TIMESTAMPTZ stays in
pg_dump's ``YYYY-MM-DD HH:MM:SS+00`` form rather than being canonicalised to
ISO-8601 ``T...+00:00``. Both are faithful TEXT representations of the same
instant and the V6 schema has no format CHECK, so importability is unaffected
(this is a verification run, not the founder's final asset).

Usage:
    python _parse_pgdump_copy.py path/to/realityos_*.sql.gz --out ./v5dump
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("v5_copy_parse")

_COPY_HEADER = re.compile(r'^COPY public\.(\w+)\s*\((.*)\) FROM stdin;$')


def _unescape_field(raw: str) -> Optional[str]:
    """Decode one COPY field. ``\\N`` → None; else resolve backslash escapes."""
    if raw == r"\N":
        return None
    out: List[str] = []
    i, n = 0, len(raw)
    simple = {"t": "\t", "n": "\n", "r": "\r", "b": "\b",
              "v": "\v", "f": "\f", "\\": "\\"}
    while i < n:
        c = raw[i]
        if c == "\\" and i + 1 < n:
            nxt = raw[i + 1]
            if nxt in simple:
                out.append(simple[nxt]); i += 2; continue
            if nxt in "01234567":  # octal, up to 3 digits
                j = i + 1
                digits = ""
                while j < n and raw[j] in "01234567" and len(digits) < 3:
                    digits += raw[j]; j += 1
                out.append(chr(int(digits, 8))); i = j; continue
            out.append(nxt); i += 2; continue  # unknown escape → literal
        out.append(c); i += 1
    return "".join(out)


def _parse_copy_header(line: str):
    """Return (table, [columns]) for a COPY header line, else None."""
    m = _COPY_HEADER.match(line)
    if not m:
        return None
    table = m.group(1)
    cols = [c.strip().strip('"') for c in m.group(2).split(",")]
    return table, cols


def parse_dump(dump_path: Path, out_dir: Path, tables: List[str]) -> Dict[str, int]:
    """Stream the dump; write ``<table>.jsonl`` for each requested table.

    Returns ``{table: rows_written}``. Non-requested COPY blocks and all
    non-COPY lines are skipped. A requested table absent from the dump simply
    produces no file (the importer treats a missing file as zero rows).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    target = set(tables)
    counts: Dict[str, int] = {}
    opener = gzip.open if str(dump_path).endswith(".gz") else open
    current: Optional[str] = None
    cols: List[str] = []
    outf = None
    written = 0
    with opener(dump_path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if current is None:
                hdr = _parse_copy_header(line)
                if hdr and hdr[0] in target:
                    current, cols = hdr
                    outf = open(out_dir / f"{current}.jsonl", "w", encoding="utf-8")
                    written = 0
                continue
            if line == r"\.":  # end of COPY block
                if outf:
                    outf.close()
                counts[current] = written
                logger.info("parsed %-20s rows=%d", current, written)
                current = None; cols = []; outf = None
                continue
            fields = [_unescape_field(f) for f in line.split("\t")]
            row = {col: val for col, val in zip(cols, fields)}
            assert outf is not None
            outf.write(json.dumps(row, ensure_ascii=False) + "\n")
            written += 1
    if outf:  # truncated dump with no terminator
        outf.close()
        counts[current] = written  # type: ignore[assignment]
    manifest = {"tables": counts, "source": str(dump_path)}
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return counts


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Parse a V5 pg_dump (COPY) → JSONL.")
    p.add_argument("dump", help="Path to the .sql or .sql.gz pg_dump.")
    p.add_argument("--out", default="./v5dump", help="Output directory.")
    p.add_argument("--tables", default=None,
                   help="Comma-subset (default: the 13 migration tables).")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    from export_v5 import TABLES  # reuse the canonical 13
    tables = args.tables.split(",") if args.tables else TABLES
    dump = Path(args.dump)
    if not dump.exists():
        print(f"error: dump not found: {dump}", file=sys.stderr)
        return 2
    counts = parse_dump(dump, Path(args.out), tables)
    print(json.dumps(counts, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

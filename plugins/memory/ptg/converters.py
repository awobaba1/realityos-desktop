"""PG → SQLite value converters for the V5 → V6 founder migration.

Schema-independent: these transform a single PostgreSQL cell value into the
SQLite representation per the type-mapping cheat sheet (ADR-V6-008):

    UUID ......... TEXT  (36-char lowercased hyphenated; "" stays NULL)
    TIMESTAMPTZ .. TEXT  (ISO-8601, UTC; naive datetimes assumed UTC)
    DATE ......... TEXT  (YYYY-MM-DD)
    JSONB ........ TEXT  (json.dumps, ensure_ascii=False; NULL stays NULL)
    BOOLEAN ...... INTEGER (1/0)
    NUMERIC/Float  REAL
    VARCHAR/TEXT . TEXT  (passthrough)
    Vector ....... dropped (re-embedded in the extraction phase; never migrated)

The per-table column map (which V5 column → which V6 column + which converter)
lives in ``migrate.py`` and is built once the V5 schema reference is fixed.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Optional


def to_uuid_text(v: Any) -> Optional[str]:
    """UUID / str / bytes → 36-char lowercased TEXT. None stays None."""
    if v is None:
        return None
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, (bytes, bytearray)):
        return str(uuid.UUID(bytes=bytes(v)))
    s = str(v).strip()
    return s.lower() or None


def to_iso8601(v: Any) -> Optional[str]:
    """datetime / date / str → ISO-8601 TEXT (UTC). Naive datetimes assumed UTC.

    V5 stored TIMESTAMPTZ; asyncpg returns timezone-aware datetimes. A naive
    datetime (rare) is treated as UTC and stamped +00:00 so the V6 TEXT is
    unambiguous and round-trips cleanly.
    """
    if v is None:
        return None
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    s = str(v).strip()
    return s or None


def to_json_text(v: Any) -> Optional[str]:
    """dict / list / scalar → JSON TEXT. None stays None (NOT the string 'null').

    V5 JSONB columns hold heterogeneous shapes (objects, arrays, scalars);
    json.dumps preserves them. ``ensure_ascii=False`` keeps CJK legible.
    """
    if v is None:
        return None
    if isinstance(v, str):
        # A string coming from JSONB is already JSON text in V5's eyes; re-dump
        # to normalize (validates + canonicalizes). If it isn't valid JSON,
        # store it verbatim under a string wrapper so nothing is lost (C2).
        try:
            parsed = json.loads(v)
            return json.dumps(parsed, ensure_ascii=False)
        except (json.JSONDecodeError, TypeError):
            return json.dumps(v, ensure_ascii=False)
    return json.dumps(v, ensure_ascii=False, default=str)


def to_bool_int(v: Any) -> Optional[int]:
    """bool / 0/1 / 't'/'f' → INTEGER 1/0. None stays None."""
    if v is None:
        return None
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, (int, float)):
        return 1 if v else 0
    if isinstance(v, str):
        return 1 if v.strip().lower() in ("t", "true", "1", "y", "yes") else 0
    return 1 if v else 0


def to_real(v: Any) -> Optional[float]:
    """Decimal / int / float / numeric str → REAL. None stays None."""
    if v is None:
        return None
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip()
        return float(s) if s else None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def to_int(v: Any) -> Optional[int]:
    """int / bool / numeric str → INTEGER. None stays None. (version,
    mention_count, evidence_count, *_tokens, retry_count, days_overdue, etc.)"""
    if v is None:
        return None
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    if isinstance(v, (Decimal,)):
        return int(v)
    if isinstance(v, str):
        s = v.strip()
        return int(s) if s else None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def to_text(v: Any) -> Optional[str]:
    """VARCHAR / TEXT passthrough. None stays None; everything else str()'d."""
    if v is None:
        return None
    return v if isinstance(v, str) else str(v)


def drop_vector(v: Any) -> Optional[str]:
    """Vector columns are NOT migrated — re-embedding happens in the extraction
    phase (ADR-V6-008). Returns a marker so the importer can SKIP the column
    rather than write it. The importer treats this sentinel as 'omit'."""
    return _OMIT


class _OmitSentinel:
    """Returned by drop_vector(); the importer skips any column whose converted
    value is this sentinel (so a dropped vector never produces a NULL/TEXT)."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debug only
        return "<omit>"


_OMIT = _OmitSentinel()


# Target-type → converter dispatch. The column map references these by name.
CONVERTERS: dict[str, Callable[[Any], Any]] = {
    "uuid": to_uuid_text,
    "text": to_text,
    "iso8601": to_iso8601,
    "json": to_json_text,
    "bool": to_bool_int,
    "real": to_real,
    "int": to_int,
    "drop": drop_vector,
}


def convert_row(
    v5_row: dict,
    column_map: list[tuple[str, str, str]],
) -> dict:
    """Transform one V5 row (dict keyed by V5 column) into a V6 row (dict keyed
    by V6 column), applying per-column converters and omitting dropped columns.

    ``column_map`` is a list of ``(v5_col, v6_col, converter_name)`` tuples.

    A V6 column whose V5 key is **absent** (the V5 schema simply lacks that
    column — e.g. V6-added ``ser_source`` / ``voiceprint_confidence``) is OMITTED
    from the output, so the INSERT does not name it and SQLite applies the V6
    column DEFAULT (``ser_source DEFAULT 'llm_text'``). Materializing it as
    explicit ``None`` instead would store NULL and *defeat* the DEFAULT, tripping
    the ``NOT NULL`` — a real-data bug the synthetic fixtures (which always set
    the value) hid (ADR-088 lesson).

    A V5 key that is **present but NULL** is still emitted as NULL — correct for
    genuinely nullable columns where individual rows are null.
    """
    out: dict = {}
    for v5_col, v6_col, conv_name in column_map:
        if v5_col not in v5_row:
            continue  # V5 schema lacks this column → omit; V6 DEFAULT / NULL applies
        converter = CONVERTERS[conv_name]
        val = converter(v5_row.get(v5_col))
        if val is _OMIT:
            continue  # dropped vector — do not write
        out[v6_col] = val
    return out

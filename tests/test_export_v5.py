"""V5 exporter — serializer + CLI guard tests (asyncpg path needs a live PG)."""

from __future__ import annotations

import importlib.util
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

# The exporter lives outside the packages (scripts/migrate_v5/); load by path.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "export_v5", _REPO_ROOT / "scripts" / "migrate_v5" / "export_v5.py")
export_v5 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(export_v5)


# ---------------------------------------------------------------------------
# serialize_pg_value — PG-native → JSON-native (mirror of the importer converters)
# ---------------------------------------------------------------------------

def test_serialize_none_and_scalars():
    assert export_v5.serialize_pg_value(None) is None
    assert export_v5.serialize_pg_value(True) is True
    assert export_v5.serialize_pg_value(42) == 42
    assert export_v5.serialize_pg_value("hi") == "hi"


def test_serialize_uuid_to_string():
    u = uuid.UUID("AABBCCDD-EEFF-0011-2233-445566778899")
    assert export_v5.serialize_pg_value(u) == "aabbccdd-eeff-0011-2233-445566778899"


def test_serialize_datetime_and_date():
    dt = datetime(2026, 7, 17, 9, 30, 0, tzinfo=timezone.utc)
    assert export_v5.serialize_pg_value(dt) == "2026-07-17T09:30:00+00:00"
    assert export_v5.serialize_pg_value(date(2026, 7, 17)) == "2026-07-17"


def test_serialize_decimal_to_float():
    assert export_v5.serialize_pg_value(Decimal("0.012")) == 0.012


def test_serialize_bytes_to_base64():
    import base64
    b = b"\x00\x01\x02\xff"
    assert export_v5.serialize_pg_value(b) == base64.b64encode(b).decode("ascii")


def test_serialize_list_and_dict_recursive():
    out = export_v5.serialize_pg_value(
        {"k": [uuid.UUID("11111111-2222-3333-4444-555555555555"), Decimal("1.5")]})
    assert out == {"k": ["11111111-2222-3333-4444-555555555555", 1.5]}


def test_serialize_unknown_falls_back_to_str():
    class Weird:
        def __str__(self):
            return "weird"
    assert export_v5.serialize_pg_value(Weird()) == "weird"  # never dropped


# ---------------------------------------------------------------------------
# row_to_json mapping
# ---------------------------------------------------------------------------

def test_row_to_json_zips_columns():
    cols = ["id", "email", "is_founder"]
    row = (uuid.UUID("11111111-2222-3333-4444-555555555555"), "a@b.c", True)
    out = export_v5.row_to_json(row, cols)
    assert out == {"id": "11111111-2222-3333-4444-555555555555",
                   "email": "a@b.c", "is_founder": True}


# ---------------------------------------------------------------------------
# CLI guards (no live PG needed)
# ---------------------------------------------------------------------------

def test_main_requires_dsn(monkeypatch):
    monkeypatch.delenv("DATABASE_URL_ADMIN", raising=False)
    assert export_v5.main([]) == 2


def test_main_rejects_unknown_table(monkeypatch):
    monkeypatch.setenv("DATABASE_URL_ADMIN", "postgresql://x/y")
    assert export_v5.main(["--tables", "bogus_table"]) == 2


def test_tables_list_is_the_13():
    assert export_v5.TABLES == [
        "users", "memos", "identity_events", "meaning_events", "entity_events",
        "feeling_events", "entities", "relations", "task_suggestions", "feedback",
        "insight_aggregation", "dlq_messages", "llm_call_logs",
    ]


# ---------------------------------------------------------------------------
# Round-trip: exporter JSON ↔ importer converters agree
# ---------------------------------------------------------------------------

def test_export_then_import_roundtrip_idempotent():
    """A value serialized by the exporter is handled cleanly by the importer's
    converters (the contract that lets JSONL be the intermediate format)."""
    from plugins.memory.ptg.converters import (
        to_bool_int, to_iso8601, to_json_text, to_real, to_uuid_text,
    )
    u = uuid.UUID("D0E0F0A0-B0C0-1234-5678-909090909090")
    dt = datetime(2026, 7, 17, 9, 30, tzinfo=timezone.utc)
    cases = [
        (to_uuid_text, u, "d0e0f0a0-b0c0-1234-5678-909090909090"),
        (to_iso8601, dt, "2026-07-17T09:30:00+00:00"),
        (to_real, Decimal("0.75"), 0.75),
        (to_bool_int, True, 1),
        (to_json_text, {"k": "v"}, '{"k": "v"}'),
    ]
    for conv, pg_val, expected in cases:
        exported = export_v5.serialize_pg_value(pg_val)   # PG → JSON
        assert conv(exported) == expected                  # JSON → SQLite

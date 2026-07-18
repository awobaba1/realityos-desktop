"""PG → SQLite converters — schema-independent unit tests for the V5→V6 migration."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from plugins.memory.ptg.converters import (
    convert_row,
    drop_vector,
    to_bool_int,
    to_int,
    to_iso8601,
    to_json_text,
    to_real,
    to_text,
    to_uuid_text,
)


# ---------------------------------------------------------------------------
# UUID → TEXT
# ---------------------------------------------------------------------------

def test_uuid_object_lowercased():
    u = uuid.UUID("AABBCCDD-EEFF-0011-2233-445566778899")
    assert to_uuid_text(u) == "aabbccdd-eeff-0011-2233-445566778899"


def test_uuid_string_passthrough_lower():
    assert to_uuid_text("D0E0F0A0-B0C0-1234-5678-909090909090") == \
        "d0e0f0a0-b0c0-1234-5678-909090909090"


def test_uuid_none_stays_none():
    assert to_uuid_text(None) is None
    assert to_uuid_text("") is None  # empty → NULL, not ""


def test_uuid_from_bytes():
    b = uuid.uuid4().bytes
    assert to_uuid_text(b) == str(uuid.UUID(bytes=b))


# ---------------------------------------------------------------------------
# datetime → ISO-8601 TEXT
# ---------------------------------------------------------------------------

def test_iso8601_aware_dt():
    dt = datetime(2026, 7, 17, 12, 30, 0, tzinfo=timezone.utc)
    assert to_iso8601(dt) == "2026-07-17T12:30:00+00:00"


def test_iso8601_naive_assumed_utc():
    dt = datetime(2026, 7, 17, 12, 30, 0)  # naive
    out = to_iso8601(dt)
    assert out == "2026-07-17T12:30:00+00:00"


def test_iso8601_date():
    assert to_iso8601(date(2026, 7, 17)) == "2026-07-17"


def test_iso8601_string_passthrough():
    assert to_iso8601("2026-07-17T12:00:00Z") == "2026-07-17T12:00:00Z"
    assert to_iso8601(None) is None
    assert to_iso8601("   ") is None


# ---------------------------------------------------------------------------
# JSONB → TEXT(json)
# ---------------------------------------------------------------------------

def test_json_dict_preserves_cjk():
    out = to_json_text({"name": "张三", "tags": ["a", "b"]})
    assert "张三" in out  # ensure_ascii=False keeps CJK legible
    assert out == '{"name": "张三", "tags": ["a", "b"]}'


def test_json_none_is_null_not_string():
    assert to_json_text(None) is None  # NULL, not 'null'


def test_json_array():
    assert to_json_text([1, 2, 3]) == "[1, 2, 3]"


def test_json_string_already_json_normalized():
    out = to_json_text('{"k": "v"}')
    assert out == '{"k": "v"}'


def test_json_non_json_string_wrapped_not_lost():
    # C2: an unparseable string is wrapped, never dropped.
    out = to_json_text("not json at all")
    assert "not json at all" in out


# ---------------------------------------------------------------------------
# BOOLEAN → INTEGER
# ---------------------------------------------------------------------------

def test_bool_true_false():
    assert to_bool_int(True) == 1
    assert to_bool_int(False) == 0
    assert to_bool_int(None) is None


def test_bool_pg_strings():
    assert to_bool_int("t") == 1
    assert to_bool_int("f") == 0
    assert to_bool_int("TRUE") == 1
    assert to_bool_int("0") == 0


def test_bool_numeric():
    assert to_bool_int(1) == 1
    assert to_bool_int(0) == 0


# ---------------------------------------------------------------------------
# NUMERIC → REAL
# ---------------------------------------------------------------------------

def test_real_decimal():
    assert to_real(Decimal("0.012")) == 0.012


def test_real_int_float():
    assert to_real(3) == 3.0
    assert to_real(2.5) == 2.5


def test_real_numeric_string():
    assert to_real("1.25") == 1.25
    assert to_real(None) is None
    assert to_real("") is None


# ---------------------------------------------------------------------------
# INTEGER
# ---------------------------------------------------------------------------

def test_int_passthrough_and_none():
    assert to_int(3) == 3
    assert to_int(None) is None
    assert to_int("") is None


def test_int_from_float_and_str():
    assert to_int(3.0) == 3
    assert to_int("7") == 7
    from decimal import Decimal
    assert to_int(Decimal("5")) == 5


def test_int_bool_is_int():
    # version-like fields: a bool shouldn't appear, but stay safe → 0/1.
    assert to_int(True) == 1
    assert to_int(False) == 0


# ---------------------------------------------------------------------------
# TEXT passthrough + drop sentinel
# ---------------------------------------------------------------------------

def test_text_passthrough():
    assert to_text("hello") == "hello"
    assert to_text(None) is None
    assert to_text(123) == "123"


def test_drop_vector_returns_omit_sentinel():
    assert drop_vector(b"\x00\x01\x02") is drop_vector(None)  # same sentinel always


# ---------------------------------------------------------------------------
# convert_row orchestration
# ---------------------------------------------------------------------------

def test_convert_row_applies_per_column_converters():
    v5 = {
        "id": uuid.UUID("11111111-2222-3333-4444-555555555555"),
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "meta": {"k": "v"},
        "active": True,
        "score": Decimal("0.75"),
        "embedding": b"\x00" * 2048,
        "name": "alice",
    }
    colmap = [
        ("id", "id", "uuid"),
        ("created_at", "created_at", "iso8601"),
        ("meta", "meta", "json"),
        ("active", "active", "bool"),
        ("score", "score", "real"),
        ("embedding", "embedding", "drop"),
        ("name", "name", "text"),
    ]
    out = convert_row(v5, colmap)
    assert out["id"] == "11111111-2222-3333-4444-555555555555"
    assert out["created_at"] == "2026-01-01T00:00:00+00:00"
    assert out["meta"] == '{"k": "v"}'
    assert out["active"] == 1
    assert out["score"] == 0.75
    assert out["name"] == "alice"
    # Dropped vector column is OMITTED entirely (not None, not present).
    assert "embedding" not in out


def test_convert_row_missing_v5_key_is_omitted_so_schema_default_applies():
    """Regression (real-data 2026-07-18): a V6 column whose V5 key is ABSENT
    (the V5 schema lacks the column — e.g. V6-added ``ser_source``) must be
    OMITTED from the converted row, so the INSERT does not name it and SQLite
    applies the V6 column DEFAULT. The previous behavior emitted ``None``, which
    stored explicit NULL, defeated the DEFAULT, and tripped NOT NULL — rejecting
    100% of real feeling_events/entities rows in the 2026-06-14 dump."""
    v5 = {"id": "x"}
    out = convert_row(v5, [("id", "id", "text"), ("absent", "absent", "json")])
    assert out == {"id": "x"}              # "absent" omitted — not {"absent": None}
    assert "absent" not in out


def test_convert_row_present_but_null_key_still_emits_none():
    """The mirror case: a V5 key that IS present but NULL (a genuinely nullable
    column where this row is null) must still emit None — distinct from a column
    the V5 schema lacks entirely. This keeps nullable columns correct."""
    v5 = {"id": "x", "memo_id": None}
    out = convert_row(v5, [("id", "id", "text"), ("memo_id", "memo_id", "uuid")])
    assert out == {"id": "x", "memo_id": None}

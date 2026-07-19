"""RealityOS V6 sovereignty layer regression tests (§6.1–§6.8, ADR-V6-014).

Locks the four exercise faces + the one legitimate hard-DELETE surface:

  * cascade_soft_delete mode A (never atoms) vs mode B (cascade atoms + edges)
  * the §6.2 grace-window purge (purge_soft_deleted) — the ONLY hard DELETE
  * export_user_data completeness (every C2 table + append-only logs)
  * minor-mode flag (§6.7)
  * consent_tag exercise face (§6.1)
  * C7: every primitive never raises into the caller
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from plugins.memory.ptg.store import PTGStore
from plugins.realityos_sovereignty import (
    MODE_A, MODE_B, cascade_soft_delete, export_user_data,
    export_user_data_json, get_consent_summary, is_minor, purge_soft_deleted,
    set_consent_tag, set_minor_mode,
)


@pytest.fixture
def store(tmp_path):
    s = PTGStore(db_path=str(tmp_path / "ptg.db"))
    s.ensure_founder("u1", "founder@realityos.local")
    yield s
    s.close()


def _seed_window(store, *, base="2026-07-19T10:00:00+00:00"):
    """Seed one memo + atoms in a known window + one outside it."""
    # In-window memo + atoms.
    mid = store.insert_memo(user_id="u1", source_text="w in", input_mode="text",
                            timestamp=base)
    store.insert_identity_event(user_id="u1", source_text="w", person_name="张三",
                                confidence_base=0.9, relation_confidence=0.9,
                                timestamp=base)
    store.insert_meaning_event(user_id="u1", source_text="w", intent_class="Need_To_Do",
                               task_description="交报告", confidence_base=0.8,
                               relation_confidence=0.8, timestamp=base)
    # Out-of-window memo (earlier) — must survive a windowed delete.
    store.insert_memo(user_id="u1", source_text="w out", input_mode="text",
                      timestamp="2026-06-01T00:00:00+00:00")
    return mid


# ---------------------------------------------------------------------------
# §6.2 cascade deletion
# ---------------------------------------------------------------------------

def test_mode_a_soft_deletes_memos_keeps_atoms(store):
    """§6.2 mode A: retire the captured memos, NEVER PTG atoms."""
    _seed_window(store)
    marked = cascade_soft_delete(store, "u1", mode=MODE_A)
    assert marked.get("memos", 0) == 2          # both memos retired
    # atoms survive mode A (the §6.2 invariant).
    assert store.count_rows("identity_events") == 1
    assert store.count_rows("meaning_events") == 1
    assert marked.get("identity_events") is None


def test_mode_b_cascades_memos_atoms_edges(store):
    """§6.2 mode B: memos + window atoms + PTG edges (cascade)."""
    _seed_window(store)
    marked = cascade_soft_delete(store, "u1", mode=MODE_B)
    assert marked.get("memos", 0) == 2
    assert marked.get("identity_events", 0) == 1
    assert marked.get("meaning_events", 0) == 1
    assert store.count_rows("memos") == 0
    assert store.count_rows("identity_events") == 0
    assert store.count_rows("meaning_events") == 0


def test_mode_b_respects_time_window(store):
    """A windowed mode B retires only in-window rows; out-of-window survives."""
    _seed_window(store)
    marked = cascade_soft_delete(
        store, "u1", mode=MODE_B,
        since="2026-07-19T00:00:00+00:00", until="2026-07-19T23:59:59+00:00")
    # 1 in-window memo retired; the June memo survives.
    assert marked.get("memos", 0) == 1
    assert store.count_rows("memos") == 1
    assert marked.get("identity_events", 0) == 1


def test_cascade_soft_delete_is_soft_only(store):
    """Phase 1 = 阶段1: cascade only sets deleted_at; rows still physically exist."""
    _seed_window(store)
    cascade_soft_delete(store, "u1", mode=MODE_B)
    # Soft-deleted rows are excluded from count_rows but still present on disk.
    physical = store.count_rows("memos", include_deleted=True)
    assert physical == 2  # nothing physically gone yet (purge is a separate step)


def test_unknown_mode_raises():
    """Mode validation is the one hard error (a typo must not silently no-op)."""
    with pytest.raises(ValueError):
        # need a store shape only for the signature; the raise fires before any DB use
        cascade_soft_delete(object(), "u1", mode="Z")


# ---------------------------------------------------------------------------
# §6.2 阶段2 purge — the ONE legitimate hard DELETE
# ---------------------------------------------------------------------------

def test_purge_only_removes_grace_expired_rows(store):
    """Hard-DELETE only rows soft-deleted more than older_than_days ago. Fresh
    soft-deletes (within the grace window) survive the purge."""
    _seed_window(store)
    cascade_soft_delete(store, "u1", mode=MODE_B)
    # Backdate the soft-delete to beyond the 1-day grace window.
    with store._lock:
        store._conn.execute(
            "UPDATE memos SET deleted_at = ? WHERE deleted_at IS NOT NULL",
            ("2026-07-01T00:00:00+00:00",))
    before = store.count_rows("memos", include_deleted=True)
    purged = purge_soft_deleted(store, older_than_days=1, tables=["memos"])
    assert purged.get("memos") == 2
    assert store.count_rows("memos", include_deleted=True) == before - 2


def test_purge_keeps_non_softdeleted_rows(store):
    """Purge must NEVER touch living rows (C2)."""
    _seed_window(store)  # nothing soft-deleted
    purged = purge_soft_deleted(store, older_than_days=1)
    assert purged == {}
    assert store.count_rows("memos") == 2  # all living rows intact


def test_purge_never_raises_on_bad_table(store):
    """C7: purge on a bogus table list is swallowed, not propagated."""
    purged = purge_soft_deleted(store, older_than_days=1,
                                tables=["memos", "totally_fake_table"])
    # memos had nothing soft-deleted → empty; the fake table didn't crash it.
    assert "totally_fake_table" not in purged


# ---------------------------------------------------------------------------
# §6.8 export (PIPL §45)
# ---------------------------------------------------------------------------

def test_export_round_trips_all_user_tables(store):
    _seed_window(store)
    store.insert_quality_metric(user_id="u1", metric_date="2026-07-19",
                                metric_type="atom_precision", value=0.7)
    data = export_user_data(store, "u1")
    # Every C2 user table key present (empty list when no rows).
    for t in ("memos", "identity_events", "meaning_events", "quality_metrics",
              "relations", "entities", "tool_events"):
        assert t in data
    assert len(data["memos"]) == 2
    assert data["memos"][0]["source_text"] in ("w in", "w out")
    # Export meta carries the schema version for portability.
    assert int(data["_export_meta"]["schema_version"]) >= 5


def test_export_excludes_soft_deleted(store):
    """Retired rows are NOT exported (they're forgotten, not portable)."""
    _seed_window(store)
    cascade_soft_delete(store, "u1", mode=MODE_A)
    data = export_user_data(store, "u1")
    assert data["memos"] == []


def test_export_json_serializable(store):
    """export_user_data_json produces valid JSON (the one-click file body)."""
    _seed_window(store)
    s = export_user_data_json(store, "u1")
    parsed = json.loads(s)
    assert "_export_meta" in parsed
    assert len(parsed["memos"]) == 2


def test_export_under_5s_for_realistic_scale(store):
    """§6.8 <5s target: a few thousand rows export well under budget (no joins,
    no LLM). Bounds the per-table scan."""
    import time
    for i in range(3000):
        store.insert_memo(user_id="u1", source_text=f"m{i}", input_mode="text")
    t0 = time.monotonic()
    data = export_user_data(store, "u1")
    elapsed = time.monotonic() - t0
    assert len(data["memos"]) == 3000
    assert elapsed < 5.0, f"export took {elapsed:.2f}s (§6.8 <5s budget)"


# ---------------------------------------------------------------------------
# §6.7 minor mode
# ---------------------------------------------------------------------------

def test_minor_mode_toggle_and_read(store):
    assert is_minor(store, "u1") is False        # default adult
    assert set_minor_mode(store, "u1", True) is True
    assert is_minor(store, "u1") is True
    set_minor_mode(store, "u1", False)
    assert is_minor(store, "u1") is False


# ---------------------------------------------------------------------------
# §6.1 consent_tag exercise face
# ---------------------------------------------------------------------------

def test_consent_tag_round_trip(store):
    """Two relations → flip consent_tag → read back via the status summary."""
    # Seed two relations via two entities + edges.
    from plugins.memory.ptg.store import _normalize_entity_name
    # Use the store's entity+relation insert path if present; else direct SQL.
    eid1, eid2 = "e1", "e2"
    with store._lock:
        for eid, name in ((eid1, "张三"), (eid2, "李四")):
            store._conn.execute(
                "INSERT OR IGNORE INTO entities(id, user_id, entity_name, "
                "entity_name_normalized, entity_type) VALUES (?,?,?,?,?)",
                (eid, "u1", name, _normalize_entity_name(name), "person"))
        store._conn.execute(
            "INSERT INTO relations(id, user_id, subject_id, object_id, "
            "relation_type, confidence) VALUES (?,?,?,?,?,?)",
            ("r1", "u1", eid1, eid2, "colleague", 0.8))
        store._conn.execute(
            "INSERT INTO relations(id, user_id, subject_id, object_id, "
            "relation_type, confidence) VALUES (?,?,?,?,?,?)",
            ("r2", "u1", eid1, eid2, "friend", 0.6))

    # NULL consent_tag buckets as the local_only default.
    summary = get_consent_summary(store, "u1")
    assert summary.get("local_only") == 2

    n = set_consent_tag(store, "u1", relation_ids=["r1"], tag="shareable")
    assert n == 1
    summary = get_consent_summary(store, "u1")
    assert summary.get("shareable") == 1
    assert summary.get("local_only") == 1


def test_consent_tag_empty_list_noop(store):
    assert set_consent_tag(store, "u1", relation_ids=[], tag="x") == 0


# ---------------------------------------------------------------------------
# C7: every primitive never raises into the caller
# ---------------------------------------------------------------------------

def test_primitives_fail_open_on_bad_store():
    """A garbage store must not propagate an exception out of any public
    primitive (sovereignty is a user-facing safety surface — never crashes)."""
    bogus = object()  # no _lock / _conn
    # cascade_soft_delete swallows the AttributeError from `with store._lock`.
    assert cascade_soft_delete(bogus, "u1", mode=MODE_A) == {}
    # export builds _export_meta first, then catches the read failure.
    data = export_user_data(bogus, "u1")
    assert "_export_error" in data
    # Predicates return safe defaults, not raises.
    assert is_minor(bogus, "u1") is False
    assert get_consent_summary(bogus, "u1") == {}
    assert set_consent_tag(bogus, "u1", relation_ids=["x"], tag="y") == 0

"""C4 regression: source-text correction + re-extraction (ADR-V6-047 / A4).

Locks the 写后删 closed loop: correct the memo → re-extract → retire OLD atoms
only on success → invalidate insights. A failed re-extraction MUST leave the old
atoms live (C2). Pure-logic coverage; the CLI smoke is in test_memo_cmd.py.
"""

from __future__ import annotations

import pytest

from plugins.memory.ptg.correction import re_extract_memo
from plugins.memory.ptg.store import PTGStore


USER = "u1"


@pytest.fixture
def store(tmp_path):
    s = PTGStore(db_path=str(tmp_path / "ptg.db"))
    s.ensure_founder(USER, "founder@realityos.local")
    yield s
    s.close()


def _memo(store, text="和掌三开会"):
    mid = store.insert_memo(user_id=USER, source_text=text, input_mode="text")
    return mid


def _seed_atoms(store, mid, person="掌三"):
    store.insert_identity_event(
        user_id=USER, source_text="x", person_name=person,
        confidence_base=0.9, relation_confidence=0.9, memo_id=mid)
    store.insert_meaning_event(
        user_id=USER, source_text="x", intent_class="Need_To_Do",
        task_description=f"和{person}开会", atom_kind="R2",
        confidence_base=0.8, relation_confidence=0.8, memo_id=mid)


class _FakeOK:
    """Atomizer double: writes one NEW identity atom, returns ok."""
    def __init__(self, store, new_person="张三"):
        self._s, self._new = store, new_person

    def atomize(self, *, memo_id, source_text, input_mode="text"):
        self._s.insert_identity_event(
            user_id=USER, source_text=source_text, person_name=self._new,
            confidence_base=0.9, relation_confidence=0.9, memo_id=memo_id)
        return {"ok": True, "written": 1}


class _FakeFail:
    def atomize(self, **kw):
        return {"ok": False, "written": 0}


# ===========================================================================
# correct_memo_source_text
# ===========================================================================

class TestCorrectMemo:
    def test_records_corrected_and_bumps_version(self, store):
        mid = _memo(store, "原文")
        r = store.correct_memo_source_text(
            user_id=USER, memo_id=mid, corrected_text="纠正后")
        assert r["ok"] and r["status"] == "corrected" and r["version"] == 2
        row = store._conn.execute(
            "SELECT source_text, corrected_text, version FROM memos WHERE id=?",
            (mid,)).fetchone()
        assert row["source_text"] == "原文"      # C2: original NEVER modified
        assert row["corrected_text"] == "纠正后"
        assert row["version"] == 2

    def test_noop_when_unchanged(self, store):
        mid = _memo(store, "原文")
        # effective text == source_text (no correction yet) → unchanged
        r = store.correct_memo_source_text(
            user_id=USER, memo_id=mid, corrected_text="原文")
        assert r["ok"] and r["status"] == "unchanged"
        assert r["version"] == 1

    def test_noop_when_equals_existing_correction(self, store):
        mid = _memo(store, "原文")
        store.correct_memo_source_text(
            user_id=USER, memo_id=mid, corrected_text="第一次纠正")
        r = store.correct_memo_source_text(
            user_id=USER, memo_id=mid, corrected_text=" 第一次纠正 ")  # whitespace
        assert r["status"] == "unchanged"

    def test_version_conflict(self, store):
        mid = _memo(store, "原文")  # version 1
        r = store.correct_memo_source_text(
            user_id=USER, memo_id=mid, corrected_text="x", expected_version=99)
        assert r["ok"] is False and r["status"] == "version_conflict"
        assert r["version"] == 1

    def test_not_found_and_deleted(self, store):
        assert store.correct_memo_source_text(
            user_id=USER, memo_id="nope", corrected_text="x")["status"] == "not_found"
        mid = _memo(store, "x")
        store.soft_delete("memos", mid)
        assert store.correct_memo_source_text(
            user_id=USER, memo_id=mid, corrected_text="y")["status"] == "deleted"


# ===========================================================================
# snapshot_memo_atom_ids
# ===========================================================================

class TestSnapshot:
    def test_captures_all_four_tables_live_only(self, store):
        mid = _memo(store)
        _seed_atoms(store, mid)  # 1 identity + 1 meaning
        snap = store.snapshot_memo_atom_ids(USER, mid)
        assert len(snap["identity_events"]) == 1
        assert len(snap["meaning_events"]) == 1
        assert snap["feeling_events"] == [] and snap["entity_events"] == []
        # soft-deleted rows are excluded
        store.soft_delete("identity_events", snap["identity_events"][0])
        snap2 = store.snapshot_memo_atom_ids(USER, mid)
        assert snap2["identity_events"] == []

    def test_isolated_to_user(self, store):
        mid = _memo(store)
        _seed_atoms(store, mid)
        # other tenant sees nothing
        assert all(v == [] for v in store.snapshot_memo_atom_ids("u-other", mid).values())


# ===========================================================================
# soft_delete_atom_ids
# ===========================================================================

class TestSoftDeleteAtomIds:
    def test_retires_exact_ids_with_audit(self, store):
        mid = _memo(store)
        _seed_atoms(store, mid)
        snap = store.snapshot_memo_atom_ids(USER, mid)
        n = store.soft_delete_atom_ids(
            user_id=USER, ids_by_table=snap, actor="user", reason="memo_corrected")
        assert n == 2
        # both retired
        live = store._conn.execute(
            "SELECT COUNT(*) FROM identity_events WHERE memo_id=? AND deleted_at IS NULL",
            (mid,)).fetchone()[0]
        assert live == 0
        # deletion_log got 2 rows
        assert store._conn.execute(
            "SELECT COUNT(*) FROM deletion_log WHERE reason LIKE 'memo_corrected%'"
        ).fetchone()[0] == 2

    def test_new_atoms_not_touched(self, store):
        """写后删: only the snapshotted OLD ids retire; a NEW atom (not in the
        snapshot) stays live — this is what disambiguates old from new."""
        mid = _memo(store)
        _seed_atoms(store, mid, person="掌三")
        old = store.snapshot_memo_atom_ids(USER, mid)
        # simulate the re-extract having written a NEW atom AFTER the snapshot
        new_id = store.insert_identity_event(
            user_id=USER, source_text="x", person_name="张三",
            confidence_base=0.9, relation_confidence=0.9, memo_id=mid)
        store.soft_delete_atom_ids(user_id=USER, ids_by_table=old, reason="test")
        # OLD retired, NEW live
        assert store._conn.execute(
            "SELECT deleted_at FROM identity_events WHERE id=?",
            (old["identity_events"][0],)).fetchone()[0] is not None
        assert store._conn.execute(
            "SELECT deleted_at FROM identity_events WHERE id=?", (new_id,)
        ).fetchone()[0] is None

    def test_empty_snapshot_is_noop(self, store):
        n = store.soft_delete_atom_ids(
            user_id=USER, ids_by_table={"identity_events": []}, reason="x")
        assert n == 0


# ===========================================================================
# invalidate_insights
# ===========================================================================

class TestInvalidateInsights:
    def test_sets_expires_at_to_now(self, store):
        store.upsert_insight(
            user_id=USER, aggregation_type="daily", period_key="2026-07-20",
            period_start="2026-07-20", period_end="2026-07-21",
            result_data="x", expires_at="2099-01-01T00:00:00+00:00",
            confidence=0.9, data_sufficiency="sufficient")
        n = store.invalidate_insights(USER)
        assert n == 1
        row = store._conn.execute(
            "SELECT expires_at FROM insight_aggregation WHERE user_id=?", (USER,)).fetchone()
        assert row["expires_at"] < "2099-01-01T00:00:00+00:00"


# ===========================================================================
# re_extract_memo — the closed loop
# ===========================================================================

class TestReExtract:
    def test_happy_writes_new_retires_old(self, store):
        mid = _memo(store, "和掌三开会")
        _seed_atoms(store, mid, person="掌三")
        r = re_extract_memo(store, _FakeOK(store, "张三"),
                            user_id=USER, memo_id=mid, corrected_text="和张三开会")
        assert r["ok"] and r["status"] == "re_extracted"
        assert r["written"] == 1 and r["retired_old"] == 2
        # source preserved, corrected recorded, version 2
        m = store._conn.execute(
            "SELECT source_text, corrected_text, version FROM memos WHERE id=?",
            (mid,)).fetchone()
        assert m["source_text"] == "和掌三开会"
        assert m["corrected_text"] == "和张三开会"
        assert m["version"] == 2
        # OLD identity retired, NEW live
        live = [x["person_name"] for x in store._conn.execute(
            "SELECT person_name FROM identity_events WHERE memo_id=? AND deleted_at IS NULL",
            (mid,))]
        dead = [x["person_name"] for x in store._conn.execute(
            "SELECT person_name FROM identity_events WHERE memo_id=? AND deleted_at IS NOT NULL",
            (mid,))]
        assert live == ["张三"] and dead == ["掌三"]

    def test_failure_keeps_old_atoms(self, store):
        """写后删 invariant: atomize failure → OLD atoms survive."""
        mid = _memo(store, "原文")
        _seed_atoms(store, mid, person="旧人")
        r = re_extract_memo(store, _FakeFail(),
                            user_id=USER, memo_id=mid, corrected_text="新文")
        assert r["ok"] is False and r["status"] == "atomize_failed"
        assert r["retired_old"] == 0
        # OLD atoms still live
        live = store._conn.execute(
            "SELECT COUNT(*) FROM meaning_events WHERE memo_id=? AND deleted_at IS NULL",
            (mid,)).fetchone()[0]
        assert live == 1
        # correction still recorded for next retry
        assert store._conn.execute(
            "SELECT corrected_text FROM memos WHERE id=?", (mid,)).fetchone()[0] == "新文"

    def test_unchanged_short_circuits(self, store):
        mid = _memo(store, "一样")
        r = re_extract_memo(store, _FakeFail(),
                            user_id=USER, memo_id=mid, corrected_text="一样")
        assert r["status"] == "unchanged" and r["ok"] is True

    def test_not_found(self, store):
        r = re_extract_memo(store, _FakeOK(store),
                            user_id=USER, memo_id="nope", corrected_text="x")
        assert r["ok"] is False and r["status"] == "not_found"

    def test_version_conflict_propagates(self, store):
        mid = _memo(store, "x")
        r = re_extract_memo(store, _FakeOK(store),
                            user_id=USER, memo_id=mid, corrected_text="y",
                            expected_version=99)
        assert r["status"] == "version_conflict"

    def test_invalidate_called_on_success(self, store):
        mid = _memo(store, "x")
        _seed_atoms(store, mid)
        store.upsert_insight(
            user_id=USER, aggregation_type="daily", period_key="2026-07-20",
            period_start="2026-07-20", period_end="2026-07-21", result_data="x",
            expires_at="2099-01-01T00:00:00+00:00", confidence=0.9,
            data_sufficiency="sufficient")
        r = re_extract_memo(store, _FakeOK(store, "新人"),
                            user_id=USER, memo_id=mid, corrected_text="y")
        assert r["ok"] and r["invalidated"] == 1

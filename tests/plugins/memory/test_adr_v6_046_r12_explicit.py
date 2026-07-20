"""C4 regression: R12 explicit user pathway (ADR-V6-046 / A3 / F8).

Until now the only way an R12 task-outcome atom existed was the LLM choosing to
extract one from chat. This locks the user-sovereign explicit surface:
``list_open_tasks`` (the #N index basis) + ``mark_task_outcome`` (resolve ref →
promote the R2 row to R12 in place with a version bump, never raise C7).
"""

from __future__ import annotations

import json

import pytest

from plugins.memory.ptg.store import PTGStore


USER = "u1"


@pytest.fixture
def store(tmp_path):
    s = PTGStore(db_path=str(tmp_path / "ptg.db"))
    s.ensure_founder(USER, "founder@realityos.local")
    yield s
    s.close()


def _task(store, desc, *, atom_kind="R2", status="pending", overdue=0):
    return store.insert_meaning_event(
        user_id=USER, source_text="x", intent_class="Need_To_Do",
        task_description=desc, atom_kind=atom_kind, task_status=status,
        is_overdue=overdue, confidence_base=0.8, relation_confidence=0.8)


def _row(store, atom_id):
    return store._conn.execute(
        "SELECT atom_kind, task_status, is_overdue, completion_note, version, "
        "completed_at FROM meaning_events WHERE id=?", (atom_id,)).fetchone()


# ===========================================================================
# list_open_tasks
# ===========================================================================

class TestListOpenTasks:
    def test_only_pending_in_progress_r2_r12(self, store):
        _task(store, "open one")
        _task(store, "open two")
        _task(store, "done", status="completed")
        _task(store, "dismissed", status="dismissed")
        _task(store, "r7 noise", atom_kind="R7")  # not a task atom
        descs = [r["task_description"] for r in store.list_open_tasks(USER)]
        assert set(descs) == {"open one", "open two"}

    def test_ordering_newest_first_is_index_basis(self, store):
        t1 = _task(store, "older", )
        t2 = _task(store, "newer")
        rows = store.list_open_tasks(USER)
        assert rows[0]["id"] == t2   # newest first
        assert rows[1]["id"] == t1
        # the #1 index used by mark_task_outcome must point at the newest
        r = store.mark_task_outcome(user_id=USER, ref="1", outcome="completed")
        assert r["resolved"] and r["atom_id"] == t2

    def test_respects_user_isolation_and_soft_delete(self, store):
        _task(store, "mine")
        # other tenant
        store.ensure_founder("u2", "g@r.local")
        store.insert_meaning_event(
            user_id="u2", source_text="x", intent_class="Need_To_Do",
            task_description="theirs", atom_kind="R2", confidence_base=0.8,
            relation_confidence=0.8)
        assert all(r["task_description"] != "theirs"
                   for r in store.list_open_tasks(USER))

    def test_never_raises_on_store_error(self):
        class _Broken:
            _conn = type("X", (), {"execute": staticmethod(
                lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))})()
            _lock = __import__("threading").RLock()
        assert PTGStore.list_open_tasks(_Broken(), USER) == []


# ===========================================================================
# mark_task_outcome — resolution paths
# ===========================================================================

class TestResolution:
    def test_by_index(self, store):
        _ = _task(store, "older")
        t2 = _task(store, "newer")
        r = store.mark_task_outcome(user_id=USER, ref="1", outcome="completed")
        assert r["ok"] and r["atom_id"] == t2

    def test_by_hash_index(self, store):
        _task(store, "x")
        r = store.mark_task_outcome(user_id=USER, ref="#1", outcome="completed")
        assert r["ok"]

    def test_by_exact_id_uuid(self, store):
        tid = _task(store, "the task")
        r = store.mark_task_outcome(user_id=USER, ref=tid, outcome="completed")
        assert r["ok"] and r["atom_id"] == tid

    def test_by_substring_unique(self, store):
        _task(store, "交季度报告")
        r = store.mark_task_outcome(user_id=USER, ref="季度报告", outcome="completed")
        assert r["ok"]

    def test_substring_ambiguous_not_resolved(self, store):
        _task(store, "报告 A")
        _task(store, "报告 B")
        r = store.mark_task_outcome(user_id=USER, ref="报告", outcome="completed")
        assert r["resolved"] is False

    def test_not_found(self, store):
        r = store.mark_task_outcome(user_id=USER, ref="不存在", outcome="completed")
        assert r["ok"] is False and r["resolved"] is False

    def test_empty_ref(self, store):
        r = store.mark_task_outcome(user_id=USER, ref="", outcome="completed")
        assert r["ok"] is False


# ===========================================================================
# mark_task_outcome — mutation contract
# ===========================================================================

class TestMutation:
    def test_promotes_r2_to_r12_in_place(self, store):
        tid = _task(store, "ship it")
        assert _row(store, tid)["atom_kind"] == "R2"
        store.mark_task_outcome(user_id=USER, ref=tid, outcome="completed")
        assert _row(store, tid)["atom_kind"] == "R12"

    def test_outcome_status_map_matches_atomizer(self, store):
        """completed→completed, failed→dismissed, delayed→pending+overdue."""
        tid = _task(store, "t1")
        store.mark_task_outcome(user_id=USER, ref=tid, outcome="completed")
        assert _row(store, tid)["task_status"] == "completed"

        tid = _task(store, "t2")
        store.mark_task_outcome(user_id=USER, ref=tid, outcome="failed")
        row = _row(store, tid)
        assert row["task_status"] == "dismissed" and row["is_overdue"] == 0

        tid = _task(store, "t3")
        store.mark_task_outcome(user_id=USER, ref=tid, outcome="delayed")
        row = _row(store, tid)
        assert row["task_status"] == "pending" and row["is_overdue"] == 1

    def test_version_bumped_and_completed_at_set(self, store):
        tid = _task(store, "t")
        store.mark_task_outcome(user_id=USER, ref=tid, outcome="completed",
                                resolution_note="done at 5pm")
        row = _row(store, tid)
        assert row["version"] == 2
        assert row["completed_at"] is not None
        note = json.loads(row["completion_note"])
        assert note["outcome"] == "completed"
        assert note["actor"] == "user"
        assert note["resolution_note"] == "done at 5pm"
        assert "changed_at" in note

    def test_failed_does_not_stamp_completed_at(self, store):
        tid = _task(store, "t")
        store.mark_task_outcome(user_id=USER, ref=tid, outcome="failed")
        assert _row(store, tid)["completed_at"] is None

    def test_re_mark_closed_row_by_exact_id(self, store):
        """Re-marking an already-closed task (e.g. correction) works via uuid path."""
        tid = _task(store, "t")
        store.mark_task_outcome(user_id=USER, ref=tid, outcome="completed")
        # user changes their mind → failed
        r = store.mark_task_outcome(user_id=USER, ref=tid, outcome="failed")
        assert r["ok"]
        assert _row(store, tid)["task_status"] == "dismissed"

    def test_completed_drops_off_open_list(self, store):
        tid = _task(store, "do it")
        assert len(store.list_open_tasks(USER)) == 1
        store.mark_task_outcome(user_id=USER, ref=tid, outcome="completed")
        assert len(store.list_open_tasks(USER)) == 0

    def test_delayed_stays_open_with_overdue(self, store):
        tid = _task(store, "later")
        store.mark_task_outcome(user_id=USER, ref=tid, outcome="delayed")
        open_rows = store.list_open_tasks(USER)
        assert len(open_rows) == 1 and open_rows[0]["is_overdue"] == 1


# ===========================================================================
# diagnostics + C7
# ===========================================================================

class TestDiagnostics:
    def test_bad_outcome_rejected(self, store):
        _task(store, "t")
        r = store.mark_task_outcome(user_id=USER, ref="1", outcome="maybe")
        assert r["ok"] is False

    def test_unknown_table_rowcount_zero_message(self, store):
        """Exact-id that matches no row → resolved False (not a crash)."""
        r = store.mark_task_outcome(
            user_id=USER, ref="00000000-0000-0000-0000-000000000000",
            outcome="completed")
        assert r["ok"] is False and r["resolved"] is False

    def test_message_is_user_visible_chinese(self, store):
        _task(store, "交报告")
        r = store.mark_task_outcome(user_id=USER, ref="1", outcome="completed")
        assert "已完成" in r["message"]

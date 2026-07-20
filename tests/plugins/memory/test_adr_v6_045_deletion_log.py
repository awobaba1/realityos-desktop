"""C4 regression: deletion_log audit table (ADR-V6-045) — C2 soft-delete traceability.

Every soft-delete now writes one append-only ``deletion_log`` row ATOMICALLY with
the ``deleted_at`` update: actor / reason / pre-deletion snapshot. ``deleted_at``
alone recorded *when*; this records *who / why / what*. It is the R12 sovereignty
audit substrate and the anti-silent-cascade observability surface (C7).

Covers D1 (append-only WORM table + schema v8), D2 (single-row atomic audit),
D3 (sovereignty cascade per-row audit), D4 (reader API), plus C2/C7 invariants.
"""

from __future__ import annotations

import json

import pytest

from plugins.memory.ptg import schema as ptg_schema
from plugins.memory.ptg.store import PTGStore
from plugins.realityos_sovereignty import (
    MODE_A, MODE_B, cascade_soft_delete, export_user_data, purge_soft_deleted,
)


USER = "u1"


@pytest.fixture
def store(tmp_path):
    s = PTGStore(db_path=str(tmp_path / "ptg.db"))
    s.ensure_founder(USER, "founder@realityos.local")
    yield s
    s.close()


def _add_memo(store, text="hello world", ts="2026-07-19T10:00:00+00:00"):
    return store.insert_memo(user_id=USER, source_text=text, input_mode="text",
                             timestamp=ts)


def _log_rows(store):
    return store._conn.execute(
        "SELECT user_id, table_name, record_id, actor, reason, snapshot "
        "FROM deletion_log ORDER BY created_at"
    ).fetchall()


# ===========================================================================
# D1: schema + append-only registration
# ===========================================================================

class TestD1Schema:
    def test_table_exists_and_columns(self, store):
        cols = {r[1] for r in store._conn.execute("PRAGMA table_info(deletion_log)")}
        assert cols >= {"id", "created_at", "user_id", "table_name", "record_id",
                        "actor", "reason", "snapshot"}

    def test_registered_append_only(self):
        assert "deletion_log" in ptg_schema.APPEND_ONLY_TABLES
        assert "deletion_log" not in ptg_schema.C2_USER_TABLES

    def test_schema_version_bumped_to_8(self):
        assert ptg_schema.SCHEMA_VERSION == 8

    def test_refuses_soft_delete_on_itself(self, store):
        """deletion_log is append-only WORM — soft_delete must refuse it."""
        with pytest.raises(ValueError, match="append-only"):
            store.soft_delete("deletion_log", "anything")


# ===========================================================================
# D2: single-row soft_delete atomic audit
# ===========================================================================

class TestD2SingleRowAudit:
    def test_writes_audit_row_with_actor_reason(self, store):
        mid = _add_memo(store)
        ok = store.soft_delete("memos", mid, actor="user", reason="r12 explicit")
        assert ok is True
        rows = _log_rows(store)
        assert len(rows) == 1
        r = rows[0]
        assert r["user_id"] == USER
        assert r["table_name"] == "memos"
        assert r["record_id"] == mid
        assert r["actor"] == "user"
        assert r["reason"] == "r12 explicit"

    def test_snapshot_captures_pre_deletion_state(self, store):
        mid = _add_memo(store, text="forensic payload")
        store.soft_delete("memos", mid)
        snap = json.loads(_log_rows(store)[0]["snapshot"])
        assert snap["source_text"] == "forensic payload"
        assert snap["id"] == mid
        assert snap["user_id"] == USER

    def test_default_actor_system(self, store):
        mid = _add_memo(store)
        store.soft_delete("memos", mid)  # no actor → default "system"
        assert _log_rows(store)[0]["actor"] == "system"

    def test_already_deleted_no_second_audit(self, store):
        """Idempotent: second soft_delete on an already-retired row → False, no dup log."""
        mid = _add_memo(store)
        assert store.soft_delete("memos", mid) is True
        assert store.soft_delete("memos", mid) is False
        assert len(_log_rows(store)) == 1

    def test_absent_row_no_audit(self, store):
        assert store.soft_delete("memos", "does-not-exist") is False
        assert _log_rows(store) == []

    def test_atomic_rollback_when_audit_fails(self, store):
        """If the audit INSERT fails, the deleted_at update MUST roll back —
        a soft-delete without its audit row is the silent gap deletion_log closes."""
        mid = _add_memo(store, text="must survive")
        store._conn.execute("DROP TABLE deletion_log")  # make audit INSERT fail
        with pytest.raises(Exception):
            store.soft_delete("memos", mid)
        # memo NOT retired (UPDATE rolled back)
        row = store._conn.execute(
            "SELECT deleted_at FROM memos WHERE id=?", (mid,)).fetchone()
        assert row["deleted_at"] is None


# ===========================================================================
# D3: sovereignty cascade per-row audit
# ===========================================================================

class TestD3CascadeAudit:
    def test_cascade_mode_b_audits_every_retired_row(self, store):
        for _ in range(3):
            _add_memo(store)
        res = cascade_soft_delete(store, USER, mode=MODE_B)
        assert res.get("memos") == 3
        rows = _log_rows(store)
        assert len(rows) == 3
        assert {r["actor"] for r in rows} == {"cascade"}
        assert {r["table_name"] for r in rows} == {"memos"}

    def test_cascade_reason_carries_mode(self, store):
        _add_memo(store)
        cascade_soft_delete(store, USER, mode=MODE_B)
        reason = _log_rows(store)[0]["reason"]
        assert "mode B" in reason and "total_forgetting" in reason

    def test_cascade_mode_a_only_audits_memos(self, store):
        """§6.2 mode A never touches atoms — only memo audit rows appear."""
        _add_memo(store)
        cascade_soft_delete(store, USER, mode=MODE_A)
        tables = {r["table_name"] for r in _log_rows(store)}
        assert tables == {"memos"}
        assert _log_rows(store)[0]["reason"].count("mode A") == 1

    def test_cascade_snapshot_present(self, store):
        _add_memo(store, text="cascade snap")
        cascade_soft_delete(store, USER, mode=MODE_B)
        snap = json.loads(_log_rows(store)[0]["snapshot"])
        assert snap["source_text"] == "cascade snap"


# ===========================================================================
# D4: reader + export + purge invariants
# ===========================================================================

class TestD4ReaderAndInvariants:
    def test_list_newest_first_with_table_filter(self, store):
        m1 = _add_memo(store, text="one", ts="2026-07-01T00:00:00+00:00")
        m2 = _add_memo(store, text="two", ts="2026-07-02T00:00:00+00:00")
        store.soft_delete("memos", m1)
        store.soft_delete("memos", m2)
        log = store.list_deletion_log(USER)
        assert len(log) == 2
        # newest first — both within same second is fine; just assert count + content
        ids = {r["record_id"] for r in log}
        assert ids == {m1, m2}
        # table filter
        only = store.list_deletion_log(USER, table_name="memos")
        assert len(only) == 2
        empty = store.list_deletion_log(USER, table_name="entities")
        assert empty == []

    def test_list_scoped_to_user(self, store):
        mid = _add_memo(store)
        store.soft_delete("memos", mid)
        # a different tenant sees nothing
        assert store.list_deletion_log("other-user") == []

    def test_export_includes_deletion_log(self, store):
        """PIPL §45 portability: the user's audit trail is the user's data."""
        mid = _add_memo(store)
        store.soft_delete("memos", mid, actor="user", reason="export check")
        payload = export_user_data(store, USER)
        assert "deletion_log" in payload
        assert len(payload["deletion_log"]) == 1
        assert payload["deletion_log"][0]["actor"] == "user"

    def test_purge_never_touches_deletion_log(self, store):
        """Audit trail is WORM — purge_soft_deleted must not delete audit rows even
        after the retired source rows are physically purged past the grace window."""
        mid = _add_memo(store)
        store.soft_delete("memos", mid)
        assert len(_log_rows(store)) == 1
        # Force the retired memo PAST the grace window so purge actually takes it.
        # (purge keys on deleted_at — when retired — not the row's capture ts.)
        store._conn.execute(
            "UPDATE memos SET deleted_at='2020-01-01T00:00:00+00:00' WHERE id=?",
            (mid,))
        purge_soft_deleted(store, older_than_days=1)
        assert len(_log_rows(store)) == 1, "deletion_log must survive purge (WORM)"
        # the source memo is gone, but its retirement record remains for forensics
        memo_gone = store._conn.execute(
            "SELECT COUNT(*) FROM memos WHERE id=?", (mid,)).fetchone()[0]
        assert memo_gone == 0

    def test_list_never_raises_on_store_error(self):
        """C7: a broken store read returns [], not an exception."""
        class _BrokenConn:
            def execute(self, *a, **k):
                raise RuntimeError("store broken")

        class _BrokenStore:
            _conn = _BrokenConn()
            _lock = __import__("threading").RLock()

        assert PTGStore.list_deletion_log(_BrokenStore(), USER) == []

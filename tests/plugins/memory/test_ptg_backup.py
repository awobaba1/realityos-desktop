"""RealityOS V6 PTG backup + restore-drill regression tests (ADR-V6-012, §6.9).

The whole point of verify_backup/restore_drill is the C7 anti-fake-green
contract: a backup that silently lost rows, lost a table, or is corrupt MUST be
reported as failed, never green. These tests lock that contract.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from plugins.memory.ptg.backup import (
    backup_ptg, restore_drill, verify_backup, _stamp,
)
from plugins.memory.ptg.store import PTGStore


def _utc(hour: int) -> datetime:
    return datetime(2026, 7, 19, hour, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path):
    s = PTGStore(db_path=str(tmp_path / "ptg.db"))
    s.ensure_founder("user-1", "founder@realityos.local")
    yield s
    s.close()


def _seed(store, *, memos=2, events=True):
    for i in range(memos):
        store.insert_memo(user_id="user-1", source_text=f"memo {i}", input_mode="text")
    if events:
        store.insert_identity_event(user_id="user-1", source_text="x", person_name="张三",
                                    confidence_base=0.9, relation_confidence=0.9)
        store.insert_meaning_event(user_id="user-1", source_text="x", intent_class="Need_To_Do",
                                   task_description="交报告", confidence_base=0.8,
                                   relation_confidence=0.8)


def _live_counts(store):
    with store._lock:
        return {
            "memos": store._conn.execute(
                "SELECT COUNT(*) FROM memos WHERE deleted_at IS NULL").fetchone()[0],
            "identity_events": store._conn.execute(
                "SELECT COUNT(*) FROM identity_events WHERE deleted_at IS NULL").fetchone()[0],
            "meaning_events": store._conn.execute(
                "SELECT COUNT(*) FROM meaning_events WHERE deleted_at IS NULL").fetchone()[0],
        }


def test_backup_creates_valid_file_with_matching_counts(store, tmp_path):
    _seed(store)
    path = backup_ptg(store, tmp_path / "backups", now=_utc(10))
    assert path.is_file()
    report = verify_backup(path, expected_counts=_live_counts(store))
    assert report["ok"] is True
    assert report["counts"]["memos"] == 2
    assert report["counts"]["identity_events"] == 1
    assert report["error"] is None


def test_backup_respects_soft_delete(store, tmp_path):
    mid = store.insert_memo(user_id="user-1", source_text="doomed", input_mode="text")
    store.insert_memo(user_id="user-1", source_text="kept", input_mode="text")
    store.soft_delete("memos", mid)                      # C2 soft delete
    path = backup_ptg(store, tmp_path / "bk", now=_utc(1))
    report = verify_backup(path)
    assert report["counts"]["memos"] == 1               # deleted row excluded


def test_verify_backup_flags_missing_file(tmp_path):
    report = verify_backup(tmp_path / "nope.db")
    assert report["ok"] is False
    assert "not found" in report["error"]


def test_verify_backup_flags_corrupt_file(tmp_path):
    bad = tmp_path / "corrupt.db"
    bad.write_bytes(b"not a sqlite database at all")
    report = verify_backup(bad)
    assert report["ok"] is False
    assert report["error"]                              # unreadable → error set, not green


def test_verify_backup_flags_missing_table(store, tmp_path):
    _seed(store)
    path = backup_ptg(store, tmp_path / "bk", now=_utc(2))
    # Simulate a backup that lost a table (partial/corrupt restore).
    conn = sqlite3.connect(str(path))
    conn.execute("DROP TABLE entities")
    conn.commit()
    conn.close()
    report = verify_backup(path, expected_counts=_live_counts(store))
    assert report["ok"] is False
    assert any("entities" in m for m in report["mismatches"])


def test_prune_keeps_n_newest(store, tmp_path):
    dest = tmp_path / "rolling"
    for h in range(5):                                  # 5 backups
        backup_ptg(store, dest, keep=3, now=_utc(h))
    files = sorted(p.name for p in dest.iterdir() if p.is_file())
    assert len(files) == 3                              # pruned to keep=3
    # newest 3 kept (hours 2,3,4), oldest 2 (hours 0,1) pruned.
    assert _stamp(_utc(4)) in files
    assert _stamp(_utc(0)) not in files


def test_restore_drill_happy(store, tmp_path):
    _seed(store)
    path = backup_ptg(store, tmp_path / "bk", now=_utc(3))
    report = restore_drill(store, path)
    assert report["ok"] is True
    assert report["counts"]["memos"] == 2


def test_restore_drill_catches_overcount_corruption(store, tmp_path):
    _seed(store, memos=1)
    path = backup_ptg(store, tmp_path / "bk", now=_utc(4))
    # Corrupt: inject a row into the backup so it claims MORE memos than live.
    conn = sqlite3.connect(str(path))
    conn.execute(
        "INSERT INTO memos (id, user_id, input_mode, source_text, timestamp) "
        "VALUES ('ghost', 'user-1', 'text', 'fake', '2026-07-19T00:00:00+00:00')")
    conn.commit()
    conn.close()
    report = restore_drill(store, path)
    assert report["ok"] is False                        # backup > live → caught
    assert any("memos" in m for m in report["mismatches"])

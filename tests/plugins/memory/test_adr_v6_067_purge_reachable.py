"""C4 regression: purge reachability + honest doc (ADR-V6-067).

Locks the two issues the third-round audit found on ``purge_soft_deleted``:
  1. It was ORPHAN CODE — zero non-test callers — while comments in
     sovereignty.py + web_server.py falsely claimed a "nightly cron" ran it
     (documentation fake-green + 做了没发, ADR-V6-037's most-fatal class).
  2. ``hermes purge`` is the sole production caller that closes that loop.

Safety contract: hard-DELETE is the single C2 exception. DRY-RUN IS THE
DEFAULT (counts eligible rows, deletes nothing); ``--confirm`` executes.
The grace window (``--older-than-days``) is honoured — a recently soft-deleted
row is NOT purged.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.memory.ptg.store import PTGStore
from plugins.realityos_sovereignty.sovereignty import purge_soft_deleted

USER = "u1"


def _seed_old_soft_deleted(db_path, *, days_old=40):
    """Create a tmp db with one memo soft-deleted ``days_old`` days ago."""
    s = PTGStore(db_path=db_path)
    s.ensure_founder(USER, "founder@realityos.local")
    mid = s.insert_memo(user_id=USER, source_text="stale memo", input_mode="text")
    with s._lock:
        s._conn.execute(
            "UPDATE memos SET deleted_at=datetime('now', ?) WHERE id=?",
            (f"-{days_old} days", mid))
    s.close()
    return mid


def _memo_exists(db_path, mid):
    s = PTGStore(db_path=db_path)
    try:
        with s._lock:
            row = s._conn.execute(
                "SELECT id FROM memos WHERE id=?", (mid,)).fetchone()
        return row is not None
    finally:
        s.close()


# ===========================================================================
# Primitive: dry_run counts without deleting
# ===========================================================================

class TestPurgePrimitiveDryRun:
    def test_dry_run_counts_without_deleting(self, tmp_path):
        db = str(tmp_path / "ptg.db")
        mid = _seed_old_soft_deleted(db)
        s = PTGStore(db_path=db)
        try:
            counts = purge_soft_deleted(s, older_than_days=30, dry_run=True)
        finally:
            s.close()
        assert counts.get("memos", 0) >= 1
        # dry-run must NOT have removed the row
        assert _memo_exists(db, mid)

    def test_execute_deletes(self, tmp_path):
        db = str(tmp_path / "ptg.db")
        mid = _seed_old_soft_deleted(db)
        s = PTGStore(db_path=db)
        try:
            counts = purge_soft_deleted(s, older_than_days=30, dry_run=False)
        finally:
            s.close()
        assert counts.get("memos", 0) >= 1
        assert not _memo_exists(db, mid)

    def test_grace_window_respected(self, tmp_path):
        """A row soft-deleted only 1 day ago must NOT purge at older_than=30."""
        db = str(tmp_path / "ptg.db")
        mid = _seed_old_soft_deleted(db, days_old=1)
        s = PTGStore(db_path=db)
        try:
            counts = purge_soft_deleted(s, older_than_days=30, dry_run=False)
        finally:
            s.close()
        assert counts.get("memos", 0) == 0  # within grace window — untouched
        assert _memo_exists(db, mid)


# ===========================================================================
# CLI: hermes purge — dry-run default, --confirm executes
# ===========================================================================

class TestPurgeCli:
    def _args(self, **kw):
        base = dict(confirm=False, older_than_days=30, tables=None, as_json=False)
        base.update(kw)
        return SimpleNamespace(**base)

    def _wire(self, monkeypatch, db):
        from hermes_cli import purge_cmd
        monkeypatch.setattr(purge_cmd, "load_ptg_config", lambda: {})
        monkeypatch.setattr(purge_cmd, "resolve_db_path", lambda cfg: db)
        return purge_cmd

    def test_default_is_dry_run_no_delete(self, tmp_path, monkeypatch, capsys):
        db = str(tmp_path / "ptg.db")
        mid = _seed_old_soft_deleted(db)
        purge_cmd = self._wire(monkeypatch, db)
        rc = purge_cmd.cmd_purge(self._args())
        out = capsys.readouterr().out
        assert rc == 0
        assert "DRY-RUN" in out
        assert _memo_exists(db, mid)  # default must NOT delete

    def test_confirm_executes_hard_delete(self, tmp_path, monkeypatch, capsys):
        db = str(tmp_path / "ptg.db")
        mid = _seed_old_soft_deleted(db)
        purge_cmd = self._wire(monkeypatch, db)
        rc = purge_cmd.cmd_purge(self._args(confirm=True))
        out = capsys.readouterr().out
        assert rc == 0
        assert "硬删执行" in out
        assert not _memo_exists(db, mid)

    def test_json_output(self, tmp_path, monkeypatch, capsys):
        db = str(tmp_path / "ptg.db")
        _seed_old_soft_deleted(db)
        purge_cmd = self._wire(monkeypatch, db)
        rc = purge_cmd.cmd_purge(self._args(as_json=True))
        out = capsys.readouterr().out
        assert rc == 0
        payload = json.loads(out)
        assert payload["dry_run"] is True
        assert payload["older_than_days"] == 30
        assert payload["total"] >= 1


# ===========================================================================
# Reachability contract + anti-fake-green static guard
# ===========================================================================

class TestPurgeReachability:
    def test_purge_in_builtin_subcommands(self):
        """``hermes purge`` must be a registered builtin (the reachability
        contract — without this the parser never wires it and purge stays
        orphan despite the handler existing)."""
        from hermes_cli.main import _BUILTIN_SUBCOMMANDS
        assert "purge" in _BUILTIN_SUBCOMMANDS

    def test_no_comment_claims_false_nightly_cron(self):
        """Static anti-fake-green guard (wrap-lucky-green lesson): the specific
        false 'nightly cron' assertions R flagged must be gone. Whitespace-
        normalized so a line-wrapped regression can't slip past."""
        import plugins.realityos_sovereignty.sovereignty as sov_mod
        import hermes_cli.web_server as ws_mod
        sov_src = re.sub(r"\s+", " ", Path(sov_mod.__file__).read_text()).lower()
        ws_src = re.sub(r"\s+", " ", Path(ws_mod.__file__).read_text()).lower()
        # the exact false claims (not the honest "no scheduler is wired" wording
        # that replaces them) must not survive.
        assert "nightly cron (phase 1+) calls this" not in sov_src
        assert "is a separate opt-in nightly cron" not in ws_src

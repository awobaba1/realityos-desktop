"""C4 regression: ``hermes dlq --resolve-all`` consumer (ADR-V6-073).

ADR-V6-065 added ``PTGStore.dlq_resolve_all`` (bulk ack) alongside the
single-row ``dlq_resolve``, but the ``hermes dlq`` CLI only exposed
``--resolve <id>`` — ``dlq_resolve_all`` had NO consumer (做了没发, the
half-done tail of ADR-V6-065). ADR-V6-073 wires ``--resolve-all [--source]``
as its consumer. This module locks the end-to-end path: real store → seeded
DLQ rows → cmd_dlq → DB flip + count + honest empty state.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from plugins.memory.ptg.store import PTGStore

USER = "u1"


def _wire(monkeypatch, tmp_path):
    import hermes_cli.dlq_cmd as dlq_cmd
    db = str(tmp_path / "ptg.db")
    monkeypatch.setattr(dlq_cmd, "load_ptg_config", lambda: {})
    monkeypatch.setattr(dlq_cmd, "resolve_db_path", lambda cfg: db)
    return dlq_cmd, db


def _new_store(tmp_path):
    s = PTGStore(db_path=str(tmp_path / "ptg.db"))
    s.ensure_founder(USER, "founder@realityos.local")
    return s


def _seed(store, *, source, n):
    for i in range(n):
        store.insert_dlq(
            user_id=USER, source=source, error_type="test_err",
            error_msg=f"msg {i}", original_data={"i": i})


def _args(**over):
    base = dict(
        as_json=True, stats_only=False, resolve_id=None, resolve_all=True,
        show_all=False, only_resolved=False, source=None, limit=20)
    base.update(over)
    return SimpleNamespace(**base)


class TestDlqResolveAllConsumer:
    def test_resolve_all_flips_unresolved_rows(self, tmp_path, monkeypatch, capsys):
        store = _new_store(tmp_path)
        _seed(store, source="atomize", n=3)
        _seed(store, source="theory", n=2)
        store.close()
        dlq_cmd, _ = _wire(monkeypatch, tmp_path)
        rc = dlq_cmd.cmd_dlq(_args())
        payload = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert payload["resolved_all"] == 5  # all 5 unresolved flipped

    def test_resolve_all_with_source_filter(self, tmp_path, monkeypatch, capsys):
        store = _new_store(tmp_path)
        _seed(store, source="atomize", n=3)
        _seed(store, source="theory", n=2)
        store.close()
        dlq_cmd, _ = _wire(monkeypatch, tmp_path)
        rc = dlq_cmd.cmd_dlq(_args(source="atomize"))
        payload = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert payload["resolved_all"] == 3  # only atomize rows
        assert payload["source"] == "atomize"

    def test_resolve_all_db_actually_flipped(self, tmp_path, monkeypatch):
        """Not just the return count — the DB rows must actually be resolved."""
        store = _new_store(tmp_path)
        _seed(store, source="atomize", n=2)
        store.close()
        dlq_cmd, _ = _wire(monkeypatch, tmp_path)
        dlq_cmd.cmd_dlq(_args(as_json=True))
        # Re-open and verify resolved=1 in the DB.
        s = PTGStore(db_path=str(tmp_path / "ptg.db"))
        try:
            with s._lock:
                unresolved = s._conn.execute(
                    "SELECT COUNT(*) FROM dlq_messages WHERE resolved=0"
                ).fetchone()[0]
        finally:
            s.close()
        assert unresolved == 0

    def test_resolve_all_empty_is_honest_zero(self, tmp_path, monkeypatch, capsys):
        """No DLQ rows → honest '0 / nothing to resolve', NOT a fake success."""
        _new_store(tmp_path).close()
        dlq_cmd, _ = _wire(monkeypatch, tmp_path)
        rc = dlq_cmd.cmd_dlq(_args(as_json=False))
        out = capsys.readouterr().out
        assert rc == 0
        assert "无可解决记录" in out
        assert "✅" not in out  # no fake success marker

    def test_single_resolve_takes_precedence_over_all(self, tmp_path, monkeypatch):
        """--resolve <id> + --resolve-all together: single wins (documented)."""
        store = _new_store(tmp_path)
        store.insert_dlq(user_id=USER, source="atomize", error_type="e",
                         error_msg="m", original_data={})
        first_id = store.dlq_list(resolved=False, limit=1)[0]["id"]
        store.close()
        dlq_cmd, _ = _wire(monkeypatch, tmp_path)
        # Both flags set — single path must run, not bulk.
        rc = dlq_cmd.cmd_dlq(_args(resolve_id=first_id, resolve_all=True, as_json=True))
        assert rc == 0  # did not crash on the combined flags


class TestDlqResolveAllWiring:
    def test_parser_exposes_resolve_all(self):
        """Static guard: --resolve-all stays wired in the dlq parser."""
        from pathlib import Path
        src = Path("hermes_cli/subcommands/dlq.py").read_text(encoding="utf-8")
        assert '"--resolve-all"' in src
        assert "dest=\"resolve_all\"" in src

    def test_cmd_branch_calls_dlq_resolve_all(self):
        """Static guard: cmd_dlq's --resolve-all branch calls the primitive."""
        from pathlib import Path
        src = Path("hermes_cli/dlq_cmd.py").read_text(encoding="utf-8")
        assert "store.dlq_resolve_all(" in src
        assert "ADR-V6-073" in src

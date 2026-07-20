"""C4 regression: write-only-triplet read surface (ADR-V6-070).

The fourth-round audit (ADR-037 维度) found a triplet of tables that all had
real producers + an ADR-added read API + tests — but ZERO CLI/API/UI consumer
(做了没发, ADR-V6-037's most-fatal fake-green class):

  * ``deletion_log``   — read API ``list_deletion_log`` (ADR-V6-045), no caller
  * ``tool_events``    — read API ``recent_tool_events``, no caller
  * ``quality_metrics``— read API ``recent_quality_metrics`` (ADR-V6-028), no caller

``hermes trail`` is the consumer. This module locks reachability (4-point
wiring + runtime read of each table) + honest empty/seeded output + a static
anti-fake-green guard that the consumer cannot be silently unwired.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from plugins.memory.ptg.store import PTGStore

USER = "u1"


def _new_store(tmp_path):
    s = PTGStore(db_path=str(tmp_path / "ptg.db"))
    s.ensure_founder(USER, "founder@realityos.local")
    return s


def _seed_all(store):
    """Write one row into each of the three write-only tables."""
    store.log_deletion(
        user_id=USER, table_name="meaning_events", record_id="atom-xyz",
        actor="founder", reason="cascade_soft_delete", snapshot={"k": "v"})
    store.insert_tool_event(
        user_id=USER, tool_name="search", status="ok", duration_ms=42)
    store.insert_quality_metric(
        user_id=USER, metric_date="2026-07-21", metric_type="atom_precision",
        value=0.84, atom_type="R3", sample_size=12)


def _wire(monkeypatch, tmp_path, db_path=None):
    """Monkeypatch trail_cmd's config resolvers to point at a tmp DB."""
    import hermes_cli.trail_cmd as trail_cmd
    db = db_path or str(tmp_path / "ptg.db")
    monkeypatch.setattr(trail_cmd, "load_ptg_config", lambda: {})
    monkeypatch.setattr(trail_cmd, "resolve_db_path", lambda cfg: db)
    return trail_cmd


def _args(**over):
    """Build a trail args namespace; defaults exercise the cmd's happy path."""
    base = dict(
        trail_type=None, limit=20, as_json=True, user_id=USER,
        table_filter=None, tool_filter=None, metric_filter=None)
    base.update(over)
    return SimpleNamespace(**base)


# ===========================================================================
# Reachability — each write-only table now has a CLI consumer
# ===========================================================================

class TestTrailReachability:
    def test_trail_in_builtin_subcommands(self):
        from hermes_cli.main import _BUILTIN_SUBCOMMANDS
        assert "trail" in _BUILTIN_SUBCOMMANDS

    def test_cmd_trail_reads_deletion_log(self, tmp_path, monkeypatch, capsys):
        store = _new_store(tmp_path)
        _seed_all(store)
        store.close()
        trail_cmd = _wire(monkeypatch, tmp_path)
        rc = trail_cmd.cmd_trail(_args(trail_type="deletion"))
        out = capsys.readouterr().out
        assert rc == 0
        payload = json.loads(out)
        assert payload["type"] == "deletion"
        assert any(r["table_name"] == "meaning_events" for r in payload["rows"])

    def test_cmd_trail_reads_tool_events(self, tmp_path, monkeypatch, capsys):
        store = _new_store(tmp_path)
        _seed_all(store)
        store.close()
        trail_cmd = _wire(monkeypatch, tmp_path)
        rc = trail_cmd.cmd_trail(_args(trail_type="tool"))
        payload = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert any(r["tool_name"] == "search" for r in payload["rows"])

    def test_cmd_trail_reads_quality_metrics(self, tmp_path, monkeypatch, capsys):
        store = _new_store(tmp_path)
        _seed_all(store)
        store.close()
        trail_cmd = _wire(monkeypatch, tmp_path)
        rc = trail_cmd.cmd_trail(_args(trail_type="quality"))
        payload = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert any(r["metric_type"] == "atom_precision" for r in payload["rows"])

    def test_overview_reads_all_three(self, tmp_path, monkeypatch, capsys):
        store = _new_store(tmp_path)
        _seed_all(store)
        store.close()
        trail_cmd = _wire(monkeypatch, tmp_path)
        rc = trail_cmd.cmd_trail(_args(trail_type=None))
        payload = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert payload["overview"] is True
        assert len(payload["deletion"]) >= 1
        assert len(payload["tool"]) >= 1
        assert len(payload["quality"]) >= 1


# ===========================================================================
# Honest empty state — no fabricated data
# ===========================================================================

class TestTrailEmptyState:
    def test_empty_overview_reports_zero_not_fabricated(self, tmp_path, monkeypatch, capsys):
        _new_store(tmp_path).close()  # founder exists, no observation rows
        trail_cmd = _wire(monkeypatch, tmp_path)
        rc = trail_cmd.cmd_trail(_args(trail_type=None, as_json=False))
        out = capsys.readouterr().out
        assert rc == 0
        # Honest zero counts — NOT a fabricated "all good" summary.
        assert "最近 0 条" in out
        assert "三表均空" in out

    def test_no_founder_emits_honest_hint(self, tmp_path, monkeypatch, capsys):
        # DB exists but no founder resolved (no user_id arg, no ptg_meta).
        PTGStore(db_path=str(tmp_path / "ptg.db")).close()
        trail_cmd = _wire(monkeypatch, tmp_path)
        rc = trail_cmd.cmd_trail(_args(trail_type=None, as_json=False, user_id=None))
        out = capsys.readouterr().out
        assert rc == 0
        assert "未找到创始人" in out


# ===========================================================================
# Static anti-fake-green guard — the consumer must stay wired
# ===========================================================================

class TestTrailConsumerGuard:
    def test_read_apis_have_cli_consumer(self):
        """Static guard: the three formerly-write-only read APIs MUST be
        referenced by trail_cmd.py (the consumer). If someone unwires the
        consumer (the exact ADR-037 regression this cures), this fails."""
        from pathlib import Path
        src = Path("hermes_cli/trail_cmd.py").read_text(encoding="utf-8")
        assert "list_deletion_log" in src
        assert "recent_tool_events" in src
        assert "recent_quality_metrics" in src

    def test_main_wires_trail(self):
        """4-point wiring guard: trail is imported + builtin + delegated +
        parser-built in main.py."""
        from pathlib import Path
        src = Path("hermes_cli/main.py").read_text(encoding="utf-8")
        assert "from hermes_cli.subcommands.trail import build_trail_parser" in src
        assert "build_trail_parser(subparsers, cmd_trail=cmd_trail)" in src
        assert "def cmd_trail(args)" in src

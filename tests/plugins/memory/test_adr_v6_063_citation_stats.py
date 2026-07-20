"""C4 regression: G1 citation-credibility READ surface (ADR-V6-063).

ADR-V6-043 promised the citation grounded/ungrounded counters are "计数器可追溯 ·
跨重启可查" — but until ADR-V6-063 there was NO consumer: no CLI, no doctor
check, no API. ``PTGProvider._observe_citation_quality`` bumped them every turn
to a ``ptg_meta`` table nothing read — ADR-V6-037's most-fatal 做了没发 (the
same class of gap ADR-V6-056 closed for ``compute_k_correlations``).

This pins the read surface: ``PTGStore.citation_stats`` reads the two counters
correctly (empty / present / malformed), and the ``hermes citation`` handler
renders them honestly (empty state when no data, ratio + soft flag when there
is — never fabricated numbers).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from plugins.memory.ptg.store import PTGStore


# ── Store layer: citation_stats reads the counters correctly ─────────────────


class TestCitationStatsStore:
    def _store_with(self, tmp_path, grounded=None, ungrounded=None):
        store = PTGStore(db_path=str(tmp_path / "ptg.db"))
        with store._lock:
            if grounded is not None:
                store._conn.execute(
                    "INSERT OR REPLACE INTO ptg_meta(key, value) VALUES (?, ?)",
                    ("citation_grounded_turns", str(grounded)))
            if ungrounded is not None:
                store._conn.execute(
                    "INSERT OR REPLACE INTO ptg_meta(key, value) VALUES (?, ?)",
                    ("citation_ungrounded_turns", str(ungrounded)))
        return store

    def test_empty_when_no_counters(self, tmp_path):
        store = self._store_with(tmp_path)
        try:
            s = store.citation_stats()
        finally:
            store.close()
        assert s == {
            "grounded": 0, "ungrounded": 0, "total": 0, "ungrounded_ratio": None}

    def test_reads_both_counters(self, tmp_path):
        store = self._store_with(tmp_path, grounded=7, ungrounded=3)
        try:
            s = store.citation_stats()
        finally:
            store.close()
        assert s["grounded"] == 7
        assert s["ungrounded"] == 3
        assert s["total"] == 10
        assert s["ungrounded_ratio"] == pytest.approx(0.3)

    def test_malformed_value_counts_as_zero(self, tmp_path):
        # A hand-edited / corrupted row must not crash the read surface (C7).
        store = self._store_with(tmp_path, grounded=5)
        with store._lock:
            store._conn.execute(
                "INSERT OR REPLACE INTO ptg_meta(key, value) VALUES (?, ?)",
                ("citation_ungrounded_turns", "not-a-number"))
        try:
            s = store.citation_stats()
        finally:
            store.close()
        assert s["grounded"] == 5
        assert s["ungrounded"] == 0  # malformed → 0, not a crash
        assert s["total"] == 5
        assert s["ungrounded_ratio"] == 0.0

    def test_only_grounded_yields_zero_ratio(self, tmp_path):
        store = self._store_with(tmp_path, grounded=10, ungrounded=0)
        try:
            s = store.citation_stats()
        finally:
            store.close()
        assert s["ungrounded_ratio"] == 0.0

    def test_round_trip_with_bump_meta_pattern(self, tmp_path):
        # The producer (PTGProvider._bump_meta) writes str(int); verify the
        # reader reads what the writer writes (no schema drift between them).
        store = self._store_with(tmp_path)
        with store._lock:
            for _ in range(4):
                row = store._conn.execute(
                    "SELECT value FROM ptg_meta WHERE key=?",
                    ("citation_grounded_turns",)).fetchone()
                cur = int(row[0]) if row and str(row[0]).isdigit() else 0
                store._conn.execute(
                    "INSERT OR REPLACE INTO ptg_meta(key, value) VALUES (?, ?)",
                    ("citation_grounded_turns", str(cur + 1)))
        try:
            s = store.citation_stats()
        finally:
            store.close()
        assert s["grounded"] == 4


# ── CLI layer: `hermes citation` renders honestly ────────────────────────────


class TestCitationCmdRender:
    """The handler renders the stats honestly — empty state vs ratio + flag."""

    def _run(self, monkeypatch, stats, as_json=False):
        from hermes_cli import citation_cmd

        class _FakeStore:
            def __init__(self_, db_path):
                pass

            def citation_stats(self_):
                return stats

            def close(self_):
                pass

        monkeypatch.setattr(citation_cmd, "PTGStore", _FakeStore)
        monkeypatch.setattr(citation_cmd, "load_ptg_config", lambda: {})
        monkeypatch.setattr(citation_cmd, "resolve_db_path", lambda c: ":memory:")
        args = SimpleNamespace(as_json=as_json)
        return citation_cmd.cmd_citation(args)

    def test_empty_state_when_no_data(self, capsys, monkeypatch):
        self._run(monkeypatch, {
            "grounded": 0, "ungrounded": 0, "total": 0, "ungrounded_ratio": None})
        out = capsys.readouterr().out
        assert "尚无观测" in out  # honest empty, NOT fabricated zeros

    def test_renders_counts_ratio_and_flag(self, capsys, monkeypatch):
        self._run(monkeypatch, {
            "grounded": 7, "ungrounded": 3, "total": 10,
            "ungrounded_ratio": 0.3})
        out = capsys.readouterr().out
        assert "grounded：7" in out
        assert "ungrounded：3" in out
        assert "30.0%" in out
        assert "⚠️" in out  # ratio >= 0.30 → soft flag

    def test_no_flag_below_threshold(self, capsys, monkeypatch):
        self._run(monkeypatch, {
            "grounded": 19, "ungrounded": 1, "total": 20,
            "ungrounded_ratio": 0.05})
        out = capsys.readouterr().out
        assert "⚠️" not in out  # 5% is fine, no flag

    def test_json_output_machine_readable(self, capsys, monkeypatch):
        self._run(monkeypatch, {
            "grounded": 7, "ungrounded": 3, "total": 10,
            "ungrounded_ratio": 0.3}, as_json=True)
        out = capsys.readouterr().out
        data = json.loads(out)  # valid JSON
        assert data["grounded"] == 7
        assert data["ungrounded_ratio"] == pytest.approx(0.3)

"""C4 regression: C7 DLQ read surface (ADR-V6-065).

Until ADR-V6-065, ``dlq_messages`` was write-only-no-consumer: 5+ producers
(atomize/quark/theory/insights/provider) inserted rows under C7, but ZERO
consumers — no CLI, no API, no UI. The C7 Phase-Gate Checklist's 'DLQ backlog
< 5/week' KR was unverifiable, and the ``resolved`` column (schema idx_dlq_
resolved) was a dead field (no UPDATE ever flipped it). This is ADR-V6-037's
most-fatal 做了没发 class, worse than the citation counters (ADR-V6-063) because
DLQ IS the C7 gate, not just an observation.

This pins the read surface + the resolve ack: dlq_stats/dlq_list read honestly
(empty / filtered / clamped), dlq_resolve flips status metadata idempotently
(append-only compliant — failure payload never mutated), and the ``hermes dlq``
handler renders it all honestly (empty state, backlog hint, never fabricated).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from plugins.memory.ptg.store import PTGStore


# ── Store layer ─────────────────────────────────────────────────────────────


def _seed(store, *, source, error_type, error_msg="boom", user_id="u1",
          resolved=False):
    """Insert one dlq row; if resolved=True, flip it via dlq_resolve."""
    dlq_id = store.insert_dlq(
        user_id=user_id, source=source, error_type=error_type,
        error_msg=error_msg, original_data={"k": "v"})
    if resolved:
        assert store.dlq_resolve(dlq_id) is True
    return dlq_id


class TestDlqStatsStore:
    def test_empty(self, tmp_path):
        store = PTGStore(db_path=str(tmp_path / "ptg.db"))
        try:
            s = store.dlq_stats()
        finally:
            store.close()
        assert s == {"total": 0, "unresolved": 0, "resolved": 0,
                     "by_source": {}, "by_error_type": {}}

    def test_aggregation_splits_resolved(self, tmp_path):
        store = PTGStore(db_path=str(tmp_path / "ptg.db"))
        try:
            _seed(store, source="atomize", error_type="json_parse")
            _seed(store, source="atomize", error_type="json_parse", resolved=True)
            _seed(store, source="theory", error_type="theory_pc_build_failed")
            s = store.dlq_stats()
        finally:
            store.close()
        assert s["total"] == 3
        assert s["unresolved"] == 2
        assert s["resolved"] == 1
        assert s["by_source"] == {"atomize": 2, "theory": 1}
        assert s["by_error_type"] == {"json_parse": 2, "theory_pc_build_failed": 1}

    def test_user_filter_scopes(self, tmp_path):
        store = PTGStore(db_path=str(tmp_path / "ptg.db"))
        try:
            _seed(store, source="atomize", error_type="x", user_id="u1")
            _seed(store, source="atomize", error_type="x", user_id="u2")
            s = store.dlq_stats(user_id="u1")
        finally:
            store.close()
        assert s["total"] == 1


class TestDlqListStore:
    def test_default_newest_first(self, tmp_path):
        store = PTGStore(db_path=str(tmp_path / "ptg.db"))
        try:
            _seed(store, source="a", error_type="e1", error_msg="first")
            _seed(store, source="b", error_type="e2", error_msg="second")
            rows = store.dlq_list(limit=10)
        finally:
            store.close()
        assert len(rows) == 2
        # newest first → "second" (b/e2) is row[0]
        assert rows[0]["source"] == "b"
        assert rows[1]["source"] == "a"

    def test_resolved_filter(self, tmp_path):
        store = PTGStore(db_path=str(tmp_path / "ptg.db"))
        try:
            _seed(store, source="a", error_type="e", resolved=False)
            _seed(store, source="b", error_type="e", resolved=True)
            unresolved = store.dlq_list(resolved=False)
            resolved = store.dlq_list(resolved=True)
        finally:
            store.close()
        assert len(unresolved) == 1 and unresolved[0]["source"] == "a"
        assert len(resolved) == 1 and resolved[0]["source"] == "b"

    def test_limit_clamped_to_max(self, tmp_path):
        store = PTGStore(db_path=str(tmp_path / "ptg.db"))
        try:
            for i in range(5):
                _seed(store, source="a", error_type="e")
            # limit over 200 clamps to 200 (no error); limit 2 returns 2
            assert len(store.dlq_list(limit=2)) == 2
            assert len(store.dlq_list(limit=999)) == 5
        finally:
            store.close()


class TestDlqResolveStore:
    def test_resolve_flips_status_and_sets_resolved_at(self, tmp_path):
        store = PTGStore(db_path=str(tmp_path / "ptg.db"))
        try:
            dlq_id = _seed(store, source="a", error_type="e")
            with store._lock:
                before = store._conn.execute(
                    "SELECT resolved, resolved_at FROM dlq_messages WHERE id=?",
                    (dlq_id,)).fetchone()
            assert before[0] == 0 and before[1] is None
            assert store.dlq_resolve(dlq_id) is True
            with store._lock:
                after = store._conn.execute(
                    "SELECT resolved, resolved_at FROM dlq_messages WHERE id=?",
                    (dlq_id,)).fetchone()
        finally:
            store.close()
        assert after[0] == 1 and after[1] is not None  # resolved_at now set

    def test_resolve_is_idempotent(self, tmp_path):
        store = PTGStore(db_path=str(tmp_path / "ptg.db"))
        try:
            dlq_id = _seed(store, source="a", error_type="e")
            assert store.dlq_resolve(dlq_id) is True   # first flip
            assert store.dlq_resolve(dlq_id) is False  # already resolved → no-op
        finally:
            store.close()

    def test_resolve_unknown_id_returns_false(self, tmp_path):
        store = PTGStore(db_path=str(tmp_path / "ptg.db"))
        try:
            assert store.dlq_resolve("nonexistent") is False
        finally:
            store.close()

    def test_resolve_all_counts_only_unresolved(self, tmp_path):
        store = PTGStore(db_path=str(tmp_path / "ptg.db"))
        try:
            _seed(store, source="atomize", error_type="e")
            _seed(store, source="theory", error_type="e")
            _seed(store, source="atomize", error_type="e", resolved=True)
            n_all = store.dlq_resolve_all()
            assert n_all == 2  # only the 2 unresolved
            n_again = store.dlq_resolve_all()
            assert n_again == 0  # idempotent
        finally:
            store.close()


# ── CLI layer: `hermes dlq` renders honestly ─────────────────────────────────


class TestDlqCmdRender:
    def _run(self, monkeypatch, stats=None, rows=None, resolve_ok=None,
             as_json=False, stats_only=False, resolve_id=None):
        from hermes_cli import dlq_cmd

        class _FakeStore:
            def __init__(self_, db_path):
                pass

            def dlq_stats(self_):
                return stats if stats is not None else {
                    "total": 0, "unresolved": 0, "resolved": 0,
                    "by_source": {}, "by_error_type": {}}

            def dlq_list(self_, *, resolved=None, source=None, limit=20):
                return rows or []

            def dlq_resolve(self_, dlq_id, user_id=None):
                return resolve_ok if resolve_ok is not None else False

            def close(self_):
                pass

        monkeypatch.setattr(dlq_cmd, "PTGStore", _FakeStore)
        monkeypatch.setattr(dlq_cmd, "load_ptg_config", lambda: {})
        monkeypatch.setattr(dlq_cmd, "resolve_db_path", lambda c: ":memory:")
        args = SimpleNamespace(
            as_json=as_json, stats_only=stats_only, resolve_id=resolve_id,
            show_all=False, only_resolved=False, source=None, limit=20)
        return dlq_cmd.cmd_dlq(args)

    def test_empty_state_no_data(self, capsys, monkeypatch):
        self._run(monkeypatch)  # total=0
        out = capsys.readouterr().out
        assert "尚无记录" in out  # honest empty, NOT fabricated

    def test_stats_render_with_backlog_flag(self, capsys, monkeypatch):
        self._run(monkeypatch, stats={
            "total": 7, "unresolved": 6, "resolved": 1,
            "by_source": {"atomize": 5, "theory": 2},
            "by_error_type": {"json_parse": 7}}, stats_only=True)
        out = capsys.readouterr().out
        assert "未解决 unresolved：6" in out
        assert "⚠️" in out  # 6 >= 5 threshold → backlog hint
        assert "atomize(5)" in out

    def test_no_backlog_flag_below_threshold(self, capsys, monkeypatch):
        self._run(monkeypatch, stats={
            "total": 3, "unresolved": 2, "resolved": 1,
            "by_source": {"atomize": 3}, "by_error_type": {"e": 3}},
            stats_only=True)
        out = capsys.readouterr().out
        assert "⚠️" not in out  # 2 < 5, no flag

    def test_list_render_rows(self, capsys, monkeypatch):
        self._run(monkeypatch, stats={
            "total": 1, "unresolved": 1, "resolved": 0,
            "by_source": {"atomize": 1}, "by_error_type": {"json_parse": 1}},
            rows=[{"id": "abc12345-6789", "created_at": "2026-07-21T00:00:00Z",
                   "source": "atomize", "error_type": "json_parse",
                   "error_msg": "broken json", "resolved": False}])
        out = capsys.readouterr().out
        assert "⏳" in out  # unresolved marker
        assert "abc12345" in out  # id prefix
        assert "[atomize/json_parse]" in out

    def test_resolve_success_render(self, capsys, monkeypatch):
        self._run(monkeypatch, resolve_ok=True, resolve_id="abc12345",
                  stats={"total": 1, "unresolved": 0, "resolved": 1,
                         "by_source": {}, "by_error_type": {}})
        out = capsys.readouterr().out
        assert "✅" in out
        assert "abc12345" in out

    def test_json_output_machine_readable(self, capsys, monkeypatch):
        self._run(monkeypatch, stats={
            "total": 2, "unresolved": 1, "resolved": 1,
            "by_source": {"a": 2}, "by_error_type": {"e": 2}},
            rows=[{"id": "x", "created_at": "t", "source": "a",
                   "error_type": "e", "error_msg": "m", "resolved": False}],
            as_json=True)
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["stats"]["unresolved"] == 1
        assert data["rows"][0]["source"] == "a"

    def test_resolve_json(self, capsys, monkeypatch):
        self._run(monkeypatch, resolve_ok=True, resolve_id="abc12345",
                  stats={"total": 1, "unresolved": 0, "resolved": 1,
                         "by_source": {}, "by_error_type": {}}, as_json=True)
        data = json.loads(capsys.readouterr().out)
        assert data["resolved"] is True
        assert data["id"] == "abc12345"

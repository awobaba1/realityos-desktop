"""C4 regression: B3 scheduling + get_memo + CLI wiring (ADR-V6-051).

Locks the Phase-2-B B3 wiring so it cannot silently regress to a fake-green
"register no-op": (1) the new ``get_memo`` store loader; (2) the startup-lazy
theory scheduler's idempotent gate (cold-start / missing-generates /
exists-skip / stale-prompt-regenerate); (3) the once-guard + opt-out; (4) the
layering iron rule (theory schedules itself, depends only on the memory layer —
never upward to insights). Mock LLM caller; no network.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from plugins.memory.ptg.store import PTGStore

USER = "u1"
PERIOD = "2026-07-20"
_BEIJING_TZ = timezone(timedelta(hours=8))


@pytest.fixture
def store(tmp_path):
    s = PTGStore(db_path=str(tmp_path / "ptg.db"))
    s.ensure_founder(USER, "founder@realityos.local")
    yield s
    s.close()


def _seed_memo(store, text="和张三开会讨论述职"):
    return store.insert_memo(user_id=USER, source_text=text, input_mode="text")


def _seed_atom(store, memo_id):
    """One identity atom so recent_atoms(user) is non-empty."""
    store.insert_identity_event(
        user_id=USER, source_text="和张三开会", person_name="张三",
        confidence_base=0.9, relation_confidence=0.9, memo_id=memo_id)


# ===========================================================================
# get_memo store loader
# ===========================================================================

class TestGetMemo:
    def test_returns_effective_text(self, store):
        mid = _seed_memo(store, "原文 ASR 有错")
        m = store.get_memo(USER, mid)
        assert m is not None
        assert m["id"] == mid
        assert m["source_text"] == "原文 ASR 有错"
        assert m["effective_text"] == "原文 ASR 有错"
        assert m["version"] == 1

    def test_prefers_corrected_text(self, store):
        mid = _seed_memo(store, "原文")
        store.correct_memo_source_text(
            memo_id=mid, user_id=USER, corrected_text="纠正后",
            actor="t", reason="t")
        m = store.get_memo(USER, mid)
        assert m["effective_text"] == "纠正后"  # corrected_text ?? source_text
        assert m["source_text"] == "原文"  # C2: source NEVER mutated
        assert m["version"] == 2

    def test_not_found_returns_none(self, store):
        assert store.get_memo(USER, "nope") is None

    def test_soft_deleted_returns_none(self, store):
        mid = _seed_memo(store, "待删")
        store._conn.execute(
            "UPDATE memos SET deleted_at=? WHERE id=?", ("2026-07-20", mid))
        store._conn.commit()
        assert store.get_memo(USER, mid) is None  # C2: deleted invisible


# ===========================================================================
# run_due_theory — the startup-lazy gate
# ===========================================================================

def _mock_theory_result():
    return {"ok": True, "derived": 12, "persisted": 12,
            "degraded_count": 4, "derivations": []}


class TestRunDueTheory:
    def test_cold_start_no_atoms_skips_llm(self, store, monkeypatch):
        """No atoms ⇒ no LLM call (don't burn a call on an empty graph)."""
        import plugins.realityos_theory as pkg
        called = []

        def _boom(**kw):
            called.append(1)
            raise AssertionError("must not derive on cold start")

        monkeypatch.setattr(pkg, "derive_and_persist", _boom)
        from plugins.realityos_theory.scheduling import run_due_theory
        r = run_due_theory(store, user_id=USER, period_key=PERIOD)
        assert r == {"generated": False, "period_key": PERIOD,
                     "reason": "no_atoms"}
        assert not called

    def test_missing_generates(self, store, monkeypatch):
        mid = _seed_memo(store)
        _seed_atom(store, mid)
        import plugins.realityos_theory as pkg
        seen = {}
        monkeypatch.setattr(
            pkg, "derive_and_persist",
            lambda store_, **kw: seen.update(kw) or _mock_theory_result())
        from plugins.realityos_theory.scheduling import run_due_theory
        r = run_due_theory(store, user_id=USER, period_key=PERIOD)
        assert r["generated"] is True
        assert r["derived"] == 12 and r["persisted"] == 12
        assert r["reason"] == "missing"
        # atoms + relations gathered + caller threaded through
        assert seen.get("period_key") == PERIOD
        assert seen.get("user_id") == USER
        assert isinstance(seen.get("atoms"), list) and seen["atoms"]

    def test_exists_current_prompt_skips(self, store, monkeypatch):
        mid = _seed_memo(store)
        _seed_atom(store, mid)
        # Pre-cache the canonical probe row under the CURRENT prompt version.
        store.upsert_insight(
            user_id=USER, aggregation_type="constraint_state",
            period_key=f"{PERIOD}|Time", period_start=PERIOD, period_end=PERIOD,
            input_data="{}", result_data="{}", confidence=0.5,
            data_sufficiency="sufficient", generated_by="scheduled",
            schema_version="v1", expires_at="2026-07-21")
        import plugins.realityos_theory as pkg
        monkeypatch.setattr(
            pkg, "derive_and_persist",
            lambda *a, **kw: pytest.fail("must skip — current-prompt row exists"))
        from plugins.realityos_theory.scheduling import run_due_theory
        r = run_due_theory(store, user_id=USER, period_key=PERIOD)
        assert r["generated"] is False and r["reason"] == "exists"

    def test_stale_prompt_regenerates(self, store, monkeypatch):
        mid = _seed_memo(store)
        _seed_atom(store, mid)
        # Probe row exists but under an OLDER prompt version → regenerate.
        store.upsert_insight(
            user_id=USER, aggregation_type="constraint_state",
            period_key=f"{PERIOD}|Time", period_start=PERIOD, period_end=PERIOD,
            input_data="{}", result_data="{}", confidence=0.5,
            data_sufficiency="sufficient", generated_by="scheduled",
            schema_version="v0", expires_at="2026-07-21")
        import plugins.realityos_theory as pkg
        monkeypatch.setattr(pkg, "derive_and_persist",
                            lambda *a, **kw: _mock_theory_result())
        from plugins.realityos_theory.scheduling import run_due_theory
        r = run_due_theory(store, user_id=USER, period_key=PERIOD)
        assert r["generated"] is True and r["reason"] == "stale_prompt"

    def test_derive_failure_isolated(self, store, monkeypatch):
        """C7: derive_and_persist raising must NOT escape the scheduler."""
        mid = _seed_memo(store)
        _seed_atom(store, mid)
        import plugins.realityos_theory as pkg
        monkeypatch.setattr(
            pkg, "derive_and_persist",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
        from plugins.realityos_theory.scheduling import run_due_theory
        r = run_due_theory(store, user_id=USER, period_key=PERIOD)
        assert r["generated"] is False and r["reason"] == "error"
        assert "boom" in r["error"]


# ===========================================================================
# once-guard + opt-out + layering
# ===========================================================================

class TestSchedulerControl:
    def test_once_guard(self, monkeypatch):
        import plugins.realityos_theory.scheduling as sch
        sch._reset_for_tests()
        monkeypatch.setattr(sch, "_run_startup_lazy", lambda **kw: None)
        assert sch.start_scheduler_if_due(enabled=True) is True
        assert sch.start_scheduler_if_due(enabled=True) is False  # 2nd = no-op
        sch._reset_for_tests()

    def test_disabled_never_starts(self, monkeypatch):
        import plugins.realityos_theory.scheduling as sch
        sch._reset_for_tests()
        spawned = []
        monkeypatch.setattr(
            sch, "_run_startup_lazy",
            lambda **kw: spawned.append(1))
        assert sch.start_scheduler_if_due(enabled=False) is False
        assert not spawned
        sch._reset_for_tests()

    def test_opt_out_env(self, monkeypatch):
        import plugins.realityos_theory.scheduling as sch
        for k in ("PYTEST_CURRENT_TEST", "PYTEST_RUN_CONFIG",
                  "REALITYOS_THEORY_AUTOSCHED"):
            monkeypatch.delenv(k, raising=False)
        assert sch._scheduler_should_start() is True
        monkeypatch.setenv("REALITYOS_THEORY_AUTOSCHED", "0")
        assert sch._scheduler_should_start() is False
        monkeypatch.setenv("REALITYOS_THEORY_AUTOSCHED", "off")
        assert sch._scheduler_should_start() is False

    def test_pytest_disables(self, monkeypatch):
        import plugins.realityos_theory.scheduling as sch
        monkeypatch.delenv("REALITYOS_THEORY_AUTOSCHED", raising=False)
        monkeypatch.setenv("PYTEST_CURRENT_TEST", "x")
        assert sch._scheduler_should_start() is False


class TestLayering:
    def test_no_insights_dependency_at_import(self):
        """Layering iron rule: theory scheduling must not pull insights."""
        import plugins.realityos_theory.scheduling as sch  # noqa: F401
        pulled = [m for m in sys.modules if "realityos_insights" in m
                  and "realityos_theory" not in m]
        # Filter to only those pulled AFTER scheduling loaded — simpler: insights
        # may already be imported by the test session, so assert scheduling's own
        # declared imports exclude it.
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(sch))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                for n in node.names:
                    imported.add(n.name)
        bad = [m for m in imported if "realityos_insights" in m]
        assert not bad, f"scheduling imports insights (upward edge): {bad}"

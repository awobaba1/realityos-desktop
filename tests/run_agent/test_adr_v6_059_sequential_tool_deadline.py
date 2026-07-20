"""C4 regression: cooperative per-tool deadline for the sequential path (ADR-V6-059).

The audit (T6/T8, 2026-07-20) found that ``execute_tool_calls_sequential`` had
NO per-tool deadline — unlike the concurrent path, a single hung tool blocked
the agent turn indefinitely with no observability (C7 gap). ADR-V6-059 adds a
COOPERATIVE deadline: a daemon watchdog that, on expiry, fires the per-thread
interrupt signal (mirroring the concurrent path's ``_set_interrupt``), sets
``_interrupt_requested`` to cascade-stop subsequent tools, and emits a
``tool_timeout`` terminal post-tool-call so the overrun is observable.

These tests pin the resolver (pure logic) + the watchdog helper directly. The
full sequential-loop wiring (arm at tool_start_time, cancel at iteration end) is
verified by import + the existing sequential test suite still passing (no
regression — the default 420s deadline never fires in fast tests).
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from agent import tool_executor
from agent.tool_executor import (
    _arm_sequential_tool_deadline,
    _resolve_sequential_tool_timeout,
)


class TestResolveSequentialToolTimeout:
    def test_default(self, monkeypatch):
        monkeypatch.delenv("HERMES_SEQUENTIAL_TOOL_TIMEOUT_S", raising=False)
        assert _resolve_sequential_tool_timeout() == 420.0

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("HERMES_SEQUENTIAL_TOOL_TIMEOUT_S", "30")
        assert _resolve_sequential_tool_timeout() == 30.0

    def test_invalid_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("HERMES_SEQUENTIAL_TOOL_TIMEOUT_S", "not-a-number")
        assert _resolve_sequential_tool_timeout() == 420.0

    def test_zero_or_negative_disables(self, monkeypatch):
        """A non-positive timeout disables the deadline (returns None)."""
        monkeypatch.setenv("HERMES_SEQUENTIAL_TOOL_TIMEOUT_S", "0")
        assert _resolve_sequential_tool_timeout() is None
        monkeypatch.setenv("HERMES_SEQUENTIAL_TOOL_TIMEOUT_S", "-5")
        assert _resolve_sequential_tool_timeout() is None


class TestArmSequentialToolDeadline:
    """The watchdog fires the cooperative interrupt + emits a timeout observation."""

    @pytest.fixture
    def patched(self, monkeypatch):
        """Patch _ra + _emit_terminal_post_tool_call to recorders; return an agent."""
        calls = {"set_interrupt": [], "emit": []}

        def _fake_ra():
            return SimpleNamespace(
                _set_interrupt=lambda flag, tid: calls["set_interrupt"].append((flag, tid))
            )

        def _fake_emit(agent, **kw):
            calls["emit"].append(kw)

        monkeypatch.setattr(tool_executor, "_ra", _fake_ra)
        monkeypatch.setattr(tool_executor, "_emit_terminal_post_tool_call", _fake_emit)
        agent = SimpleNamespace(_interrupt_requested=False, _current_tool="hung_tool")
        return agent, calls

    def test_disabled_returns_none(self):
        """timeout_s=None or ≤0 → no timer armed."""
        agent = SimpleNamespace(_interrupt_requested=False)
        assert _arm_sequential_tool_deadline(agent, "x", "tc1", None, "t1", []) is None
        assert _arm_sequential_tool_deadline(agent, "x", "tc1", 0, "t1", []) is None
        assert _arm_sequential_tool_deadline(agent, "x", "tc1", -1, "t1", []) is None

    def test_fires_sets_interrupt_and_emits(self, patched):
        """On expiry: cooperative _set_interrupt + _interrupt_requested + tool_timeout emit."""
        agent, calls = patched
        timer = _arm_sequential_tool_deadline(
            agent, "hung_tool", "tc-1", 0.2, "task-1", ["mw"])
        assert timer is not None
        # Wait past the deadline (3.5x margin for CI runner variance).
        time.sleep(0.7)
        assert agent._interrupt_requested is True, "deadline must cascade-stop via interrupt"
        assert calls["set_interrupt"], "cooperative _set_interrupt(True, main_tid) must fire"
        assert calls["set_interrupt"][0][0] is True
        assert calls["emit"], "timeout must be observable (tool_timeout emit)"
        emit_kw = calls["emit"][0]
        assert emit_kw["status"] == "timeout"
        assert emit_kw["error_type"] == "tool_timeout"
        assert emit_kw["function_name"] == "hung_tool"
        assert emit_kw["tool_call_id"] == "tc-1"

    def test_cancel_prevents_firing(self, patched):
        """Cancelling the timer before expiry → watchdog does NOT fire."""
        agent, calls = patched
        timer = _arm_sequential_tool_deadline(agent, "fast_tool", "tc-2", 0.2, "t2", [])
        timer.cancel()  # cancelled immediately (tool returned fast)
        time.sleep(0.7)
        assert agent._interrupt_requested is False
        assert calls["set_interrupt"] == []
        assert calls["emit"] == []

    def test_watchdog_never_raises_on_patch_failure(self, patched, monkeypatch):
        """Even if _ra()._set_interrupt raises, the watchdog swallows + still emits."""
        agent, calls = patched

        def _broken_ra():
            return SimpleNamespace(_set_interrupt=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

        monkeypatch.setattr(tool_executor, "_ra", _broken_ra)
        timer = _arm_sequential_tool_deadline(agent, "x", "tc-3", 0.2, "t3", [])
        time.sleep(0.7)
        # _set_interrupt raised → best-effort, swallowed; but interrupt flag + emit still happen.
        assert agent._interrupt_requested is True
        assert calls["emit"], "emit still fires even if cooperative signal raised"

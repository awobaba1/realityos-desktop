"""Regression tests for the run_eval gate banner honesty (ADR-V6-026).

WHY THIS TEST EXISTS (C4 — every defect becomes a test case)
-------------------------------------------------------------
The 10-lens audit (ADR-V6-022, lens 5) found the evaluation gate banner read
``GATE: ALL GREEN ✅`` while the gate checks RECALL per type only — precision
(~63%) is intentionally UNGATED (the ⑦ structural ceiling, commit 7758542e0 /
ADR-V6-012). "ALL GREEN" thus read as "extraction fully validated" when only
recall was checked: a fake-green label papering over a deferred metric.

ADR-V6-026 splits the banner honestly: recall-pass ⇒ "RECALL GREEN / PRECISION
DEFERRED", never a blanket green. These tests pin that contract so the banner
can't silently revert to a blanket "ALL GREEN ✅".
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "_run_eval", Path(__file__).parent / "run_eval.py")
_run_eval = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_run_eval)
_gate_banner = _run_eval._gate_banner


def _gates(pass_map: dict[str, bool | None]) -> dict:
    """Build a gates dict shaped like run_eval's: {type: {"pass": bool|None}}."""
    return {t: {"pass": p} for t, p in pass_map.items()}


def test_recall_green_never_says_all_green():
    # The whole point: when recall passes the banner must NOT claim a blanket
    # green — it must call out that precision is deferred.
    gates = _gates({"R3_Person": True, "R2_Task": True, "R1_SelfState": True})
    banner = _gate_banner(True, precision=0.63, gates=gates, errors=0)
    assert "RECALL GREEN" in banner
    assert "PRECISION DEFERRED" in banner
    # The precise fake-green verdict phrase ("GATE: ALL GREEN ✅") must be gone.
    # (Note: "RECALL GREEN" literally contains the substring "ALL GREEN", so we
    # match the full old verdict prefix, not the bare substring.)
    assert "GATE: ALL GREEN" not in banner
    assert "63%" in banner  # the deferred precision value is surfaced


def test_red_banner_when_recall_misses():
    gates = _gates({"R3_Person": False, "R2_Task": True})
    banner = _gate_banner(False, precision=0.9, gates=gates, errors=0)
    assert "RED" in banner
    assert "do NOT ship" in banner
    assert "R3_Person" in banner  # the missed type is named


def test_red_banner_when_errors_present():
    # all_green is False when errors > 0 even if every gated type passed.
    gates = _gates({"R3_Person": True})
    banner = _gate_banner(False, precision=0.7, gates=gates, errors=3)
    assert "RED" in banner
    assert "errors=3" in banner


@pytest.mark.parametrize("precision", [0.50, 0.631, 0.70, 0.85])
def test_precision_value_always_surfaced_when_recall_green(precision):
    gates = _gates({"R3_Person": True})
    banner = _gate_banner(True, precision=precision, gates=gates, errors=0)
    # Whatever the deferred precision, it is shown so the reader sees the real
    # number rather than a reassuring "green".
    assert f"{precision:.0%}" in banner

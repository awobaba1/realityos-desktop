"""ADR-V6-030: run_eval --ci-gate exit-code contract (recall-only gate).

Pure unit tests over ``ci_gate_exit_code()`` — no LLM, no network, no fixtures.
Pins the ADR-V6-030 contract that CI fails on a RECALL regression ONLY; precision
stays deferred per ADR-V6-026 (``deferred_structural`` — the ~70% ⑦ ceiling is
structural, not a regression). This is the honesty guarantee that the nightly
eval lane (eval-nightly.yml) cannot accidentally start gating precision.
"""
from __future__ import annotations

from tests.benchmark.run_eval import ci_gate_exit_code


def _report(gate_all_green: bool, precision: float = 0.63) -> dict:
    return {
        "gate_all_green": gate_all_green,
        "gate_recall_green": gate_all_green,
        "gate_precision_status": "deferred_structural",
        "precision": precision,
        "recall": 0.88,
    }


class TestCiGateExitCode:
    def test_green_report_exits_zero(self):
        assert ci_gate_exit_code(_report(True)) == 0

    def test_red_report_exits_nonzero(self):
        assert ci_gate_exit_code(_report(False)) == 1

    def test_low_precision_does_not_fail_ci(self):
        # Precision at the structural floor (~40%) with green recall → still 0.
        # This is the anti-fake-green core: precision can NEVER turn CI red.
        assert ci_gate_exit_code(_report(True, precision=0.40)) == 0

    def test_missing_gate_key_is_red(self):
        # Defensive: an empty/partial report must not masquerade as green.
        assert ci_gate_exit_code({}) == 1

    def test_explicit_false_is_red(self):
        assert ci_gate_exit_code(_report(False, precision=0.95)) == 1

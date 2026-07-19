"""Regression test for ADR-V6-027 — the run_eval → quality_metrics bridge.

``_record_report_to_quality_metrics`` is the opt-in bridge that persists an
eval run's metrics (overall + per-atom-type precision/recall/f1) into the
user's REAL PTG ``quality_metrics`` time-series (§11.2 — the sole evidence
source for every §8 Phase-Gate KR). C4: this bridge had ZERO coverage, and the
live ``quality_metrics`` deadlock (V5-era schema drift + the untested bridge
silently no-op'ing on column mismatch) hid that the "0 行" operational gap was
really a heal gap. ADR-V6-027 heals the drift in ``schema.py``; this test locks
the bridge's happy path + its C7 fail-open contract.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from plugins.memory.ptg.store import PTGStore

# run_eval.py is a script (has ``if __name__ == "__main__"``); load it as a
# module without executing main. Same importlib pattern as test_gate_banner.
_RUN_EVAL = Path(__file__).parent / "run_eval.py"
_spec = importlib.util.spec_from_file_location("_run_eval_mod", _RUN_EVAL)
run_eval = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_eval)
_record = run_eval._record_report_to_quality_metrics


def _report() -> dict:
    return {
        "samples": 50,
        "provider": "mock",
        "precision": 0.72,
        "recall": 0.88,
        "f1": 0.79,
        "per_type": {
            "R3_Person": {"precision": 0.8, "recall": 0.9, "expected": 20},
            # recall=None simulates a type with no expected atoms → skipped.
            "R2_Task": {"precision": 0.6, "recall": None, "expected": 0},
            "R7_Expression": {"precision": 0.75, "recall": 0.85, "expected": 30},
        },
    }


def test_bridge_writes_overall_and_per_type_rows(tmp_path):
    db = str(tmp_path / "ptg.db")
    _record(_report(), ptg_db=db, user_id="founder", model="mock-model")

    s = PTGStore(db_path=db)
    try:
        rows = s._conn.execute(
            "SELECT metric_type, atom_type, value, sample_size, note "
            "FROM quality_metrics WHERE deleted_at IS NULL "
            "ORDER BY metric_type, atom_type").fetchall()
    finally:
        s.close()

    # 3 overall (precision/recall/f1, atom_type=NULL)
    # + per-type: R3(2) + R2(precision only, recall skipped) + R7(2) = 5
    # = 8 rows total.
    assert len(rows) == 8
    overall = {(r[0], r[1]) for r in rows if r[1] is None}
    assert overall == {("atom_precision", None), ("atom_recall", None),
                       ("atom_f1", None)}
    # Per-type rows carry atom_type + expected as sample_size.
    per_type = {(r[0], r[1], r[3]) for r in rows if r[1] is not None}
    assert ("atom_precision", "R3_Person", 20) in per_type
    assert ("atom_recall", "R3_Person", 20) in per_type
    assert ("atom_precision", "R7_Expression", 30) in per_type
    assert ("atom_recall", "R7_Expression", 30) in per_type
    # R2_Task recall was None → only its precision row (expected=0) lands.
    assert ("atom_precision", "R2_Task", 0) in per_type
    assert ("atom_recall", "R2_Task", 0) not in per_type
    # note = "{provider}/{model}".
    assert all(r[4] == "mock/mock-model" for r in rows)


def test_bridge_never_raises_on_bad_db(tmp_path):
    """C7: a metrics-write failure is swallowed, never crashes the eval loop."""
    # Opening a DB under a non-existent dir fails; the bridge must swallow it.
    _record(_report(), ptg_db=str(tmp_path / "nope" / "missing.db"),
            user_id="founder", model="m")  # no exception raised

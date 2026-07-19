"""C4 regression: the threshold-separability analysis tool (ADR-V6-012 ⑦).

Locks the artifact that PROVED precision⑦ is unreachable by per-type confidence
threshold tuning. A silent regression here (e.g. wrong denominator, miscounted
survivors) would either fake-green ⑦ or false-alarm it. The test feeds a tiny
synthetic dump with KNOWN separability and asserts the tool reports it correctly
— both the "separable → high precision" case and the real-world "overlapping →
ceiling below target" case.
"""
import json
from pathlib import Path

from tests.benchmark.analyze_threshold_separability import analyze


def _write_dump(tmp_path: Path, records: list[dict]) -> str:
    p = tmp_path / "dump.jsonl"
    with open(p, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return str(p)


def test_separable_fps_yield_high_precision(tmp_path, capsys):
    """FPs clustered LOW, TPs clustered HIGH → a threshold cleanly separates
    them and precision reaches ~100% while recall clears the gate."""
    recs = []
    # 10 TPs at 0.95, 2 FPs at 0.6 → threshold 0.9 keeps all TPs, drops all FPs.
    for i in range(10):
        recs.append({"id": f"tp{i}", "tp_atoms": [
            {"type": "R1_SelfState", "confidence": 0.95}], "fp_atoms": [], "fn_atoms": []})
    for i in range(2):
        recs.append({"id": f"fp{i}", "tp_atoms": [], "fp_atoms": [
            {"type": "R1_SelfState", "confidence": 0.6}], "fn_atoms": []})
    dump = _write_dump(tmp_path, recs)
    # expected=10 so recall = 10/10 = 100% ≥ 0.70 gate once FPs (at 0.6) drop.
    out = analyze(dump, {"R3_Person": 122, "R2_Task": 93, "R1_SelfState": 10, "R7_Expression": 44})
    # The contract is "separable FPs → a threshold lifts R1 precision to ~100%",
    # not any specific threshold value (the sweep picks the lowest achieving max).
    assert out["per_type"]["R1_SelfState"][1] >= 0.99  # precision ~100%
    captured = capsys.readouterr()
    assert "REACHABLE" in captured.out    # separable case clears 85% overall


def test_overlapping_fps_make_target_unreachable(tmp_path, capsys):
    """The REAL ⑦ shape: TP/FP confidence overlap (FPs confidently-wrong).
    Mirrors the 2026-07-19 finding — target NOT reachable by threshold tuning."""
    recs = []
    # 8 TPs + 8 FPs ALL at 0.95 → no threshold separates them. expected=9 so
    # recall gate (0.70 → ≥7 TPs) forces keeping most atoms → FP can't drop.
    for i in range(8):
        recs.append({"id": f"t{i}", "tp_atoms": [
            {"type": "R2_Task", "confidence": 0.95}], "fp_atoms": [], "fn_atoms": []})
    for i in range(8):
        recs.append({"id": f"f{i}", "tp_atoms": [], "fp_atoms": [
            {"type": "R2_Task", "confidence": 0.95}], "fn_atoms": []})
    dump = _write_dump(tmp_path, recs)
    out = analyze(dump, {"R3_Person": 122, "R2_Task": 9, "R1_SelfState": 79, "R7_Expression": 44})
    captured = capsys.readouterr()
    assert "NOT reachable" in captured.out   # overlapping → below 85%
    # Overall precision ≈ 8/(8+8) = 50% for R2 alone, drags the total under target.
    assert out["overall_precision_at_optimal_thresholds"] < 0.85

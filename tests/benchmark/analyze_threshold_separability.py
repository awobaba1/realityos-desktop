"""RealityOS V6 — C5 confidence threshold separability analysis (ADR-V6-012 ⑦).

The decisive test for whether precision⑦ (≥85%) is reachable by **per-type
confidence threshold tuning** (post-gate, prompt-untouched). Reads a TP/FP/FN
dump produced by ``run_eval.py --dump-fp`` and answers: are a type's TPs and
FPs separable by confidence? If FP confidence ≈ TP confidence (the LLM is
*confidently wrong* — right by the prompt's standard, uncredited by the
samples), then no threshold can lift precision without sinking recall below the
gate, and ⑦ is structurally unreachable on this axis.

Usage:
    .venv/bin/python -m tests.benchmark.analyze_threshold_separability \\
        --dump tests/benchmark/_dump.jsonl

The dump is the full-sample TP/FP/FN JSONL from ``run_eval --dump-fp``. Expected
per-type counts default to the v0 sample set (R3=122/R2=93/R1=79/R7=44); override
with --expected R2=100 if the sample set changes.

This script is the artifact that REPRODUCES the 2026-07-19 finding (commit
6e280e207 + this): at optimal per-type thresholds the 4-type precision ceiling
is ~68%, far below 85%, because TP/FP confidence distributions overlap. The
honest conclusion: ⑦ needs sample re-labeling or dedup (ADR-049), NOT prompt
tweaks (v12 regressed) and NOT threshold tuning (this).
"""
from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path

_TYPES = ["R3_Person", "R2_Task", "R1_SelfState", "R7_Expression"]
# Phase Gate recall targets (run_eval.py line 230). A threshold is admissible
# only if the recall it leaves is still ≥ the gate — else it's a non-starter.
_GATE = {"R3_Person": 0.85, "R2_Task": 0.80, "R1_SelfState": 0.70, "R7_Expression": 0.60}
_DEFAULT_EXPECTED = {"R3_Person": 122, "R2_Task": 93, "R1_SelfState": 79, "R7_Expression": 44}


def _stat(xs: list[float]) -> str:
    if not xs:
        return "n=0"
    s = sorted(xs)
    return f"n={len(xs)} min={s[0]:.2f} med={s[len(s)//2]:.2f} max={s[-1]:.2f}"


def analyze(dump_path: str, expected: dict[str, int]) -> dict:
    tp_confs: dict[str, list[float]] = collections.defaultdict(list)
    fp_confs: dict[str, list[float]] = collections.defaultdict(list)
    with open(dump_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            for a in rec.get("tp_atoms", []):
                if a.get("type"):
                    tp_confs[a["type"]].append(float(a.get("confidence") or 0))
            for a in rec.get("fp_atoms", []):
                if a.get("type"):
                    fp_confs[a["type"]].append(float(a.get("confidence") or 0))

    print("per-type TP/FP confidence distributions:")
    all_types = _TYPES + ["R0_Entity"]
    for t in all_types:
        print(f"  {t:13} TP {_stat(tp_confs[t]):45} FP {_stat(fp_confs[t])}")

    print("\nper-type threshold sweep (max precision s.t. recall ≥ gate):")
    best: dict[str, tuple] = {}
    for t in _TYPES:
        tpc, fpc = sorted(tp_confs[t]), sorted(fp_confs[t])
        exp, gate = expected[t], _GATE[t]
        cands = sorted(set(tpc + fpc + [0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95]))
        best_p, best_T, best_r = 0.0, None, 0.0
        for T in cands:
            tp_sur = sum(1 for c in tpc if c >= T)
            fp_sur = sum(1 for c in fpc if c >= T)
            prec = tp_sur / (tp_sur + fp_sur) if (tp_sur + fp_sur) else 1.0
            rec = tp_sur / exp if exp else 1.0
            if rec >= gate and prec > best_p:
                best_p, best_T, best_r = prec, T, rec
        best[t] = (best_T, best_p, best_r)
        if best_T:
            print(f"  {t:13} gate {gate:.2f}: T={best_T:<4} -> prec={best_p:.1%} recall={best_r:.1%}")
        else:
            print(f"  {t:13} gate {gate:.2f}: no threshold keeps recall ≥ gate")

    # Resulting overall precision applying the per-type best thresholds.
    # R0_Entity is unscored (not in expected set) so it is excluded from the
    # precision denominator — counting unscored R0 atoms as FP would understate
    # the real 4-type precision (the metric nuance documented in ADR-V6-012).
    tot_tp = tot_fp = 0
    per_type_survival = {}
    for t in _TYPES:
        T = best[t][0]
        tp_sur = sum(1 for c in tp_confs[t] if c >= T) if T else len(tp_confs[t])
        fp_sur = sum(1 for c in fp_confs[t] if c >= T) if T else len(fp_confs[t])
        tot_tp += tp_sur
        tot_fp += fp_sur
        per_type_survival[t] = {"threshold": T, "tp": tp_sur, "fp": fp_sur}
    overall = tot_tp / (tot_tp + tot_fp) if (tot_tp + tot_fp) else 0.0
    print(f"\nresulting 4-type precision at optimal per-type thresholds: "
          f"{overall:.1%} (TP={tot_tp} FP={tot_fp})")
    print(f"target 85% — {'REACHABLE' if overall >= 0.85 else 'NOT reachable by per-type threshold tuning'}")
    return {"overall_precision_at_optimal_thresholds": overall, "per_type": best,
            "survival": per_type_survival,
            "r0_unscored_fp": len(fp_confs["R0_Entity"])}


def _parse_expected(s: str) -> dict[str, int]:
    out = dict(_DEFAULT_EXPECTED)
    if s:
        for pair in s.split(","):
            k, v = pair.split("=")
            out[k.strip()] = int(v)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="C5 threshold separability analysis for precision⑦")
    ap.add_argument("--dump", required=True, help="TP/FP/FN JSONL dump from run_eval --dump-fp")
    ap.add_argument("--expected", default="", help="override expected counts, e.g. R2=100,R1=80")
    args = ap.parse_args()
    if not Path(args.dump).exists():
        raise SystemExit(f"dump not found: {args.dump}")
    analyze(args.dump, _parse_expected(args.expected))

"""RealityOS V6 — HL-12 extraction eval benchmark (§11.3, ADR-V6-012).

The anti-fake-green gate. Unlike V5's eval (which called the LLM directly and
scored raw JSON output), V6 runs the FULL Atomizer pipeline — extraction → C5
confidence gate → event-table write → graph materialize — and reads back what
ACTUALLY landed in the PTG via ``PTGStore.recent_atoms(memo_id=…)``. So the
precision/recall here is the number the USER experiences, post-gate, post-write.
That is the only honest unit of "extraction quality" for a data-asset product.

Usage (from fork root):
    .venv/bin/python -m tests.benchmark.run_eval --limit 50 --workers 8
    .venv/bin/python -m tests.benchmark.run_eval --api-key sk-... --base-url https://api.deepseek.com --model deepseek-chat

Credentials resolve in order: --api-key → $DEEPSEEK_API_KEY → V5 .env (dev only).
The key is NEVER printed (ADR-403 PII hygiene). Samples live in a SEPARATE temp
SQLite store, not the user's real PTG (§11.3 — eval DB must not pollute PTG).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import httpx

from tests.benchmark.match_atom import match_atom

_BENCHMARK_DIR = Path(__file__).parent
_SAMPLES_FILE = _BENCHMARK_DIR / "samples_v0.jsonl"
# Dev fallback: read creds from the V5 backend .env (never shipped, never printed).
_V5_ENV = Path("/Users/wugang/danao13/backend/.env")


def load_samples(limit: Optional[int] = None) -> list[dict]:
    samples = []
    with open(_SAMPLES_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                samples.append(json.loads(line))
    return samples[:limit] if limit else samples


# ── credentials (never print the key) ──────────────────────────────────────
def _resolve_creds(cli_key: Optional[str], cli_base: Optional[str]) -> tuple[str, str]:
    key = cli_key or os.environ.get("DEEPSEEK_API_KEY")
    base = cli_base or os.environ.get("DEEPSEEK_BASE_URL")
    if not key and _V5_ENV.exists():
        for line in _V5_ENV.read_text().splitlines():
            if line.startswith("DEEPSEEK_API_KEY="):
                key = line.split("=", 1)[1].strip()
            elif line.startswith("DEEPSEEK_BASE_URL="):
                base = base or line.split("=", 1)[1].strip()
    if not key:
        sys.exit("ERROR: no API key (--api-key, $DEEPSEEK_API_KEY, or V5 .env).")
    return key, (base or "https://api.deepseek.com")


# ── OpenAI-compatible LLM caller injected into the Atomizer ─────────────────
def make_llm_caller(*, base_url: str, api_key: str, model: str, provider: str,
                    timeout: float = 30.0, max_retries: int = 3):
    """Return a ``call_llm``-shaped callable that hits an OpenAI-compat endpoint
    directly (httpx), no hermes import. Retries 429/5xx with linear backoff."""

    def _caller(**kwargs):  # noqa: ANN003 — matches the Atomizer's call signature
        messages = kwargs["messages"]
        extra_body = kwargs.get("extra_body") or {}
        payload: dict = {
            "model": model,
            "messages": messages,
            "temperature": kwargs.get("temperature", 0),
        }
        if kwargs.get("max_tokens"):
            payload["max_tokens"] = kwargs["max_tokens"]
        if extra_body.get("response_format"):
            payload["response_format"] = extra_body["response_format"]
        last_err = None
        for attempt in range(max_retries):
            try:
                with httpx.Client(timeout=timeout) as client:
                    r = client.post(
                        f"{base_url.rstrip('/')}/v1/chat/completions",
                        headers={"Authorization": f"Bearer {api_key}"},
                        json=payload,
                    )
                    if r.status_code in (429, 500, 502, 503, 504):
                        last_err = RuntimeError(f"HTTP {r.status_code}")
                        time.sleep(1.0 * (attempt + 1))
                        continue
                    r.raise_for_status()
                data = r.json()
                usage = data.get("usage") or {}
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(
                        content=data["choices"][0]["message"]["content"]))],
                    model=data.get("model", model),
                    usage={"prompt_tokens": usage.get("prompt_tokens", 0),
                           "completion_tokens": usage.get("completion_tokens", 0)},
                    provider=provider,
                )
            except (httpx.HTTPError, KeyError) as exc:
                last_err = exc
                time.sleep(1.0 * (attempt + 1))
        raise last_err if last_err else RuntimeError("LLM call failed")

    return _caller


# ── per-sample scoring (ported from V5 evaluate_sample) ─────────────────────
def evaluate_sample(predicted_atoms: list[dict], expected: dict) -> dict:
    pred_atoms = predicted_atoms
    exp_atoms = expected.get("atoms", [])

    matched_expected: set[int] = set()
    matched_pred: set[int] = set()
    for pi, pred in enumerate(pred_atoms):
        for i, exp in enumerate(exp_atoms):
            if i not in matched_expected and match_atom(pred, exp):
                matched_expected.add(i)
                matched_pred.add(pi)
                break
    true_positives = len(matched_pred)
    false_positives = len(pred_atoms) - true_positives
    false_negatives = len(exp_atoms) - true_positives
    # FP/FN atom dicts (for --dump-fp root-cause diagnosis, ADR-V6-012 ⑦).
    fp_atoms = [pred_atoms[pi] for pi in range(len(pred_atoms)) if pi not in matched_pred]
    fn_atoms = [exp_atoms[i] for i in range(len(exp_atoms)) if i not in matched_expected]

    type_stats: dict[str, dict] = {}
    for atom_type in ["R3_Person", "R2_Task", "R1_SelfState", "R7_Expression"]:
        type_exp = [a for a in exp_atoms if a.get("type") == atom_type]
        type_pred = [a for a in pred_atoms if a.get("type") == atom_type]
        matched: set[int] = set()
        for pred in type_pred:
            for i, exp in enumerate(type_exp):
                if i not in matched and match_atom(pred, exp):
                    matched.add(i)
                    break
        type_stats[atom_type] = {"expected": len(type_exp), "predicted": len(type_pred),
                                 "matched": len(matched)}
    return {"tp": true_positives, "fp": false_positives, "fn": false_negatives,
            "fp_atoms": fp_atoms, "fn_atoms": fn_atoms,
            "type_stats": type_stats}


def run_evaluation(*, api_key: str, base_url: str, model: str, provider: str,
                   limit: Optional[int], workers: int, out: Optional[str],
                   dump_fp: Optional[str] = None) -> dict:
    from plugins.memory.ptg.atomizer import Atomizer
    from plugins.memory.ptg.store import PTGStore

    samples = load_samples(limit)
    tmpdir = tempfile.mkdtemp(prefix="ptg_eval_")
    store = PTGStore(db_path=str(Path(tmpdir) / "eval.db"))
    store.ensure_founder("eval-user", "eval@realityos.local")
    atomizer = Atomizer(
        store, user_id="eval-user",
        llm_caller=make_llm_caller(base_url=base_url, api_key=api_key, model=model,
                                   provider=provider),
        # Match production gate (V5 thresholds); the eval measures post-gate.
    )

    print(f"\n{'='*64}\nRealityOS V6 HL-12 Extraction Benchmark (post-gate, end-to-end)\n{'='*64}", flush=True)
    print(f"Samples: {len(samples)} | Model: {model} | Workers: {workers}", flush=True)
    print(f"Pipeline: Atomizer (v11 prompt + C5 gate + write + graph materialize)\n{'='*64}\n", flush=True)

    fpfn_records: list[dict] = []  # for --dump-fp root-cause diagnosis

    def _process(sample):
        sid = sample["id"]
        memo_id = store.insert_memo(user_id="eval-user", source_text=sample["input_text"])
        counts = atomizer.atomize(memo_id=memo_id, source_text=sample["input_text"])
        if not counts.get("ok"):
            return sid, None, "atomize failed (see DLQ)", None
        predicted = store.recent_atoms(user_id="eval-user", memo_id=memo_id)
        res = evaluate_sample(predicted, sample["expected_output"])
        if dump_fp and (res["fp_atoms"] or res["fn_atoms"]):
            fpfn_records.append({
                "id": sid, "input_text": sample["input_text"],
                "fp_atoms": res["fp_atoms"], "fn_atoms": res["fn_atoms"],
            })
        return sid, res, None, sample["input_text"]

    results = [None] * len(samples)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futures = {ex.submit(_process, s): i for i, s in enumerate(samples)}
        done = 0
        for fut in as_completed(futures):
            results[futures[fut]] = fut.result()
            done += 1
            if done % 10 == 0 or done == len(samples):
                print(f"  ... {done}/{len(samples)} done", flush=True)

    total = {"tp": 0, "fp": 0, "fn": 0, "errors": 0,
             "type_stats": {t: {"expected": 0, "predicted": 0, "matched": 0}
                            for t in ["R3_Person", "R2_Task", "R1_SelfState", "R7_Expression"]}}
    for i, item in enumerate(results):
        sid, res, err, _input = item
        if err or res is None:
            total["errors"] += 1
            print(f"  [{i+1}/{len(samples)}] ❌ {sid}: {err}")
            continue
        total["tp"] += res["tp"]; total["fp"] += res["fp"]; total["fn"] += res["fn"]
        for t, ts in res["type_stats"].items():
            for k in ("expected", "predicted", "matched"):
                total["type_stats"][t][k] += ts[k]
        ok = "✅" if res["fn"] == 0 and res["fp"] == 0 else "⚠️"
        print(f"  [{i+1}/{len(samples)}] {ok} {sid}: TP={res['tp']} FP={res['fp']} FN={res['fn']}")

    store.close()

    tp, fp, fn = total["tp"], total["fp"], total["fn"]
    precision = tp / (tp + fp) if (tp + fp) else 0
    recall = tp / (tp + fn) if (tp + fn) else 0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0

    print(f"\n{'='*64}\nRESULTS (post C5-gate, post-write)\n{'='*64}")
    print(f"Samples: {len(samples)} | Errors: {total['errors']}")
    print(f"Overall: TP={tp} FP={fp} FN={fn}")
    print(f"  Precision: {precision:.1%}\n  Recall:    {recall:.1%}\n  F1:        {f1:.1%}\n")

    targets = {"R3_Person": 0.85, "R2_Task": 0.80, "R1_SelfState": 0.70, "R7_Expression": 0.60}
    print(f"{'Type':<15}{'Exp':>7}{'Pred':>7}{'Match':>7}{'Prec':>9}{'Recall':>9}  gate")
    print("-" * 64)
    gates = {}
    for t, ts in total["type_stats"].items():
        e, p, m = ts["expected"], ts["predicted"], ts["matched"]
        pr = m / p if p else 0
        if e == 0:
            # No expected atoms of this type in the sample set → recall undefined.
            # Mark N/A (not a failure) so a small/sample-skewed run can't go red
            # on a 0/0 division. A type with real expected atoms is what's gated.
            gates[t] = {"precision": pr, "recall": None, "target": targets[t], "pass": None}
            print(f"  {t:<13}{e:>7}{p:>7}{m:>7}{pr:>9.1%}{'   N/A':>9}  {targets[t]:.0%} —")
            continue
        rc = m / e
        ok = rc >= targets[t]
        gates[t] = {"precision": pr, "recall": rc, "target": targets[t], "pass": ok}
        print(f"  {t:<13}{e:>7}{p:>7}{m:>7}{pr:>9.1%}{rc:>9.1%}  {targets[t]:.0%} {'✅' if ok else '❌'}")
    gated = [g for g in gates.values() if g["pass"] is not None]
    all_green = bool(gated) and all(g["pass"] for g in gated) and total["errors"] == 0
    print(f"\nGATE: {'ALL GREEN ✅' if all_green else 'RED ❌ — do NOT ship'}")
    print(f"{'='*64}")

    report = {"model": model, "provider": provider, "samples": len(samples),
              "errors": total["errors"], "precision": precision, "recall": recall, "f1": f1,
              "per_type": {t: {"expected": total['type_stats'][t]['expected'],
                               "predicted": total['type_stats'][t]['predicted'],
                               "matched": total['type_stats'][t]['matched'],
                               **gates[t]} for t in total["type_stats"]},
              "gate_all_green": all_green}
    if out:
        Path(out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Report written: {out}")
    if dump_fp:
        with open(dump_fp, "w", encoding="utf-8") as f:
            for rec in fpfn_records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"FP/FN dump written: {dump_fp} ({len(fpfn_records)} samples with errors)")
    return report


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Run the V6 HL-12 extraction benchmark")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--api-key", type=str, default=None)
    ap.add_argument("--base-url", type=str, default=None)
    ap.add_argument("--model", type=str, default="deepseek-chat")
    ap.add_argument("--provider", type=str, default="deepseek")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--out", type=str, default=None, help="Write JSON report to this path")
    ap.add_argument("--dump-fp", type=str, default=None,
                    help="Dump per-sample FP/FN atoms (JSONL) for root-cause diagnosis")
    args = ap.parse_args()
    key, base = _resolve_creds(args.api_key, args.base_url)
    run_evaluation(api_key=key, base_url=base, model=args.model, provider=args.provider,
                   limit=args.limit, workers=args.workers, out=args.out, dump_fp=args.dump_fp)

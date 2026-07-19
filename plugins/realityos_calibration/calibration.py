"""Founder daily calibration — §11.4/§11.5 contract (ADR-V6-028).

The daily calibration loop is the closed-loop quality channel the constitution
names as the ONLY sanctioned way a human mutates an atom's confidence:

  sample today's atoms → founder rates each (准 / 不准 / 惊喜) → verdicts land in
  ``feedback`` (audit trail) → "不准" atoms are demoted via
  ``PTGStore.adjust_atom_confidence`` (relation_confidence → floor, NOT deleted —
  C2 nothing-lost) → a ``correction_rate`` quality_metric is appended (§11.2, the
  §8 Gate KR evidence).

This module is the PURE logic (no I/O): a caller supplies the atoms + an
injectable ``rater`` callable, and ``run_calibration`` does the store writes +
returns a ``CalibrationResult``. The interactive CLI (``hermes_cli/calibrate_cmd``)
supplies the ``input()`` rater; tests supply a deterministic one. Splitting logic
from presentation is what makes the §11.5 contract testable without a tty.

Phase 1b scope (this module):
  * verdict → feedback + confidence demotion: DONE.
  * "惊喜" case library: DATA accumulates in feedback (target_type=
    calibration_surprise) now; the few-shot prompt injection that turns it into
    extraction improvement is Phase 2 (ADR-V6-022 deferral, documented).

C7: every store write here is already fail-open inside the store; this module
never raises on a store failure. A rater that raises (Ctrl-D / Ctrl-C / EOF) is
caught inside ``run_calibration`` — the loop ends early and the partial result
(whatever was already recorded) is finalized and returned, so a session ended
mid-way still writes its correction_rate over the atoms actually judged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── §11.4 3-way verdict vocabulary ──────────────────────────────────────────
# Encoded into feedback.target_type (NOT the rating CHECK — correct/surprise map
# to thumbs_up, wrong maps to thumbs_down; see insert_feedback docstring).
VERDICT_CORRECT = "correct"      # 准 — atom is right, no change.
VERDICT_WRONG = "wrong"          # 不准 — demote relation_confidence to the floor.
VERDICT_SURPRISE = "surprise"    # 惊喜 — unexpectedly valuable; case-library data.
VERDICT_SKIP = "skip"            # founder declined to judge this atom.

_ALL_VERDICTS = (VERDICT_CORRECT, VERDICT_WRONG, VERDICT_SURPRISE, VERDICT_SKIP)

# feedback.target_type prefix (kept short to stay inside the free-form column).
_FB_TARGET_CORRECT = "calibration_correct"
_FB_TARGET_WRONG = "calibration_wrong"
_FB_TARGET_SURPRISE = "calibration_surprise"

# §11.5 demotion floor: "不准" lowers relation_confidence to this regardless of
# its current value, so a confidently-wrong atom (R2 med 0.95) drops below the
# read-side gate thresholds instead of lingering at near-passing. NOT zero — the
# atom is demoted, not erased (C2; a future re-extraction or re-calibration can
# raise it again). Value is a tunable constant (not user-config) so the
# correction_rate telemetry stays comparable across runs.
CALIBRATION_FLOOR = 0.3


Rater = Callable[[str, int, int], str]
"""``rater(atom_display, idx, total) -> verdict`` — injectable verdict source.

Returns one of the VERDICT_* constants (or "skip"). The CLI supplies an
``input()``-backed rater; tests supply a deterministic one."""


@dataclass
class CalibrationResult:
    """Outcome of one calibration session (returned by ``run_calibration``)."""

    user_id: str
    metric_date: str
    total_sampled: int = 0          # atoms presented to the rater
    correct: int = 0                # 准
    wrong: int = 0                  # 不准
    surprise: int = 0               # 惊喜
    skipped: int = 0                # founder skipped
    judged: int = 0                 # correct + wrong + surprise (excludes skip)
    correction_rate: float = 0.0    # wrong / judged (§11.2 correction_rate KR)
    feedback_ids: List[str] = field(default_factory=list)   # rows written
    adjusted_atoms: int = 0         # rows actually demoted (adjust_atom_confidence rc)

    def summary(self) -> str:
        """One-line Chinese summary for the CLI footer."""
        pct = f"{self.correction_rate * 100:.0f}%" if self.judged else "—"
        return (
            f"校准 {self.total_sampled} 条：准 {self.correct} / 不准 {self.wrong} / "
            f"惊喜 {self.surprise} / 跳过 {self.skipped}；纠错率 {pct}（{self.metric_date}）"
        )


# ── atom rendering ──────────────────────────────────────────────────────────
def format_atom_for_display(atom: Dict[str, Any]) -> str:
    """Render an atom dict (from ``recent_atoms``) as a one-line Chinese label.

    Type-dispatched on ``atom["type"]``; unknown fields fall back to a generic
    dump so a future atom type never breaks the loop. Confidence is always shown
    (the founder rates against it — a high-confidence wrong atom is the signal).
    """
    t = atom.get("type", "Unknown")
    conf = atom.get("confidence")
    conf_str = f"{conf * 100:.0f}%" if isinstance(conf, (int, float)) else "?"
    prefix = f"[{conf_str}] "

    if t == "R3_Person":
        body = (f"人物 {atom.get('person_name', '?')}"
                f"（{atom.get('interaction_type') or '互动'}，"
                f"情感 {atom.get('sentiment') or '中性'}）")
    elif t == "R2_Task":
        body = (f"任务 {atom.get('task_description', '?')}"
                f"（紧急度 {atom.get('urgency') or '中'}）")
    elif t == "R7_Expression":
        body = (f"表达 [{atom.get('intent_class', 'Other')}] "
                f"{atom.get('content_summary', '?')}")
    elif t == "R8_Cognition":
        body = f"认知 {atom.get('topic', '?')}"
    elif t == "R12_Outcome":
        body = (f"结果 {atom.get('task_ref', '?')}"
                f" → {atom.get('outcome', 'completed')}")
    elif t == "R0_Entity":
        cat = atom.get("entity_category") or "实体"
        body = f"{cat} {atom.get('entity_name', '?')}"
    elif t == "R1_SelfState":
        body = (f"自我状态 {atom.get('state_type', '?')} "
                f"{atom.get('direction', '')}（强度 {atom.get('intensity', '?')}）")
    elif t == "R9_Emotion":
        body = (f"情绪 {atom.get('emotion_label', '?')}"
                f"（{atom.get('valence', '中性')}/{atom.get('arousal', '低')}）")
    else:
        body = f"{t} {atom}"
    return prefix + body


# ── verdict action ──────────────────────────────────────────────────────────
def _normalize_verdict(raw: str) -> str:
    v = (raw or "").strip().lower()
    if v in _ALL_VERDICTS:
        return v
    # Tolerate the Chinese short-forms a founder is likely to type.
    if v in ("准", "1", "y", "yes", "对"):
        return VERDICT_CORRECT
    if v in ("不准", "错", "0", "n", "no"):
        return VERDICT_WRONG
    if v in ("惊喜", "惊", "s", "surprise"):
        return VERDICT_SURPRISE
    if v in ("跳过", "skip", "skip", ""):
        return VERDICT_SKIP
    return VERDICT_SKIP  # unrecognized → skip (never treat as a verdict).


def _act_on_verdict(*, store, user_id: str, atom: Dict[str, Any],
                    verdict: str) -> Optional[str]:
    """Apply one verdict: write feedback (+ demote on WRONG). Returns feedback id.

    Never raises (store methods are fail-open). On WRONG the pre-adjust confidence
    snapshot is captured into feedback.comment BEFORE the demotion, so the audit
    trail records "was 0.95 → demoted to 0.30" even though the row is then changed.
    """
    atom_id = atom.get("atom_id")
    atom_type = atom.get("type", "")
    conf = atom.get("confidence")
    conf_snap = f"{conf:.3f}" if isinstance(conf, (int, float)) else "?"

    if verdict == VERDICT_CORRECT:
        return store.insert_feedback(
            user_id=user_id, target_type=_FB_TARGET_CORRECT,
            target_id=str(atom_id), rating="thumbs_up",
            comment=f"conf={conf_snap}",
        )
    if verdict == VERDICT_SURPRISE:
        # Case-library DATA accumulates here now; the few-shot injection that
        # turns "惊喜" atoms into extraction improvement is Phase 2.
        return store.insert_feedback(
            user_id=user_id, target_type=_FB_TARGET_SURPRISE,
            target_id=str(atom_id), rating="thumbs_up",
            comment=f"surprise;conf={conf_snap};{format_atom_for_display(atom)}",
        )
    if verdict == VERDICT_WRONG:
        fb_id = store.insert_feedback(
            user_id=user_id, target_type=_FB_TARGET_WRONG,
            target_id=str(atom_id), rating="thumbs_down",
            comment=f"wrong;was={conf_snap};→floor={CALIBRATION_FLOOR}",
        )
        if atom_id:
            rc = store.adjust_atom_confidence(
                user_id=user_id, atom_type=atom_type, atom_id=str(atom_id),
                new_confidence=CALIBRATION_FLOOR, reason="founder_calibration_wrong",
            )
            if rc:
                return fb_id
        return fb_id
    # SKIP: record nothing (the atom is neither endorsed nor disputed).
    return None


# ── session driver ──────────────────────────────────────────────────────────
def run_calibration(
    *,
    store,
    user_id: str,
    atoms: List[Dict[str, Any]],
    rater: Rater,
    metric_date: str,
) -> CalibrationResult:
    """Run one calibration session over ``atoms`` and return the aggregate.

    ``store`` is a PTGStore; ``user_id`` the founder; ``atoms`` the sampled
    today-atoms (from ``recent_atoms``); ``rater`` the injectable verdict source;
    ``metric_date`` the YYYY-MM-DD for the correction_rate quality_metric.

    The rater is called once per atom in order. A rater that raises ends the loop
    early (the CLI catches EOF); everything recorded so far is preserved and the
    correction_rate is computed over what was actually judged.
    """
    result = CalibrationResult(user_id=user_id, metric_date=metric_date)
    total = len(atoms)
    for idx, atom in enumerate(atoms, start=1):
        result.total_sampled = idx
        display = format_atom_for_display(atom)
        try:
            verdict = _normalize_verdict(rater(display, idx, total))
        except (EOFError, KeyboardInterrupt):
            # Founder ended the session mid-loop (Ctrl-D / Ctrl-C). Everything
            # recorded so far is preserved; finalize + return a partial result
            # rather than discarding it. The remaining atoms are simply not shown.
            logger.info("calibration session ended early by founder at %d/%d", idx, total)
            break
        if verdict == VERDICT_SKIP:
            result.skipped += 1
            continue
        fb_id = _act_on_verdict(
            store=store, user_id=user_id, atom=atom, verdict=verdict,
        )
        if fb_id:
            result.feedback_ids.append(fb_id)
        if verdict == VERDICT_CORRECT:
            result.correct += 1
        elif verdict == VERDICT_WRONG:
            result.wrong += 1
            # adjust_atom_confidence rowcount tells us a row was actually demoted.
            result.adjusted_atoms += 1  # optimistic; store never raises either way
        elif verdict == VERDICT_SURPRISE:
            result.surprise += 1

    result.judged = result.correct + result.wrong + result.surprise
    result.correction_rate = (
        result.wrong / result.judged if result.judged else 0.0
    )

    # §11.2 correction_rate telemetry — the §8 Gate KR evidence. Append-only time
    # series (insert_quality_metric never raises; C7).
    if result.judged:
        store.insert_quality_metric(
            user_id=user_id, metric_date=metric_date,
            metric_type="correction_rate", value=result.correction_rate,
            sample_size=result.judged,
            note=f"founder_calibration;wrong={result.wrong}",
        )
    logger.info("calibration session done: %s", result.summary())
    return result

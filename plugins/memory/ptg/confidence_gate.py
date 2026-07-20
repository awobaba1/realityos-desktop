"""Sample-size graded confidence gate (ADR-V6-042 / F6) — anti-fake-green.

The single biggest fake-green source in V6 (Agent ③ diagnosis, strategy-02):
the insight_aggregation.confidence column was graded by COARSE cold-start
metrics (total memo count + registration days) via InsightReportService
CONFIDENCE_MAP. A week with 50 memos but only 2 R9-emotion atoms would get
confidence=0.8 ("sufficient") for its EMOTION conclusion — even though that
conclusion rests on n=2. The LLM is happy to write a strong, specific
emotional narrative on 2 data points; the cache then presents it as
high-confidence insight. That is the textbook fake-green.

This module is the universal PRE-GATE for every insight producer (daily
report, weekly mirror, K-domain correlation, quark, theory): a pure
sample-size counter that floors confidence when the backing sample is thin.
Ported from danao14 ``realityos_correlation/compute.py:23-27`` and generalized
from a single K-domain call site to all V6 LLM insight output.

PRD §8.6.7 grading (verbatim thresholds):
  - sample < 10   → too few to draw any edge (floor; do NOT trust)
  - 10 ≤ sample < 30 → mid confidence (0.6)
  - sample ≥ 30   → high confidence (0.9)

Pure functions, no store/LLM deps — fully unit-testable in isolation.
"""

from __future__ import annotations

from typing import Dict, Mapping, Optional, Sequence, Tuple

# ── PRD §8.6.7 thresholds (do not change without an ADR) ────────────────────
MIN_SAMPLE: int = 10      # < MIN_SAMPLE → insufficient, no strong conclusion
MID_SAMPLE: int = 30      # MIN_SAMPLE..MID_SAMPLE-1 → partial; ≥ MID → sufficient
CONF_FLOOR: float = 0.1   # sample < MIN_SAMPLE → floor (signal "do not trust")
CONF_MID: float = 0.6     # 10-29
CONF_HIGH: float = 0.9    # ≥30

_SUFFICIENCY_INSUFFICIENT = "insufficient"
_SUFFICIENCY_PARTIAL = "partial"
_SUFFICIENCY_SUFFICIENT = "sufficient"
_VALID_LABELS = frozenset({_SUFFICIENCY_INSUFFICIENT, _SUFFICIENCY_PARTIAL,
                           _SUFFICIENCY_SUFFICIENT})


def grade_confidence_by_sample(sample_size: int) -> float:
    """PRD §8.6.7 sample-size grading → confidence in [0.0, 1.0].

    Returns CONF_FLOOR (not None) when sample < MIN_SAMPLE: the
    insight_aggregation.confidence column is CHECK BETWEEN 0 AND 1, so a null
    confidence cannot be persisted. CONF_FLOOR (0.1) signals "do not trust
    this conclusion — the backing sample is too thin" while remaining a valid
    cache value. Downstream renderers SHOULD treat confidence ≤ CONF_FLOOR as
    "display with caveat", never as "strong finding".

    Negative inputs are clamped to 0 (defensive — a buggy count is itself a
    signal to floor, not to crash).
    """
    n = max(0, int(sample_size))
    if n < MIN_SAMPLE:
        return CONF_FLOOR
    return CONF_HIGH if n >= MID_SAMPLE else CONF_MID


def sample_sufficiency(sample_size: int) -> str:
    """Map a sample size to the ``data_sufficiency`` label vocabulary used by
    insight_aggregation and the cold-start gate.

    Returns one of {'insufficient', 'partial', 'sufficient'}.
    """
    n = max(0, int(sample_size))
    if n < MIN_SAMPLE:
        return _SUFFICIENCY_INSUFFICIENT
    return _SUFFICIENCY_SUFFICIENT if n >= MID_SAMPLE else _SUFFICIENCY_PARTIAL


def cap_confidence_by_atom_samples(
    base_confidence: float,
    atom_counts: Mapping[str, int],
    kinds: Sequence[str],
    *,
    skip_absent: bool = True,
) -> Tuple[float, str, Optional[str]]:
    """Cap ``base_confidence`` by the WEAKEST atom-kind sample across ``kinds``.

    This is the wiring point for InsightReportService and any producer that
    draws conclusions spanning several atom kinds (emotions/cognitions/...):
    the cached confidence must reflect the thinnest backing sample, not just
    the coarse cold-start verdict. A report whose emotion section rests on
    n=2 but whose task section rests on n=40 is capped to the n=2 verdict
    (CONF_FLOOR) because the report as a whole cannot be trusted more than its
    weakest claim.

    Args:
        base_confidence: the cold-start / a-priori confidence (e.g. 0.8 for
            ``data_sufficiency='sufficient'``).
        atom_counts: mapping of atom-kind → count for this period (e.g.
            ``agg['atom_counts']`` from aggregate_window).
        kinds: the atom kinds the report draws conclusions from (e.g.
            ``['R9_Emotion','R8_Cognition','R1_SelfState']``).
        skip_absent: if True (default), kinds with count 0 are SKIPPED — an
            absent kind drives no conclusion (the report prompt omits empty
            sections, aggregation.py §14), so it must not drag the cap to
            CONF_FLOOR. This prevents a week that legitimately has 0 task
            outcomes from flooring an otherwise well-backed report. If ALL
            kinds are absent (or ``kinds`` empty), the cap floors defensively
            (no data at all → cannot trust). Set False for strict per-kind
            enforcement (rare; e.g. a producer that MUST see every kind).

    Returns:
        (capped_confidence, weakest_sufficiency_label, weakest_kind):
        ``capped_confidence`` = min(base_confidence, weakest-PRESENT-sample
        grade); ``weakest_sufficiency_label`` ∈ the vocabulary above;
        ``weakest_kind`` = the atom kind that drove the cap (None if no kind
        was considered — all absent / empty kinds — in which case the cap
        floors and the label is 'insufficient').
    """
    if not kinds:
        # No kinds declared → cannot prove sufficiency → floor defensively.
        return min(float(base_confidence), CONF_FLOOR), _SUFFICIENCY_INSUFFICIENT, None

    weakest_conf: Optional[float] = None
    weakest_label = _SUFFICIENCY_SUFFICIENT
    weakest_kind: Optional[str] = None
    for kind in kinds:
        n = int(atom_counts.get(kind, 0))
        if skip_absent and n <= 0:
            continue  # absent kind drives no conclusion → does not cap
        grade = grade_confidence_by_sample(n)
        # Track the minimum grade (most pessimistic). Ties keep the first kind.
        if weakest_conf is None or grade < weakest_conf:
            weakest_conf = grade
            weakest_label = sample_sufficiency(n)
            weakest_kind = kind

    if weakest_conf is None:
        # Every kind was absent (skip_absent) → no data at all → floor.
        return min(float(base_confidence), CONF_FLOOR), _SUFFICIENCY_INSUFFICIENT, None
    capped = min(float(base_confidence), weakest_conf)
    return capped, weakest_label, weakest_kind


def merge_sufficiency(*labels: str) -> str:
    """Take the most pessimistic of several sufficiency labels.

    Used when a producer has BOTH a cold-start verdict (from registration
    days / memo total) and a sample-size verdict (from this gate): the
    effective data_sufficiency is the worse of the two, never the better.
    Unknown / empty labels are treated as 'insufficient' (defensive — a
    missing verdict cannot improve trust).
    """
    rank = {_SUFFICIENCY_SUFFICIENT: 2, _SUFFICIENCY_PARTIAL: 1,
            _SUFFICIENCY_INSUFFICIENT: 0}
    worst = 2  # start optimistic
    for label in labels:
        worst = min(worst, rank.get(label, 0))
    return [_SUFFICIENCY_INSUFFICIENT, _SUFFICIENCY_PARTIAL,
            _SUFFICIENCY_SUFFICIENT][worst]


def is_valid_sufficiency_label(label: str) -> bool:
    """True if ``label`` is one of the three canonical sufficiency labels."""
    return label in _VALID_LABELS

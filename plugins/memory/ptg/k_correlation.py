"""K-domain correlation discovery (ADR-V6-044 / F7) — Phase 2 co-occurrence.

Pure statistics: read R9 feeling_events, group by ``trigger_source.entity``,
compute per-entity ``P(negative | entity)`` vs the baseline ``P(negative)``,
sample-size graded confidence (PRD §8.6.7 via the F6 confidence gate), and
write ``K_Correlation`` edges to relations with full evidence. **Zero LLM.**
correlation != causation (PRD 01:93): edges state co-occurrence only —
``delta.method = "conditional_probability"`` makes that explicit.

Ported from danao14 ``realityos_correlation/compute.py``, adapted to V6:

  * **Valence is categorical.** danao14's R9 ``emotion_vad.valence`` is a 0-1
    float (threshold 0.5). V6's R9 ``valence`` is the string
    ``positive|negative|neutral`` (atom_schemas.R9EmotionAtom pattern), so
    "negative" = ``valence == "negative"`` — cleaner than a float cut.
  * **Confidence via F6.** ``grade_confidence_by_sample`` (the same PRD §8.6.7
    gate the insight reports use): <10 → no edge, 10-29 → 0.6, ≥30 → 0.9.
  * **Real entity FKs.** V6 ``relations.object_id`` FKs ``entities(id)`` (danao14
    stored the name directly). So edges resolve entity NAME → entity ID.
  * **Atomic recompute.** recompute + revive + invalidate land in one
    ``store.transaction()`` (the F4 helper) so a mid-run failure can't leave
    orphan edges.

Calibration exclusion (wrong-atom filtering) is DEFERRED — danao14 drops
atoms marked wrong via the calibration DB. The F6 sample gate (≥10) is the
baseline credibility floor here; calibration exclusion is a documented
Phase 2-A follow-up, NOT pretended present.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from .confidence_gate import MIN_SAMPLE, grade_confidence_by_sample

logger = logging.getLogger(__name__)

K_RELATION_TYPE = "K_Correlation"
NEG_VALENCE = "negative"   # V6 categorical: this string marks a negative emotion
LIFT_NEG = 1.2             # P(neg|A)/P(neg|all) >= 1.2 → A skews negative
LIFT_POS = 0.83            # <= 0.83 → A skews non-negative (positive edge)
MODEL_VERSION = "k-cooccurrence-v1"


def _polarity(lift: float) -> Optional[str]:
    """Map a lift ratio to a polarity label, or None (no edge) when neutral."""
    if lift >= LIFT_NEG:
        return "negative"
    if lift <= LIFT_POS:
        return "positive"
    return None


def _entity_name_to_id_map(store: Any, user_id: str) -> Dict[str, str]:
    """Lowercased entity name → entity id, for the user's non-deleted nodes.

    K-edges object_id FK entities(id); trigger_source.entity is a name, so this
    resolves the FK. Fail-open: returns {} on any error (→ no edges this run).
    """
    out: Dict[str, str] = {}
    try:
        rows = store._conn.execute(
            "SELECT id, entity_name FROM entities "
            "WHERE user_id = ? AND deleted_at IS NULL",
            (user_id,),
        ).fetchall()
    except Exception:  # noqa: BLE001
        logger.warning("K-correlation entity map read failed:", exc_info=True)
        return out
    for r in rows:
        nm = (r["entity_name"] or "").strip().lower()
        if nm:
            out[nm] = r["id"]
    return out


def compute_k_correlations(
    store: Any, user_id: str, *, now_ts: Optional[float] = None,
) -> int:
    """Compute R9 trigger-entity valence correlation edges → write relations.

    Returns the count of edges confirmed this run (sample-gated + significant).
    Never raises (C7): any store error is logged and the run returns 0. Edges
    that no longer pass the gate are invalidated (stale_at set) inside the same
    transaction; the "current view" reads ``stale_at IS NULL``.
    """
    if now_ts is None:
        now_ts = time.time()

    try:
        rows = store._conn.execute(
            "SELECT id, trigger_source, emotion_vad FROM feeling_events "
            "WHERE user_id = ? AND atom_kind = 'R9' AND emotion_vad IS NOT NULL "
            "AND deleted_at IS NULL",
            (user_id,),
        ).fetchall()
    except Exception:  # noqa: BLE001
        logger.warning("K-correlation R9 read failed (user %s):", user_id, exc_info=True)
        return 0

    name_to_id = _entity_name_to_id_map(store, user_id)

    # samples: entity_id → list of (valence, event_id). entity_id so the K-edge
    # object_id FK is satisfiable and mark_stale's keep_object_ids are ids too.
    samples: Dict[str, List[Tuple[str, str]]] = {}
    total = 0
    total_neg = 0
    for r in rows:
        try:
            trig = json.loads(r["trigger_source"]) if r["trigger_source"] else {}
            vad = json.loads(r["emotion_vad"]) if r["emotion_vad"] else {}
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        entity_name = (trig.get("entity") or "").strip().lower()
        valence = (vad.get("valence") or "").strip()
        if not entity_name or valence not in ("positive", "negative", "neutral"):
            continue
        eid = name_to_id.get(entity_name)
        if eid is None:
            # entity vanished from the graph (soft-deleted) → can't FK; skip.
            continue
        samples.setdefault(eid, []).append((valence, r["id"]))
        total += 1
        if valence == NEG_VALENCE:
            total_neg += 1

    try:
        self_id = store.ensure_self_entity(user_id)
    except Exception:  # noqa: BLE001
        logger.warning("K-correlation self-entity resolve failed:", exc_info=True)
        return 0

    # No usable R9 data → every existing K edge loses its current backing.
    # Still append-only (pure UPDATE), value/delta preserved for history.
    if total == 0:
        try:
            with store.transaction():
                store.mark_k_correlation_stale(
                    user_id=user_id, subject_id=self_id,
                    keep_object_ids=set(), now_ts=now_ts)
        except Exception:  # noqa: BLE001
            logger.warning("K-correlation all-stale mark failed:", exc_info=True)
        return 0

    p_neg_baseline = total_neg / total
    confirmed: set = set()  # entity_ids passing the gate this run
    written = 0
    try:
        with store.transaction():
            for eid, vals in samples.items():
                sample_size = len(vals)
                # PRD §8.6.7 hard gate: <10 samples → NO edge (not a low-conf edge
                # — a correlation on <10 data points is not worth asserting).
                # NB: grade_confidence_by_sample returns CONF_FLOOR for <10 (the
                # insight context keeps generating with floor confidence); the
                # K-domain needs the harder danao14 semantic, so check the
                # threshold explicitly rather than relying on a None return.
                if sample_size < MIN_SAMPLE:
                    continue
                conf = grade_confidence_by_sample(sample_size)  # 0.6 (10-29) / 0.9 (≥30)
                neg_count = sum(1 for v, _ in vals if v == NEG_VALENCE)
                p_neg_given = neg_count / sample_size
                lift = (p_neg_given / p_neg_baseline) if p_neg_baseline > 0 else 1.0
                polarity = _polarity(lift)
                if polarity is None:
                    continue  # neutral co-occurrence → no edge
                # Most common valence for this entity (descriptive; categorical
                # analogue of danao14's valence_mean).
                counts = {"positive": 0, "negative": 0, "neutral": 0}
                for v, _ in vals:
                    counts[v] += 1
                valence_mode = max(counts, key=counts.get)
                store.upsert_relation(
                    user_id=user_id, subject_id=self_id, object_id=eid,
                    relation_type=K_RELATION_TYPE, value=polarity, confidence=conf,
                    delta={
                        "dimension": "emotion_valence",
                        "method": "conditional_probability",
                        "correlation_not_causation": True,
                        "sample_size": sample_size,
                        "neg_count": neg_count,
                        "p_neg_given_entity": round(p_neg_given, 4),
                        "p_neg_baseline": round(p_neg_baseline, 4),
                        "lift": round(lift, 4),
                        "polarity": polarity,
                        "valence_mode": valence_mode,
                        "evidence_event_ids": [eid_ for _, eid_ in vals],
                        "first_detected": now_ts,
                        "model_version": MODEL_VERSION,
                    },
                )
                # upsert_relation already cleared stale_at on the revive path;
                # no separate UPDATE needed (cleaner than danao14's two-step).
                confirmed.add(eid)
                written += 1
            # Invalidate K edges that did NOT pass the gate this run (pure
            # UPDATE; value/delta/evidence preserved — C2 append-only).
            store.mark_k_correlation_stale(
                user_id=user_id, subject_id=self_id,
                keep_object_ids=confirmed, now_ts=now_ts)
    except Exception:  # noqa: BLE001 — the transaction rolled back; report 0
        logger.warning("K-correlation write tx failed (user %s):", user_id, exc_info=True)
        return 0
    return written

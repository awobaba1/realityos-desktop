"""RealityOS V6 — Quark → atom aggregation (ADR-V6-039 Batch1 / ADR-V6-049 B1).

Materializes validated ``QuarkRecord`` instances into the R-atom event tables
via the fixed ``QUARK_TO_ATOM_MAP`` (phase2_contracts.py:108). For the Phase-2
text subset we materialize the PRIMARY atom per kind only:

- ``Identity`` → ``identity_events`` (R3_Person) — ``person_name = value``.
- ``Meaning``  → ``meaning_events`` (R7_Expression) — ``task_description =
  value``. We deliberately use R7 (Expression), NOT R2 (Task): a Quark is a
  signal ("saw '要交报告'"), and stamping task_status/deadline on it would
  fabricate lifecycle the signal doesn't carry. A real task is promoted
  explicitly later (A3 / the atomizer). Honest, not lossy: the value is
  preserved verbatim.
- ``Feeling``  → ``feeling_events`` atom_kind='R9' (R9_Emotion) —
  ``trigger_source = {trigger: value}``.

The AUXILIARY atoms in the map (R1_SelfState from Identity/Feeling) are
deferred: R1 needs acoustic-derived intensity, and the contract marks Feeling
as "文本弱版" (text-weak). Fabricating a self-state atom from every text
mention would be the exact noise ADR-V6-039 warns against — so aux R1 is a
Phase 2.5 (acoustic) step, recorded here as a deliberate non-fake-green
boundary.

Every aggregated atom carries the Quark's ``source_id`` as ``memo_id`` (so the
correction loop A4 / the founder can trace it back) and the Quark's confidence
as both ``confidence_base`` and ``relation_confidence``. Never raises (C7);
returns a per-kind written-count summary.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from plugins.memory.ptg.phase2_contracts import QuarkRecord

logger = logging.getLogger(__name__)


def aggregate_quarks_to_atoms(
    store, quarks: List[QuarkRecord], *, user_id: str,
    source_text: str = "",
    llm_call_id: Optional[str] = None,
) -> Dict[str, int]:
    """Write ``quarks`` into the R-atom event tables (PRIMARY mapping).

    Returns ``{Identity, Meaning, Feeling, written, dropped}`` counts.
    ``written`` = total atoms inserted; ``dropped`` = records whose kind isn't
    in the Phase-2 subset (defensive — the extractor already filters, but a
    later-phase kind reaching here is ignored, not fabricated). Never raises
    (C7) — each insert is fail-isolated so one bad record can't abort the batch.

    ``llm_call_id`` (ADR-V6-071) threads the extractor's LLM-call id into every
    written atom event (C6 traceability — every event MUST carry llm_call_id).
    Previously dropped → all quark-derived atoms landed with NULL llm_call_id.
    """
    counts = {"Identity": 0, "Meaning": 0, "Feeling": 0, "written": 0, "dropped": 0}
    for q in quarks:
        try:
            n = _aggregate_one(store, q, user_id=user_id, source_text=source_text,
                               llm_call_id=llm_call_id)
            if n:
                counts[q.kind] = counts.get(q.kind, 0) + 1
                counts["written"] += 1
            else:
                counts["dropped"] += 1
        except Exception as exc:  # noqa: BLE001 — C7: one record's failure isolates
            logger.warning("quark aggregate failed (kind=%s value=%r): %s",
                           q.kind, q.value, exc)
            counts["dropped"] += 1
    return counts


def _aggregate_one(
    store, q: QuarkRecord, *, user_id: str, source_text: str,
    llm_call_id: Optional[str] = None,
) -> bool:
    """Materialize one Quark into its PRIMARY atom. Returns True if written."""
    conf = float(q.confidence)
    memo_id = q.source_id or None
    src = source_text or q.evidence.get("span") or q.value

    if q.kind == "Identity":
        store.insert_identity_event(
            user_id=user_id, source_text=src, person_name=q.value,
            confidence_base=conf, relation_confidence=conf, memo_id=memo_id,
            llm_call_id=llm_call_id)
        return True

    if q.kind == "Meaning":
        # R7_Expression (PRIMARY). intent_class='Other' + the signal as
        # task_description — no fabricated task_status/deadline.
        store.insert_meaning_event(
            user_id=user_id, source_text=src, intent_class="Other",
            task_description=q.value, atom_kind="R7",
            confidence_base=conf, relation_confidence=conf, memo_id=memo_id,
            llm_call_id=llm_call_id)
        return True

    if q.kind == "Feeling":
        # R9_Emotion (PRIMARY). trigger_source carries the emotion token.
        store.insert_feeling_event(
            user_id=user_id, source_text=src,
            confidence_base=conf, relation_confidence=conf,
            state_type="mood", direction="stable", intensity="low",
            atom_kind="R9",
            trigger_source=json.dumps(
                {"trigger": q.value, "entity": "",
                 "quark": "R9_Emotion"}, ensure_ascii=False),
            memo_id=memo_id, llm_call_id=llm_call_id)
        return True

    # Later-phase kind (Time/Behavior/Context/Network) — pinned but not
    # produced in Phase 2. Ignore, don't fabricate (防空跑).
    logger.debug("quark kind %s not in Phase-2 subset; dropped", q.kind)
    return False

"""RealityOS V6 — quark plugin (架构 §4.3E, ADR-V6-039 Batch1 / ADR-V6-049 B1).

The Quark extraction layer: the concrete ``QuarkExtractor`` implementer behind
the Phase-2 Protocol (``plugins.memory.ptg.phase2_contracts``), plus the
fixed Quark→atom aggregation (``QUARK_TO_ATOM_MAP``). Phase-2 text subset only
(Identity / Meaning / Feeling); the other 4 kinds are pinned for later phases
but NOT produced (防空跑 — depends on cut acoustic/multi-person/SED pipelines).

Single entry point ``extract_and_aggregate(store, *, user_id, capture_text,
quark_evidence_rows, source_text)`` runs the closed loop: extract (LLM + C5
QuarkRecord gate) → aggregate (write PRIMARY atoms). Every LLM call logged
(C6); every failure DLQ + degrade (C7); prompt versioned (C6).

``register`` is a no-op debug log (like ``realityos_insights``) — the service
is called explicitly by the desktop capture flow / a CLI trigger (the wiring
is B3, not fake-green'd here).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from plugins.memory.ptg.phase2_contracts import QuarkRecord
from plugins.realityos_quark.aggregation import aggregate_quarks_to_atoms
from plugins.realityos_quark.extractor import QuarkExtractorImpl

__all__ = ["QuarkExtractorImpl", "aggregate_quarks_to_atoms",
           "extract_and_aggregate", "register"]

logger = logging.getLogger(__name__)


def extract_and_aggregate(
    store, *, user_id: str, capture_text: str,
    quark_evidence_rows: Optional[List[dict]] = None,
    source_text: str = "",
    extractor: Optional[QuarkExtractorImpl] = None,
    caller: Any = None,
) -> Dict[str, Any]:
    """Closed loop: extract Quarks → aggregate into PRIMARY atoms.

    Returns ``{ok, extracted, aggregated, counts, llm_call_id}``. The
    ``llm_call_id`` (ADR-V6-071) is the extractor's LLM-call id, threaded into
    every atom event aggregation writes (C6 traceability — every event MUST
    carry llm_call_id) AND returned for the caller. ``None`` when no LLM call
    was made (empty input) — honest, not stale. Never raises (C7) — extraction
    failure ⇒ empty result + DLQ, aggregation of whatever validated records did
    come back proceeds (a partial batch is honest, not all-or-nothing). ``ok``
    reflects whether extraction produced records.
    """
    ext = extractor or QuarkExtractorImpl(store, caller=caller)
    ext.set_user_id(user_id)
    rows = quark_evidence_rows or []
    quarks: List[QuarkRecord] = []
    try:
        quarks = ext.extract(rows, capture_text)
    except Exception as exc:  # noqa: BLE001 — defensive; extract itself is C7
        logger.warning("quark extract raised (user=%s): %s", user_id, exc)
    # ADR-V6-071: thread the extractor's llm_call_id into aggregation so every
    # written atom event carries it (C6 traceability). Previously the id was
    # discarded inside extract() → NULL llm_call_id on every quark-derived atom,
    # and this return key was a documentation lie (promised but never populated).
    llm_call_id = getattr(ext, "_last_llm_call_id", None)
    counts = aggregate_quarks_to_atoms(
        store, quarks, user_id=user_id, source_text=source_text or capture_text,
        llm_call_id=llm_call_id)
    return {
        "ok": len(quarks) > 0,
        "extracted": len(quarks),
        "aggregated": counts["written"],
        "counts": counts,
        "llm_call_id": llm_call_id,
    }


def register(*_args, **_kwargs) -> None:
    """Plugin registration no-op (mirrors realityos_insights/sovereignty).

    The service is invoked explicitly; registration only logs discovery so a
    misconfigured plugin never silently appears "live" (反假绿).
    """
    logger.debug("realityos_quark registered (explicit invocation only)")

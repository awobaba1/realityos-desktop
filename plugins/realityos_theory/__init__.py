"""RealityOS V6 — theory plugin (架构 §4.3E line 389, ADR-V6-039 Batch2 / ADR-V6-050 B2).

The Theory derivation layer: the concrete ``TheoryEngine`` implementer behind
the Phase-2 Protocol (``plugins.memory.ptg.phase2_contracts``). Phase 2 = LLM
approximation of the 7-PC / 5-FR derivation skeletons; Phase 2.5+ swaps in
statistical formulas behind the SAME interface.

Honest-degradation contract (ADR-V6-040 D4 / contract v2): every derivation
carries a machine-readable ``degraded`` flag + ``basis``. The 3 PC dims with no
text source (Energy / Social / Environment) are forced ``degraded=True,
score=0.0``; Cognition is ``degraded=True`` (V6 R8 has no continuous score);
only Time / Emotion / Execution are non-degraded text approximations. The
engine stamps this deterministically AFTER the LLM returns — the LLM cannot
know it lacks data, so the engine enforces the contract (never the LLM).

Single-direction data flow (架构 §4.7): ``derive`` READS atoms/relations, writes
ONLY to ``insight_aggregation`` (PC → constraint_state, FR → fr_snapshot) —
never back to the atom layer.

``derive_and_persist`` is the closed-loop entry: gather atoms/relations → derive
→ persist each TheoryDerivation to insight_aggregation. ``register`` is a no-op
(scheduled/CLI invocation is B3).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from plugins.memory.ptg.phase2_contracts import TheoryDerivation
from plugins.realityos_theory.engine import TheoryEngineImpl

__all__ = ["TheoryEngineImpl", "derive_and_persist", "register"]

logger = logging.getLogger(__name__)


def derive_and_persist(
    store, *, user_id: str, atoms: List[dict], relations: List[dict],
    engine: Optional[TheoryEngineImpl] = None, caller: Any = None,
    period_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Closed loop: derive PC/FR skeletons → persist to insight_aggregation.

    Returns ``{ok, derived, persisted, degraded_count, derivations}``. Each
    TheoryDerivation lands as its own insight_aggregation row keyed by
    (user, aggregation_type, period_key+"|"+name) so PC + FR coexist. Never
    raises (C7); a persist failure on one row is isolated (logged, not fatal).
    ``period_key`` defaults to the current Beijing date (caller may override).
    """
    eng = engine or TheoryEngineImpl(store, caller=caller)
    derivations: List[TheoryDerivation] = []
    try:
        derivations = eng.derive(user_id, atoms or [], relations or [])
    except Exception as exc:  # noqa: BLE001 — defensive; derive itself is C7
        logger.warning("theory derive raised (user=%s): %s", user_id, exc)

    pk = period_key or _default_period_key()
    persisted = 0
    for d in derivations:
        if _persist_one(store, user_id=user_id, derivation=d, period_key=pk):
            persisted += 1
    return {
        "ok": len(derivations) > 0,
        "derived": len(derivations),
        "persisted": persisted,
        "degraded_count": sum(1 for d in derivations if d.degraded),
        "derivations": derivations,
    }


def _persist_one(store, *, user_id: str, derivation: TheoryDerivation,
                 period_key: str) -> bool:
    """Write one TheoryDerivation to insight_aggregation. Returns True on success.

    result_data carries the full derivation (score + rationale + basis +
    degraded) so a consumer renders degradation honestly. The row's own
    ``confidence`` mirrors the derivation's (degraded ⇒ low). Never raises.
    """
    try:
        result = {
            "kind": derivation.kind, "name": derivation.name,
            "score": derivation.score, "rationale": derivation.rationale,
            "basis": derivation.basis, "degraded": derivation.degraded,
        }
        store.upsert_insight(
            user_id=user_id,
            aggregation_type=derivation.aggregation_type,
            period_key=f"{period_key}|{derivation.name}",
            period_start=period_key, period_end=period_key,
            input_data=json.dumps({"atom_count": "n/a"}, ensure_ascii=False),
            result_data=json.dumps(result, ensure_ascii=False),
            confidence=derivation.confidence,
            data_sufficiency="partial" if derivation.degraded else "sufficient",
            generated_by="manual", schema_version="v1",
            expires_at=_expiry(period_key),
        )
        return True
    except Exception as exc:  # noqa: BLE001 — C7: one row's failure isolates
        logger.warning("theory persist failed (%s/%s): %s",
                       derivation.kind, derivation.name, exc)
        return False


def _default_period_key() -> str:
    from plugins.realityos_insights._base import beijing_now
    return beijing_now().strftime("%Y-%m-%d")


def _expiry(period_key: str) -> str:
    """insight_aggregation.expires_at = period_end + 1 day (short TTL — theory
    re-derives on each scheduled run, so a long cache would serve stale scores)."""
    from plugins.realityos_insights._base import _add_days_iso
    return _add_days_iso(period_key, 1)


def register(*_args, **_kwargs) -> None:
    """Register the theory plugin + start the startup-lazy derivation scheduler.

    B3 (ADR-V6-051): wires the startup-lazy scheduler (mirrors insights,
    ADR-V6-019). Once per process, opt-out; the daemon thread opens the shared
    store, waits for the founder, and re-derives today's PC/FR skeletons when
    missing/stale-prompt. Fail-open throughout (C7); disabled under pytest +
    ``REALITYOS_THEORY_AUTOSCHED=0`` so a misconfigured plugin never silently
    burns an LLM call (反假绿).
    """
    from plugins.realityos_theory.scheduling import (
        _scheduler_should_start, start_scheduler_if_due)
    started = start_scheduler_if_due(enabled=_scheduler_should_start())
    logger.debug(
        "realityos_theory registered (TheoryEngine + derive_and_persist live; "
        "startup-lazy scheduler %s)",
        "started" if started else "not started (disabled / already running)")

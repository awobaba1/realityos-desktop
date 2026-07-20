"""RealityOS V6 — Theory engine (ADR-V6-039 Batch2 / ADR-V6-050 B2).

The concrete ``TheoryEngine`` implementer behind the
``plugins.memory.ptg.phase2_contracts.TheoryEngine`` Protocol. Derives the 7
Personal-Constraint (PC) + 5 Life-Framework (FR) skeleton scores from the
materialized atom + relation graph, via an LLM call that is then
**deterministically post-processed** to enforce the honest-degradation
contract (ADR-V6-040 D4 / contract v2).

The iron rule: a Theory derivation whose data source is missing or severely
degraded (no acoustic / multi-person / sleep-continuity chain) MUST carry
``degraded=True`` + a ``basis`` explaining the gap — and the consumer MUST
render it as "数据不足/降级", never as a real score or "平稳". The LLM cannot
know it lacks data, so the ENGINE stamps ``degraded`` / ``basis`` after the
LLM returns. This is the anti-fake-green core: degradation is a deterministic
contract, not the LLM's self-assessment.

Honest scope (ADR-V6-040 line 37, Agent ④ 实证): without acoustics, only 4/7
PC dims are text-reachable (Time / Emotion / Execution / Cognition), and
Cognition is severely degraded (V6 R8 has no continuous cognition score). The
other 3 (Energy / Social / Environment) have no text source at all → forced
``degraded=True``, ``score=0.0`` (the LLM's neutral 0.5 guess is discarded so
no consumer can render an unsupported dim as a measured value).

C5/C6/C7 rails mirror ``realityos_quark.extractor`` + ``realityos_insights._base``:
TheoryDerivation pydantic gate, llm_call_logs, DLQ + degrade to []. Single-
direction data flow (架构 §4.7): derive READS atoms/relations, writes ONLY to
insight_aggregation (the insight cache) — never back to the atom layer.
"""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from plugins.memory.ptg.phase2_contracts import (
    FR_DIMENSIONS, PC_CONSTRAINTS, TheoryDerivation)

logger = logging.getLogger(__name__)

_PROMPT_FILE = Path(__file__).parent / "prompt_templates" / "theory_derive_v1.md"
PROMPT_VERSION = "v1"
ENGINE = "theory"

# Deterministic PC-degradation map (ADR-V6-040 D4 / Agent ④ 实证).
# (degraded, basis, keep_score). keep_score=False ⇒ the LLM's score is discarded
# and forced to 0.0 (an unsupported dim must never read as a measured value);
# keep_score=True ⇒ the LLM score is kept but degraded=True + low confidence.
#
# Without acoustics only Time/Emotion/Execution/Cognition are text-reachable;
# Cognition is degraded because V6 R8 has no continuous score.
_PC_DEGRADATION: Dict[str, Tuple[bool, str, bool]] = {
    "Time":        (False, "基于事件时间戳的活动节奏文本近似", True),
    "Energy":      (True,  "需 R1 fatigue + R10 sleep 连续值（Phase 2.5 统计公式），"
                           "文本无据，score 不采信", False),
    "Cognition":   (True,  "V6 R8 无连续 cognition score，仅离散任务近似，严重降级", True),
    "Emotion":     (False, "基于 R1/R9 情绪原子", True),
    "Social":      (True,  "需 Network quark（多人场景，Phase 2.5+），文本无据，"
                           "score 不采信", False),
    "Execution":   (False, "基于 R2/R12 任务完成率", True),
    "Environment": (True,  "需 Context quark（声学场景，Phase 3），文本无据，"
                           "score 不采信", False),
}


def _default_llm_caller(**kwargs: Any) -> Any:
    from agent.auxiliary_client import call_llm  # type: ignore[import-not-found]

    return call_llm(**kwargs)


class TheoryEngineImpl:
    """Concrete TheoryEngine (satisfies the Phase-2 Protocol structurally).

    Inject ``store`` (for llm_call_logs + DLQ + insight_aggregation),
    ``caller`` (LLM), ``now_fn``. ``derive`` returns validated
    ``TheoryDerivation`` instances with deterministic degraded/basis stamps.
    """

    def __init__(
        self, store, *,
        caller: Optional[Callable[..., Any]] = None,
        timeout: float = 30.0,
        main_runtime: Any = None,
        temperature: float = 0.3,
        max_tokens: int = 900,
    ) -> None:
        self._store = store
        self._caller = caller or _default_llm_caller
        self._timeout = timeout
        self._main_runtime = main_runtime
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._system_prompt: Optional[str] = None
        self._user_id = ""

    # -- Protocol implementation -----------------------------------------

    def derive(
        self, user_id: str, atoms: list[dict], relations: list[dict],
    ) -> list[TheoryDerivation]:
        """Derive PC + FR skeletons. Never raises (C7)."""
        self._user_id = user_id
        raw, _llm_id, ok = self._llm(atoms or [], relations or [])
        if not ok:
            return []
        return self._build_derivations(raw)

    # -- LLM + C5/C6/C7 rails --------------------------------------------

    def _system(self) -> str:
        if self._system_prompt is None:
            self._system_prompt = _PROMPT_FILE.read_text(encoding="utf-8")
        return self._system_prompt

    def _llm(
        self, atoms: list[dict], relations: list[dict],
    ) -> tuple[dict, Optional[str], bool]:
        llm_call_id = str(uuid.uuid4())
        system_prompt = self._system()
        user_prompt = self._format_user_prompt(atoms, relations)
        prompt_input = {
            "engine": ENGINE,
            "prompt_version": PROMPT_VERSION,
            "atom_count": len(atoms),
            "relation_count": len(relations),
            "system_prompt_hash": _sha_short(system_prompt),
        }
        start = _monotonic()
        try:
            response = self._caller(
                task=ENGINE,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=self._temperature,
                max_tokens=self._max_tokens,
                timeout=self._timeout,
                main_runtime=self._main_runtime,
            )
        except Exception as exc:  # noqa: BLE001 — fail-open, C7
            latency = int((_monotonic() - start) * 1000)
            self._log_call(llm_call_id, prompt_input, response=None, model="unknown",
                           provider=None, in_toks=0, out_toks=0, latency_ms=latency,
                           success=False, error_type=type(exc).__name__, error_msg=str(exc))
            self._safe_dlq(f"{ENGINE}_error", str(exc),
                           {"atom_count": len(atoms)})
            logger.warning("theory LLM call failed: %s", exc)
            return {}, llm_call_id, False

        raw_text = _response_text(response)
        model = getattr(response, "model", "unknown") or "unknown"
        provider = getattr(response, "provider", None)
        usage = getattr(response, "usage", None)
        in_toks = _usage_tokens(usage, "prompt_tokens")
        out_toks = _usage_tokens(usage, "completion_tokens")
        latency = int((_monotonic() - start) * 1000)

        parsed, schema_valid = self._parse_json(raw_text)
        self._log_call(llm_call_id, prompt_input, response={"content": raw_text[:2000]},
                       model=model, provider=provider, in_toks=in_toks, out_toks=out_toks,
                       latency_ms=latency, success=True, schema_valid=schema_valid)
        if not schema_valid:
            self._safe_dlq(f"{ENGINE}_schema_invalid",
                           "LLM output failed theory JSON shape",
                           {"raw_len": len(raw_text), "preview": raw_text[:200]})
            logger.warning("theory C5 invalid (llm_call=%s)", llm_call_id)
            return {}, llm_call_id, False
        return parsed, llm_call_id, True

    def _format_user_prompt(self, atoms: list[dict], relations: list[dict]) -> str:
        # Cap evidence size for prompt token budget.
        payload = {
            "atoms": atoms[:120],
            "relations": relations[:60],
        }
        return "请推导 PC/FR 骨架（纯 JSON 对象）：\n" + json.dumps(payload, ensure_ascii=False)

    def _parse_json(self, raw: str) -> tuple[dict, bool]:
        if not raw:
            return {}, False
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return {}, False
        if not isinstance(data, dict) or "PC" not in data or "FR" not in data:
            return {}, False
        return data, True

    # -- deterministic derivation build (the honest-degradation core) ----

    def _build_derivations(self, raw: dict) -> list[TheoryDerivation]:
        pc_raw = raw.get("PC") or {}
        fr_raw = raw.get("FR") or {}
        out: list[TheoryDerivation] = []
        # PC → constraint_state
        for dim in PC_CONSTRAINTS:
            entry = pc_raw.get(dim) if isinstance(pc_raw, dict) else None
            llm_score = _entry_score(entry)
            rationale = _entry_rationale(entry)
            degraded, basis, keep_score = _PC_DEGRADATION[dim]
            # keep_score=False (fully unsupported: Energy/Social/Environment) ⇒
            # force 0.0 regardless of the LLM's neutral guess, so no consumer can
            # render an unsupported dim as a measured value (iron rule).
            score = llm_score if keep_score else 0.0
            # Degraded-but-kept (Cognition): cap confidence low; keep LLM score.
            conf = 0.25 if degraded else 0.5
            try:
                out.append(TheoryDerivation(
                    kind="PC", name=dim, score=score, rationale=rationale,
                    aggregation_type="constraint_state", confidence=conf,
                    basis=basis, degraded=degraded))
            except Exception as exc:  # noqa: BLE001 — C5 per-row isolation
                logger.warning("theory PC %s build failed: %s", dim, exc)
                # C7 / ADR-V6-057: per-row build failure → DLQ, not warn-only.
                # Extends the LLM-call + C5 DLQ paths (above) to per-row isolation.
                self._safe_dlq(
                    f"{ENGINE}_pc_build_failed", f"{type(exc).__name__}: {exc}",
                    {"kind": "PC", "dim": dim, "entry": entry})
        # FR → fr_snapshot (all LLM-approx by design; not "degraded" — basis notes approx)
        for dim in FR_DIMENSIONS:
            entry = fr_raw.get(dim) if isinstance(fr_raw, dict) else None
            try:
                out.append(TheoryDerivation(
                    kind="FR", name=dim, score=_entry_score(entry),
                    rationale=_entry_rationale(entry),
                    aggregation_type="fr_snapshot", confidence=0.3,
                    basis="LLM 文本近似骨架（Phase 2.5 统计公式待替）",
                    degraded=False))
            except Exception as exc:  # noqa: BLE001
                logger.warning("theory FR %s build failed: %s", dim, exc)
                # C7 / ADR-V6-057: per-row build failure → DLQ, not warn-only.
                self._safe_dlq(
                    f"{ENGINE}_fr_build_failed", f"{type(exc).__name__}: {exc}",
                    {"kind": "FR", "dim": dim, "entry": entry})
        return out

    # -- C6/C7 helpers ---------------------------------------------------

    def _safe_dlq(self, error_type: str, error_msg: str, original_data: dict) -> None:
        try:
            self._store.insert_dlq(
                user_id=self._user_id, source=ENGINE,
                error_type=error_type, error_msg=error_msg,
                original_data=original_data)
        except Exception as exc:  # noqa: BLE001
            logger.debug("theory DLQ insert failed: %s", exc)

    def _log_call(self, llm_call_id: str, prompt_input: dict, *,
                  response: Optional[dict], model: str, provider: Optional[str],
                  in_toks: int, out_toks: int, latency_ms: int, success: bool,
                  schema_valid: Optional[bool] = None,
                  error_type: Optional[str] = None,
                  error_msg: Optional[str] = None) -> None:
        try:
            self._store.insert_llm_call_log(
                log_id=llm_call_id, user_id=self._user_id, model=model,
                prompt_input=prompt_input, response=response, provider=provider,
                prompt_template_version=PROMPT_VERSION,
                input_tokens=in_toks or None, output_tokens=out_toks or None,
                latency_ms=latency_ms, success=success, schema_valid=schema_valid,
                error_type=error_type, error_msg=error_msg)
        except Exception as exc:  # noqa: BLE001
            logger.debug("theory llm_call_log insert failed: %s", exc)


# ── module helpers ─────────────────────────────────────────────────────────

def _entry_score(entry: Any) -> float:
    """Extract a 0..1 score from an LLM PC/FR entry (dict or bare number)."""
    if isinstance(entry, (int, float)):
        return float(max(0.0, min(1.0, entry)))
    if isinstance(entry, dict):
        s = entry.get("score", 0.5)
        try:
            return float(max(0.0, min(1.0, s)))
        except (TypeError, ValueError):
            return 0.5
    return 0.5


def _entry_rationale(entry: Any) -> str:
    if isinstance(entry, dict):
        r = entry.get("rationale") or entry.get("reason") or ""
        return str(r)[:500]
    return ""


def _response_text(response: Any) -> str:
    try:
        return (response.choices[0].message.content or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _usage_tokens(usage: Any, key: str) -> int:
    if not usage:
        return 0
    try:
        return int(getattr(usage, key, 0) or 0)
    except Exception:  # noqa: BLE001
        return 0


def _sha_short(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _monotonic() -> float:
    import time
    return time.monotonic()

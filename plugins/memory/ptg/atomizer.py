"""RealityOS V6 Atomizer — the HL-12 extraction heart (ADR-V6-011).

Turns a captured memo into structured R-atoms. This is the "data brain" the V6
fork was missing: V5 ships an 11-version-iterated extraction pipeline (R1 84.8 /
R3 85.2 / R2 89.2 baseline); the fork had only the memo-capture shell. The
Atomizer ports that pipeline faithfully — same v11 prompt, same C5 confidence
gate, same C6 llm_call_logs, same C7 DLQ taxonomy — onto the local PTG store.

Pipeline (mirrors V5 ``extraction_service.run_extraction_pipeline``):
  1. load v11 system prompt + build user prompt (Beijing time + weekday +
     optional location + memo text + JSON suffix).
  2. one-shot LLM call via ``agent.auxiliary_client.call_llm`` (the
     ``title_generator`` pattern — NOT the heavy background_review AIAgent fork,
     which is overkill for a JSON extraction). ``task="extraction"`` resolves the
     model from ``config.yaml auxiliary.extraction`` (empty = inherit main model).
  3. C6: log every call to ``llm_call_logs`` (success AND failure), full
     prompt_input + parsed response, so reality is replayable.
  4. parse JSON → ``ConfidenceEngine.validate`` (C5 gate: per-atom schema +
     confidence; R3>0.8 / R2>0.7 / R1·R7>0.5 / R0>0.7).
  5. dispatch each valid atom to its event table; each filtered / invalid atom
     to the DLQ with V5's source/error_type taxonomy — never silently dropped.
  6. fail-open: any exception is logged + DLQ'd; the caller (sync_turn) is never
     broken (the capture surface is observation-only, C7).

Deferred (out of Phase 1a scope, per ADR-V6-011): entity/relation node/edge
materialisation (events ARE the captured atoms and are queryable now); the
optional dedup_filter; SER/acoustic fusion (Phase 2.5).
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from .confidence import ConfidenceEngine, ValidationResult
from .store import PTGStore

logger = logging.getLogger(__name__)

PROMPT_VERSION = "v11"
_PROMPT_FILE = Path(__file__).parent / "prompts" / f"hl12_extract_{PROMPT_VERSION}.md"
_JSON_SUFFIX = "\n\n请从以上用户输入中提取所有 Atom，输出严格 JSON 格式。"

# V5 llm_service.py:300-313 pricing (CNY per million tokens). Unknown provider
# over-estimates as zhipu (V5 convention) so cost is never under-reported.
_PRICES = {
    "zhipu": {"input_per_m": 2.0, "output_per_m": 8.0},
    "deepseek": {"input_per_m": 1.0, "output_per_m": 2.0},
    "qwen": {"input_per_m": 0.8, "output_per_m": 2.0},
}

_BEIJING_TZ = timezone(timedelta(hours=8))
_WEEKDAYS = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]

# Per-type routing: (table, intent_class override, extra-builder). Mirrors V5
# extraction_service._write_*_atom → V6 PTGStore._insert_event extras.
_Weekday = str


def _estimate_cost(input_tokens: int, output_tokens: int, provider: Optional[str]) -> float:
    price = _PRICES.get(provider or "", _PRICES["zhipu"])
    return (
        input_tokens * price["input_per_m"] / 1_000_000
        + output_tokens * price["output_per_m"] / 1_000_000
    )


def _beijing_now() -> datetime:
    return datetime.now(_BEIJING_TZ)


def _format_user_prompt(source_text: str, now: datetime,
                        location_context: Optional[dict]) -> str:
    wd = _WEEKDAYS[now.weekday()]
    # f-string (not strftime %-m/%-d) so it's portable to the Linux CI runner.
    stamp = f"{now.year}年{now.month}月{now.day}日 {wd} {now.hour:02d}:{now.minute:02d}"
    lines = [f"当前时间：{stamp}（北京时间）"]
    if location_context:
        loc = location_context.get("name") or location_context.get("address")
        if loc:
            lines.append(f"地点：{loc}")
    lines.append("")
    lines.append(source_text.strip())
    lines.append("")
    lines.append(_JSON_SUFFIX.strip())
    return "\n".join(lines)


def _usage_tokens(usage: Any, key: str) -> int:
    """Read a token count from an OpenAI-shape usage (object or dict)."""
    if usage is None:
        return 0
    if isinstance(usage, dict):
        return int(usage.get(key, 0) or 0)
    return int(getattr(usage, key, 0) or 0)


class Atomizer:
    """One-shot HL-12 extractor over a PTGStore.

    The LLM caller and clock are injected so the full pipeline is unit-testable
    without a real model. ``main_runtime`` is forwarded to ``call_llm`` to
    inherit the parent agent's live provider credentials when available (None →
    ``call_llm`` resolves from ``config.yaml``).
    """

    def __init__(
        self,
        store: PTGStore,
        *,
        user_id: str,
        confidence_engine: Optional[ConfidenceEngine] = None,
        llm_caller: Optional[Callable[..., Any]] = None,
        now_fn: Callable[[], datetime] = _beijing_now,
        main_runtime: Optional[dict] = None,
        timeout: float = 30.0,
    ) -> None:
        self._store = store
        self._user_id = user_id
        self._gate = confidence_engine or ConfidenceEngine()
        self._llm_caller = llm_caller or _default_llm_caller
        self._now_fn = now_fn
        self._main_runtime = main_runtime
        self._timeout = timeout
        self._system_prompt: Optional[str] = None

    # -- prompt -----------------------------------------------------------

    def _system(self) -> str:
        if self._system_prompt is None:
            self._system_prompt = _PROMPT_FILE.read_text(encoding="utf-8")
        return self._system_prompt

    # -- public entry -----------------------------------------------------

    def atomize(
        self,
        *,
        memo_id: str,
        source_text: str,
        input_mode: str = "text",
        location_context: Optional[dict] = None,
    ) -> dict:
        """Run the full extraction pipeline for one memo. Never raises (C7).

        Returns a counts dict (for diagnostics/logging): written / filtered /
        invalid / llm_call_id / latency_ms. All failures are logged + DLQ'd.
        """
        counts = {"written": 0, "filtered": 0, "invalid": 0,
                  "llm_call_id": None, "latency_ms": 0, "ok": False}
        llm_call_id = str(uuid.uuid4())
        counts["llm_call_id"] = llm_call_id
        start = time.monotonic()

        system_prompt = self._system()
        user_prompt = _format_user_prompt(source_text, self._now_fn(), location_context)
        prompt_input = {
            "engine": "hl12_extract",
            "prompt_version": PROMPT_VERSION,
            "text_length": len(source_text),
            "system_prompt_hash": _sha_short(system_prompt),
            "full_prompt": user_prompt,
        }

        # Step 1 — LLM call.
        try:
            response = self._llm_caller(
                task="extraction",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.1,
                max_tokens=2000,
                timeout=self._timeout,
                main_runtime=self._main_runtime,
                extra_body={"response_format": {"type": "json_object"}},
            )
        except Exception as exc:  # noqa: BLE001 — fail-open, C7
            latency = int((time.monotonic() - start) * 1000)
            counts["latency_ms"] = latency
            self._log_call(
                llm_call_id, prompt_input, response=None, model="unknown",
                provider=None, in_toks=0, out_toks=0, latency_ms=latency,
                success=False, error_type=type(exc).__name__, error_msg=str(exc),
            )
            self._store.insert_dlq(
                user_id=self._user_id, source="llm_extract",
                error_type="llm_error", error_msg=str(exc),
                original_data={"memo_id": memo_id, "source_text": source_text},
            )
            logger.warning("Atomizer LLM call failed for memo %s: %s", memo_id, exc)
            return counts

        text = (response.choices[0].message.content or "").strip()
        model = getattr(response, "model", "unknown") or "unknown"
        provider = getattr(response, "provider", None)
        usage = getattr(response, "usage", None)
        in_toks = _usage_tokens(usage, "prompt_tokens")
        out_toks = _usage_tokens(usage, "completion_tokens")
        latency = int((time.monotonic() - start) * 1000)
        counts["latency_ms"] = latency

        # Step 2 — JSON parse.
        try:
            parsed = json.loads(text) if text else None
        except json.JSONDecodeError as exc:
            self._log_call(
                llm_call_id, prompt_input, response={"raw": text}, model=model,
                provider=provider, in_toks=in_toks, out_toks=out_toks,
                latency_ms=latency, success=False, schema_valid=False,
                error_type="json_parse_error", error_msg=str(exc),
            )
            self._store.insert_dlq(
                user_id=self._user_id, source="llm_extract",
                error_type="json_parse_error", error_msg=str(exc),
                original_data={"memo_id": memo_id, "raw_response": text},
            )
            logger.warning("Atomizer JSON parse failed for memo %s: %s", memo_id, exc)
            return counts

        # Step 3 — C6 log the successful call (schema_valid filled after gate).
        self._log_call(
            llm_call_id, prompt_input, response={"content": parsed}, model=model,
            provider=provider, in_toks=in_toks, out_toks=out_toks,
            latency_ms=latency, success=True, cost_cny=_estimate_cost(in_toks, out_toks, provider),
        )

        # Step 4 — C5 gate.
        validation: ValidationResult = self._gate.validate(parsed)
        if not validation.schema_valid:
            self._mark_schema_valid(llm_call_id, False)
            self._store.insert_dlq(
                user_id=self._user_id, source="schema_validate",
                error_type="schema_invalid",
                error_msg="; ".join(validation.errors),
                original_data={"memo_id": memo_id, "llm_output": parsed},
            )
            logger.warning("Atomizer schema-invalid for memo %s: %s",
                           memo_id, "; ".join(validation.errors))
            return counts
        self._mark_schema_valid(llm_call_id, True)

        # Step 5 — dispatch valid atoms; DLQ filtered + invalid.
        for atom in validation.valid_atoms:
            try:
                self._write_atom(atom, memo_id=memo_id, source_text=source_text,
                                 input_mode=input_mode, llm_call_id=llm_call_id)
                counts["written"] += 1
            except Exception as exc:  # noqa: BLE001 — per-atom isolation, C7
                self._store.insert_dlq(
                    user_id=self._user_id, source="atom_write",
                    error_type="write_error",
                    error_msg=f"Failed to write {getattr(atom, 'type', '?')} atom: {exc}",
                    original_data={"memo_id": memo_id, "atom": _safe_dump(atom)},
                )
                logger.warning("Atomizer write failed (%s): %s",
                               getattr(atom, "type", "?"), exc)

        for f_atom in validation.filtered_atoms:
            self._store.insert_dlq(
                user_id=self._user_id, source="confidence_filter",
                error_type="below_confidence_threshold",
                error_msg=f_atom.get("_filter_reason", "below threshold"),
                original_data={"memo_id": memo_id, "atom": f_atom},
            )
            counts["filtered"] += 1

        for inv in validation.invalid_atoms:
            self._store.insert_dlq(
                user_id=self._user_id, source="schema_validate",
                error_type="schema_invalid",
                error_msg=inv.get("error", "schema invalid"),
                original_data={"memo_id": memo_id, "atom": inv.get("atom")},
            )
            counts["invalid"] += 1

        counts["ok"] = True
        return counts

    # -- writers ----------------------------------------------------------

    def _write_atom(self, atom: Any, *, memo_id: str, source_text: str,
                    input_mode: str, llm_call_id: str) -> None:
        from .atom_schemas import (
            R0EntityAtom, R1SelfStateAtom, R2TaskAtom, R3PersonAtom, R7ExpressionAtom,
        )
        common = dict(
            user_id=self._user_id, source_text=source_text,
            confidence_base=atom.confidence, relation_confidence=atom.confidence,
            memo_id=memo_id, input_mode=input_mode, llm_call_id=llm_call_id,
        )
        if isinstance(atom, R3PersonAtom):
            self._store.insert_identity_event(
                person_name=atom.person_name, mention_context=atom.mention_context,
                sentiment=atom.sentiment, interaction_type=atom.interaction_type, **common)
        elif isinstance(atom, R2TaskAtom):
            self._store.insert_meaning_event(
                intent_class="Need_To_Do", task_description=atom.task_description,
                urgency=atom.urgency, deadline=atom.deadline, task_status="pending", **common)
        elif isinstance(atom, R7ExpressionAtom):
            self._store.insert_meaning_event(
                intent_class=atom.intent_class, task_description=atom.content_summary, **common)
        elif isinstance(atom, R1SelfStateAtom):
            self._store.insert_feeling_event(
                state_type=atom.state_type, direction=atom.direction,
                intensity=atom.intensity, ser_source="llm_text", **common)
        elif isinstance(atom, R0EntityAtom):
            ctx = atom.mention_context or f"提及{atom.entity_category}: {atom.entity_name}"
            self._store.insert_entity_event(
                entity_name=atom.entity_name, entity_category=atom.entity_category,
                mention_context=ctx, **common)
        else:  # pragma: no cover — gate only emits the 5 known types
            raise ValueError(f"unsupported atom type: {type(atom)!r}")

    # -- C6 log helpers ---------------------------------------------------

    def _log_call(self, llm_call_id: str, prompt_input: dict, *,
                  response: Optional[dict], model: str, provider: Optional[str],
                  in_toks: int, out_toks: int, latency_ms: int, success: bool,
                  schema_valid: Optional[bool] = None, cost_cny: Optional[float] = None,
                  error_type: Optional[str] = None, error_msg: Optional[str] = None) -> None:
        try:
            self._store.insert_llm_call_log(
                log_id=llm_call_id, user_id=self._user_id, model=model,
                prompt_input=prompt_input, response=response, provider=provider,
                prompt_template_version=PROMPT_VERSION, input_tokens=in_toks or None,
                output_tokens=out_toks or None, latency_ms=latency_ms, success=success,
                schema_valid=schema_valid, cost_cny=cost_cny,
                error_type=error_type, error_msg=error_msg,
            )
        except Exception as exc:  # noqa: BLE001 — logging must never break capture
            logger.debug("Atomizer llm_call_log insert failed: %s", exc)

    def _mark_schema_valid(self, llm_call_id: str, valid: bool) -> None:
        """V5 leaves llm_call_logs.schema_valid NULL; V6 fills it (C6 honesty)."""
        try:
            with self._store._lock:
                self._store._conn.execute(
                    "UPDATE llm_call_logs SET schema_valid = ? WHERE id = ?",
                    (1 if valid else 0, llm_call_id),
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Atomizer schema_valid backfill failed: %s", exc)


def _default_llm_caller(**kwargs: Any) -> Any:
    """Default caller: the hermes auxiliary client (sync). Imported lazily so the
    module (and its tests) load without the agent package present."""
    from agent.auxiliary_client import call_llm  # type: ignore[import-not-found]
    return call_llm(**kwargs)


def _sha_short(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _safe_dump(atom: Any) -> dict:
    try:
        return atom.model_dump()
    except Exception:  # noqa: BLE001
        return {"repr": repr(atom)}

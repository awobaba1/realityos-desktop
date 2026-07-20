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
from typing import Any, Callable, List, Optional

from .confidence import ConfidenceEngine, ValidationResult
from .store import PTGStore

logger = logging.getLogger(__name__)

# Two-pass extraction (ADR-V6-016). Single-pass 8-atom extraction regressed R3
# recall below the 85% Phase Gate on the 200-sample baseline (attention dilution:
# v12-singlepass R3=81-82% vs v11 ≥85%). So pass 1 runs v11 VERBATIM (R0/R1/R2/
# R3/R7 — baseline preserved byte-for-byte, C6), and pass 2 runs v12 as a focused
# R8/R9/R12 supplement (no attention competition). PROMPT_VERSION (the value
# logged as prompt_template_version on the primary pass) stays the v11 baseline.
PROMPT_VERSION = "v11"
_PRIMARY_VERSION = "v11"       # pass 1: R0/R1/R2/R3/R7 (V5 baseline, untouched)
_SUPPLEMENT_VERSION = "v12"    # pass 2: R8/R9/R12 (Phase 1b supplement)
_PROMPT_FILE = Path(__file__).parent / "prompts"
_PROMPT_FILES = {
    _PRIMARY_VERSION: _PROMPT_FILE / f"hl12_extract_{_PRIMARY_VERSION}.md",
    _SUPPLEMENT_VERSION: _PROMPT_FILE / f"hl12_extract_{_SUPPLEMENT_VERSION}.md",
}
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
                        location_context: Optional[dict],
                        entity_vocab: Optional[str] = None) -> str:
    wd = _WEEKDAYS[now.weekday()]
    # f-string (not strftime %-m/%-d) so it's portable to the Linux CI runner.
    stamp = f"{now.year}年{now.month}月{now.day}日 {wd} {now.hour:02d}:{now.minute:02d}"
    lines = [f"当前时间：{stamp}（北京时间）"]
    if location_context:
        loc = location_context.get("name") or location_context.get("address")
        if loc:
            lines.append(f"地点：{loc}")
    # ADR-V6-013 (ADR-049 port): inject the user's known-entity vocabulary so
    # the LLM can (a) normalise near/synonymous mentions to the canonical name
    # and (b) correct obvious ASR near-homophones (字体跳动→字节跳动). Only when
    # the user already has entities; first-run omits it (no behaviour change).
    # Lives in the VARIABLE user prompt (not the system template) — C6: the v11
    # system prompt is untouched, no template overwrite.
    if entity_vocab:
        lines.append("")
        lines.append(entity_vocab)
    lines.append("")
    lines.append(source_text.strip())
    lines.append("")
    lines.append(_JSON_SUFFIX.strip())
    return "\n".join(lines)


# Bucket labels for the 4 entity_type values → human-readable section headers.
_VOCAB_BUCKETS = [("person", "人物"), ("context", "情境"), ("topic", "话题"), ("task", "任务")]


def _format_entity_vocab(entities: list[dict], *, per_bucket: int = 20) -> Optional[str]:
    """Render the known-entity vocabulary section (ADR-V6-013). Returns None
    when there are no entities (first-run → omit the section entirely).

    Each line: ``canonical 名（别名1、别名2）; canonical2; …``. Buckets with no
    entities are dropped so the section stays compact (~150-400 tokens).
    """
    by_type: dict[str, list[dict]] = {}
    for e in entities:
        by_type.setdefault(e["entity_type"], []).append(e)
    sections: list[str] = []
    for etype, label in _VOCAB_BUCKETS:
        bucket = by_type.get(etype, [])[:per_bucket]
        if not bucket:
            continue
        items = []
        for e in bucket:
            aliases = e.get("aliases") or []
            if aliases:
                items.append(f"{e['entity_name']}（{'、'.join(aliases)}）")
            else:
                items.append(e["entity_name"])
        sections.append(f"[{label}] {'; '.join(items)}")
    if not sections:
        return None
    return ("## 已知实体词汇（用于：① 把用户输入里的近似/同义称呼归一到下列标准名；"
            "② 修正明显的 ASR 同音误识别（如「字体跳动」→「字节跳动」）。不强制只抽这些。）\n"
            + "\n".join(sections))


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
        materialize_graph: bool = True,
        self_name: str = "我",
    ) -> None:
        self._store = store
        self._user_id = user_id
        self._gate = confidence_engine or ConfidenceEngine()
        self._llm_caller = llm_caller or _default_llm_caller
        self._now_fn = now_fn
        self._main_runtime = main_runtime
        self._timeout = timeout
        self._system_prompts: dict[str, str] = {}  # version → cached system text
        # Graph materialization (ADR-V6-011 决策6): turn R0/R3/R2 atoms into
        # entities/relations nodes+edges. Default ON (same heart-must-beat spirit
        # as atomize); fail-isolated so a graph error never breaks event capture.
        self._materialize_enabled = materialize_graph
        # §6.7 PIPL §31 minor mode (ADR-V6-023): re-read per memo in atomize()
        # so a live toggle takes effect immediately; when on, R1/R9 biometric
        # atoms are dropped at the materialization boundary.
        self._minor_mode = False
        self._self_name = self_name
        self._self_entity_id: Optional[str] = None
        # ADR-V6-044 (F3): R9→entity resolution cache. The user's known entity
        # names, loaded once per atomize() and substring-matched against each
        # R9 atom's trigger to populate trigger_source.entity (so K-correlation
        # can group emotions by entity). User-scoped + semaphore-bounded (max 2
        # concurrent atomize threads) ⇒ a cross-memo race only ever yields the
        # SAME user's entity list, which is harmless for best-effort matching.
        self._trigger_entity_cache: Optional[List[str]] = None

    # -- prompt -----------------------------------------------------------

    def _system(self, version: str) -> str:
        """Load (and cache) the system prompt for a pass version (v11/v12)."""
        if version not in self._system_prompts:
            self._system_prompts[version] = _PROMPT_FILES[version].read_text(encoding="utf-8")
        return self._system_prompts[version]

    def _entity_vocab_section(self) -> Optional[str]:
        """Build the known-entity vocabulary section (ADR-V6-013). Returns None
        on first-run (no entities) OR on any load failure — extraction must
        NEVER be blocked by a vocabulary hiccup (C7 fail-isolation: vocab is an
        enrichment, not a gate). The Atomizer owns no entity state; the store
        is the single source of which entities the user has.
        """
        try:
            entities = self._store.list_top_entities(self._user_id, limit=100)
        except Exception:  # noqa: BLE001 — enrichment must never break extraction
            logger.warning("entity vocab load failed; extracting vocab-less",
                           exc_info=True)
            return None
        if not entities:
            return None
        return _format_entity_vocab(entities)

    def _trigger_entity_names(self) -> List[str]:
        """ADR-V6-044 (F3): the user's known entity names for R9→entity matching.

        Loaded once per atomize() (cached on the instance) from the same
        ``list_top_entities`` the prompt-vocab uses. Fail-open (C7): a load
        error yields an empty list → R9 trigger_source.entity stays "" → that
        emotion is excluded from K-grouping (correct: we can't prove which
        entity it ties to). The self-node is already excluded by
        ``list_top_entities`` (properties.is_self), so self→self correlation
        can't arise.
        """
        if self._trigger_entity_cache is not None:
            return self._trigger_entity_cache
        try:
            ents = self._store.list_top_entities(self._user_id, limit=100)
        except Exception:  # noqa: BLE001 — enrichment must never break extraction
            logger.debug("trigger-entity load failed; R9 entity resolution skipped",
                         exc_info=True)
            ents = []
        names = [str(e.get("entity_name") or "").strip()
                 for e in ents if str(e.get("entity_name") or "").strip()]
        # Dedupe, keep order; drop names shorter than 2 chars (1-char matches
        # are too noisy — e.g. a single-char entity would match almost any trigger).
        seen, out = set(), []
        for n in names:
            if len(n) >= 2 and n not in seen:
                seen.add(n)
                out.append(n)
        self._trigger_entity_cache = out
        return out

    @staticmethod
    def _resolve_trigger_entity(trigger: Optional[str],
                                entity_names: List[str]) -> str:
        """ADR-V6-044 (F3): longest entity name that appears in ``trigger``.

        Longest-match wins so "张三丰" is preferred over "张三" when both are
        entities and both appear. Returns "" when no entity appears in the
        trigger (a situation-type trigger like "甲方改需求" with no person —
        correctly excluded from K-grouping). Case-insensitive; trims whitespace.
        """
        if not trigger or not entity_names:
            return ""
        hay = trigger.strip()
        if not hay:
            return ""
        hay_low = hay.lower()
        best = ""
        for name in entity_names:
            if not name:
                continue
            if name.lower() in hay_low and len(name) > len(best):
                best = name
        return best

    # -- public entry -----------------------------------------------------

    def atomize(
        self,
        *,
        memo_id: str,
        source_text: str,
        input_mode: str = "text",
        location_context: Optional[dict] = None,
    ) -> dict:
        """Run the full two-pass extraction pipeline for one memo. Never raises (C7).

        Pass 1 (v11, primary): R0/R1/R2/R3/R7 — the V5 baseline, run verbatim so
        its 85%+ R3 recall is preserved (single-pass 8-atom extraction regressed
        it, ADR-V6-016). Pass 2 (v12, supplement): R8/R9/R12 only. Each pass is
        fail-isolated (one pass erroring never blocks the other). Returns a
        counts dict: written / filtered / invalid / llm_call_ids / latency_ms.
        """
        start = time.monotonic()
        # §6.7 PIPL §31 minor mode (ADR-V6-023): read once per memo so a live
        # toggle takes effect immediately. When on, R1SelfState/R9Emotion
        # biometric atoms are dropped at the materialization boundary —
        # extraction still runs (the prompt is untouched, C6). is_minor never
        # raises (C7); on a store/meta error it returns False (adult default).
        from plugins.realityos_sovereignty.sovereignty import is_minor
        self._minor_mode = is_minor(self._store, self._user_id)
        # ADR-V6-044 (F3): fresh entity-name cache for this memo's R9→entity
        # resolution (loaded lazily by _trigger_entity_names()).
        self._trigger_entity_cache = None
        # ADR-V6-013: build the known-entity vocab once (None on first run / load
        # failure → extraction proceeds vocab-less, never blocked).
        entity_vocab = self._entity_vocab_section()

        pass1 = self._extract_and_write_pass(
            _PRIMARY_VERSION, memo_id=memo_id, source_text=source_text,
            input_mode=input_mode, location_context=location_context,
            entity_vocab=entity_vocab)
        pass2 = self._extract_and_write_pass(
            _SUPPLEMENT_VERSION, memo_id=memo_id, source_text=source_text,
            input_mode=input_mode, location_context=location_context,
            entity_vocab=entity_vocab)

        latency = int((time.monotonic() - start) * 1000)
        return {
            "ok": pass1["ok"] or pass2["ok"],
            "written": pass1["written"] + pass2["written"],
            "filtered": pass1["filtered"] + pass2["filtered"],
            "invalid": pass1["invalid"] + pass2["invalid"],
            # primary pass's id first (the baseline pass), supplement second.
            "llm_call_id": pass1["llm_call_id"],
            "llm_call_ids": [pass1["llm_call_id"], pass2["llm_call_id"]],
            "latency_ms": latency,
        }

    def _extract_and_write_pass(
        self, version: str, *, memo_id: str, source_text: str,
        input_mode: str, location_context: Optional[dict],
        entity_vocab: Optional[str],
    ) -> dict:
        """Run ONE extraction pass (LLM call → parse → C5 gate → write → graph).

        Fail-isolated and never raises (C7): every failure path logs + DLQs and
        returns ``{ok: False, written: 0, ...}``. Written atoms carry this pass's
        llm_call_id (C6 traceability per call). Used for both the v11 primary
        pass and the v12 supplement pass.
        """
        out = {"ok": False, "written": 0, "filtered": 0, "invalid": 0,
               "llm_call_id": None}
        llm_call_id = str(uuid.uuid4())
        out["llm_call_id"] = llm_call_id
        start = time.monotonic()

        system_prompt = self._system(version)
        user_prompt = _format_user_prompt(
            source_text, self._now_fn(), location_context, entity_vocab)
        prompt_input = {
            "engine": "hl12_extract",
            "prompt_version": version,
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
            self._log_call(
                llm_call_id, prompt_input, response=None, model="unknown",
                provider=None, in_toks=0, out_toks=0, latency_ms=latency,
                success=False, error_type=type(exc).__name__, error_msg=str(exc),
            )
            self._store.insert_dlq(
                user_id=self._user_id, source="llm_extract",
                error_type="llm_error", error_msg=str(exc),
                original_data={"memo_id": memo_id, "pass": version,
                               "source_text": source_text},
            )
            logger.warning("Atomizer LLM call failed (pass %s, memo %s): %s",
                           version, memo_id, exc)
            return out

        text = (response.choices[0].message.content or "").strip()
        model = getattr(response, "model", "unknown") or "unknown"
        provider = getattr(response, "provider", None)
        usage = getattr(response, "usage", None)
        in_toks = _usage_tokens(usage, "prompt_tokens")
        out_toks = _usage_tokens(usage, "completion_tokens")
        latency = int((time.monotonic() - start) * 1000)

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
                original_data={"memo_id": memo_id, "pass": version,
                               "raw_response": text},
            )
            logger.warning("Atomizer JSON parse failed (pass %s, memo %s): %s",
                           version, memo_id, exc)
            return out

        # Step 3 — C6 log the successful call (schema_valid filled after gate).
        self._log_call(
            llm_call_id, prompt_input, response={"content": parsed}, model=model,
            provider=provider, in_toks=in_toks, out_toks=out_toks,
            latency_ms=latency, success=True,
            cost_cny=_estimate_cost(in_toks, out_toks, provider),
        )

        # Step 4 — C5 gate.
        validation: ValidationResult = self._gate.validate(parsed)
        if not validation.schema_valid:
            self._mark_schema_valid(llm_call_id, False)
            self._store.insert_dlq(
                user_id=self._user_id, source="schema_validate",
                error_type="schema_invalid",
                error_msg="; ".join(validation.errors),
                original_data={"memo_id": memo_id, "pass": version,
                               "llm_output": parsed},
            )
            logger.warning("Atomizer schema-invalid (pass %s, memo %s): %s",
                           version, memo_id, "; ".join(validation.errors))
            return out
        self._mark_schema_valid(llm_call_id, True)

        # Step 5 — dispatch valid atoms; DLQ filtered + invalid.
        for atom in validation.valid_atoms:
            # §6.7 minor-mode gate (ADR-V6-023): drop R1SelfState/R9Emotion
            # biometric atoms for minor tenants. Extraction runs unchanged
            # (prompt untouched, C6); only these two atom kinds are gated at
            # the materialization boundary. Counted as filtered, not invalid.
            if self._minor_mode:
                from .atom_schemas import R1SelfStateAtom, R9EmotionAtom
                if isinstance(atom, (R1SelfStateAtom, R9EmotionAtom)):
                    out["filtered"] += 1
                    continue
            try:
                self._write_atom(atom, memo_id=memo_id, source_text=source_text,
                                 input_mode=input_mode, llm_call_id=llm_call_id)
                out["written"] += 1
            except Exception as exc:  # noqa: BLE001 — per-atom isolation, C7
                self._store.insert_dlq(
                    user_id=self._user_id, source="atom_write",
                    error_type="write_error",
                    error_msg=f"Failed to write {getattr(atom, 'type', '?')} atom: {exc}",
                    original_data={"memo_id": memo_id, "atom": _safe_dump(atom)},
                )
                logger.warning("Atomizer write failed (%s): %s",
                               getattr(atom, "type", "?"), exc)
                continue  # event capture failed → skip graph materialization
            if self._materialize_enabled:
                try:
                    self._materialize_graph(atom, memo_id=memo_id)
                except Exception as exc:  # noqa: BLE001 — enrichment isolated
                    self._store.insert_dlq(
                        user_id=self._user_id, source="graph_materialize",
                        error_type="materialize_error",
                        error_msg=f"Failed to materialize {getattr(atom, 'type', '?')}: {exc}",
                        original_data={"memo_id": memo_id, "atom": _safe_dump(atom)},
                    )
                    logger.warning("Atomizer graph materialize failed (%s): %s",
                                   getattr(atom, "type", "?"), exc)

        for f_atom in validation.filtered_atoms:
            self._store.insert_dlq(
                user_id=self._user_id, source="confidence_filter",
                error_type="below_confidence_threshold",
                error_msg=f_atom.get("_filter_reason", "below threshold"),
                original_data={"memo_id": memo_id, "atom": f_atom},
            )
            out["filtered"] += 1

        for inv in validation.invalid_atoms:
            self._store.insert_dlq(
                user_id=self._user_id, source="schema_validate",
                error_type="schema_invalid",
                error_msg=inv.get("error", "schema invalid"),
                original_data={"memo_id": memo_id, "atom": inv.get("atom")},
            )
            out["invalid"] += 1

        out["ok"] = True
        return out

    # -- writers ----------------------------------------------------------

    def _write_atom(self, atom: Any, *, memo_id: str, source_text: str,
                    input_mode: str, llm_call_id: str) -> None:
        from .atom_schemas import (
            R0EntityAtom, R1SelfStateAtom, R2TaskAtom, R3PersonAtom, R7ExpressionAtom,
            R8CognitionAtom, R9EmotionAtom, R12OutcomeAtom,
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
            # atom_kind='R2' is explicit (not the column default 'R7') so recall
            # reconstruction dispatches on atom_kind, not intent_class alone.
            self._store.insert_meaning_event(
                intent_class="Need_To_Do", task_description=atom.task_description,
                urgency=atom.urgency, deadline=atom.deadline, task_status="pending",
                atom_kind="R2", **common)
        elif isinstance(atom, R7ExpressionAtom):
            self._store.insert_meaning_event(
                intent_class=atom.intent_class, task_description=atom.content_summary,
                atom_kind="R7", **common)
        elif isinstance(atom, R8CognitionAtom):
            # R8 → meaning_events. intent_class enum has no 'learning' value, so
            # 'Other' (atom_kind='R8' is the source of truth). topic→description,
            # knowledge_tags→topic_tags (JSON). engagement/is_question land in
            # completion_note for full-fidelity recall (no dedicated columns).
            self._store.insert_meaning_event(
                intent_class="Other", task_description=atom.topic,
                topic_tags=json.dumps(atom.knowledge_tags, ensure_ascii=False),
                completion_note=json.dumps(
                    {"engagement": atom.engagement, "is_question": atom.is_question},
                    ensure_ascii=False),
                atom_kind="R8", **common)
        elif isinstance(atom, R12OutcomeAtom):
            # R12 → meaning_events, intent_class='Need_To_Do' (task outcome).
            # outcome→task_status best-effort (completed→completed; failed→
            # dismissed [closed, no enum for 'failed']; delayed→pending+overdue);
            # the precise outcome + resolution_note survive in completion_note.
            outcome_status = {"completed": "completed",
                              "failed": "dismissed",
                              "delayed": "pending"}[atom.outcome]
            self._store.insert_meaning_event(
                intent_class="Need_To_Do", task_description=atom.task_ref,
                task_status=outcome_status, is_overdue=1 if atom.outcome == "delayed" else 0,
                completion_note=json.dumps(
                    {"outcome": atom.outcome, "resolution_note": atom.resolution_note},
                    ensure_ascii=False),
                atom_kind="R12", **common)
        elif isinstance(atom, R1SelfStateAtom):
            self._store.insert_feeling_event(
                state_type=atom.state_type, direction=atom.direction,
                intensity=atom.intensity, ser_source="llm_text", **common)
        elif isinstance(atom, R9EmotionAtom):
            # R9 → feeling_events, atom_kind='R9'. state_type must be one of the
            # CHECK enum → 'mood'; direction mirrors valence. R9-specific fields
            # (label/valence/arousal/trigger) land in emotion_vad + trigger_source
            # (JSON) for full-fidelity recall; the CHECK-bound columns are the
            # coarse projection.
            r9_direction = {"positive": "up", "negative": "down",
                            "neutral": "stable"}[atom.valence]
            # ADR-V6-044 (F3): resolve the trigger against the user's known
            # entities so trigger_source carries an `entity` key — K-correlation
            # groups emotions by this. "" when the trigger is a situation, not
            # a known entity (correctly excluded from K-grouping). Option B
            # (post-hoc resolution) — avoids touching the v12 prompt baseline.
            r9_entity = self._resolve_trigger_entity(
                atom.trigger, self._trigger_entity_names())
            self._store.insert_feeling_event(
                state_type="mood", direction=r9_direction, intensity=atom.intensity,
                ser_source="llm_text",
                emotion_vad=json.dumps(
                    {"valence": atom.valence, "arousal": atom.arousal,
                     "label": atom.emotion_label}, ensure_ascii=False),
                trigger_source=json.dumps(
                    {"trigger": atom.trigger, "entity": r9_entity,
                     "atom": "R9_Emotion"},
                    ensure_ascii=False),
                atom_kind="R9", **common)
        elif isinstance(atom, R0EntityAtom):
            ctx = atom.mention_context or f"提及{atom.entity_category}: {atom.entity_name}"
            self._store.insert_entity_event(
                entity_name=atom.entity_name, entity_category=atom.entity_category,
                mention_context=ctx, **common)
        else:  # pragma: no cover — gate only emits the 8 known types
            raise ValueError(f"unsupported atom type: {type(atom)!r}")

    # -- graph materialization (决策6) -----------------------------------

    def _materialize_graph(self, atom: Any, *, memo_id: str) -> None:
        """Turn an R0/R3/R2/R8/R12 atom into a graph node + a self→node edge.

        The self entity (the founder — implicit subject of every personal memo)
        is upserted once and cached. Each eligible atom becomes one node and one
        self→node semantic edge. R1/R7/R9 carry no node in Phase 1 (states,
        expressions, and co-occurrence emotions are not entities). Failures here
        are isolated by the caller (graph errors never break event-table capture).
        """
        from .atom_schemas import (
            R0EntityAtom, R2TaskAtom, R3PersonAtom,
            R8CognitionAtom, R12OutcomeAtom,
        )
        if isinstance(atom, R3PersonAtom):
            ent_name, ent_type = atom.person_name, "person"
            edge_type, edge_val = "interacts_with", atom.interaction_type
            # ADR-V6-013: persist aliases so the entity vocabulary (ADR-049 port)
            # can surface canonical-name + aliases for cross-memo resolution +
            # ASR near-homophone correction.
            props = {"sentiment": atom.sentiment,
                     "interaction_type": atom.interaction_type}
            if atom.aliases:
                props["aliases"] = list(atom.aliases)
        elif isinstance(atom, R0EntityAtom):
            ent_name = atom.entity_name
            # place/organization → context node; term → topic node (决策6 mapping).
            ent_type = "topic" if atom.entity_category == "term" else "context"
            edge_type, edge_val = "mentions", atom.entity_category
            props = {"category": atom.entity_category}
        elif isinstance(atom, R2TaskAtom):
            ent_name, ent_type = atom.task_description, "task"
            edge_type, edge_val = "has_task", atom.urgency
            props = {"urgency": atom.urgency, "status": "pending"}
        elif isinstance(atom, R8CognitionAtom):
            # R8 cognition → topic node (a thing the founder is learning/thinking
            # about), self→topic `learns` edge weighted by engagement.
            ent_name, ent_type = atom.topic, "topic"
            edge_type, edge_val = "learns", atom.engagement
            props = {"engagement": atom.engagement,
                     "is_question": atom.is_question}
            if atom.knowledge_tags:
                props["knowledge_tags"] = list(atom.knowledge_tags)
        elif isinstance(atom, R12OutcomeAtom):
            # R12 outcome → task node (upserted; may already exist from an R2 in
            # an earlier memo), self→task `has_task` edge carrying the outcome so
            # the task's latest resolution state is reflected in the graph.
            ent_name, ent_type = atom.task_ref, "task"
            edge_type, edge_val = "has_task", atom.outcome
            props = {"outcome": atom.outcome}
            if atom.resolution_note:
                props["resolution_note"] = atom.resolution_note
        else:
            return  # R1/R7/R9 — no graph node in Phase 1
        props = {k: v for k, v in props.items() if v is not None}
        self_id = self._ensure_self_entity()
        ent_id = self._store.upsert_entity(
            user_id=self._user_id, entity_name=ent_name,
            entity_type=ent_type, properties=props or None,
        )
        self._store.upsert_relation(
            user_id=self._user_id, subject_id=self_id, object_id=ent_id,
            relation_type=edge_type, value=edge_val, confidence=atom.confidence,
        )

    def _ensure_self_entity(self) -> str:
        """Upsert the founder self-node once, cache its id for the turn."""
        if self._self_entity_id is None:
            self._self_entity_id = self._store.upsert_entity(
                user_id=self._user_id, entity_name=self._self_name,
                entity_type="person", properties={"is_self": True},
            )
        return self._self_entity_id

    # -- C6 log helpers ---------------------------------------------------

    def _log_call(self, llm_call_id: str, prompt_input: dict, *,
                  response: Optional[dict], model: str, provider: Optional[str],
                  in_toks: int, out_toks: int, latency_ms: int, success: bool,
                  schema_valid: Optional[bool] = None, cost_cny: Optional[float] = None,
                  error_type: Optional[str] = None, error_msg: Optional[str] = None) -> None:
        try:
            # prompt_input carries this pass's prompt_version (v11 primary or v12
            # supplement); log it verbatim for per-call traceability (C6).
            pver = prompt_input.get("prompt_version", PROMPT_VERSION)
            self._store.insert_llm_call_log(
                log_id=llm_call_id, user_id=self._user_id, model=model,
                prompt_input=prompt_input, response=response, provider=provider,
                prompt_template_version=pver, input_tokens=in_toks or None,
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

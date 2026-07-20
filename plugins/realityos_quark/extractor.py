"""RealityOS V6 — Quark extractor (ADR-V6-039 Batch1 / ADR-V6-049 B1).

The concrete ``QuarkExtractor`` implementer behind the
``plugins.memory.ptg.phase2_contracts.QuarkExtractor`` Protocol. Extracts the
3 text-reachable Quark kinds (Identity / Meaning / Feeling) from a capture +
its ``tool_events.quark_evidence`` rows, via an LLM call that is:

- C5-gated: every record is validated as a ``QuarkRecord`` (pydantic); a
  parse/type failure DLQs the raw output and returns [] — never a half-built
  record reaches aggregation.
- C6-logged: the LLM call is recorded in ``llm_call_logs`` (prompt version,
  model, tokens, latency, success, schema_valid).
- C7-safe: any exception (LLM timeout / network / JSON garbage) → DLQ + [],
  never raises. Extraction is enrichment; it must never break capture.
- Honest scope: only Identity/Meaning/Feeling. Time/Behavior/Context/Network
  are pinned in the contract for later phases but NOT produced here
  (ADR-V6-039 防空跑 — they depend on the cut Layer B/C acoustic / multi-person
  / SED pipelines; producing them from text alone would be fabrication).

Mirrors the ``realityos_insights._base`` LLM/log/DLQ rails so the two insight
producers share one failure discipline.
"""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from plugins.memory.ptg.phase2_contracts import (
    PHASE2_QUARK_KINDS, QuarkRecord)

logger = logging.getLogger(__name__)

_PROMPT_FILE = Path(__file__).parent / "prompt_templates" / "quark_extract_v1.md"
PROMPT_VERSION = "v1"
ENGINE = "quark"


def _default_llm_caller(**kwargs: Any) -> Any:
    """Delegate to the shared ``call_llm`` (same as atomizer/insights)."""
    from agent.auxiliary_client import call_llm  # type: ignore[import-not-found]

    return call_llm(**kwargs)


class QuarkExtractorImpl:
    """Concrete QuarkExtractor (satisfies the Phase-2 Protocol structurally).

    Inject ``store`` (for llm_call_logs + DLQ), ``caller`` (LLM; default
    resolves provider from config.yaml), and ``now_fn``. ``extract`` returns
    validated ``QuarkRecord`` instances only; failures degrade to [] (C7).
    """

    def __init__(
        self, store, *,
        caller: Optional[Callable[..., Any]] = None,
        timeout: float = 30.0,
        main_runtime: Any = None,
        temperature: float = 0.2,
        max_tokens: int = 600,
    ) -> None:
        self._store = store
        self._caller = caller or _default_llm_caller
        self._timeout = timeout
        self._main_runtime = main_runtime
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._system_prompt: Optional[str] = None
        # ADR-V6-071: the llm_call_id of the most recent _llm() call, exposed
        # so aggregation can thread it into the atom event rows (C6
        # traceability — every event MUST carry llm_call_id). None when no
        # call was made (empty-input early return) or the extractor was never
        # run. Previously extract() discarded this id (``_llm_id``), so every
        # quark-derived atom landed with NULL llm_call_id — a C6 断链.
        self._last_llm_call_id: Optional[str] = None

    # -- Protocol implementation -----------------------------------------

    def extract(
        self, quark_evidence_rows: list[dict], capture_text: str,
    ) -> list[QuarkRecord]:
        """Extract Quarks from one capture. Never raises (C7)."""
        text = (capture_text or "").strip()
        if not text and not quark_evidence_rows:
            self._last_llm_call_id = None  # no call made — honest None, not stale
            return []  # nothing to extract — not an error
        records, llm_id, _ok = self._llm(quark_evidence_rows or [], text)
        # ADR-V6-071: expose the llm_call_id so aggregation threads it into the
        # atom event rows (C6). Previously discarded as ``_llm_id`` → NULL
        # llm_call_id on every quark-derived atom (C6 断链).
        self._last_llm_call_id = llm_id
        return records

    # -- LLM + C5/C6/C7 rails --------------------------------------------

    def _system(self) -> str:
        if self._system_prompt is None:
            self._system_prompt = _PROMPT_FILE.read_text(encoding="utf-8")
        return self._system_prompt

    def _llm(
        self, evidence_rows: list[dict], capture_text: str,
    ) -> tuple[list[QuarkRecord], Optional[str], bool]:
        llm_call_id = str(uuid.uuid4())
        system_prompt = self._system()
        user_prompt = self._format_user_prompt(evidence_rows, capture_text)
        prompt_input = {
            "engine": ENGINE,
            "prompt_version": PROMPT_VERSION,
            "capture_len": len(capture_text),
            "evidence_rows": len(evidence_rows),
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
                           {"capture_len": len(capture_text)})
            logger.warning("quark LLM call failed: %s", exc)
            return [], llm_call_id, False

        raw = _response_text(response)
        model = getattr(response, "model", "unknown") or "unknown"
        provider = getattr(response, "provider", None)
        usage = getattr(response, "usage", None)
        in_toks = _usage_tokens(usage, "prompt_tokens")
        out_toks = _usage_tokens(usage, "completion_tokens")
        latency = int((_monotonic() - start) * 1000)

        records, schema_valid = self._parse(raw)
        self._log_call(llm_call_id, prompt_input, response={"content": raw[:2000]},
                       model=model, provider=provider, in_toks=in_toks, out_toks=out_toks,
                       latency_ms=latency, success=True, schema_valid=schema_valid)
        if not schema_valid:
            self._safe_dlq(f"{ENGINE}_schema_invalid",
                           "LLM output failed QuarkRecord C5 validation",
                           {"raw_len": len(raw), "preview": raw[:200]})
            logger.warning("quark C5 invalid (llm_call=%s)", llm_call_id)
            return [], llm_call_id, False
        return records, llm_call_id, True

    def _format_user_prompt(self, evidence_rows: list[dict], capture_text: str) -> str:
        payload = {
            "capture_text": capture_text,
            "quark_evidence_rows": evidence_rows[:20],  # cap for prompt size
        }
        return "请提取 Quark（纯 JSON 数组）：\n" + json.dumps(payload, ensure_ascii=False)

    def _parse(self, raw: str) -> tuple[list[QuarkRecord], bool]:
        """Parse + C5-validate the LLM JSON output into QuarkRecord instances.

        Returns (records, schema_valid). schema_valid=False on any parse/type
        error (whole batch DLQ'd — never emit a half-validated record). Kind is
        restricted to the Phase-2 text subset (PHASE2_QUARK_KINDS); a record
        with a later-phase kind is dropped (not fatal — the model was told the
        subset, but a stray Time/Behavior is ignored, not fabricated).
        """
        if not raw:
            return [], False
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return [], False
        if not isinstance(data, list):
            return [], False
        records: list[QuarkRecord] = []
        for item in data:
            if not isinstance(item, dict):
                return [], False
            try:
                rec = QuarkRecord(**item)
            except Exception:  # noqa: BLE001 — pydantic ValidationError
                return [], False
            if rec.kind not in PHASE2_QUARK_KINDS:
                continue  # later-phase kind: ignore, don't fabricate
            records.append(rec)
        return records, True

    # -- C6/C7 helpers (mirror insights._base) ---------------------------

    def _safe_dlq(self, error_type: str, error_msg: str, original_data: dict) -> None:
        try:
            self._store.insert_dlq(
                user_id=self._user_id(), source=ENGINE,
                error_type=error_type, error_msg=error_msg,
                original_data=original_data)
        except Exception as exc:  # noqa: BLE001
            logger.debug("quark DLQ insert failed: %s", exc)

    def _user_id(self) -> str:
        # Quark extraction is founder-scoped at the call site; the store's DLQ
        # needs a user_id, so the caller sets _extract_user_id before extract().
        return getattr(self, "_extract_user_id", "") or ""

    def set_user_id(self, user_id: str) -> None:
        """Scope the next extract() + its llm_call_log/DLQ rows to this user."""
        self._extract_user_id = user_id  # type: ignore[attr-defined]

    def _log_call(self, llm_call_id: str, prompt_input: dict, *,
                  response: Optional[dict], model: str, provider: Optional[str],
                  in_toks: int, out_toks: int, latency_ms: int, success: bool,
                  schema_valid: Optional[bool] = None,
                  error_type: Optional[str] = None,
                  error_msg: Optional[str] = None) -> None:
        try:
            self._store.insert_llm_call_log(
                log_id=llm_call_id, user_id=self._user_id(), model=model,
                prompt_input=prompt_input, response=response, provider=provider,
                prompt_template_version=PROMPT_VERSION,
                input_tokens=in_toks or None, output_tokens=out_toks or None,
                latency_ms=latency_ms, success=success, schema_valid=schema_valid,
                error_type=error_type, error_msg=error_msg)
        except Exception as exc:  # noqa: BLE001 — logging must never break extract
            logger.debug("quark llm_call_log insert failed: %s", exc)


# ── module helpers (shared with insights._base idiom) ──────────────────────

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

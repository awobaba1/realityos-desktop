"""RealityOS V6 — shared base for period-scoped LLM insight reports.

The weekly mirror (ADR-V6-017) and the daily report (ADR-V6-018) share an
identical flow — resolve a time window → aggregate atoms → cold-start gate →
one LLM call → C5-validate → C6-log → cache in ``insight_aggregation``. This
module is that flow; subclasses configure the period kind, the prompt, the
gate, and the placeholder/cold-start text (§0.5③). Extracted when the second
report (daily) arrived to avoid ~250 lines of duplication; the 17 weekly
mirror tests guard the refactor.

All observation-only: never raises into a caller (C7). The shared PTGStore is
injected (sovereignty pattern); tests pass a temp store + a mock caller.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional, Tuple

from plugins.memory.ptg.store import PTGStore

from .aggregation import aggregate_window

logger = logging.getLogger(__name__)

_BEIJING_TZ = timezone(timedelta(hours=8))

# Default confidence per data_sufficiency (the insight_aggregation.confidence
# column is CHECK BETWEEN 0 AND 1 — a proxy for "how much to trust this cache").
_DEFAULT_CONFIDENCE = {"sufficient": 0.8, "partial": 0.5, "insufficient": 0.2}

# F6 sample-size gate (ADR-V6-042): the eight atom kinds a period report may
# draw conclusions from. The gate caps cached confidence by the weakest
# PRESENT kind's sample (absent kinds are skipped — they drive no conclusion,
# the prompt omits empty sections). Override in a subclass to narrow.
_SAMPLE_GATE_KINDS_DEFAULT = (
    "R3_Person", "R2_Task", "R7_Expression", "R8_Cognition",
    "R12_Outcome", "R1_SelfState", "R9_Emotion", "R0_Entity",
)


def beijing_now() -> datetime:
    return datetime.now(_BEIJING_TZ)


class InsightReportService:
    """Shared period-report flow. Subclasses set the class attributes below and
    override ``_resolve_period`` / ``_gate`` / ``_placeholder`` (and optionally
    ``_format_user_prompt``)."""

    # --- subclass configuration -----------------------------------------
    AGGREGATION_TYPE: str = ""        # 'weekly_mirror' / 'daily_report'
    PROMPT_FILE: str = ""             # 'weekly_mirror_v1.md' / 'daily_report_v1.md'
    PROMPT_VERSION: str = "v1"
    ENGINE: str = "insight"           # llm_call_logs prompt_input.engine / dlq source
    REPORT_STATUS: str = "report"     # the non-placeholder status (weekly: 'mirror')
    PLACEHOLDER_STATUS: str = "placeholder"
    LLM_TEMPERATURE: float = 0.4
    LLM_MAX_TOKENS: int = 800
    MIN_CHARS: int = 80               # C5 text floor
    CONFIDENCE_MAP: Dict[str, float] = _DEFAULT_CONFIDENCE
    CACHE_TTL_DAYS: int = 14          # insight_aggregation.expires_at = period_end + N
    # F6 (ADR-V6-042): atom kinds the sample-size gate grades against. The
    # cached confidence is capped by the weakest PRESENT kind's sample.
    SAMPLE_GATE_KINDS: Tuple[str, ...] = _SAMPLE_GATE_KINDS_DEFAULT

    def __init__(
        self,
        store: PTGStore,
        *,
        user_id: str,
        caller: Optional[Callable[..., Any]] = None,
        now_fn: Callable[[], datetime] = beijing_now,
        timeout: float = 30.0,
        main_runtime: Any = None,
    ) -> None:
        self._store = store
        self._user_id = user_id
        self._caller = caller or _default_llm_caller
        self._now_fn = now_fn
        self._timeout = timeout
        self._main_runtime = main_runtime
        self._system_prompt: Optional[str] = None

    # --- public flow ----------------------------------------------------

    def generate(self, *, period_start: Optional[str] = None,
                 generated_by: str = "scheduled") -> Dict[str, Any]:
        """Generate the report for one period. Never raises (C7).

        ``period_start`` pins the period (format depends on subclass — a
        YYYY-MM-DD for both weekly and daily). None ⇒ the most recently
        COMPLETED period. Returns a dict with ``status`` ∈ {'report','placeholder'},
        ``content``, the window, ``data_sufficiency``, ``llm_call_id`` (None on
        the placeholder path), and ``schema_valid``.
        """
        now = self._now_fn()
        win = self._resolve_period(now, period_start)

        memo_total = self._safe_memo_count()
        reg_days = _registration_days(self._store, self._user_id, now)
        agg = aggregate_window(
            self._store, user_id=self._user_id,
            week_start=win["start_utc"], week_end=win["end_utc"])
        agg["memo_count_total"] = memo_total
        agg["registration_days"] = reg_days
        agg["data_sufficiency"] = None  # filled by the gate below
        agg["period_start"] = win["start_display"]
        agg["period_end"] = win["end_display"]

        sufficiency, status = self._gate(memo_total, reg_days, agg)
        agg["data_sufficiency"] = sufficiency

        if status == self.PLACEHOLDER_STATUS:
            content = self._placeholder(agg)
            llm_call_id: Optional[str] = None
            schema_valid: Optional[bool] = None
        else:
            content, llm_call_id, schema_valid = self._llm(agg)

        confidence = self.CONFIDENCE_MAP.get(sufficiency, 0.2)

        # F6 sample-size gate (ADR-V6-042): cap cached confidence by the weakest
        # PRESENT atom-kind sample. The cold-start ``data_sufficiency`` gates
        # generate-vs-placeholder (kept as-is); this gate reflects "how much to
        # trust the GENERATED content" — a report whose emotion section rests on
        # n=2 cannot be cached at 0.8 just because the week had many memos.
        # The sample verdict is recorded in input_data for traceability.
        from plugins.memory.ptg.confidence_gate import cap_confidence_by_atom_samples
        sample_conf, sample_suff, sample_kind = cap_confidence_by_atom_samples(
            confidence, agg.get("atom_counts", {}), self.SAMPLE_GATE_KINDS)
        confidence = sample_conf
        agg["sample_sufficiency"] = sample_suff
        agg["sample_weakest_kind"] = sample_kind

        insight_id = self._safe_upsert(
            period_key=win["period_key"],
            period_start=win["start_utc"], period_end=win["end_utc"],
            input_data=agg, result_data=content, confidence=confidence,
            data_sufficiency=sufficiency, generated_by=generated_by,
            llm_call_id=llm_call_id, schema_valid=schema_valid)

        return {
            "status": status,
            "content": content,
            "period_start": win["start_display"],
            "period_end": win["end_display"],
            "data_sufficiency": sufficiency,
            "llm_call_id": llm_call_id,
            "schema_valid": schema_valid,
            "insight_id": insight_id,
            "atom_counts": agg["atom_counts"],
            "memo_count_total": memo_total,
            "registration_days": reg_days,
        }

    # --- subclass hooks -------------------------------------------------

    def _resolve_period(self, now: datetime, period_start: Optional[str]) -> Dict[str, str]:
        """Return {period_key, start_utc, end_utc (half-open query window),
        start_display, end_display}. Subclass-specific."""
        raise NotImplementedError

    def _gate(self, memo_total: int, reg_days: int,
              agg: Dict[str, Any]) -> Tuple[str, str]:
        """Cold-start gate → (data_sufficiency, status). status ∈
        {'report','placeholder'}. Subclass-specific (§0.5③)."""
        raise NotImplementedError

    def _placeholder(self, agg: Dict[str, Any], *, error: bool = False) -> str:
        raise NotImplementedError

    def _format_user_prompt(self, agg: Dict[str, Any]) -> str:
        """Default: hand the structured aggregation to the prompt as JSON."""
        data = {k: v for k, v in agg.items()
                if k in ("period_start", "period_end", "data_sufficiency",
                         "atom_counts", "people", "tasks", "task_outcomes",
                         "emotions", "cognitions", "self_states", "expressions",
                         "top_entities")}
        return (
            f"以下是用户本期（{agg.get('period_start')} ~ {agg.get('period_end')}）"
            f"的结构化数据（data_sufficiency={agg.get('data_sufficiency')}）。"
            "请按系统提示的格式与铁律，生成这份报告：\n\n"
            f"```json\n{json.dumps(data, ensure_ascii=False, indent=2)}\n```"
        )

    # --- shared LLM + C5 + C6 + cache (never raise, C7) -----------------

    def _system(self) -> str:
        if self._system_prompt is None:
            assert self.PROMPT_FILE, "subclass must set PROMPT_FILE"
            from pathlib import Path
            path = Path(__file__).parent / "prompt_templates" / self.PROMPT_FILE
            self._system_prompt = path.read_text(encoding="utf-8")
        return self._system_prompt

    def _llm(self, agg: Dict[str, Any]) -> Tuple[str, Optional[str], Optional[bool]]:
        llm_call_id = str(uuid.uuid4())
        system_prompt = self._system()
        user_prompt = self._format_user_prompt(agg)
        prompt_input = {
            "engine": self.ENGINE,
            "prompt_version": self.PROMPT_VERSION,
            "period_start": agg.get("period_start"),
            "atom_total": agg.get("atom_total", 0),
            "system_prompt_hash": _sha_short(system_prompt),
        }
        start = _monotonic()
        try:
            response = self._caller(
                task=self.ENGINE,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=self.LLM_TEMPERATURE,
                max_tokens=self.LLM_MAX_TOKENS,
                timeout=self._timeout,
                main_runtime=self._main_runtime,
            )
        except Exception as exc:  # noqa: BLE001 — fail-open, C7
            latency = int((_monotonic() - start) * 1000)
            self._log_call(llm_call_id, prompt_input, response=None, model="unknown",
                           provider=None, in_toks=0, out_toks=0, latency_ms=latency,
                           success=False, error_type=type(exc).__name__, error_msg=str(exc))
            self._safe_dlq(self._dlq_error_type(), str(exc),
                           {"period_start": agg.get("period_start")})
            logger.warning("%s LLM call failed: %s", self.ENGINE, exc)
            return self._placeholder(agg, error=True), llm_call_id, False

        text = _response_text(response)
        model = getattr(response, "model", "unknown") or "unknown"
        provider = getattr(response, "provider", None)
        usage = getattr(response, "usage", None)
        in_toks = _usage_tokens(usage, "prompt_tokens")
        out_toks = _usage_tokens(usage, "completion_tokens")
        latency = int((_monotonic() - start) * 1000)

        schema_valid = self._validate(text)
        self._log_call(llm_call_id, prompt_input, response={"content": text[:2000]},
                       model=model, provider=provider, in_toks=in_toks, out_toks=out_toks,
                       latency_ms=latency, success=True, schema_valid=schema_valid)
        if not schema_valid:
            self._safe_dlq(self._dlq_schema_invalid_type(),
                           "LLM output failed the report C5 floor",
                           {"period_start": agg.get("period_start"),
                            "raw_len": len(text), "preview": text[:200]})
            logger.warning("%s C5 invalid (llm_call=%s)", self.ENGINE, llm_call_id)
            return self._placeholder(agg, error=True), llm_call_id, False
        return text, llm_call_id, True

    def _validate(self, text: str) -> bool:
        """C5 floor: real prose, not empty / too short / JSON-leak."""
        if not text:
            return False
        t = text.strip()
        if len(t) < self.MIN_CHARS:
            return False
        if t.startswith("{") or t.startswith("["):
            return False
        return True

    def _safe_memo_count(self) -> int:
        try:
            return self._store.memo_count(self._user_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s memo_count failed (gate→placeholder): %s",
                           self.ENGINE, exc)
            return 0

    def _safe_upsert(self, *, period_key, period_start, period_end, input_data,
                     result_data, confidence, data_sufficiency, generated_by,
                     llm_call_id, schema_valid) -> str:
        expires = _add_days_iso(period_end, self.CACHE_TTL_DAYS)
        try:
            return self._store.upsert_insight(
                user_id=self._user_id,
                aggregation_type=self.AGGREGATION_TYPE,
                period_key=period_key, period_start=period_start, period_end=period_end,
                input_data=json.dumps(input_data, ensure_ascii=False),
                result_data=result_data, confidence=confidence,
                data_days=self._cache_data_days(), data_sufficiency=data_sufficiency,
                generated_by=generated_by, llm_call_id=llm_call_id,
                schema_version=self.PROMPT_VERSION, expires_at=expires)
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s upsert failed (%s): %s",
                           self.ENGINE, period_key, exc)
            return ""

    def _cache_data_days(self) -> int:
        """Days of data the report covers (weekly=7, daily=1). Subclass override."""
        return 7

    def _dlq_error_type(self) -> str:
        """DLQ error_type for an LLM call failure. Subclass may override to
        preserve a shipped label."""
        return f"{self.ENGINE}_error"

    def _dlq_schema_invalid_type(self) -> str:
        """DLQ error_type for a C5-validation failure. Subclass may override."""
        return f"{self.ENGINE}_schema_invalid"

    def _safe_dlq(self, error_type: str, error_msg: str, original_data: dict) -> None:
        try:
            self._store.insert_dlq(
                user_id=self._user_id, source=self.ENGINE,
                error_type=error_type, error_msg=error_msg,
                original_data=original_data)
        except Exception as exc:  # noqa: BLE001
            logger.debug("%s DLQ insert failed: %s", self.ENGINE, exc)

    def _log_call(self, llm_call_id: str, prompt_input: dict, *,
                  response: Optional[dict], model: str, provider: Optional[str],
                  in_toks: int, out_toks: int, latency_ms: int, success: bool,
                  schema_valid: Optional[bool] = None, cost_cny: Optional[float] = None,
                  error_type: Optional[str] = None, error_msg: Optional[str] = None) -> None:
        try:
            self._store.insert_llm_call_log(
                log_id=llm_call_id, user_id=self._user_id, model=model,
                prompt_input=prompt_input, response=response, provider=provider,
                prompt_template_version=self.PROMPT_VERSION,
                input_tokens=in_toks or None, output_tokens=out_toks or None,
                latency_ms=latency_ms, success=success, schema_valid=schema_valid,
                cost_cny=cost_cny, error_type=error_type, error_msg=error_msg)
        except Exception as exc:  # noqa: BLE001 — logging must never break the service
            logger.debug("%s llm_call_log insert failed: %s", self.ENGINE, exc)


# ── module helpers (shared) ───────────────────────────────────────────────

def _registration_days(store: PTGStore, user_id: str, now: datetime) -> int:
    try:
        created = store.user_created_at(user_id)
        if not created:
            return 0
        created_dt = datetime.fromisoformat(created)
        if created_dt.tzinfo is None:
            created_dt = created_dt.replace(tzinfo=timezone.utc)
        return max(0, (now.astimezone(timezone.utc) - created_dt).days)
    except Exception:  # noqa: BLE001
        return 0


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


def _monotonic() -> float:
    import time
    return time.monotonic()


def _add_days_iso(utc_iso: str, days: int) -> str:
    try:
        dt = datetime.fromisoformat(utc_iso)
        return (dt + timedelta(days=days)).isoformat()
    except Exception:  # noqa: BLE001
        return utc_iso


def _sha_short(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _default_llm_caller(**kwargs: Any) -> Any:
    """Default caller: the hermes auxiliary client (sync). Imported lazily so the
    module (and its tests) load without the agent package present."""
    from agent.auxiliary_client import call_llm  # type: ignore[import-not-found]
    return call_llm(**kwargs)

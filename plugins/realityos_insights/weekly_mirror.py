"""RealityOS V6 — Weekly Mirror service (PRD #4, 架构 §0.5③/§4.4, ADR-V6-017).

The first INSIGHT product built on the Phase-1 atom layer. Each week, weave
the founder's atoms (people / tasks / states / cognitions / emotions /
outcomes) into a warm, specific mirror — the retention-critical feature the
architecture singles out (§0.5③: a Day-3 "你这周提了 0 次家人" mirror would
trigger an uninstall, so a cold-start gate gates it).

Flow (``generate``):
  1. resolve the Beijing-local previous Mon–Sun week → query window in UTC
     (event timestamps are stored UTC; the week is a local concept).
  2. cold-start gate (§0.5③): registration < 14 days OR total memos < 15 →
     ``insufficient`` ⇒ emit a guidance placeholder, NO LLM call. < 30 memos ⇒
     ``partial`` (soft conclusions). else ``sufficient``.
  3. aggregate the week's atoms (``aggregation.aggregate_week``).
  4. if not insufficient: one LLM call with ``weekly_mirror_v1`` (C6 versioned)
     + the aggregation JSON → markdown mirror. C5-validates the output, logs
     the call (C6), DLQs + placeholders on failure (C7).
  5. upsert into ``insight_aggregation`` (aggregation_type='weekly_mirror',
     one row per user/week via the unique index — regenerate replaces in place).

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

from .aggregation import aggregate_week

logger = logging.getLogger(__name__)

PROMPT_VERSION = "v1"
AGGREGATION_TYPE = "weekly_mirror"
_PROMPT_FILE = "weekly_mirror_v1.md"

# Cold-start gate thresholds (架构 §0.5③).
MIN_MEMOS = 15            # < 15 total memos → insufficient (placeholder, no LLM)
MIN_REGISTRATION_DAYS = 14  # < 14 days since registration → insufficient
PARTIAL_MEMO_THRESHOLD = 30  # < 30 → partial (soft conclusions); ≥ 30 → sufficient

_BEIJING_TZ = timezone(timedelta(hours=8))

# Mirror-output C5 floor: the LLM must return real prose, not empty/error.
_MIN_MIRROR_CHARS = 80


def _beijing_now() -> datetime:
    return datetime.now(_BEIJING_TZ)


class WeeklyMirrorService:
    """Generate (or regenerate) one user's weekly mirror."""

    def __init__(
        self,
        store: PTGStore,
        *,
        user_id: str,
        caller: Optional[Callable[..., Any]] = None,
        now_fn: Callable[[], datetime] = _beijing_now,
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

    # -- public -----------------------------------------------------------

    def generate(self, *, week_start: Optional[str] = None,
                 generated_by: str = "scheduled") -> Dict[str, Any]:
        """Generate the mirror for one week. Never raises (C7).

        ``week_start`` (YYYY-MM-DD, Beijing-local) pins the week; None ⇒ the
        most recently COMPLETED Mon–Sun week. ``generated_by`` ∈
        {'scheduled','manual','on_demand'} (the insight_aggregation enum).
        Returns a result dict with ``status`` ∈ {'mirror','placeholder'},
        the ``content``, the week window, ``data_sufficiency``, and
        ``llm_call_id`` (None for the placeholder path).
        """
        now = self._now_fn()
        win = _resolve_week(now, week_start)

        memo_total = self._safe_memo_count()
        reg_days = _registration_days(self._store, self._user_id, now)
        sufficiency, status = self._gate(memo_total, reg_days)

        agg = aggregate_week(
            self._store, user_id=self._user_id,
            week_start=win["week_start_utc"], week_end=win["week_end_utc"])
        agg["memo_count_total"] = memo_total
        agg["registration_days"] = reg_days
        agg["data_sufficiency"] = sufficiency
        agg["week_start"] = win["week_start_display"]
        agg["week_end"] = win["week_end_display"]

        if status == "placeholder":
            content = _placeholder(agg)
            llm_call_id: Optional[str] = None
            schema_valid: Optional[bool] = None
        else:
            content, llm_call_id, schema_valid = self._llm_mirror(agg)

        confidence = _confidence_for(sufficiency)
        insight_id = self._safe_upsert(
            period_key=win["period_key"],
            period_start=win["week_start_utc"],
            period_end=win["week_end_utc"],
            input_data=agg, result_data=content, confidence=confidence,
            data_sufficiency=sufficiency, generated_by=generated_by,
            llm_call_id=llm_call_id, schema_valid=schema_valid,
            week_end_display=win["week_end_display"])

        return {
            "status": status,
            "content": content,
            "week_start": win["week_start_display"],
            "week_end": win["week_end_display"],
            "data_sufficiency": sufficiency,
            "llm_call_id": llm_call_id,
            "schema_valid": schema_valid,
            "insight_id": insight_id,
            "atom_counts": agg["atom_counts"],
            "memo_count_total": memo_total,
            "registration_days": reg_days,
        }

    # -- gate -------------------------------------------------------------

    def _gate(self, memo_total: int, reg_days: int) -> Tuple[str, str]:
        """§0.5③ cold-start gate → (data_sufficiency, status)."""
        if reg_days < MIN_REGISTRATION_DAYS or memo_total < MIN_MEMOS:
            return "insufficient", "placeholder"
        if memo_total < PARTIAL_MEMO_THRESHOLD:
            return "partial", "mirror"
        return "sufficient", "mirror"

    # -- LLM --------------------------------------------------------------

    def _system(self) -> str:
        if self._system_prompt is None:
            self._system_prompt = _load_prompt(_PROMPT_FILE)
        return self._system_prompt

    def _llm_mirror(self, agg: Dict[str, Any]) -> Tuple[str, Optional[str], Optional[bool]]:
        """One LLM call → markdown mirror. Returns (content, llm_call_id, schema_valid).

        On any failure (call error / C5 invalid / parse) the mirror degrades to
        a placeholder + DLQ entry, but the call is still logged (C6) and the
        llm_call_id returned for traceability. Never raises (C7).
        """
        llm_call_id = str(uuid.uuid4())
        system_prompt = self._system()
        user_prompt = _format_user_prompt(agg)
        prompt_input = {
            "engine": "weekly_mirror",
            "prompt_version": PROMPT_VERSION,
            "week_start": agg.get("week_start"),
            "atom_total": agg.get("atom_total", 0),
            "system_prompt_hash": _sha_short(system_prompt),
        }
        start = _monotonic()
        try:
            response = self._caller(
                task="weekly_mirror",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.4,
                max_tokens=800,
                timeout=self._timeout,
                main_runtime=self._main_runtime,
            )
        except Exception as exc:  # noqa: BLE001 — fail-open, C7
            latency = int((_monotonic() - start) * 1000)
            self._log_call(llm_call_id, prompt_input, response=None, model="unknown",
                           provider=None, in_toks=0, out_toks=0, latency_ms=latency,
                           success=False, error_type=type(exc).__name__,
                           error_msg=str(exc))
            self._safe_dlq("llm_mirror_error", str(exc),
                           {"week_start": agg.get("week_start")})
            logger.warning("Weekly mirror LLM call failed: %s", exc)
            return _placeholder(agg, error=True), llm_call_id, False

        text = _response_text(response)
        model = getattr(response, "model", "unknown") or "unknown"
        provider = getattr(response, "provider", None)
        usage = getattr(response, "usage", None)
        in_toks = _usage_tokens(usage, "prompt_tokens")
        out_toks = _usage_tokens(usage, "completion_tokens")
        latency = int((_monotonic() - start) * 1000)

        schema_valid = _validate_mirror(text)
        self._log_call(llm_call_id, prompt_input, response={"content": text[:2000]},
                       model=model, provider=provider, in_toks=in_toks, out_toks=out_toks,
                       latency_ms=latency, success=True, schema_valid=schema_valid)
        if not schema_valid:
            self._safe_dlq("mirror_schema_invalid",
                           "LLM output failed the mirror C5 floor",
                           {"week_start": agg.get("week_start"),
                            "raw_len": len(text), "preview": text[:200]})
            logger.warning("Weekly mirror C5 invalid (llm_call=%s)", llm_call_id)
            return _placeholder(agg, error=True), llm_call_id, False
        return text, llm_call_id, True

    # -- store helpers (never raise, C7) ----------------------------------

    def _safe_memo_count(self) -> int:
        try:
            return self._store.memo_count(self._user_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("memo_count failed; treating as 0 (gate→placeholder): %s", exc)
            return 0

    def _safe_upsert(self, *, period_key, period_start, period_end, input_data,
                     result_data, confidence, data_sufficiency, generated_by,
                     llm_call_id, schema_valid, week_end_display) -> str:
        expires = _add_days_iso(period_end, 14)
        try:
            return self._store.upsert_insight(
                user_id=self._user_id, aggregation_type=AGGREGATION_TYPE,
                period_key=period_key, period_start=period_start, period_end=period_end,
                input_data=json.dumps(input_data, ensure_ascii=False),
                result_data=result_data, confidence=confidence,
                data_days=7, data_sufficiency=data_sufficiency,
                generated_by=generated_by, llm_call_id=llm_call_id,
                schema_version=PROMPT_VERSION, expires_at=expires)
        except Exception as exc:  # noqa: BLE001
            logger.warning("weekly mirror upsert failed (%s): %s", period_key, exc)
            return ""

    def _safe_dlq(self, error_type: str, error_msg: str, original_data: dict) -> None:
        try:
            self._store.insert_dlq(
                user_id=self._user_id, source="weekly_mirror",
                error_type=error_type, error_msg=error_msg,
                original_data=original_data)
        except Exception as exc:  # noqa: BLE001
            logger.debug("weekly mirror DLQ insert failed: %s", exc)

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
                error_type=error_type, error_msg=error_msg)
        except Exception as exc:  # noqa: BLE001 — logging must never break the service
            logger.debug("weekly mirror llm_call_log insert failed: %s", exc)


# ── module helpers ────────────────────────────────────────────────────────

def _resolve_week(now: datetime, week_start: Optional[str]) -> Dict[str, str]:
    """Beijing-local week → {period_key, week_start/end_utc (for query), displays}.

    Default = the most recently COMPLETED Mon–Sun week in Beijing time. The
    event tables store timestamps in UTC (``_now_iso``), so the window is
    converted to UTC for the query; displays stay Beijing-local for humans.
    """
    if week_start:
        ws_local = datetime.strptime(week_start, "%Y-%m-%d").replace(
            tzinfo=_BEIJING_TZ, hour=0, minute=0, second=0, microsecond=0)
    else:
        # Monday 00:00 Beijing of the current week, then step back 7d.
        this_monday = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0)
        ws_local = this_monday - timedelta(days=7)
    we_local = ws_local + timedelta(days=7)
    ws_utc = ws_local.astimezone(timezone.utc).isoformat()
    we_utc = we_local.astimezone(timezone.utc).isoformat()
    last_day = (we_local - timedelta(days=1)).strftime("%Y-%m-%d")
    return {
        "period_key": ws_local.strftime("%Y-%m-%d"),
        "week_start_utc": ws_utc,
        "week_end_utc": we_utc,
        "week_start_display": ws_local.strftime("%Y-%m-%d"),
        "week_end_display": last_day,
    }


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


def _confidence_for(sufficiency: str) -> float:
    return {"sufficient": 0.8, "partial": 0.5, "insufficient": 0.2}.get(sufficiency, 0.2)


def _placeholder(agg: Dict[str, Any], *, error: bool = False) -> str:
    """The §0.5③ cold-start (or error) placeholder — never an LLM call product."""
    week_start = agg.get("week_start", "本周")
    week_end = agg.get("week_end", "")
    span = f"{week_start} ~ {week_end}" if week_end else week_start
    if error:
        return (
            f"# 本周镜面（{span}）\n\n"
            "这周的镜面我还没准备好（生成时遇到一点问题）。\n"
            "你的数据都在，稍后再让我照一次。继续和我聊，我越聊越懂你。"
        )
    total = agg.get("memo_count_total", 0)
    return (
        f"# 本周镜面（{span}）\n\n"
        "我还在了解你，继续和我聊几天，下周给你第一份镜面。\n\n"
        f"（目前已记住 {total} 条，等积累再多一些，镜面会更准。）"
    )


def _validate_mirror(text: str) -> bool:
    """C5 floor: real prose, not empty/error/JSON-garbage."""
    if not text:
        return False
    t = text.strip()
    if len(t) < _MIN_MIRROR_CHARS:
        return False
    # Must read as mirror prose, not a leaked JSON/error block.
    if t.startswith("{") or t.startswith("["):
        return False
    return True


def _format_user_prompt(agg: Dict[str, Any]) -> str:
    data = {k: v for k, v in agg.items()
            if k in ("week_start", "week_end", "data_sufficiency", "atom_counts",
                     "people", "tasks", "task_outcomes", "emotions", "cognitions",
                     "self_states", "expressions", "top_entities")}
    return (
        f"以下是用户本周（{agg.get('week_start')} ~ {agg.get('week_end')}）"
        f"的结构化数据（data_sufficiency={agg.get('data_sufficiency')}）。"
        "请按系统提示的格式与铁律，生成这份周镜面：\n\n"
        f"```json\n{json.dumps(data, ensure_ascii=False, indent=2)}\n```"
    )


def _load_prompt(filename: str) -> str:
    from pathlib import Path
    path = Path(__file__).parent / "prompt_templates" / filename
    return path.read_text(encoding="utf-8")


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

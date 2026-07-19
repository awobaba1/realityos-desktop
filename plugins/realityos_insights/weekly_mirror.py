"""RealityOS V6 — Weekly Mirror service (PRD #4, 架构 §0.5③/§4.4, ADR-V6-017).

The first INSIGHT product built on the Phase-1 atom layer. Each week, weave
the founder's atoms (people / tasks / states / cognitions / emotions /
outcomes) into a warm, specific mirror — the retention-critical feature the
architecture singles out (§0.5③: a Day-3 "你这周提了 0 次家人" mirror would
trigger an uninstall, so a cold-start gate gates it).

The shared report flow (resolve period → aggregate → gate → LLM → C5 → C6 →
cache) lives in ``InsightReportService`` (``_base.py``, extracted ADR-V6-018
when the daily report arrived). This module is the **weekly configuration**:
the Mon–Sun period resolver, the §0.5③ memo/registration gate, the warm
weekly placeholders, and the v1 weekly prompt. The 17 weekly-mirror tests pin
this contract; the refactor changed only plumbing, not behavior.

All observation-only: never raises into a caller (C7). The shared PTGStore is
injected (sovereignty pattern); tests pass a temp store + a mock caller.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from ._base import InsightReportService

logger = logging.getLogger(__name__)

PROMPT_VERSION = "v1"
AGGREGATION_TYPE = "weekly_mirror"
_PROMPT_FILE = "weekly_mirror_v1.md"

# Cold-start gate thresholds (架构 §0.5③).
MIN_MEMOS = 15              # < 15 total memos → insufficient (placeholder, no LLM)
MIN_REGISTRATION_DAYS = 14  # < 14 days since registration → insufficient
PARTIAL_MEMO_THRESHOLD = 30  # < 30 → partial (soft conclusions); ≥ 30 → sufficient

_BEIJING_TZ = timezone(timedelta(hours=8))

# Mirror-output C5 floor: the LLM must return real prose, not empty/error.
_MIN_MIRROR_CHARS = 80


class WeeklyMirrorService(InsightReportService):
    """Generate (or regenerate) one user's weekly mirror.

    Thin weekly configuration over ``InsightReportService``. The report status
    is ``'mirror'`` (LLM path) or ``'placeholder'`` (cold-start / error).
    """

    AGGREGATION_TYPE = AGGREGATION_TYPE
    PROMPT_FILE = _PROMPT_FILE
    PROMPT_VERSION = PROMPT_VERSION
    ENGINE = "weekly_mirror"
    REPORT_STATUS = "mirror"          # the non-placeholder status (test contract)
    MIN_CHARS = _MIN_MIRROR_CHARS

    # -- public: accept the weekly-named kwarg the callers/tests use ----------

    def generate(self, *, week_start: Optional[str] = None,
                 generated_by: str = "scheduled") -> Dict[str, Any]:
        """Generate the mirror for one week. Never raises (C7).

        ``week_start`` (YYYY-MM-DD, Beijing-local) pins the week; None ⇒ the
        most recently COMPLETED Mon–Sun week. Returns a result dict with
        ``status`` ∈ {'mirror','placeholder'}.
        """
        return super().generate(period_start=week_start, generated_by=generated_by)

    # -- subclass hooks ------------------------------------------------------

    def _resolve_period(self, now: datetime,
                        period_start: Optional[str]) -> Dict[str, str]:
        w = _resolve_week(now, period_start)
        # Map the weekly-named keys to the base's window-vocabulary.
        return {
            "period_key": w["period_key"],
            "start_utc": w["week_start_utc"],
            "end_utc": w["week_end_utc"],
            "start_display": w["week_start_display"],
            "end_display": w["week_end_display"],
        }

    def _gate(self, memo_total: int, reg_days: int,
              agg: Dict[str, Any]) -> Tuple[str, str]:
        """§0.5③ cold-start gate → (data_sufficiency, status)."""
        if reg_days < MIN_REGISTRATION_DAYS or memo_total < MIN_MEMOS:
            return "insufficient", self.PLACEHOLDER_STATUS
        if memo_total < PARTIAL_MEMO_THRESHOLD:
            return "partial", self.REPORT_STATUS
        return "sufficient", self.REPORT_STATUS

    def _placeholder(self, agg: Dict[str, Any], *, error: bool = False) -> str:
        return _placeholder(agg, error=error)

    def _validate(self, text: str) -> bool:
        return _validate_mirror(text)

    # Preserve the exact shipped DLQ labels (source='weekly_mirror',
    # error_type 'llm_mirror_error' / 'mirror_schema_invalid') — the weekly
    # test contract pins these.
    def _dlq_error_type(self) -> str:
        return "llm_mirror_error"

    def _dlq_schema_invalid_type(self) -> str:
        return "mirror_schema_invalid"


# ── module helpers (period resolution + placeholders + C5 floor) ──────────


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


def _placeholder(agg: Dict[str, Any], *, error: bool = False) -> str:
    """The §0.5③ cold-start (or error) placeholder — never an LLM call product."""
    start = agg.get("period_start") or agg.get("week_start") or "本周"
    end = agg.get("period_end") or agg.get("week_end") or ""
    span = f"{start} ~ {end}" if end else start
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

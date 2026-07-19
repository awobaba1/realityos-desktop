"""RealityOS V6 — Daily Report service (PRD #2, 架构 §4.4/§18.5, ADR-V6-018).

The second INSIGHT product on the Phase-1 atom layer. Each day, weave that
**one day's** atoms into a short, specific end-of-day recap ("今天我都在忙
什么、和谁、状态如何"). Lighter and more immediate than the weekly mirror
(ADR-V6-017) — a quick look back at a single day, not a week's arc.

The shared report flow (resolve period → aggregate → gate → LLM → C5 → C6 →
cache) lives in ``InsightReportService`` (``_base.py``). This module is the
**daily configuration**: the 1-day period resolver, an atom-count gate (a day
with < 3 atoms has nothing to say → placeholder, no LLM), the daily
placeholders, and the v1 daily prompt.

Gate rationale: the §0.5③ memo/registration gate is weekly-specific (it guards
against a Day-3 "你这周提了 0 次家人"). For a *daily* report the right signal is
"did the user talk enough **today** to say anything" — an atom-count gate on
the day's window. A brand-new user (day 1) naturally has ~0 atoms that day →
insufficient → placeholder; no separate registration check needed.

All observation-only: never raises into a caller (C7). The shared PTGStore is
injected; tests pass a temp store + a mock caller.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from ._base import InsightReportService

logger = logging.getLogger(__name__)

PROMPT_VERSION = "v1"
AGGREGATION_TYPE = "daily_report"
_PROMPT_FILE = "daily_report_v1.md"

# Cold-start gate thresholds for a single day (the §0.5③ memo/registration gate
# is weekly-specific; a day gates on that day's atom count).
MIN_ATOMS = 3              # < 3 atoms that day → insufficient (placeholder, no LLM)
PARTIAL_ATOM_THRESHOLD = 8  # < 8 → partial (soft); ≥ 8 → sufficient

_BEIJING_TZ = timezone(timedelta(hours=8))


class DailyReportService(InsightReportService):
    """Generate (or regenerate) one user's daily report.

    Thin daily configuration over ``InsightReportService``. The report status
    is ``'report'`` (LLM path) or ``'placeholder'`` (cold-start / error).
    """

    AGGREGATION_TYPE = AGGREGATION_TYPE
    PROMPT_FILE = _PROMPT_FILE
    PROMPT_VERSION = PROMPT_VERSION
    ENGINE = "daily_report"
    REPORT_STATUS = "report"
    MIN_CHARS = 60            # a day's recap is shorter than a week's mirror
    CACHE_TTL_DAYS = 7        # a daily report is stale sooner than a weekly one

    # -- public: accept the daily-named kwarg the callers/tests use ----------

    def generate(self, *, day: Optional[str] = None,
                 generated_by: str = "scheduled") -> Dict[str, Any]:
        """Generate the report for one day. Never raises (C7).

        ``day`` (YYYY-MM-DD, Beijing-local) pins the day; None ⇒ the most
        recently COMPLETED day (yesterday in Beijing time). Returns a result
        dict with ``status`` ∈ {'report','placeholder'}.
        """
        return super().generate(period_start=day, generated_by=generated_by)

    # -- subclass hooks ------------------------------------------------------

    def _resolve_period(self, now: datetime,
                        period_start: Optional[str]) -> Dict[str, str]:
        return _resolve_day(now, period_start)

    def _gate(self, memo_total: int, reg_days: int,
              agg: Dict[str, Any]) -> Tuple[str, str]:
        """Atom-count gate for one day → (data_sufficiency, status)."""
        atom_total = int(agg.get("atom_total", 0))
        if atom_total < MIN_ATOMS:
            return "insufficient", self.PLACEHOLDER_STATUS
        if atom_total < PARTIAL_ATOM_THRESHOLD:
            return "partial", self.REPORT_STATUS
        return "sufficient", self.REPORT_STATUS

    def _placeholder(self, agg: Dict[str, Any], *, error: bool = False) -> str:
        return _placeholder(agg, error=error)

    def _cache_data_days(self) -> int:
        return 1


# ── module helpers (period resolution + placeholders) ─────────────────────


def _resolve_day(now: datetime, day: Optional[str]) -> Dict[str, str]:
    """Beijing-local day → {period_key, start/end_utc (for query), displays}.

    Default = the most recently COMPLETED day = yesterday in Beijing time. The
    event tables store timestamps in UTC, so the window is converted to UTC for
    the query; displays stay Beijing-local. The window is half-open
    ``[day 00:00, next-day 00:00)`` Beijing.
    """
    if day:
        ds_local = datetime.strptime(day, "%Y-%m-%d").replace(
            tzinfo=_BEIJING_TZ, hour=0, minute=0, second=0, microsecond=0)
    else:
        # Yesterday 00:00 Beijing (now is Beijing-aware).
        ds_local = (now - timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
    de_local = ds_local + timedelta(days=1)
    ds_utc = ds_local.astimezone(timezone.utc).isoformat()
    de_utc = de_local.astimezone(timezone.utc).isoformat()
    date_str = ds_local.strftime("%Y-%m-%d")
    return {
        "period_key": date_str,
        "start_utc": ds_utc,
        "end_utc": de_utc,
        "start_display": date_str,
        "end_display": date_str,   # a single-day report's end display = same day
    }


def _placeholder(agg: Dict[str, Any], *, error: bool = False) -> str:
    """The cold-start (or error) placeholder for one day — never an LLM product."""
    day = agg.get("period_start") or "今天"
    if error:
        return (
            f"# 今日报告（{day}）\n\n"
            "今天的报告我还没准备好（生成时遇到一点问题）。\n"
            "你的数据都在，稍后再让我整理一次。继续和我聊，我越聊越懂你。"
        )
    return (
        f"# 今日报告（{day}）\n\n"
        "今天我们聊得不多，还没有足够的内容来整理一份报告。\n"
        "继续和我聊几句你今天在忙什么、心情如何，下次就能给你一份像样的回看了。"
    )

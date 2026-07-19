"""RealityOS V6 — insights plugin (PRD #4/#2, 架构 §4.4/§18.5, ADR-V6-017/018).

The INSIGHT layer built on the Phase-1 atom layer. Two period reports ship in
Phase 1b, both thin configurations over the shared ``InsightReportService``
flow (``_base.py``, extracted ADR-V6-018):

- **Weekly mirror** (PRD #4, ADR-V6-017): each week, weave the founder's atoms
  into a warm, specific mirror, gated by the §0.5③ cold-start rule
  (registration < 14 days OR < 15 memos ⇒ a guidance placeholder, never a
  premature "你这周提了 0 次家人").
- **Daily report** (PRD #2, ADR-V6-018): each day, a short end-of-day recap of
  that one day's atoms, gated by an atom-count rule (< 3 atoms that day ⇒
  placeholder, no LLM).

Inject a shared ``PTGStore`` (same ``resolve_db_path`` singleton as
PTGProvider/capture, ADR-V6-008 decision 3) and call ``generate()``. Output
lands in the existing ``insight_aggregation`` table (aggregation_type=
'weekly_mirror' / 'daily_report'); no schema change (SCHEMA_VERSION stays 6).
Every LLM call is logged (C6); failures DLQ + degrade to a placeholder (C7);
the v1 prompts are versioned, never overwritten (C6).

``register`` is a no-op debug log, like ``realityos_sovereignty`` — the
services are called explicitly by the desktop UI / scheduled job (the wiring
is the documented next step, not fake-green'd here).
"""

from __future__ import annotations

import logging

from .daily_report import (
    MIN_ATOMS as DAILY_MIN_ATOMS,
    PARTIAL_ATOM_THRESHOLD as DAILY_PARTIAL_ATOM_THRESHOLD,
    DailyReportService,
)
from .weekly_mirror import (
    MIN_MEMOS,
    MIN_REGISTRATION_DAYS,
    PARTIAL_MEMO_THRESHOLD,
    PROMPT_VERSION,
    WeeklyMirrorService,
)

logger = logging.getLogger(__name__)

__all__ = [
    "WeeklyMirrorService",
    "DailyReportService",
    "PROMPT_VERSION",
    "MIN_MEMOS",
    "MIN_REGISTRATION_DAYS",
    "PARTIAL_MEMO_THRESHOLD",
    "DAILY_MIN_ATOMS",
    "DAILY_PARTIAL_ATOM_THRESHOLD",
    "register",
]


def register(ctx) -> None:  # pragma: no cover — insights is called explicitly
    """No-op registration. The report services are invoked explicitly (desktop
    UI / scheduled job), mirroring the sovereignty plugin's Phase-1 surface."""
    logger.debug(
        "realityos_insights registered (weekly mirror + daily report services "
        "live; desktop/cron wiring is the documented next step)")

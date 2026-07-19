"""RealityOS V6 — insights plugin (PRD #4/#2, 架构 §4.4/§18.5, ADR-V6-017).

The INSIGHT layer built on the Phase-1 atom layer. Phase 1b-2 ships the
**weekly mirror** (PRD #4): each week, weave the founder's atoms into a warm,
specific mirror, gated by the §0.5③ cold-start rule (registration < 14 days OR
< 15 memos ⇒ a guidance placeholder, never a premature "你这周提了 0 次家人").

The core is ``WeeklyMirrorService`` — inject a shared ``PTGStore`` (same
``resolve_db_path`` singleton as PTGProvider/capture, ADR-V6-008 decision 3)
and call ``generate()``. Output lands in the existing ``insight_aggregation``
table (aggregation_type='weekly_mirror'); no schema change (SCHEMA_VERSION
stays 6). Every LLM call is logged (C6); failures DLQ + degrade to a
placeholder (C7); the v1 prompt is versioned, never overwritten (C6).

``register`` is a no-op debug log, like ``realityos_sovereignty`` — the service
is called explicitly by the desktop UI / scheduled job (the wiring is the
documented next step, not fake-green'd here).
"""

from __future__ import annotations

import logging

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
    "PROMPT_VERSION",
    "MIN_MEMOS",
    "MIN_REGISTRATION_DAYS",
    "PARTIAL_MEMO_THRESHOLD",
    "register",
]


def register(ctx) -> None:  # pragma: no cover — insights is called explicitly
    """No-op registration. The weekly mirror is invoked explicitly (desktop UI /
    scheduled job), mirroring the sovereignty plugin's Phase-1 surface."""
    logger.debug(
        "realityos_insights registered (weekly mirror service live; "
        "desktop/cron wiring is the documented next step)")

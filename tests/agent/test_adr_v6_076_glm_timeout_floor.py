"""ADR-V6-076 P0-3 regression: GLM-5.x reasoning-timeout floor + extraction default.

The 10-agent audit (subagent #3 — agent LLM transport) found that GLM-5.2
(the ADR-093 primary LLM, measured 60-80s thinking TTFB during extraction)
was absent from ``_REASONING_STALE_TIMEOUT_FLOORS`` AND the ``extraction``
auxiliary task defaulted to a 30s request timeout. Either gap alone let
GLM-5.2 silently DLQ every atomize pass — the user sees an API 200 and
assumes green, but zero atoms are written. Together they guaranteed it.

These guards pin both halves of the fix.
"""

from __future__ import annotations

import re
from pathlib import Path


def test_glm5_family_in_reasoning_timeout_floors():
    """GLM-5.x must be in the stale-timeout floor list. Without it the
    primary LLM (ADR-093) gets no thinking-model floor protection."""
    from agent.reasoning_timeouts import _REASONING_STALE_TIMEOUT_FLOORS

    slugs = {slug for slug, _ in _REASONING_STALE_TIMEOUT_FLOORS}
    for expected in ("glm-5", "glm-5.1", "glm-5.2"):
        assert expected in slugs, (
            f"{expected!r} missing from _REASONING_STALE_TIMEOUT_FLOORS — the "
            f"primary LLM (ADR-093) gets no stale-timeout protection and "
            f"silently DLQs atomize passes (ADR-V6-076 P0-3)."
        )


def test_glm5_floor_exceeds_observed_thinking_ttfb():
    """The floor must exceed GLM-5.2's observed 60-80s thinking TTFB, else
    atomize still times out under the floor."""
    from agent.reasoning_timeouts import _REASONING_STALE_TIMEOUT_FLOORS

    floors = dict(_REASONING_STALE_TIMEOUT_FLOORS)
    for slug in ("glm-5", "glm-5.1", "glm-5.2"):
        assert floors[slug] >= 180, (
            f"{slug} floor {floors[slug]}s < 180s — below GLM-5.x thinking "
            f"TTFB (60-80s observed); atomize would still DLQ (ADR-V6-076 P0-3)."
        )


def test_extraction_default_timeout_accommodates_thinking_models():
    """The ``extraction`` auxiliary task default timeout must be >= 120s so a
    thinking model (GLM-5.x 60-80s) isn't cut off at the request layer.
    Was 30s (P0-3). Read from the DEFAULT_CONFIG literal in config.py."""
    src = Path("hermes_cli/config.py").read_text(encoding="utf-8")
    m = re.search(r'"extraction":\s*\{(.*?)\}', src, re.S)
    assert m, "no 'extraction' auxiliary block in hermes_cli/config.py — regression"
    tm = re.search(r'"timeout":\s*(\d+)', m.group(1))
    assert tm, "extraction block has no 'timeout' key — regression"
    assert int(tm.group(1)) >= 120, (
        f"extraction timeout {tm.group(1)}s < 120s — GLM-5.x thinking "
        f"(60-80s TTFB) would DLQ every atomize pass (ADR-V6-076 P0-3)."
    )

"""C4 regression: post_gateway_send defer honesty (ADR-V6-064).

ADR-V6-003:11 designed ``post_gateway_send`` as the 'agent 主动出站' capture
point ('新建'). But ``plugins/observability/ptg_capture`` deferred it with a
docstring claiming 'deferred to v2 per D4' — and ADR-V6-003 has NO D4 section
(D4 never existed). Combined with zero emit/handler (grep-confirmed), this was
a triple fake-green: design promise + non-existent-decision excuse + zero
implementation (T5 audit finding, same class as the citation counters ADR-V6-063
fixed, but worse — citation at least wrote).

ADR-V6-064 formalizes the defer with a REAL ADR + real rationale, and replaces
the fake 'per D4' reference. These tests pin the honesty:
  * the docstring cites the real ADR-V6-064 (not a phantom D4),
  * the defer state is grep-confirmed (no emit/handler),
  * ADR-V6-003's lack of a D4 section is recorded as fact (the original excuse
    was baseless).
"""

from __future__ import annotations

import re
from pathlib import Path

# tests/plugins/observability/<file> → parents[3] = repo root
REPO_ROOT = Path(__file__).resolve().parents[3]


def _ptg_capture_source() -> str:
    return (REPO_ROOT / "plugins/observability/ptg_capture/__init__.py").read_text()


def _adr_text(name: str) -> str:
    return (REPO_ROOT / f"docs/adr/V6/{name}.md").read_text()


class TestPostGatewaySendDeferHonesty:
    def test_docstring_references_real_adr_not_phantom_d4(self):
        # The defer excuse must cite the REAL ADR-V6-064, not the phantom "D4"
        # (ADR-V6-003 has no D4 section — confirmed by the test below).
        src = _ptg_capture_source()
        assert "ADR-V6-064" in src, (
            "ptg_capture defer must reference the real ADR-V6-064, not the "
            "phantom 'per D4' (ADR-V6-003 has no D4 section)")
        # The original baseless EXCUSE phrase "deferred to v2 per D4" must be
        # gone — replaced by the real ADR. (The docstring MAY mention "per D4"
        # inside an explanatory note about why it was replaced — that's honesty
        # about the fix, not the excuse recurring.) Whitespace is NORMALIZED so
        # a line-wrapped excuse can't slip through (the module docstring once
        # hid "deferred to v2 / per D4" across a newline — a wrap-lucky-green
        # that this ADR exists to kill).
        _normalized = re.sub(r"\s+", " ", src).lower()
        assert "deferred to v2 per d4" not in _normalized, (
            "the baseless 'deferred to v2 per D4' excuse (D4 doesn't exist in "
            "ADR-V6-003) must be removed — replaced by the real ADR-V6-064")

    def test_post_gateway_send_has_no_emit_or_handler(self):
        # Confirm the defer is HONEST: post_gateway_send is genuinely not wired
        # (no emit/register/handler call). If this fails, someone implemented
        # it — either flip ADR-V6-064 to "implemented" or add the real
        # implementation ADR. Do NOT silently leave both the wiring and the
        # "deferred" ADR (that re-creates the fake-green this ADR fixed).
        emit_re = re.compile(
            r'(emit|register|subscribe|add_hook|on)\s*\(?\s*["\']post_gateway_send')
        offenders = []
        for d in ("agent", "gateway", "plugins"):
            for f in (REPO_ROOT / d).rglob("*.py"):
                text = f.read_text(errors="ignore")
                if emit_re.search(text):
                    offenders.append(str(f.relative_to(REPO_ROOT)))
        assert not offenders, (
            f"post_gateway_send emit/register found in {offenders} — ADR-V6-064 "
            f"says it's deferred; update the ADR or remove the wiring")

    def test_adr_v6_003_has_no_d4_section(self):
        # Pin the fact that the original 'per D4' excuse was baseless —
        # ADR-V6-003 has no D4 subsection. If a real D4 is later added, update
        # ADR-V6-064 + ptg_capture accordingly.
        adr003 = _adr_text("ADR-V6-003")
        assert not re.search(r"^#+\s*D4\b", adr003, re.MULTILINE), (
            "ADR-V6-003 now has a D4 section — if real, update ptg_capture to "
            "cite it; ADR-V6-064 assumed D4 didn't exist")

    def test_adr_v6_064_exists_and_states_rationale(self):
        # The real defer ADR exists + states the rationale (pre_gateway_dispatch
        # covers Phase 0 outbound audit; post-send semantic capture needs a
        # gateway completion callback = cross-layer behavior change).
        assert (REPO_ROOT / "docs/adr/V6/ADR-V6-064.md").exists()
        body = _adr_text("ADR-V6-064")
        assert "pre_gateway_dispatch" in body
        assert "defer" in body.lower()

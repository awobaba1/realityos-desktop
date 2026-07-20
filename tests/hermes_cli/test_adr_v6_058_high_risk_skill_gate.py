"""C4 regression: high-risk skill install gate (ADR-V6-058).

The audit (T6, 2026-07-20) found that ``godmode`` — an official optional skill
tagged ``jailbreak / red-teaming / safety-bypass`` — installed behind the benign
"Official Skill" panel with no special warning. ADR-V6-058 adds a high-risk
detection layer: skills carrying those tags (or with those names) get a strong
bilingual risk warning + explicit confirm regardless of source.

These tests pin the DECISION logic (``_high_risk_skill_indicators``) — the
load-bearing new code. The gate wiring (``if _high_risk / elif official /
else``) is a 3-branch dispatch verified by reading; the panel rendering is
cosmetic. Belt-and-suspenders tag collection (meta.tags / nested hermes.tags /
flat tags / name) is covered so the gate fires regardless of where the upstream
parser parked the tags.
"""

from __future__ import annotations

from types import SimpleNamespace

from hermes_cli.skills_hub import (
    _collect_skill_tags,
    _high_risk_skill_indicators,
    _HIGH_RISK_SKILL_NAMES,
    _HIGH_RISK_SKILL_TAGS,
)


def _meta(tags=None, extra=None):
    return SimpleNamespace(tags=tags or [], extra=extra or {})


class TestHighRiskIndicators:
    def test_godmode_matched_by_name(self):
        """godmode is a high-risk official skill — matched by NAME even with no
        tags parsed, so it never slips behind the benign Official panel."""
        ind = _high_risk_skill_indicators(_meta(), {}, "godmode")
        assert "godmode" in ind
        assert ind  # non-empty → gate fires

    def test_obliteratus_matched_by_name(self):
        assert "obliteratus" in _high_risk_skill_indicators(_meta(), {}, "obliteratus")

    def test_jailbreak_tag_matched(self):
        """A skill carrying the jailbreak tag is flagged regardless of name."""
        ind = _high_risk_skill_indicators(
            _meta(tags=["jailbreak", "prompt-engineering"]), {}, "my-redteam-tool")
        assert "jailbreak" in ind

    def test_safety_bypass_and_uncensoring_tags_matched(self):
        for tag in ("safety-bypass", "uncensoring", "red-teaming", "weight-tampering"):
            ind = _high_risk_skill_indicators(_meta(tags=[tag]), {}, "some-skill")
            assert tag in ind, f"{tag} must be flagged high-risk"

    def test_benign_skill_not_flagged(self):
        """A normal official skill (memory, search...) is NOT high-risk."""
        ind = _high_risk_skill_indicators(
            _meta(tags=["memory", "productivity"]), {}, "memory-helper")
        assert ind == set()

    def test_case_insensitive(self):
        """Tags arrive lowercased by the collector; mixed-case input still matches."""
        ind = _high_risk_skill_indicators(_meta(tags=["Jailbreak"]), {}, "X")
        assert "jailbreak" in ind


class TestBeltAndSuspendersCollection:
    """Tags may land in meta.tags, nested metadata.hermes.tags, a flat tags list,
    or be implied by the name. All four sources must feed the high-risk check."""

    def test_nested_hermes_tags_detected(self):
        """The SKILL.md ``metadata.hermes.tags`` block rides in extra_metadata."""
        extra = {"hermes": {"tags": ["jailbreak", "G0DM0D3", "safety-bypass"]}}
        ind = _high_risk_skill_indicators(_meta(), extra, "some-skill")
        assert "jailbreak" in ind
        assert "safety-bypass" in ind

    def test_flat_tags_in_extra_metadata_detected(self):
        extra = {"tags": ["uncensoring"]}
        assert "uncensoring" in _high_risk_skill_indicators(_meta(), extra, "x")

    def test_name_alone_flags_known_high_risk(self):
        """Even if NO tags are parsed at all, the known-name set catches godmode."""
        assert _high_risk_skill_indicators(_meta(), {}, "godmode")

    def test_collect_lowercases_and_dedupes(self):
        tags = _collect_skill_tags(
            _meta(tags=["Jailbreak", "jailbreak"]),
            {"hermes": {"tags": ["Red-Teaming"]}, "tags": ["red-teaming"]},
            "Godmode")
        assert "jailbreak" in tags
        assert "red-teaming" in tags
        assert "godmode" in tags
        # set dedupes the repeated entries
        assert sum(1 for t in tags if t == "jailbreak") == 1


class TestHighRiskSetsAreLocked:
    """Pin the high-risk vocabularies so a refactor doesn't silently shrink them."""

    def test_tag_set_covers_audit_findings(self):
        # Every tag the audit called out must be in the gate.
        for required in ("jailbreak", "red-teaming", "safety-bypass", "uncensoring"):
            assert required in _HIGH_RISK_SKILL_TAGS, f"{required} missing from gate"

    def test_name_set_covers_godmode_and_obliteratus(self):
        assert _HIGH_RISK_SKILL_NAMES == frozenset({"godmode", "obliteratus"})

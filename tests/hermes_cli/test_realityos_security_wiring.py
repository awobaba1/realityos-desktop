"""Regression tests for ADR-V6-025 — wire the net-policy layer (§6.5/§6.6)
into the real call sites + the data-sovereignty dormant guard.

WHY THIS TEST EXISTS (C4 — every defect becomes a test case)
-------------------------------------------------------------
The 10-lens audit (ADR-V6-022, lens 6) found the net-policy enforcement layer
(``plugins/realityos_security/policy.py``) was a real, tested primitive with
ZERO call sites — the §6.6 sole-egress pin was orphaned, so the "主权护城河"
(data-sovereignty moat) was a paper tiger. The external SaaS memory providers
(honcho/hindsight/…) were also still activatable, which would silently
exfiltrate the user data V6 promises to keep local.

ADR-V6-025 wires two sovereignty-enforcing call sites, both fail-open (C7):
  * ``classify_url``/§6.6 audit warn-once in ``providers.get_provider_profile``
    — warns when the LLM base_url is a PUBLIC host NOT on the sole-egress
    allowlist; silent for loopback (local model servers = most sovereign) and
    allowlisted hosts.
  * honcho/hindsight/… dormant guard in ``plugins.memory.load_memory_provider``
    — refuses external SaaS memory providers that would ship data off-device.

NOTE on ``fetch_guard``: it is NOT wired into ``open_credentialed_url``. That
shared chokepoint serves BOTH the LLM egress path AND legitimate local model
servers (ollama @127.0.0.1); applying fetch_guard's SSRF floor there would
block loopback — the most sovereign option — and break local providers. The
tool-fetch surface is already SSRF-guarded by ``url_safety`` directly (the §0.6
inheritance), so fetch_guard's marginal value at that chokepoint is negative.
``fetch_guard`` remains a tested primitive for future per-site audit wiring;
this ADR wires the two call sites that genuinely enforce sovereignty.
"""

from __future__ import annotations

import logging

import pytest

from plugins.memory import (
    _V6_EXTERNAL_MEMORY_BLOCKLIST,
    _v6_should_dormant,
    load_memory_provider,
)


# ── B. honcho dormant guard ──────────────────────────────────────────────────


class TestExternalMemoryDormantGuard:
    @pytest.mark.parametrize("name", sorted(_V6_EXTERNAL_MEMORY_BLOCKLIST))
    def test_predicate_dormant_for_every_external_saas(self, name):
        # The activation predicate must mark every external SaaS backend dormant
        # — activating any would exfiltrate user data off-device.
        assert _v6_should_dormant(name) is True

    def test_predicate_active_for_local_providers(self):
        # Local providers (the ptg brain; holographic local SQLite) are NOT
        # dormant — they keep data on-device, so activation is allowed.
        assert _v6_should_dormant("ptg") is False
        assert _v6_should_dormant("holographic") is False

    def test_escape_hatch_flips_predicate(self, monkeypatch):
        # A user who explicitly assumes the sovereignty risk can opt out.
        monkeypatch.delenv("HERMES_REALITYOS_ALLOW_EXTERNAL_MEMORY", raising=False)
        assert _v6_should_dormant("honcho") is True
        monkeypatch.setenv("HERMES_REALITYOS_ALLOW_EXTERNAL_MEMORY", "1")
        assert _v6_should_dormant("honcho") is False

    def test_discovery_unblocked_for_local_holographic(self):
        # The guard is at ACTIVATION (agent_init), NOT discovery. The local
        # holographic provider must still load via the discovery path — this is
        # the regression that broke tests/agent/test_memory_provider.py when the
        # guard was wrongly placed inside load_memory_provider.
        prov = load_memory_provider("holographic")
        assert prov is not None
        assert prov.name == "holographic"

    def test_discovery_does_not_log_sovereignty_warning(self, caplog):
        # Discovery (load_memory_provider) must NOT apply the sovereignty guard —
        # the warning lives at activation (agent_init), so config/listing/backup
        # of providers is silent here. Loading any provider via discovery logs
        # no sovereignty warning.
        with caplog.at_level(logging.WARNING, logger="plugins.memory"):
            load_memory_provider("holographic")
        assert not any("sovereignty" in r.message.lower() for r in caplog.records)


# ── A. §6.6 LLM egress audit (warn-once, public-non-allowlist only) ──────────


class TestLlmEgressAudit:
    def test_warns_once_for_public_non_allowlisted_host(self, monkeypatch, caplog):
        import providers as providers_mod
        import plugins.realityos_security.policy as policy

        monkeypatch.setattr(providers_mod, "_llm_egress_warned_hosts", set())
        # Deterministic classification — don't depend on DNS in the test env.
        # The host is a PUBLIC host NOT on the allowlist → CAT_TOOL_FETCH → warn.
        monkeypatch.setattr(policy, "classify_url", lambda _u: policy.CAT_TOOL_FETCH)

        class _FakeProfile:
            name = "shadow-llm"
            base_url = "https://api.shadow-llm.example.com/v1"

        with caplog.at_level(logging.WARNING, logger="providers"):
            providers_mod._realityos_assert_llm_egress(_FakeProfile())
            providers_mod._realityos_assert_llm_egress(_FakeProfile())  # silent 2nd

        warnings = [r for r in caplog.records if "sovereignty" in r.message.lower()]
        assert len(warnings) == 1, "must warn exactly once per host"
        assert "shadow-llm.example.com" in warnings[0].message

    def test_silent_for_allowlisted_host(self, monkeypatch, caplog):
        import providers as providers_mod

        monkeypatch.setattr(providers_mod, "_llm_egress_warned_hosts", set())

        class _FakeProfile:
            name = "deepseek"
            base_url = "https://api.deepseek.com/v1"

        with caplog.at_level(logging.WARNING, logger="providers"):
            providers_mod._realityos_assert_llm_egress(_FakeProfile())
        assert not any("sovereignty" in r.message.lower() for r in caplog.records)

    def test_silent_for_loopback_local_provider(self, monkeypatch, caplog):
        # A LOCAL model server (ollama/lm-studio @ loopback) is the MOST
        # sovereign option — data never leaves the device — so the §6.6 audit
        # must NOT warn for it. (Wiring fetch_guard's SSRF floor here would
        # wrongly block/warn the most-private provider.)
        import providers as providers_mod

        monkeypatch.setattr(providers_mod, "_llm_egress_warned_hosts", set())

        class _FakeProfile:
            name = "ollama"
            base_url = "http://127.0.0.1:11434/v1"

        with caplog.at_level(logging.WARNING, logger="providers"):
            providers_mod._realityos_assert_llm_egress(_FakeProfile())
        assert not any("sovereignty" in r.message.lower() for r in caplog.records)

    def test_silent_for_empty_base_url(self, monkeypatch, caplog):
        import providers as providers_mod

        monkeypatch.setattr(providers_mod, "_llm_egress_warned_hosts", set())

        class _FakeProfile:
            name = "env-only"
            base_url = ""

        with caplog.at_level(logging.WARNING, logger="providers"):
            providers_mod._realityos_assert_llm_egress(_FakeProfile())
        assert not caplog.records

    def test_fail_open_when_policy_missing(self, monkeypatch, caplog):
        import providers as providers_mod
        import sys

        monkeypatch.setattr(providers_mod, "_llm_egress_warned_hosts", set())

        class _FakeProfile:
            name = "x"
            base_url = "https://api.x.com/v1"

        # Force the policy import to fail; the audit must swallow it silently.
        monkeypatch.setitem(sys.modules, "plugins.realityos_security.policy", None)
        with caplog.at_level(logging.WARNING, logger="providers"):
            providers_mod._realityos_assert_llm_egress(_FakeProfile())  # must not raise
        assert not any("sovereignty" in r.message.lower() for r in caplog.records)

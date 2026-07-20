"""C4 regression: sovereignty ACTIVATION guard end-to-end (ADR-V6-062).

The ADR-V6-053 P2-T3 audit found ADR-V6-025's existing tests pinned only the
``_v6_should_dormant`` PREDICATE (a private function) — the ACTIVATION
chokepoint (``agent_init`` refusing ``add_provider`` + WARNING + falling back
to local) had ZERO end-to-end coverage. Someone flipping
``if _v6_should_dormant(...)`` to ``if False`` would leave CI green while
silently exfiltrating user conversation data off-device — a "test-coverage
illusion" green.

ADR-V6-062 extracts the activation logic into a tested pure helper
``_activate_memory_provider_guarded`` (behavior-equivalent to the prior inline
block) so the contract — *a dormant name never reaches add_provider* — is
unit-pinned end-to-end. Plus an empirical-invariant test pinning that every
blocklist entry genuinely ships data to a third-party cloud host (ADR-V6-025
grounding: the blocklist is evidence-driven, not name-driven).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

from plugins.memory import (
    _V6_EXTERNAL_MEMORY_BLOCKLIST,
    _activate_memory_provider_guarded,
)


class _FakeProvider:
    def __init__(self, name: str, available: bool = True):
        self.name = name
        self._available = available

    def is_available(self) -> bool:
        return self._available


class _FakeManager:
    def __init__(self):
        self.added = []

    def add_provider(self, mp):
        self.added.append(mp)


def _loader(providers: dict):
    """Build a load_fn returning the provider for a name (or None)."""
    def load(name):
        return providers.get(name)
    return load


# ── Activation contract (the C4 gap ADR-V6-062 closes) ───────────────────────


class TestActivateMemoryProviderGuarded:
    """The activation contract: a dormant name NEVER reaches add_provider."""

    def test_activates_local_provider(self):
        # A local provider (holographic — local SQLite, NOT dormant) is loaded,
        # available, and registered.
        mgr = _FakeManager()
        prov = _FakeProvider("holographic")
        load = _loader({"holographic": prov})
        assert _activate_memory_provider_guarded(mgr, "holographic", load) is True
        assert mgr.added == [prov]

    @pytest.mark.parametrize("name", sorted(_V6_EXTERNAL_MEMORY_BLOCKLIST))
    def test_refuses_every_external_saas(self, name, caplog):
        # THE C4 BEARING TEST: every blocklisted external SaaS backend must be
        # REFUSED at activation — add_provider never called — + warned (C7).
        # If this fails for any name, sovereignty is breached: user conversation
        # data would ship off-device.
        mgr = _FakeManager()
        prov = _FakeProvider(name)
        load = _loader({name: prov})
        with caplog.at_level(logging.WARNING, logger="plugins.memory"):
            result = _activate_memory_provider_guarded(mgr, name, load)
        assert result is False, f"{name} must NOT be activated (sovereignty)"
        assert mgr.added == [], f"{name} reached add_provider — sovereignty breach"
        assert any(
            "sovereignty" in r.message.lower() for r in caplog.records
        ), f"{name} refusal must be observable (C7 — never silent)"

    def test_escape_hatch_allows_external_activation(self, monkeypatch):
        # A user who explicitly assumes the sovereignty risk can opt out.
        monkeypatch.setenv("HERMES_REALITYOS_ALLOW_EXTERNAL_MEMORY", "1")
        mgr = _FakeManager()
        prov = _FakeProvider("honcho")
        load = _loader({"honcho": prov})
        assert _activate_memory_provider_guarded(mgr, "honcho", load) is True
        assert mgr.added == [prov]

    def test_unavailable_provider_not_activated(self):
        # A provider that loads but reports unavailable is skipped.
        mgr = _FakeManager()
        prov = _FakeProvider("holographic", available=False)
        load = _loader({"holographic": prov})
        assert _activate_memory_provider_guarded(mgr, "holographic", load) is False
        assert mgr.added == []

    def test_not_found_returns_false(self):
        # load_fn returning None (unknown provider) → no activation.
        mgr = _FakeManager()
        load = _loader({})
        assert _activate_memory_provider_guarded(mgr, "nonexistent", load) is False
        assert mgr.added == []

    def test_dormant_warning_on_plugins_memory_logger(self, caplog):
        # D3 (ADR-V6-062): the sovereignty warning now lives on the
        # plugins.memory logger (co-located with the guard), not agent_init's.
        mgr = _FakeManager()
        load = _loader({"honcho": _FakeProvider("honcho")})
        with caplog.at_level(logging.WARNING, logger="plugins.memory"):
            _activate_memory_provider_guarded(mgr, "honcho", load)
        rec = next(r for r in caplog.records if "sovereignty" in r.message.lower())
        assert rec.name == "plugins.memory"
        assert "honcho" in rec.message


# ── Empirical invariant: blocklist is evidence-driven (ADR-V6-025) ───────────


class TestBlocklistEmpiricalInvariant:
    """The blocklist is evidence-driven: each entry genuinely ships data to a
    third-party cloud host (ADR-V6-025). Pins the invariant so a future local
    provider can't be silently mis-added, nor a cloud provider dropped — the
    exact "name-driven overreach" bug ADR-V6-025 fixed for holographic."""

    @pytest.fixture
    def plugins_root(self):
        import plugins.memory
        return Path(plugins.memory.__file__).parent

    # A third-party cloud egress signal: a non-loopback https/http URL, OR a
    # cloud SDK client (MemoryClient), OR an API-key env var naming a cloud host.
    _CLOUD_URL = re.compile(r"https?://(?!127\.0\.0\.1|localhost)([a-z0-9.-]+)")
    _CLOUD_SDK = re.compile(r"MemoryClient")
    _CLOUD_API_KEY = re.compile(r"[A-Z][A-Z0-9_]*_API_KEY")

    def _source_text(self, plugins_root, name):
        d = plugins_root / name
        assert d.exists(), f"blocklist entry '{name}' has no plugin dir"
        return "\n".join(p.read_text(errors="ignore") for p in d.rglob("*.py"))

    @pytest.mark.parametrize("name", sorted(_V6_EXTERNAL_MEMORY_BLOCKLIST))
    def test_every_blocklist_entry_has_cloud_egress_signal(self, plugins_root, name):
        # Each blocklisted provider must genuinely egress to a third-party
        # cloud — otherwise it's name-driven overreach (the holographic
        # regression ADR-V6-025 fixed).
        text = self._source_text(plugins_root, name)
        has_url = bool(self._CLOUD_URL.search(text))
        has_sdk = bool(self._CLOUD_SDK.search(text))
        has_key = bool(self._CLOUD_API_KEY.search(text))
        assert has_url or has_sdk or has_key, (
            f"'{name}' is blocklisted but its source shows NO third-party "
            f"cloud egress signal — blocklist must be empirical (ADR-V6-025)"
        )

    def test_local_holographic_has_no_cloud_egress(self, plugins_root):
        # The canonical local provider (holographic — local SQLite) must NOT
        # egress to a third-party cloud; that's why it's correctly NOT on the
        # blocklist. If this fails, holographic grew a cloud call (re-check the
        # blocklist premise) or was mis-categorized.
        text = self._source_text(plugins_root, "holographic")
        assert not self._CLOUD_URL.search(text), (
            "holographic is the canonical LOCAL provider (local SQLite) but "
            "its source contains a third-party cloud URL — either it changed "
            "or the blocklist premise (ADR-V6-025) needs rechecking"
        )
        assert not self._CLOUD_SDK.search(text), (
            "holographic source references a cloud SDK (MemoryClient) — "
            "expected a purely local SQLite store"
        )

"""RealityOS V6 net-policy enforcement tests (§6.5 + §6.6, ADR-V6-014).

Locks the policy primitive that the §6.5 ``realityos_security`` layer adds ON
TOP of Hermes' inherited ``url_safety`` SSRF defense:

  * The §6.6 LLM-egress allowlist — exact-host match (suffix attack rejected).
  * ``classify_url`` categories + the reuse of ``url_safety.is_safe_url`` for the
    SSRF/private/metadata floor (deterministic via literal IPs — no DNS needed).
  * ``fetch_guard`` toggle semantics (allow_llm / allow_tool_fetch) + the
    non-negotiable blocked_internal floor (never bypassed by toggles).
  * Config-supplied allowlist additions.
  * Fail-CLOSED if the url_safety primitive is ever unavailable.

These tests do NOT cover the (separate, already-tested) ``url_safety`` IP
classification — only the V6 layer that consumes it. ``is_safe_url`` is patched
to a fixed boolean for the public-hostname cases so they are deterministic
offline (no real DNS).
"""

from __future__ import annotations

import pytest

from plugins.realityos_security import (
    LLM_PROVIDER_ALLOWLIST, CAT_BLOCKED_INTERNAL, CAT_LLM_PROVIDER,
    CAT_TOOL_FETCH, CAT_UNKNOWN, assert_llm_egress, classify_url, fetch_guard,
)
from plugins.realityos_security import policy


def _force_safe(monkeypatch, value: bool) -> None:
    """Force tools.url_safety.is_safe_url to a fixed verdict (imported lazily by
    classify_url at call time, so the patch takes effect)."""
    import tools.url_safety
    monkeypatch.setattr(tools.url_safety, "is_safe_url", lambda _url: value)


# ---------------------------------------------------------------------------
# classify_url: §6.6 allowlist (pure string match — deterministic, no DNS)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "https://api.deepseek.com/v1/chat/completions",
    "https://open.bigmodel.cn/api/paas/v4/chat/completions",
    "https://api.openai.com/v1/chat/completions",
])
def test_classify_llm_provider_allowlist(url):
    assert classify_url(url) == CAT_LLM_PROVIDER


def test_classify_llm_provider_is_case_insensitive():
    assert classify_url("HTTPS://API.DeepSeek.COM/v1") == CAT_LLM_PROVIDER


def test_classify_rejects_suffix_attack(monkeypatch):
    """Exact-host match: ``api.deepseek.com.evil.com`` must NOT be classified as
    an LLM provider (a spoofed host must not slip through the §6.6 pin).
    Forced-safe so the spoofed host is treated as a clean public host — the
    point is it lands in tool_fetch, NEVER llm_provider."""
    _force_safe(monkeypatch, True)
    assert classify_url("https://api.deepseek.com.evil.com/v1") == CAT_TOOL_FETCH


def test_classify_unknown_when_no_host():
    assert classify_url("not a url") == CAT_UNKNOWN
    assert classify_url("") == CAT_UNKNOWN


# ---------------------------------------------------------------------------
# classify_url: SSRF / private / metadata floor — literal IPs (no DNS, deterministic)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data/",   # AWS/GCP metadata
    "http://169.254.170.2/",                       # AWS ECS task metadata
    "http://100.100.100.200/",                     # Alibaba Cloud metadata
    "http://127.0.0.1:9119/",                      # loopback
    "http://10.0.0.5/",                            # private RFC1918
    "http://192.168.1.1/",                         # private RFC1918
    "http://[fd00:ec2::254]/",                     # AWS metadata IPv6
])
def test_classify_blocked_internal_literal_ips(url):
    """These never reach DNS (literal IPs); url_safety rejects them via
    ipaddress classification alone — so the floor is deterministic offline."""
    assert classify_url(url) == CAT_BLOCKED_INTERNAL


def test_classify_public_tool_fetch(monkeypatch):
    """A public, non-allowlist host is a tool_fetch (agent doing work). Forced
    safe so this is deterministic without real DNS."""
    _force_safe(monkeypatch, True)
    assert classify_url("https://example.com/some/page") == CAT_TOOL_FETCH


# ---------------------------------------------------------------------------
# fetch_guard: toggle semantics + non-negotiable floor
# ---------------------------------------------------------------------------

def test_fetch_guard_allows_llm_by_default():
    allowed, cat, reason = fetch_guard("https://api.deepseek.com/v1")
    assert allowed is True and cat == CAT_LLM_PROVIDER
    assert "allowlist" in reason


def test_fetch_guard_blocks_llm_when_disallowed():
    """A caller that locks its surface to non-LLM (e.g. a pure-data path) can
    reject even allowlisted LLM hosts."""
    allowed, cat, _ = fetch_guard("https://api.deepseek.com/v1", allow_llm=False)
    assert allowed is False and cat == CAT_LLM_PROVIDER


def test_fetch_guard_blocks_tool_fetch_when_disallowed(monkeypatch):
    _force_safe(monkeypatch, True)
    allowed, cat, _ = fetch_guard("https://example.com/", allow_tool_fetch=False)
    assert allowed is False and cat == CAT_TOOL_FETCH


def test_fetch_guard_blocked_internal_never_bypassed():
    """The SSRF/metadata floor is non-negotiable: no toggle combination opens it."""
    for kw in [{}, {"allow_llm": False}, {"allow_tool_fetch": False},
               {"allow_llm": False, "allow_tool_fetch": False}]:
        allowed, cat, _ = fetch_guard("http://169.254.169.254/", **kw)
        assert allowed is False, f"metadata leaked through with {kw}"
        assert cat == CAT_BLOCKED_INTERNAL


def test_fetch_guard_never_raises():
    """C7: policy is an observer — malformed input returns a verdict, never raises."""
    allowed, cat, _ = fetch_guard("")
    # Empty → unknown → fail-open (caller validates). The point: no exception.
    assert cat == CAT_UNKNOWN


# ---------------------------------------------------------------------------
# assert_llm_egress: §6.6 provider base_url guard
# ---------------------------------------------------------------------------

def test_assert_llm_egress_on_allowlist():
    ok, host = assert_llm_egress("https://open.bigmodel.cn/api/paas/v4")
    assert ok is True and host == "open.bigmodel.cn"


def test_assert_llm_egress_off_allowlist():
    ok, host = assert_llm_egress("https://my-private-llm.corp.local/v1")
    assert ok is False
    assert host == "my-private-llm.corp.local"


# ---------------------------------------------------------------------------
# Config extensibility — a user-configured provider host is honored
# ---------------------------------------------------------------------------

def test_allowlist_extensible_via_config(monkeypatch):
    """realityos_security.llm_provider_allowlist in config adds a host."""
    monkeypatch.setattr(policy, "_extended_allowlist",
                        lambda: LLM_PROVIDER_ALLOWLIST | {"llm.myorg.cn"})
    assert classify_url("https://llm.myorg.cn/v1") == CAT_LLM_PROVIDER


# ---------------------------------------------------------------------------
# Fail-closed if the url_safety primitive is unavailable
# ---------------------------------------------------------------------------

def test_fail_closed_when_url_safety_missing(monkeypatch):
    """If url_safety can't be called, a non-allowlist host is treated as blocked
    (never silently open the egress)."""
    import tools.url_safety
    monkeypatch.setattr(tools.url_safety, "is_safe_url",
                        lambda _url: (_ for _ in ()).throw(
                            ImportError("url_safety primitive gone")))
    assert classify_url("https://example.com/") == CAT_BLOCKED_INTERNAL

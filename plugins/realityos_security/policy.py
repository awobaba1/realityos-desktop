"""RealityOS V6 — net-policy enforcement layer (架构 §6.5 + §6.6).

WHY THIS MODULE EXISTS
----------------------
Hermes already ships a mature SSRF defense (``tools/url_safety.is_safe_url`` —
private/loopback/link-local/reserved/multicast/CGNAT blocking, cloud-metadata
floor, redirect re-validation, DNS fail-closed). That is the §0.6 "fork 即得"
inheritance — we do NOT duplicate it. What V6 adds (§6.5 "新增 realityos_security
强制中间层") is a **policy layer on top of that primitive**:

  1. The §6.6 **sole-egress pin**: the ONLY data-processing outbound channel is
     the LLM provider API. User text (PIPL L3) may leave the device only to a
     host on the ``LLM_PROVIDER_ALLOWLIST``. A fetch to any OTHER public host is
     a tool fetch (web_extract etc.), which is fine for the agent doing work but
     must NOT be conflated with the LLM egress that carries user content.
  2. A single ``fetch_guard`` entry point the V6 capture/provider paths route
     every outbound URL through, classifying it as llm_provider / tool_fetch /
     blocked_internal so each category can be toggled independently.

PHASE 0 SCOPE (honest)
----------------------
The **policy primitive is real and tested**. What is NOT yet done (documented,
not fake-green): wiring ``fetch_guard`` into every hermes network call site
(web_tools, vision_tools, browser, the LLM provider base_url). That integration
is the next step; this module is the tested chokepoint those sites will call.

C7: ``fetch_guard`` never raises — it returns a verdict (allowed + reason) so a
policy check can never crash the agent loop. Callers decide allow vs block.
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# ── §6.6 sole data-processing egress allowlist ──────────────────────────────
# These are the ONLY hosts that may receive user text (PIPL L3) via the LLM API.
# ADR-093: Zhipu GLM primary, DeepSeek backup. Plus the OpenAI-compatible hosts
# hermes supports (so a user-configured provider still works). Extensible via
# config: ``realityos_security.llm_provider_allowlist`` (a list appended to this).
#
# Hostnames are matched case-insensitively, exact (NOT suffix) — ``api.evil.com``
# must not match a ``com`` entry. Subdomain matching is explicit per entry.
LLM_PROVIDER_ALLOWLIST = frozenset({
    "api.deepseek.com",          # DeepSeek (ADR-093 backup)
    "open.bigmodel.cn",          # 智谱 GLM (ADR-093 primary)
    "api.bigmodel.cn",           # 智谱 alternate
    "api.z.ai",                  # 智谱 newer domain
    "api.openai.com",            # OpenAI-compatible providers
    "api.anthropic.com",
    "api.x.ai",
})

# Category tags returned by classify_url / fetch_guard.
CAT_LLM_PROVIDER = "llm_provider"
CAT_TOOL_FETCH = "tool_fetch"
CAT_BLOCKED_INTERNAL = "blocked_internal"   # url_safety said NO (SSRF/private/meta)
CAT_UNKNOWN = "unknown"


def _hostname(url: str) -> str:
    """Lowercased bare hostname from a URL, '' if unparseable / no scheme."""
    try:
        return (urlparse(url).hostname or "").strip().lower().rstrip(".")
    except Exception:  # noqa: BLE001 — policy must never raise
        return ""


def _extended_allowlist() -> frozenset[str]:
    """Merge the default allowlist with any config-supplied additions.

    Read-once-per-call is fine (policy checks are not hot-path). Failures to
    read config silently fall back to the hardcoded set — never weaken policy
    by accident, never crash the caller.
    """
    base = LLM_PROVIDER_ALLOWLIST
    try:
        from hermes_cli.config import cfg_get, read_raw_config
        extra = cfg_get(read_raw_config() or {},
                        "realityos_security", "llm_provider_allowlist",
                        default=None)
        if extra:
            return base | {str(h).strip().lower().rstrip(".")
                           for h in extra if str(h).strip()}
    except Exception:  # noqa: BLE001
        pass
    return base


def classify_url(url: str) -> str:
    """Classify an outbound URL into a policy category (§6.5).

    * ``llm_provider`` — host on the §6.6 allowlist (may carry user text)
    * ``blocked_internal`` — url_safety blocks it (SSRF / private / metadata)
    * ``tool_fetch`` — public, non-allowlist host (agent doing tool work)
    * ``unknown`` — unparseable / no hostname

    Reuses ``url_safety.is_safe_url`` for the SSRF floor (single source of
    truth for IP-class blocking). Never raises.
    """
    host = _hostname(url)
    if not host:
        return CAT_UNKNOWN
    if host in _extended_allowlist():
        return CAT_LLM_PROVIDER
    try:
        from tools.url_safety import is_safe_url
        if not is_safe_url(url):
            return CAT_BLOCKED_INTERNAL
    except Exception as exc:  # noqa: BLE001 — if the hermes primitive is gone,
        # fail CLOSED for an unclassifiable URL (treat as blocked) — never let a
        # missing primitive silently open the egress.
        logger.warning("realityos_security: url_safety unavailable (%s); "
                       "treating %s as blocked", exc, host)
        return CAT_BLOCKED_INTERNAL
    return CAT_TOOL_FETCH


def fetch_guard(
    url: str,
    *,
    allow_llm: bool = True,
    allow_tool_fetch: bool = True,
) -> Tuple[bool, str, str]:
    """The §6.5 single chokepoint. Returns ``(allowed, category, reason)``.

    Every V6 outbound URL routes through here. The two booleans let a caller
    lock down a surface independently: e.g. the LLM provider path calls with
    ``allow_tool_fetch=False`` so ONLY allowlisted LLM hosts pass; a pure-data
    extraction path might call with ``allow_tool_fetch=False`` too. Internal
    (SSRF/private/metadata) is ALWAYS blocked regardless of toggles.

    Never raises (C7). Default is fail-OPEN for unknown categories (a parse
    failure shouldn't brick the agent) but fail-CLOSED for blocked_internal.
    """
    category = classify_url(url)
    if category == CAT_BLOCKED_INTERNAL:
        return (False, category,
                "blocked by url_safety SSRF/private/metadata floor")
    if category == CAT_LLM_PROVIDER:
        if not allow_llm:
            return (False, category, "llm_provider egress disallowed by caller")
        return (True, category, "llm_provider on §6.6 allowlist")
    if category == CAT_TOOL_FETCH:
        if not allow_tool_fetch:
            return (False, category, "tool_fetch egress disallowed by caller")
        return (True, category, "public tool fetch (non-LLM)")
    # CAT_UNKNOWN — unparseable. Fail open with a warning (caller decides).
    logger.warning("realityos_security: unclassifiable URL %r — allowing "
                   "fail-open (caller should validate)", url)
    return (True, category, "unparseable URL; fail-open")


def assert_llm_egress(base_url: str) -> Tuple[bool, str]:
    """§6.6 guard for the LLM provider base_url specifically.

    Returns ``(on_allowlist, hostname)``. Use when configuring/resolving the LLM
    provider to confirm user text will only egress to an allowlisted host. A
    non-allowlisted base_url is a §6.6 sovereignty violation — the caller logs
    a WARNING and decides whether to fail-open (Phase 0: warn + proceed, since
    a user-configured provider may legitimately add a host) or fail-closed.
    """
    host = _hostname(base_url)
    if not host:
        return (False, "")
    return (host in _extended_allowlist(), host)


def register(ctx) -> None:  # pragma: no cover — Phase 0 policy is import-time
    """Plugin entry point. Phase 0: the policy is available as a module; the
    network-call-site wiring is the documented next step (no hooks to register
    yet — fetch_guard is called explicitly by the V6 capture/provider paths)."""
    logger.debug("realityos_security policy registered (Phase 0: primitive live, "
                 "call-site wiring next).")

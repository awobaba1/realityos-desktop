"""RealityOS V6 net-policy enforcement plugin (§6.5 + §6.6).

Exposes ``fetch_guard`` / ``classify_url`` / ``assert_llm_egress`` from
``policy.py`` as the plugin's public surface. Reuses Hermes' ``url_safety`` SSRF
primitive and adds the V6 §6.6 LLM-egress allowlist pin. See ``policy.py`` for
the Phase-0 scope (primitive live + tested; call-site wiring is the next step).
"""

from __future__ import annotations

from .policy import (
    LLM_PROVIDER_ALLOWLIST,
    CAT_BLOCKED_INTERNAL,
    CAT_LLM_PROVIDER,
    CAT_TOOL_FETCH,
    CAT_UNKNOWN,
    assert_llm_egress,
    classify_url,
    fetch_guard,
    register,
)

__all__ = [
    "LLM_PROVIDER_ALLOWLIST",
    "CAT_BLOCKED_INTERNAL",
    "CAT_LLM_PROVIDER",
    "CAT_TOOL_FETCH",
    "CAT_UNKNOWN",
    "assert_llm_egress",
    "classify_url",
    "fetch_guard",
    "register",
]

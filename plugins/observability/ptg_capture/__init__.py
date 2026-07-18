"""RealityOS V6 PTG capture observer plugin (the OBSERVER half of the PTG).

WHY THIS PLUGIN EXISTS (ADR-V6-008 decision 2)
----------------------------------------------
The memory-plugin loader harvests providers via ``_ProviderCollector``, whose
``register_hook`` is a NO-OP. So the observer hooks that turn tool executions,
outbound messages, and session boundaries into personal-timeline assets
CANNOT live in ``plugins/memory/ptg/``. This plugin registers them against the
real PluginContext and shares the same ``PTGStore`` process-wide singleton as
the memory provider (both resolve to ``<HERMES_HOME>/ptg.db`` → one connection
via the shared-connection registry).

PHASE 0 SCOPE (ADR-V6-008 decision 5)
-------------------------------------
These hooks are AUDIT-LOG only. Their semantic DB sinks arrive with the
extraction phase (behind the C5 schema-validation gate):
  * post_tool_call        → tool-execution events (the 操作电脑 capture surface)
  * pre_gateway_dispatch  → outbound-message capture (deferred to v2 per D4)
  * on_session_end        → session batch extraction

Logging now proves the wiring end-to-end and fixes the exact hook contracts
so the extraction phase only adds DB writes. C7: every callback is wrapped so
a capture failure can NEVER break the agent loop — hooks are observers; they
return None (allow) and never raise.
"""

from __future__ import annotations

import logging
from typing import Optional

from plugins.memory.ptg.store import PTGStore, load_ptg_config, resolve_db_path

logger = logging.getLogger(__name__)

# Lazily-resolved shared handle. We do NOT open the store at register() time
# (the memory provider may not have initialized yet, and a hook can fire
# before any turn). We resolve on first hook fire and cache. Both this plugin
# and the memory provider resolve db_path via the SAME load_ptg_config() +
# resolve_db_path(), so they open the SAME file → ONE shared connection + lock
# (PTGStore shared-connection registry, ADR-V6-008 decision 3).
_store: Optional[PTGStore] = None
_user_id: Optional[str] = None


def _get_store() -> Optional[PTGStore]:
    """Open (once) the shared PTGStore at the resolved path. None if unopenable."""
    global _store
    if _store is not None:
        return _store
    try:
        _store = PTGStore(db_path=resolve_db_path(load_ptg_config()))
    except Exception as exc:  # noqa: BLE001
        logger.debug("ptg_capture: PTGStore open failed: %s", exc)
        return None
    return _store


def _get_user_id() -> Optional[str]:
    """Resolve the founder user_id from ptg_meta (same tenant as the provider).

    Returns None if the memory provider hasn't bootstrapped the founder yet
    (e.g. a hook fires before the first turn). Phase-0 logs then show user=None,
    which is acceptable for an audit trail.
    """
    global _user_id
    if _user_id:
        return _user_id
    store = _get_store()
    if store is None:
        return None
    try:
        row = store._conn.execute(
            "SELECT value FROM ptg_meta WHERE key='founder_user_id'"
        ).fetchone()
        if row is not None:
            _user_id = row[0]
            return _user_id
    except Exception as exc:  # noqa: BLE001
        logger.debug("ptg_capture: founder_user_id resolve failed: %s", exc)
    return None


def _safe(label: str, fn, *args, **kwargs):
    """C7 — observers never raise. A capture failure is logged and swallowed."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.debug("ptg_capture %s failed: %s", label, exc)
        return None


# ---------------------------------------------------------------------------
# Hooks (all **kwargs; invoked via invoke_hook — each wrapped in its own
# try/except by the dispatcher, but we wrap again defensively).
# ---------------------------------------------------------------------------

def _on_post_tool_call(**kwargs):
    """Every tool call — audit log. The 操作电脑 execution surface is captured
    here once the extraction phase lands (Phase 0 = log only)."""
    tool_name = (kwargs.get("tool_name") or kwargs.get("function_name")
                 or "<unknown>")
    status = kwargs.get("status") or ("error" if kwargs.get("error_type") else "ok")
    dur = kwargs.get("duration_ms")
    uid = _get_user_id()
    logger.info("PTG capture [tool] user=%s tool=%s status=%s dur=%sms",
                uid, tool_name, status, dur)
    return None


def _on_pre_gateway_dispatch(**kwargs):
    """Outbound (gateway) message before auth/dispatch — audit log.

    Phase 0 returns None (allow, no rewrite/skip). post_gateway_send semantic
    capture is deferred to v2 per D4.
    """
    event = kwargs.get("event")
    gateway = kwargs.get("gateway")
    logger.info("PTG capture [outbound] gateway=%s event_type=%s",
                gateway, type(event).__name__ if event is not None else None)
    return None  # allow — never block/rewrite in Phase 0


def _on_session_end(**kwargs):
    """Session boundary — batch extraction hook point (Phase 0 = log only)."""
    messages = kwargs.get("messages") or []
    uid = _get_user_id()
    logger.info("PTG capture [session_end] user=%s turns=%d", uid, len(messages))
    return None


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    """Wire the three Phase-0 capture hooks against the real PluginContext."""
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("pre_gateway_dispatch", _on_pre_gateway_dispatch)
    ctx.register_hook("on_session_end", _on_session_end)

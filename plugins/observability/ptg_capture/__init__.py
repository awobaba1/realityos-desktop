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

PHASE 0/1 SCOPE (ADR-V6-008 decision 5; v5 upgrade for post_tool_call)
---------------------------------------------------------------------
The three observer hooks and their sink status:
  * post_tool_call        → tool_events SINK (v5, §9#4 + §0.6) — every tool the
                            agent runs becomes a personal-timeline asset.
                            Validated through CaptureEvent (C5-adjacent); a
                            malformed payload → DLQ, never silently dropped.
  * pre_gateway_dispatch  → outbound-message audit-log (Phase 0: allow + log,
                            no rewrite). post-send semantic capture deferred
                            per ADR-V6-064 (needs gateway send-completion
                            callback — cross-layer, Phase 1+ scope).
  * on_session_end        → session batch extraction hook point (audit-log only;
                            semantic extraction arrives with the extraction phase).

C7: every callback is wrapped so a capture failure can NEVER break the agent
loop — hooks are observers; they return None (allow) and never raise.
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
    """Every tool call — sunk to ``tool_events`` as a personal-timeline asset
    (v5, §9#4 + §0.6). The 操作电脑 execution surface. The payload is validated
    through ``CaptureEvent`` (C5-adjacent gate); a malformed payload → DLQ,
    never silently dropped (C7). Returns None (observer; never blocks).

    When the store or founder user_id isn't resolvable yet (a hook can fire
    before the first turn bootstraps the founder), the event is log-only — we
    can't sink a NOT NULL user_id, and we never block the loop guessing one.
    """
    tool_name = (kwargs.get("tool_name") or kwargs.get("function_name")
                 or "<unknown>")
    uid = _get_user_id()
    store = _get_store()
    if store is None or not uid:
        logger.debug("PTG capture [tool] no store/user yet; log only tool=%s",
                     tool_name)
        return None
    # C5-adjacent gate: validate the payload shape before sink. A structurally
    # bad dispatch (junk kwargs, a model_tools shape change) → DLQ, not garbage.
    try:
        from plugins.memory.ptg.capture_schemas import CaptureEvent
        event = CaptureEvent.from_hook_kwargs(kwargs)
    except Exception as exc:  # noqa: BLE001 — malformed payload → DLQ (C7)
        logger.warning("PTG capture [tool] invalid payload (tool=%s): %s",
                       tool_name, exc)
        _safe("dlq", store.insert_dlq,
              user_id=uid, source="ptg_capture.post_tool_call",
              error_type="invalid_capture_payload",
              error_msg=str(exc)[:1000],
              # Minimal diagnostic — never re-dump the full (possibly huge/L3)
              # args payload into the DLQ row.
              original_data={"tool_name": tool_name,
                             "args_keys": list(kwargs.get("args", {}).keys())
                             if isinstance(kwargs.get("args"), dict) else []})
        return None
    _safe("tool_event", store.insert_tool_event,
          user_id=uid,
          tool_name=event.tool_name,
          status=event.status,
          tool_args=event.tool_args,
          result_summary=event.result_summary,
          session_id=event.session_id,
          duration_ms=event.duration_ms,
          error_type=event.error_type,
          error_msg=event.error_msg,
          extracted_via=event.extracted_via,
          quark_evidence=event.quark_evidence)
    logger.info("PTG capture [tool] user=%s tool=%s status=%s dur=%sms",
                uid, event.tool_name, event.status, event.duration_ms)
    return None


def _on_pre_gateway_dispatch(**kwargs):
    """Outbound (gateway) message before auth/dispatch — audit log.

    Phase 0 returns None (allow, no rewrite/skip). post_gateway_send semantic
    capture is deferred per ADR-V6-064: pre_gateway_dispatch already audits
    outbound in Phase 0, and post-send semantic capture needs a gateway
    send-completion callback (cross-layer behavior change, out of Phase 0
    scope). Replaces the prior baseless "per D4" reference — ADR-V6-003 has no
    D4 section (T5 audit finding).
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

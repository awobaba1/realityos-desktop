"""RealityOS V6 PTG memory plugin — registers ``PTGProvider`` as the active
MemoryProvider (the recall + turn-capture surface).

WHY A SEPARATE CAPTURE PLUGIN EXISTS (ADR-V6-008 decision 2)
------------------------------------------------------------
The memory-plugin loader harvests providers via ``_ProviderCollector``, whose
``register_hook`` is a NO-OP (plugins/memory/__init__.py). So the observer
hooks — ``post_tool_call`` / ``pre_gateway_dispatch`` / ``on_session_end`` —
CANNOT be registered here; they live in the separate
``plugins/observability/ptg_capture/`` plugin, which registers against the
real PluginContext. Both plugins share the same ``PTGStore`` process-wide
singleton via the shared-connection registry (same ``<herMES_HOME>/ptg.db``).

Activate: set ``memory.provider: ptg`` in ``$HERMES_HOME/config.yaml``.
Config section: ``plugins.ptg``.
"""

from __future__ import annotations

from .provider import PTG_SEARCH_SCHEMA, PTGProvider
from .store import PTGStore, load_ptg_config

__all__ = ["PTGProvider", "PTGStore", "PTG_SEARCH_SCHEMA", "load_ptg_config"]


def register(ctx) -> None:
    """Register the PTG memory provider with the plugin system."""
    config = load_ptg_config()
    provider = PTGProvider(config=config)
    ctx.register_memory_provider(provider)

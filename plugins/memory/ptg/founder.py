"""Founder resolution — the single 3-layer source for "whose data is this?".

Desktop V6 is a single-founder product, but the founder user_id still has to be
resolved at runtime: it may be set explicitly in the ptg config, OR persisted in
``ptg_meta`` by the memory provider on the first turn. Every founder-scoped read
(the insight API, the insight scheduler, and now the calibration CLI) must agree
on the SAME resolution or they silently read different tenants' atoms.

ADR-V6-028 lifts this logic out of ``hermes_cli.web_server._resolve_insight_founder``
into a shared module so the calibration CLI does not duplicate the 3-layer
priority (and so the next founder-scoped surface does not reinvent it).

Priority:
  1. explicit ptg config ``founder_user_id`` (operator-set, highest authority)
  2. persisted ``ptg_meta.founder_user_id`` value (written by PTGProvider on the
     first turn)

Returns ``""`` when no founder is established yet (first-launch race); callers
gate on emptiness rather than guessing.
"""

from __future__ import annotations

from typing import Optional


def resolve_founder(store, *, config: Optional[dict] = None) -> str:
    """Resolve the founder user_id for this desktop instance (3-layer).

    Parameters
    ----------
    store:
        A :class:`PTGStore` (provides ``founder_user_id`` → the ``ptg_meta`` read).
    config:
        An already-loaded ptg config dict, to avoid a re-read when the caller
        already has it. When ``None`` the live config is loaded lazily.

    Never raises — a resolution failure is logged inside
    ``store.founder_user_id`` and surfaces as ``""`` (first-launch race).
    """
    if config is None:
        from plugins.memory.ptg.store import load_ptg_config

        config = load_ptg_config() or {}
    cfg_id = config.get("founder_user_id")
    if cfg_id:
        return str(cfg_id)
    uid = store.founder_user_id()
    return uid or ""

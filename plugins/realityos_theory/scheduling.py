"""RealityOS V6 — startup-lazy scheduling for theory derivation (ADR-V6-051 B3).

The Theory engine (``TheoryEngineImpl`` / ``derive_and_persist``) is the *what*;
this module is the *when* — at startup, decide whether today's PC/FR derivation
is missing a cached row, and if so, derive + persist it. This mirrors the
insights startup-lazy philosophy (ADR-V6-019): the desktop brain may not be open
at the ideal wall-clock time, so we catch up on launch instead of relying on a
cron the machine might miss.

**Layering (the iron rule "lower cannot call upper"):** this module lives in the
THEORY layer and depends only on the memory layer (PTGStore). It never calls
upward, and the memory layer never calls it. The trigger thread is spawned from
this plugin's ``register()`` (once per process, opt-out) — theory owns its
scheduling; memory owns the store. Single-direction data flow (架构 §4.7):
``derive`` reads atoms/relations and writes only insight_aggregation.

**Idempotent + fail-open (C7):** a missing/stale-prompt row ⇒ one
``derive_and_persist()`` (itself fail-open, gated, C5/C6-logged); an existing
current-prompt row ⇒ skip (don't re-spend an LLM call until the period rolls
over). Never raises.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional

from plugins.memory.ptg.store import PTGStore, load_ptg_config, resolve_db_path

from .engine import PROMPT_VERSION

logger = logging.getLogger(__name__)

# Process-global once-guard: register() can be called from several entry points
# (cli / gateway / oneshot) within one process; only the first spawns the
# scheduler. Subsequent calls are no-ops.
_scheduler_started = False
_scheduler_guard = threading.Lock()

# The canonical PC dim used as the probe key — if the "Time" constraint_state
# row for today exists under the CURRENT prompt version, the whole 12-row batch
# is assumed present (derive_and_persist writes all-or-isolated-per-row, C7).
_PROBE_DIM = "Time"


def run_due_theory(
    store: PTGStore,
    *,
    user_id: str,
    now: Optional[datetime] = None,
    caller: Optional[Callable[..., Any]] = None,
    period_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Derive today's PC/FR if missing/stale. Never raises (C7).

    Gathers the user's atoms (``recent_atoms``) + relations
    (``relations_for_user``); if the canonical probe row
    ``(user, constraint_state, "{period}|Time")`` is absent OR was generated
    under an older prompt version, runs ``derive_and_persist`` (writes all 12
    derivations). Otherwise skips (idempotent until the period rolls over).

    ``caller`` is an optional injectable LLM caller (tests pass a mock;
    production leaves it None → the engine's default ``auxiliary_client``).
    Returns a summary dict.
    """
    pk = period_key or _period_key(now)
    probe = store.get_insight(
        user_id=user_id, aggregation_type="constraint_state",
        period_key=f"{pk}|{_PROBE_DIM}")
    # Skip ONLY if a row exists AND it was generated under the CURRENT prompt
    # version. A row from an older prompt is stale → regenerate (mirrors the
    # insights stale-prompt-refresh gate, ADR-V6-026).
    if probe is not None and probe.get("schema_version") == PROMPT_VERSION:
        return {"generated": False, "period_key": pk, "reason": "exists"}
    atoms = store.recent_atoms(user_id=user_id, limit=500)
    if not atoms:
        # Cold-start guard: nothing to derive from. Don't burn an LLM call on
        # an empty graph; next launch with data retries.
        return {"generated": False, "period_key": pk, "reason": "no_atoms"}
    relations = store.relations_for_user(user_id, limit=50)
    try:
        # Lazy import avoids a top-level __init__↔scheduling cycle (register()
        # imports this module; this module reads derive_and_persist from
        # __init__). At call time __init__ is fully loaded.
        from plugins.realityos_theory import derive_and_persist

        result = derive_and_persist(
            store, user_id=user_id, atoms=atoms, relations=relations,
            caller=caller, period_key=pk)
        return {"generated": True, "period_key": pk,
                "derived": result.get("derived", 0),
                "persisted": result.get("persisted", 0),
                "degraded_count": result.get("degraded_count", 0),
                "reason": "stale_prompt" if probe is not None else "missing"}
    except Exception as exc:  # noqa: BLE001 — scheduler never raises (C7)
        logger.warning("theory scheduling failed: %s", exc)
        return {"generated": False, "period_key": pk, "reason": "error",
                "error": str(exc)}


def _period_key(now: Optional[datetime]) -> str:
    """Today's Beijing date as YYYY-MM-DD (the theory period granularity).

    ``beijing_now`` is inlined here (not imported from ``realityos_insights``)
    to keep this module free of any insights dependency — theory owns its
    scheduling and depends only on the memory layer (layering iron rule).
    """
    if now is None:
        now = datetime.now(_BEIJING_TZ)
    return now.strftime("%Y-%m-%d")


# Beijing timezone (UTC+8), inlined to avoid importing realityos_insights.
_BEIJING_TZ = timezone(timedelta(hours=8))


# ── startup-lazy trigger ───────────────────────────────────────────────────


def start_scheduler_if_due(*, enabled: bool = True,
                           founder_wait_seconds: float = 60.0) -> bool:
    """Spawn the startup-lazy theory thread once per process. Returns whether
    this call actually started it.

    Opt-out (``enabled=False``) and the once-guard make this safe to call from
    every ``register()`` invocation. The thread is a daemon: it never blocks
    process exit. Fail-open throughout (C7).
    """
    global _scheduler_started
    if not enabled:
        return False
    with _scheduler_guard:
        if _scheduler_started:
            return False
        _scheduler_started = True
    t = threading.Thread(
        target=_run_startup_lazy,
        kwargs={"founder_wait_seconds": founder_wait_seconds},
        name="realityos-theory-scheduler", daemon=True)
    t.start()
    logger.debug("theory startup-lazy scheduler thread started")
    return True


def _run_startup_lazy(*, founder_wait_seconds: float = 60.0,
                      poll_interval: float = 2.0) -> None:
    """Daemon body: open the shared store, wait for the founder, run due theory,
    close. Bounded founder wait so the thread always exits. Fail-open.
    """
    store: Optional[PTGStore] = None
    try:
        store = PTGStore(db_path=resolve_db_path(load_ptg_config()))
    except Exception as exc:  # noqa: BLE001 — observer surface
        logger.warning("theory scheduler: shared store open failed: %s", exc)
        return
    try:
        user_id = _wait_for_founder(store, founder_wait_seconds, poll_interval)
        if user_id is None:
            logger.debug(
                "theory scheduler: founder not ready after %.0fs; skipping "
                "this run (next launch retries).", founder_wait_seconds)
            return
        run_due_theory(store, user_id=user_id)
    except Exception as exc:  # noqa: BLE001 — never escape the daemon
        logger.warning("theory scheduler run failed: %s", exc)
    finally:
        try:
            store.close()
        except Exception:  # noqa: BLE001
            pass


def _wait_for_founder(store: PTGStore, wait_seconds: float,
                      poll_interval: float) -> Optional[str]:
    """Poll ptg_meta for founder_user_id until present or the budget elapses.

    register() may fire before PTGProvider.ensure_founder() on a first launch;
    the founder row is created moments later. Bounded so the thread always
    exits. On a typical (non-first) launch the founder id is already persisted
    and this returns immediately.
    """
    deadline = time.monotonic() + wait_seconds
    while True:
        uid = store.founder_user_id()
        if uid:
            return uid
        if time.monotonic() >= deadline:
            return store.founder_user_id()
        time.sleep(poll_interval)


def _scheduler_should_start() -> bool:
    """Should register() start the scheduler in THIS process context?

    Default enabled (production desktop brain). Disabled when:
      - under pytest (``PYTEST_CURRENT_TEST``) — tests must never spawn a
        real-LLM scheduler that opens the shared store; and
      - opted out via ``REALITYOS_THEORY_AUTOSCHED=0/false/no/off`` (transient
        CLI contexts / explicit disable).
    """
    if "PYTEST_CURRENT_TEST" in os.environ or "PYTEST_RUN_CONFIG" in os.environ:
        return False
    val = os.environ.get("REALITYOS_THEORY_AUTOSCHED", "").strip().lower()
    return val not in ("0", "false", "no", "off")


# Reset hook for tests that want to exercise start_scheduler_if_due in isolation.
def _reset_for_tests() -> None:  # pragma: no cover — test utility
    global _scheduler_started
    with _scheduler_guard:
        _scheduler_started = False

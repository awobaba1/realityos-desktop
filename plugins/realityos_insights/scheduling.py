"""RealityOS V6 — startup-lazy scheduling for insight reports (ADR-V6-019).

The report services (``WeeklyMirrorService``, ``DailyReportService``) are the
*what*; this module is the *when* — at startup, decide whether each report's
current target period is missing a cached row, and if so, generate it. This
mirrors backup's startup-lazy philosophy (ADR-V6-014): the desktop brain may
not be open at the ideal wall-clock time (Monday 09:00 / each evening), so we
catch up on launch instead of relying on a cron the machine might miss.

**Layering (the iron rule "lower cannot call upper"):** this module lives in
the INSIGHTS layer and depends only on the memory layer (PTGStore). It never
calls upward, and — crucially — the memory layer (PTGProvider) never calls it.
The trigger thread is spawned from this plugin's ``register()`` (once per
process, opt-out), NOT from ``PTGProvider.initialize()``, so the lower memory
layer stays free of any insights dependency. Insights owns its scheduling;
memory owns the store. (Backup is different: it is itself a memory-layer
concern, so it spawns from the provider — same layer, no upward edge.)

**Idempotent + fail-open (C7):** a missing row ⇒ one ``generate()`` (itself
fail-open, gated, C5/C6-logged); an existing row — including a cold-start
placeholder — ⇒ skip (don't re-spend an LLM call until the period rolls over).
Never raises.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, Optional, Tuple, Type

from plugins.memory.ptg.store import PTGStore, load_ptg_config, resolve_db_path

from ._base import InsightReportService, beijing_now
from .daily_report import DailyReportService, _resolve_day
from .weekly_mirror import WeeklyMirrorService, _resolve_week

logger = logging.getLogger(__name__)

# Process-global once-guard: register() can be called from several entry points
# (cli / gateway / oneshot) within one process; only the first spawns the
# scheduler. Subsequent calls are no-ops.
_scheduler_started = False
_scheduler_guard = threading.Lock()

# Each report kind: (result key, service class, period resolver). The resolver
# is the SAME one the service uses in generate() ⇒ the period_key we probe
# matches the period_key generate() would upsert under.
_ReportSpec = Tuple[str, Type[InsightReportService], Callable[[datetime, Optional[str]], Dict[str, str]]]
_SPECS: Tuple[_ReportSpec, ...] = (
    ("daily", DailyReportService, _resolve_day),
    ("weekly", WeeklyMirrorService, _resolve_week),
)


def run_due_reports(
    store: PTGStore,
    *,
    user_id: str,
    now: Optional[datetime] = None,
    caller: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """Generate any missing current-period insight reports. Never raises (C7).

    For each report kind, compute the CURRENT target period_key (the most
    recently COMPLETED period — yesterday for daily, the previous Mon–Sun for
    weekly), and if no cached row exists for ``(user, type, period_key)``,
    generate it. Existing rows (including cold-start placeholders) are skipped.

    ``caller`` is an optional injectable LLM caller (tests pass a mock;
    production leaves it None → the service's default ``auxiliary_client``).
    Returns a per-kind summary dict.
    """
    if now is None:
        now = beijing_now()
    results: Dict[str, Any] = {}
    for kind, svc_cls, resolver in _SPECS:
        results[kind] = _run_one(store, user_id=user_id, now=now,
                                 svc_cls=svc_cls, resolver=resolver, caller=caller)
    return results


def _run_one(store: PTGStore, *, user_id: str, now: datetime,
             svc_cls: Type[InsightReportService],
             resolver: Callable[[datetime, Optional[str]], Dict[str, str]],
             caller: Optional[Callable[..., Any]]) -> Dict[str, Any]:
    """Probe + maybe-generate one report kind. Never raises (C7).

    The service is constructed with ``now_fn=lambda: now`` so its ``generate()``
    resolves the SAME period_key we just probed (both use ``now``) — without
    this, generate() would re-derive the period from real ``beijing_now`` and
    could upsert under a different key than the one we checked.
    """
    try:
        period_key = resolver(now, None)["period_key"]
        agg_type = svc_cls.AGGREGATION_TYPE
        existing = store.get_insight(
            user_id=user_id, aggregation_type=agg_type, period_key=period_key)
        if existing is not None:
            return {"generated": False, "period_key": period_key,
                    "reason": "exists",
                    "data_sufficiency": existing.get("data_sufficiency")}
        svc = svc_cls(store, user_id=user_id, caller=caller, now_fn=lambda: now)
        res = svc.generate(generated_by="scheduled")
        return {"generated": True, "period_key": period_key,
                "status": res.get("status"),
                "data_sufficiency": res.get("data_sufficiency"),
                "llm_call_id": res.get("llm_call_id")}
    except Exception as exc:  # noqa: BLE001 — scheduler never raises (C7)
        logger.warning("insight scheduling failed (%s): %s",
                       getattr(svc_cls, "AGGREGATION_TYPE", "?"), exc)
        return {"generated": False, "reason": "error", "error": str(exc)}


# ── startup-lazy trigger ───────────────────────────────────────────────────


def start_scheduler_if_due(*, enabled: bool = True,
                           founder_wait_seconds: float = 60.0) -> bool:
    """Spawn the startup-lazy report thread once per process. Returns whether
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
        name="realityos-insights-scheduler", daemon=True)
    t.start()
    logger.debug("insights startup-lazy scheduler thread started")
    return True


def _run_startup_lazy(*, founder_wait_seconds: float = 60.0,
                      poll_interval: float = 2.0) -> None:
    """Daemon body: open the shared store, wait for the founder, run due
    reports, close. Bounded founder wait so the thread always exits. Fail-open.
    """
    store: Optional[PTGStore] = None
    try:
        store = PTGStore(db_path=resolve_db_path(load_ptg_config()))
    except Exception as exc:  # noqa: BLE001 — observer surface
        logger.warning("insights scheduler: shared store open failed: %s", exc)
        return
    try:
        user_id = _wait_for_founder(
            store, founder_wait_seconds, poll_interval)
        if user_id is None:
            logger.debug(
                "insights scheduler: founder not ready after %.0fs; skipping "
                "this run (next launch retries).", founder_wait_seconds)
            return
        run_due_reports(store, user_id=user_id)
    except Exception as exc:  # noqa: BLE001 — never escape the daemon
        logger.warning("insights scheduler run failed: %s", exc)
    finally:
        try:
            store.close()
        except Exception:  # noqa: BLE001
            pass


def _read_founder_user_id(store: PTGStore) -> Optional[str]:
    # Thin wrapper over the public store read (ADR-V6-020 centralized it).
    return store.founder_user_id()


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
        uid = _read_founder_user_id(store)
        if uid:
            return uid
        if time.monotonic() >= deadline:
            return _read_founder_user_id(store)
        time.sleep(poll_interval)


def _scheduler_should_start() -> bool:
    """Should register() start the scheduler in THIS process context?

    Default enabled (production desktop brain). Disabled when:
      - under pytest (``PYTEST_CURRENT_TEST``) — tests must never spawn a
        real-LLM scheduler that opens the shared store; and
      - opted out via ``REALITYOS_INSIGHTS_AUTOSCHED=0/false/no/off`` (transient
        CLI contexts / explicit disable).
    """
    if "PYTEST_CURRENT_TEST" in os.environ or "PYTEST_RUN_CONFIG" in os.environ:
        return False
    val = os.environ.get("REALITYOS_INSIGHTS_AUTOSCHED", "").strip().lower()
    return val not in ("0", "false", "no", "off")


# Reset hook for tests that want to exercise start_scheduler_if_due in isolation.
def _reset_for_tests() -> None:  # pragma: no cover — test utility
    global _scheduler_started
    with _scheduler_guard:
        _scheduler_started = False

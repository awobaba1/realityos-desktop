"""``hermes calibrate`` handler (ADR-V6-028 §11.4/§11.5).

The pure calibration logic lives in ``plugins.realityos_calibration``; this module
is the I/O adapter for the CLI — opens the shared PTGStore, resolves the founder,
computes the Beijing "today" window, samples today's atoms, drives the
``input()`` rater, and prints the session summary. Keeping I/O out of the plugin
is what makes the §11.5 contract unit-testable without a tty.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

from plugins.memory.ptg.founder import resolve_founder
from plugins.memory.ptg.store import PTGStore, load_ptg_config, resolve_db_path
from plugins.realityos_calibration import run_calibration
from plugins.realityos_insights._base import _BEIJING_TZ, beijing_now


def _today_window(now: datetime, date_str=None):
    """Beijing-local calendar day → (date_str, since_utc, until_utc) half-open.

    Events store ``timestamp`` in UTC; the query window is the Beijing day
    ``[00:00, next-day 00:00)`` converted to UTC, mirroring the daily-report
    resolver but defaulting to TODAY (the founder rates the live day's atoms),
    not yesterday.
    """
    if date_str:
        day_local = datetime.strptime(date_str, "%Y-%m-%d").replace(
            tzinfo=_BEIJING_TZ, hour=0, minute=0, second=0, microsecond=0)
    else:
        day_local = now.replace(hour=0, minute=0, second=0, microsecond=0)
    next_local = day_local + timedelta(days=1)
    return (
        day_local.strftime("%Y-%m-%d"),
        day_local.astimezone(timezone.utc).isoformat(),
        next_local.astimezone(timezone.utc).isoformat(),
    )


def _make_interactive_rater(stream_out, stream_in):
    """input()-backed rater: print the atom, read a verdict short-form.

    Returns the raw stripped line; ``run_calibration`` normalizes 准/1/对 etc.
    Raises EOFError on closed stdin so the session finalizes cleanly (the loop
    catches it and returns a partial result over the atoms already judged).
    """

    def rater(display, idx, total):
        stream_out.write(f"\n[{idx}/{total}] {display}\n")
        stream_out.write("  评分 (1=准 / 0=不准 / s=惊喜 / 回车=跳过): ")
        stream_out.flush()
        line = stream_in.readline()
        if line == "":
            raise EOFError
        return line.strip()

    return rater


def cmd_calibrate(args) -> int:
    limit = max(1, int(getattr(args, "limit", 50) or 50))
    date_override = getattr(args, "date", None)

    store = PTGStore(db_path=resolve_db_path(load_ptg_config()))
    try:
        founder = resolve_founder(store)
        if not founder:
            print("未找到创始人：先发一条消息（任意一句话）建立数据，再回来校准。")
            return 0

        now = beijing_now()
        day_str, since_utc, until_utc = _today_window(now, date_override)
        atoms = store.recent_atoms(
            user_id=founder, since=since_utc, until=until_utc, limit=limit,
        )
        if not atoms:
            print(f"「{day_str}」还没有抽取到的原子可校准。")
            print("继续和我聊几句，等原子积累后再来 `hermes calibrate`。")
            return 0

        print(f"=== 创始人每日校准 · {day_str}（采样 {len(atoms)} 条）===")
        print("对每条原子评分。不准的会被降权（不会删除）；惊喜的进案例库；"
              "全部记录留作质量回测证据。")
        rater = _make_interactive_rater(sys.stdout, sys.stdin)
        result = run_calibration(
            store=store, user_id=founder, atoms=atoms, rater=rater,
            metric_date=day_str,
        )
        print("\n" + result.summary())
        return 0
    finally:
        try:
            store.close()
        except Exception:  # noqa: BLE001 — CLI teardown (C7)
            pass

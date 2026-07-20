#!/usr/bin/env python3
"""D1/D7 本地留存查询 — 创始人自看回访(ADR-V6-038 D4)。

只读 state.db 的 sessions 表(started_at REAL = Unix timestamp 秒)。

诚实标注(反假绿):
- 这是**单用户本地视角**的「我作为用户 D1/D7 是否回访」,**非**跨用户运营留存。
  V6 是纯本地桌面拓扑,无中心运营库(quality_metrics 是 per-user 本地 SQLite,
  ADR-V6-027)。跨用户运营留存须外部用户上量后才有数据(母纲桶 D,本脚本不伪装)。
- 「安装日」= min(sessions.started_at) 的 date(首次 session ≈ 首次使用 ≈ 安装后
  首启),是**近似**(若装后未立即用,则把首次使用日当安装日)。

D1 = 安装日 +1 天有 session;D7 = 安装日 +7 天有 session。
"""

from __future__ import annotations

import argparse
import datetime
import os
import sqlite3
import sys
from pathlib import Path

State = str  # "revisited" | "missed" | "not_yet"


def get_state_db_path() -> Path:
    home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    return Path(home) / "state.db"


def query_started_ats(db_path: Path) -> list[float]:
    """Read-only probe of sessions.started_at. Returns [] if table empty/absent."""
    if not db_path.exists():
        return []
    uri = f"file:{db_path.as_posix()}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=1.0) as conn:
            conn.execute("PRAGMA query_only = ON")
            # 防御:sessions 表可能未建(全新 state.db),gracefully 降级。
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sessions'"
            ).fetchone()
            if not exists:
                return []
            rows = conn.execute(
                "SELECT started_at FROM sessions WHERE started_at IS NOT NULL"
            ).fetchall()
    except sqlite3.Error:
        return []
    return [float(r[0]) for r in rows if r[0] is not None]


def _day_state(target: datetime.date, active_dates: set[datetime.date], today: datetime.date) -> State:
    if target in active_dates:
        return "revisited"
    if today < target:
        return "not_yet"
    return "missed"


def compute_retention(
    started_ats: list[float], today: datetime.date
) -> dict[str, object]:
    """Pure retention computation. Inject `today` for testability."""
    if not started_ats:
        return {"has_data": False}
    # 本地时区日期(用户的一个自然日)。
    dates = {datetime.datetime.fromtimestamp(ts).date() for ts in started_ats}
    install_date = min(dates)
    last_date = max(dates)
    d1_target = install_date + datetime.timedelta(days=1)
    d7_target = install_date + datetime.timedelta(days=7)
    return {
        "has_data": True,
        "install_date": install_date,
        "last_date": last_date,
        "total_sessions": len(started_ats),
        "active_days": len(dates),
        "days_since_install": (today - install_date).days,
        "d1": _day_state(d1_target, dates, today),
        "d7": _day_state(d7_target, dates, today),
    }


def _label(state: State) -> str:
    return {
        "revisited": "✓ 已回访",
        "missed": "✗ 未回访",
        "not_yet": "· 未到",
    }[state]


def format_report(ret: dict[str, object]) -> str:
    if not ret.get("has_data"):
        return "无 session 数据(尚未使用过),无法计算留存。"
    lines = [
        "📊 本地留存(单用户自看,非运营库)",
        f"  安装日(首次使用):{ret['install_date']}",
        f"  最后使用:{ret['last_date']}",
        f"  距安装:{ret['days_since_install']} 天",
        f"  总 session 数:{ret['total_sessions']}",
        f"  活跃天数:{ret['active_days']}",
        f"  D1(安装+1 天):{_label(ret['d1'])}",
        f"  D7(安装+7 天):{_label(ret['d7'])}",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="D1/D7 本地留存查询(ADR-V6-038 D4):创始人自看回访。",
    )
    parser.add_argument(
        "--today",
        default=None,
        help="覆盖'今天'日期(YYYY-MM-DD,测试/回看用;默认系统今天)",
    )
    args = parser.parse_args(argv)

    today = (
        datetime.date.fromisoformat(args.today)
        if args.today
        else datetime.date.today()
    )

    started_ats = query_started_ats(get_state_db_path())
    ret = compute_retention(started_ats, today)
    print(format_report(ret))
    return 0


if __name__ == "__main__":
    sys.exit(main())

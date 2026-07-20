"""``hermes trail`` handler (ADR-V6-070).

I/O adapter that gives the three write-only-no-consumer tables a READ surface.
The fourth-round audit (ADR-037 维度) found a triplet of tables that all have
real producers, an ADR-added read API, and C4 tests pinning the API — but ZERO
CLI/API/UI consumer (做了没发, ADR-V6-037's most-fatal fake-green class — the same
disease ADR-V6-063 cured for ``citation_counters`` and ADR-V6-065 for
``dlq_messages``):

  * ``deletion_log``   — written by soft_delete / cascade_soft_delete / purge
                         (ADR-V6-045); read API ``list_deletion_log`` had no caller.
                         The R12 sovereignty audit promise ("可追溯 · 跨重启可查")
                         was on-paper-only.
  * ``tool_events``    — written by ptg_capture on every post_tool_call; read API
                         ``recent_tool_events`` had no caller (pure write-only telemetry).
  * ``quality_metrics``— written by calibration (ADR-V6-028); read API
                         ``recent_quality_metrics`` had no caller. The calibration
                         time-series was unobservable — the exact gap ADR-V6-063
                         closed for citation counters.

``hermes trail`` is the single consumer that closes all three loops. Read-only
(架构 §4.7): never writes, never mutates. ``--type`` selects one table for a
detail listing; the default overview reads a small window from each so the
founder can see at a glance which observation surfaces have data. Thin delegate
mirrors ``dlq_cmd`` / ``k_cmd`` / ``citation_cmd`` (store open → read → close).
"""

from __future__ import annotations

import json

from plugins.memory.ptg.founder import resolve_founder
from plugins.memory.ptg.store import PTGStore, load_ptg_config, resolve_db_path

# Overview window per type — small on purpose (the overview is a "is there
# anything here?" glance; --type <X> is the detailed read).
_OVERVIEW_LIMIT = 3


def cmd_trail(args) -> int:
    config = load_ptg_config()
    store = PTGStore(db_path=resolve_db_path(config))
    try:
        founder = getattr(args, "user_id", None) or resolve_founder(store)
        if not founder:
            print("未找到创始人：先发一条消息建立数据，再用 `hermes trail`。")
            return 0
        trail_type = getattr(args, "trail_type", None)
        limit = _clamp(getattr(args, "limit", 20) or 20, 0, 200)
        as_json = getattr(args, "as_json", False)

        if trail_type == "deletion":
            rows = store.list_deletion_log(
                founder, limit=limit,
                table_name=getattr(args, "table_filter", None))
            return _emit("deletion", rows, as_json, _print_deletion)
        if trail_type == "tool":
            rows = store.recent_tool_events(
                user_id=founder,
                tool_name=getattr(args, "tool_filter", None), limit=limit)
            return _emit("tool", rows, as_json, _print_tool)
        if trail_type == "quality":
            rows = store.recent_quality_metrics(
                user_id=founder,
                metric_type=getattr(args, "metric_filter", None), limit=limit)
            return _emit("quality", rows, as_json, _print_quality)
        # default: overview — a small window from each write-only table
        deletion = store.list_deletion_log(founder, limit=_OVERVIEW_LIMIT)
        tool = store.recent_tool_events(user_id=founder, limit=_OVERVIEW_LIMIT)
        quality = store.recent_quality_metrics(user_id=founder, limit=_OVERVIEW_LIMIT)
    finally:
        try:
            store.close()
        except Exception:  # noqa: BLE001 — CLI teardown (C7)
            pass

    if as_json:
        print(json.dumps(
            {"overview": True, "deletion": deletion,
             "tool": tool, "quality": quality}, ensure_ascii=False))
        return 0
    _print_overview(deletion, tool, quality)
    return 0


def _clamp(n: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(n)))
    except (TypeError, ValueError):
        return lo


def _emit(kind: str, rows, as_json: bool, printer) -> int:
    if as_json:
        print(json.dumps({"type": kind, "rows": rows}, ensure_ascii=False))
        return 0
    printer(rows)
    return 0


def _print_overview(deletion, tool, quality) -> None:
    print("=== 观察面三件套 · 概览（ADR-V6-070）===")
    print(f"  软删审计 deletion_log：最近 {len(deletion)} 条  → `hermes trail --type deletion`")
    print(f"  工具捕获 tool_events：最近 {len(tool)} 条  → `hermes trail --type tool`")
    print(f"  质量时序 quality_metrics：最近 {len(quality)} 条  → `hermes trail --type quality`")
    if not deletion and not tool and not quality:
        print("  （三表均空：尚未产生记录。写入由 soft_delete / ptg_capture / calibration 触发。）")


def _print_deletion(rows) -> None:
    if not rows:
        print("=== 软删审计 deletion_log：空（ADR-V6-045）===")
        print("暂无软删记录。soft_delete / cascade_soft_delete / `hermes purge` 会写一行；")
        print("此处回答「我的什么被退役了、由谁、何时、为何」(R12 sovereignty audit)。")
        return
    print(f"=== 软删审计 deletion_log · 最近 {len(rows)} 条（ADR-V6-045）===")
    for r in rows:
        when = (r.get("created_at") or "")[:19]
        print(f"  {when}  [{r.get('table_name')}/{(r.get('record_id') or '')[:8]}]  "
              f"by {r.get('actor')}  {r.get('reason') or ''}")


def _print_tool(rows) -> None:
    if not rows:
        print("=== 工具捕获 tool_events：空 ===")
        print("暂无工具执行记录。ptg_capture 在每次 post_tool_call 写一行。")
        return
    print(f"=== 工具捕获 tool_events · 最近 {len(rows)} 条 ===")
    for r in rows:
        when = (r.get("captured_at") or "")[:19]
        status = r.get("status") or "?"
        dur = r.get("duration_ms")
        dur_s = f"{int(dur)}ms" if dur is not None else ""
        print(f"  {when}  {r.get('tool_name')}  [{status}]  {dur_s}")


def _print_quality(rows) -> None:
    if not rows:
        print("=== 质量时序 quality_metrics：空 ===")
        print("暂无质量指标。calibration（`hermes calibrate`）会写时序行。")
        return
    print(f"=== 质量时序 quality_metrics · 最近 {len(rows)} 条 ===")
    for r in rows:
        print(f"  {r.get('metric_date')}  {r.get('metric_type')}/"
              f"{r.get('atom_type')}  value={r.get('value')}  n={r.get('sample_size')}")

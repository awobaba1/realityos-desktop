"""``hermes dlq`` handler (ADR-V6-065).

I/O adapter for the C7 DLQ read/ack loop. ``dlq_messages`` is written by every
failure path (atomize/quark/theory/insights/provider — C7 never-silent-failure),
but until ADR-V6-065 it was write-only-no-consumer: no CLI, no API, no UI. The
C7 Phase-Gate Checklist's 'DLQ backlog < 5/week' KR was unverifiable. This
handler + ``PTGStore.dlq_stats/dlq_list/dlq_resolve`` ARE the read surface.

Single-direction data flow (架构 §4.7): reads ``dlq_messages``; ``--resolve``
flips the status-metadata ``resolved`` flag (allowed under append-only — see
ADR-V6-065 D3; the failure payload itself is never mutated). Thin delegate
mirrors ``citation_cmd`` / ``k_cmd`` (store open → read/ack → close).
"""

from __future__ import annotations

import json

from plugins.memory.ptg.store import PTGStore, load_ptg_config, resolve_db_path

# C7 Phase-Gate soft threshold: at or above this many UNRESOLVED rows, flag a
# backlog hint (Phase-Gate Checklist: 'DLQ backlog < 5/week'). NOT a gate —
# surfaced as a ⚠️ hint, never enforcement.
_BACKLOG_FLAG_THRESHOLD = 5


def cmd_dlq(args) -> int:
    config = load_ptg_config()
    store = PTGStore(db_path=resolve_db_path(config))
    try:
        resolve_id = getattr(args, "resolve_id", None)
        if resolve_id:
            ok = store.dlq_resolve(resolve_id)
            stats = store.dlq_stats()
            if getattr(args, "as_json", False):
                print(json.dumps(
                    {"resolved": ok, "id": resolve_id, "stats": stats},
                    ensure_ascii=False))
                return 0
            _print_resolve_result(ok, resolve_id, stats)
            return 0
        if getattr(args, "resolve_all", False):
            # ADR-V6-073: bulk-resolve consumer for dlq_resolve_all (the
            # half-done ADR-V6-065 primitive that had no CLI caller until now).
            count = store.dlq_resolve_all(source=getattr(args, "source", None))
            stats = store.dlq_stats()
            if getattr(args, "as_json", False):
                print(json.dumps(
                    {"resolved_all": count,
                     "source": getattr(args, "source", None), "stats": stats},
                    ensure_ascii=False))
                return 0
            _print_resolve_all_result(count, getattr(args, "source", None), stats)
            return 0
        if getattr(args, "stats_only", False):
            stats = store.dlq_stats()
            if getattr(args, "as_json", False):
                print(json.dumps(stats, ensure_ascii=False))
                return 0
            _print_stats(stats)
            return 0
        # default list view — resolved filter is tri-state (None=all/True/False)
        show_all = getattr(args, "show_all", False)
        only_resolved = getattr(args, "only_resolved", False)
        if show_all:
            resolved_filter = None
        elif only_resolved:
            resolved_filter = True
        else:
            resolved_filter = False  # default: unresolved only (most actionable)
        rows = store.dlq_list(
            resolved=resolved_filter,
            source=getattr(args, "source", None),
            limit=getattr(args, "limit", 20),
        )
        stats = store.dlq_stats()
    finally:
        try:
            store.close()
        except Exception:  # noqa: BLE001 — CLI teardown (C7)
            pass
    if getattr(args, "as_json", False):
        print(json.dumps({"stats": stats, "rows": rows}, ensure_ascii=False))
        return 0
    _print_list(stats, rows, resolved_filter)
    return 0


def _print_stats(stats) -> None:
    total = stats["total"]
    if total == 0:
        # Honest empty state — NOT fabricated zeros sold as a result.
        print("=== C7 DLQ 积压（尚无记录）===")
        print("暂无 DLQ 记录。每条失败路径（atomize/quark/theory/insights/provider）")
        print("在 C7 下都会写一行；此处聚合可观测 Phase-Gate「backlog < 5/week」。")
        return
    print(f"=== C7 DLQ 积压 · {total} 条（ADR-V6-065）===")
    print(f"  未解决 unresolved：{stats['unresolved']}   ← Phase-Gate 门禁指标")
    print(f"  已解决 resolved：{stats['resolved']}")
    if stats["unresolved"] >= _BACKLOG_FLAG_THRESHOLD:
        print(
            f"  ⚠️ 未解决 ≥{_BACKLOG_FLAG_THRESHOLD}：超出 Phase-Gate「backlog "
            f"< 5/week」soft 阈值，建议逐条 ack 或 `hermes dlq --resolve <id>`。"
        )
    if stats["by_source"]:
        src = "、".join(f"{k}({v})" for k, v in sorted(
            stats["by_source"].items(), key=lambda x: -x[1]))
        print(f"  来源：{src}")
    if stats["by_error_type"]:
        et = "、".join(f"{k}({v})" for k, v in sorted(
            stats["by_error_type"].items(), key=lambda x: -x[1]))
        print(f"  错误类型：{et}")


def _print_list(stats, rows, resolved_filter) -> None:
    _print_stats(stats)
    print()
    if resolved_filter is True:
        label = "（已解决）"
    elif resolved_filter is False:
        label = "（未解决，默认）"
    else:
        label = "（全部）"
    if not rows:
        print(f"=== DLQ 明细 {label}：空 ===")
        return
    print(f"=== DLQ 明细 {label} · 最近 {len(rows)} 条 ===")
    for r in rows:
        flag = "✅" if r["resolved"] else "⏳"
        msg = r["error_msg"]
        if len(msg) > 80:
            msg = msg[:77] + "..."
        print(f"  {flag} {r['id'][:8]}  [{r['source']}/{r['error_type']}]  {msg}")


def _print_resolve_result(ok, dlq_id, stats) -> None:
    if ok:
        print(f"✅ DLQ {dlq_id} 已标记 resolved。")
    else:
        print(f"⚠️ DLQ {dlq_id} 未翻状态（不存在 / 已 resolved / 异常）。")
    print(f"  当前未解决：{stats['unresolved']} / 总 {stats['total']}")


def _print_resolve_all_result(count, source, stats) -> None:
    scope = f"来源 {source!r} 的" if source else "全部"
    if count == 0:
        # Honest empty state — NOT "0 resolved" sold as success.
        print(f"=== 批量 resolve（{scope}）：无可解决记录 ===")
        print("没有未解决的 DLQ 行（可能本就为空，或 --source 不匹配）。")
    else:
        print(f"=== 批量 resolve（{scope}）：✅ {count} 条已标记 resolved（ADR-V6-073）===")
    print(f"  当前未解决：{stats['unresolved']} / 总 {stats['total']}")

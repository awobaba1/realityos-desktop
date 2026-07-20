"""``hermes citation`` handler (ADR-V6-063).

I/O adapter for the G1 citation-credibility READ loop. The counters live in
``ptg_meta`` (``citation_grounded_turns`` / ``citation_ungrounded_turns``),
bumped per turn by ``PTGProvider._observe_citation_quality`` (ADR-V6-043).
This handler + ``PTGStore.citation_stats`` ARE the read surface — until them,
the counters were write-only-no-consumer (ADR-V6-037 做了没发); ADR-V6-043's
"计数器可追溯 · 跨重启可查" promise was unfulfilled.

Single-direction data flow (架构 §4.7): reads ``ptg_meta``, writes nothing.
Never raises (C7). Thin delegate mirrors ``k_cmd`` (store open → read → close).
"""

from __future__ import annotations

import json

from plugins.memory.ptg.store import PTGStore, load_ptg_config, resolve_db_path

# Empirical soft threshold: at or above this ungrounded share, flag a
# credibility drift hint. NOT a gate (observation-only per ADR-V6-043) —
# surfaced as a ⚠️ hint, never enforcement. The hard refuse-to-render gate is
# a future agent-loop answer hook.
_UNGROUNDED_FLAG_RATIO = 0.30


def cmd_citation(args) -> int:
    config = load_ptg_config()
    store = PTGStore(db_path=resolve_db_path(config))
    try:
        stats = store.citation_stats()
    finally:
        try:
            store.close()
        except Exception:  # noqa: BLE001 — CLI teardown (C7)
            pass
    if getattr(args, "as_json", False):
        print(json.dumps(stats, ensure_ascii=False))
        return 0
    _print_stats(stats)
    return 0


def _print_stats(stats) -> None:
    g = stats["grounded"]
    u = stats["ungrounded"]
    total = stats["total"]
    ratio = stats["ungrounded_ratio"]
    if total == 0:
        # Honest empty state — NOT fabricated zeros sold as a result.
        print("=== G1 引用可信度（尚无观测）===")
        print("暂无 citation 观测。当 agent 回答涉及用户过去、且召回范围内有 chunk 时，")
        print("每轮会累积 grounded（有据引用）/ ungrounded（无据断言）计数。")
        return
    ratio_txt = f"{ratio:.1%}" if ratio is not None else "?"
    print(f"=== G1 引用可信度 · {total} 轮观测（ADR-V6-043）===")
    print(f"  有据引用 grounded：{g}")
    print(f"  无据断言 ungrounded：{u}   ← G1 可信度事件")
    print(f"  无据占比：{ratio_txt}")
    if ratio is not None and ratio >= _UNGROUNDED_FLAG_RATIO:
        print(
            f"  ⚠️ 无据占比 ≥{_UNGROUNDED_FLAG_RATIO:.0%}：agent 可能在对用户过去"
            f"做无引用断言（漏引 / 幻觉）。"
        )
    print(
        "\n注：observation-only（不阻断渲染）；硬执行门待 agent-loop answer hook"
        "（ADR-V6-043 next）。"
    )

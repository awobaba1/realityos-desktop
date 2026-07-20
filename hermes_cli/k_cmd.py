"""``hermes k`` handler (ADR-V6-056).

I/O adapter for the K-domain correlation compute + read loop. The closed-loop
statistics live in ``plugins.memory.ptg.k_correlation.compute_k_correlations``
(pure stats: R9 feeling_events → P(negative|entity) lift → K_Correlation edges,
sample-gated by F6 ≥10, zero LLM, never raises (C7)).

Until this CLI existed, ``compute_k_correlations`` was **production-zero-
reachable** — implemented by ADR-V6-044 but with no CLI, no scheduler, no hook
(A1 audit, 2026-07-20): 做了没发, ADR-V6-037's most-fatal fake-green. ``hermes k
compute`` is the explicit entry that arms the K-domain; ``hermes k show`` is the
consumer so compute's output is not write-only-no-consumer.

Single-direction data flow (架构 §4.7): reads feeling_events / entities, writes
only the relations graph (K_Correlation edges). correlation != causation
(PRD 01:93) — edges state co-occurrence, never causation.
"""

from __future__ import annotations

from plugins.memory.ptg.founder import resolve_founder
from plugins.memory.ptg.store import PTGStore, load_ptg_config, resolve_db_path


def cmd_k(args) -> int:
    action = getattr(args, "k_command", None)
    if action == "compute":
        return _cmd_compute(args)
    if action == "show":
        return _cmd_show(args)
    # no subcommand → usage hint
    print("`hermes k compute` 计算 K 域相关性 · `hermes k show` 查看（相关性≠因果）")
    return 0


def _cmd_compute(args) -> int:
    config = load_ptg_config()
    store = PTGStore(db_path=resolve_db_path(config))
    try:
        founder = getattr(args, "user_id", None) or resolve_founder(store)
        if not founder:
            print("未找到创始人：先发一条消息建立数据，再用 `hermes k compute`。")
            return 0
        # Lazy import keeps the CLI shell light (uniform with theory_cmd).
        from plugins.memory.ptg.k_correlation import compute_k_correlations

        written = compute_k_correlations(store, founder)
        _print_compute_result(written)
        return 0
    finally:
        try:
            store.close()
        except Exception:  # noqa: BLE001 — CLI teardown (C7)
            pass


def _print_compute_result(written: int) -> None:
    if written == 0:
        # Zero is HONEST, not failure: not enough R9 feeling_events per entity to
        # clear the F6 ≥10 sample gate, or no entity skews significantly. The
        # user sees why, never a fabricated edge (反假绿).
        print("K 域本轮未确认新边：R9 情绪样本不足（每实体需 ≥10 条过 F6 门）或无显著偏移。")
        return
    print(f"=== K 域相关性 · 确认 {written} 条 K_Correlation 边 ===")
    print("（纯统计：P(负|实体)/P(负) lift ≥1.2 或 ≤0.83；样本门 ≥10；相关性≠因果）")


def _cmd_show(args) -> int:
    """Consumer-side read: render the current K_Correlation edges.

    The read surface that makes ``compute``'s output observable — without it,
    K-edges are write-only-no-consumer (the A1 P1 做了没发 symptom). Each edge
    renders its polarity + lift + sample size + the 相关性≠因果 caveat.
    """
    config = load_ptg_config()
    store = PTGStore(db_path=resolve_db_path(config))
    try:
        founder = getattr(args, "user_id", None) or resolve_founder(store)
        if not founder:
            print("未找到创始人：先发一条消息建立数据，再用 `hermes k show`。")
            return 0
        edges = store.k_correlation_edges(founder)
        _print_edges(edges)
        return 0
    finally:
        try:
            store.close()
        except Exception:  # noqa: BLE001 — CLI teardown (C7)
            pass


def _print_edges(edges: list) -> None:
    if not edges:
        # Honest empty state — NOT "无相关性/平稳". Cold start, or run compute first.
        print("=== K 域相关性（尚无边）===")
        print("无当前 K_Correlation 边。先 `hermes k compute` 计算，或 R9 情绪数据不足。")
        return
    print(f"=== K 域相关性 · {len(edges)} 条 K_Correlation 边（相关性≠因果）===")
    for e in edges:
        name = e.get("object_name") or "?"
        polarity = e.get("polarity") or "?"
        lift = e.get("lift")
        n = e.get("sample_size")
        lift_txt = f"{float(lift):.2f}" if isinstance(lift, (int, float)) else "?"
        n_txt = str(n) if n is not None else "?"
        # polarity=negative → 实体与负情绪共现；positive → 与非负共现。
        arrow = "负偏" if polarity == "negative" else ("正偏" if polarity == "positive" else polarity)
        print(f"  {name}：{arrow}（lift {lift_txt}，n={n_txt}）")
    print("\n注：K_Correlation 表共现，不表因果（PRD 01:93）。")

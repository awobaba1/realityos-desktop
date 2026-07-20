"""``hermes theory`` handler (ADR-V6-051 / B3).

I/O adapter for the Theory derivation + persist loop. The closed-loop logic
lives in ``plugins.realityos_theory.derive_and_persist``; this module opens the
shared PTGStore, resolves the founder, gathers the user's atoms
(``recent_atoms``) + relations (``relations_for_user``), and prints. The
closed loop is unit-testable without a tty or a real LLM (the test
monkeypatches ``derive_and_persist`` on this module).

Single-direction data flow (架构 §4.7): ``derive`` reads atoms/relations and
writes only insight_aggregation — never back to the atom layer.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from plugins.memory.ptg.founder import resolve_founder
from plugins.memory.ptg.store import PTGStore, load_ptg_config, resolve_db_path


def _today_beijing() -> str:
    """Today's Beijing date as YYYY-MM-DD (the theory period granularity).

    Inlined (mirrors ``realityos_theory/scheduling._period_key``) so the store
    stays tz-free and this CLI module has no theory-package import at module
    level (lazy import keeps the CLI shell light).
    """
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")


def cmd_theory(args) -> int:
    action = getattr(args, "theory_command", None)
    if action == "derive":
        return _cmd_derive(args)
    if action == "show":
        return _cmd_show(args)
    # no subcommand → usage hint
    print("`hermes theory derive` 推导 PC/FR · `hermes theory show` 查看（诚实降级）")
    return 0


def _cmd_derive(args) -> int:
    config = load_ptg_config()
    store = PTGStore(db_path=resolve_db_path(config))
    try:
        founder = getattr(args, "user_id", None) or resolve_founder(store)
        if not founder:
            print("未找到创始人：先发一条消息建立数据，再用 `hermes theory derive`。")
            return 0
        atoms = store.recent_atoms(user_id=founder, limit=500)
        relations = store.relations_for_user(founder, limit=50)
        if not atoms:
            print("还没有原子数据：先聊几条，等 Atomizer 产出后再 `hermes theory derive`。")
            return 0

        # Imported lazily so importing this CLI module never pulls the (heavy)
        # theory engine + prompt files unless the command actually runs.
        from plugins.realityos_theory import derive_and_persist

        result = derive_and_persist(
            store, user_id=founder, atoms=atoms, relations=relations,
            period_key=getattr(args, "period_key", None))
        _print_derive_result(result, len(atoms), len(relations))
        return 0
    finally:
        try:
            store.close()
        except Exception:  # noqa: BLE001 — CLI teardown (C7)
            pass


def _print_derive_result(result: dict, atom_n: int, rel_n: int) -> None:
    derived = int(result.get("derived", 0))
    persisted = int(result.get("persisted", 0))
    degraded = int(result.get("degraded_count", 0))
    if derived == 0:
        # derive returned no records: LLM call failed / bad batch (C7 → DLQ).
        print("理论推导未产出（LLM 失败或批次非法，已入 DLQ）。")
        return
    print("=== Theory 推导 · PC/FR 骨架 ===")
    print(f"输入原子 {atom_n} · 关系 {rel_n}")
    print(f"推导 {derived} 项 · 落库 {persisted} 项 · 降级 {degraded} 项")
    if degraded:
        # The honest-degradation contract: degraded dims (Energy/Social/
        # Environment/Cognition) are machine-flagged; the consumer must render
        # them as "数据不足/降级", never as a real score or "平稳".
        print("  注：降级项已标 degraded=True（Phase 2 文本无据维度），见 result_data。")


def _cmd_show(args) -> int:
    """Consumer-side read: render the derived PC/FR for one period.

    This is the B3 "UI" — the surface that makes the honest-degradation contract
    observable (ADR-V6-040 D4 / ADR-V6-050). Without it, theory writes scores +
    ``degraded`` flags that nothing renders. Degraded dims are shown as
    「数据不足/降级」with their basis, NEVER as a score or "平稳" (the iron rule).
    """
    config = load_ptg_config()
    store = PTGStore(db_path=resolve_db_path(config))
    try:
        founder = getattr(args, "user_id", None) or resolve_founder(store)
        if not founder:
            print("未找到创始人：先发一条消息建立数据，再用 `hermes theory show`。")
            return 0
        period_key = getattr(args, "period_key", None) or _today_beijing()
        snapshot = store.theory_snapshot(founder, period_key)
        _print_snapshot(snapshot)
        return 0
    finally:
        try:
            store.close()
        except Exception:  # noqa: BLE001 — CLI teardown (C7)
            pass


def _print_snapshot(snapshot: dict) -> None:
    period_key = snapshot.get("period_key", "")
    if not snapshot.get("found"):
        # Honest empty state — NOT "平稳". The user sees: nothing derived yet
        # for this period (cold start, or run `hermes theory derive` first).
        print(f"=== Theory · {period_key}（尚无推导）===")
        print("该周期未推导 PC/FR。先 `hermes theory derive` 生成，或换 --period-key。")
        return
    pc = snapshot.get("pc") or []
    fr = snapshot.get("fr") or []
    degraded_n = sum(1 for e in pc + fr if e.get("degraded"))
    print(f"=== Theory · {period_key}（PC {len(pc)} · FR {len(fr)} · 降级 {degraded_n}）===")
    if pc:
        print("— 个人约束 PC（7 维）—")
        for e in pc:
            print("  " + _render_dim(e))
    if fr:
        print("— 生活框架 FR（5 维）—")
        for e in fr:
            print("  " + _render_dim(e))
    if degraded_n:
        print("\n注：标「数据不足」的维度为 Phase 2 文本无据（需声学/多人/节律链路，"
              "Phase 2.5+），未测量——非平稳。")


def _render_dim(e: dict) -> str:
    """Render one PC/FR dim HONESTLY: degraded ⇒ 「数据不足」+ basis, never a score."""
    name = e.get("name", "?")
    basis = (e.get("basis") or "").strip()
    if e.get("degraded"):
        # Iron rule (ADR-V6-050): an unsupported dim must never read as a
        # measured value. Show 「数据不足」+ why, not the (forced 0.0 / untrusted) score.
        return f"{name}：数据不足/降级 — {basis}" if basis else f"{name}：数据不足/降级"
    score = e.get("score", 0.0)
    try:
        score_txt = f"{float(score):.2f}"
    except (TypeError, ValueError):
        score_txt = "?"
    tail = f"（{basis}）" if basis else ""
    return f"{name}：{score_txt}{tail}"


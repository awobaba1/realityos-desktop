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

from plugins.memory.ptg.founder import resolve_founder
from plugins.memory.ptg.store import PTGStore, load_ptg_config, resolve_db_path


def cmd_theory(args) -> int:
    action = getattr(args, "theory_command", None)
    if action == "derive":
        return _cmd_derive(args)
    # no subcommand → usage hint
    print("`hermes theory derive` — 从原子图谱推导 PC/FR 骨架并落库")
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

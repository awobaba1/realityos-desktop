"""``hermes quark`` handler (ADR-V6-051 / B3).

I/O adapter for the Quark extraction + aggregation loop. The closed-loop logic
lives in ``plugins.realityos_quark.extract_and_aggregate``; this module opens
the shared PTGStore, resolves the founder, loads the memo's effective capture
text (corrected_text ?? source_text), and prints. Keeping I/O out of the plugin
is what makes the closed loop unit-testable without a tty or a real LLM (the
test monkeypatches ``extract_and_aggregate`` on this module).
"""

from __future__ import annotations

from plugins.memory.ptg.founder import resolve_founder
from plugins.memory.ptg.store import PTGStore, load_ptg_config, resolve_db_path


def cmd_quark(args) -> int:
    action = getattr(args, "quark_command", None)
    if action == "extract":
        return _cmd_extract(args)
    # no subcommand → usage hint
    print("`hermes quark extract <memo_id>` — 从某条 memo 抽取 quark 并聚合为原子")
    return 0


def _cmd_extract(args) -> int:
    config = load_ptg_config()
    store = PTGStore(db_path=resolve_db_path(config))
    try:
        founder = getattr(args, "user_id", None) or resolve_founder(store)
        if not founder:
            print("未找到创始人：先发一条消息建立数据，再用 `hermes quark extract`。")
            return 0
        memo_id = getattr(args, "memo_id", "") or ""
        memo = store.get_memo(founder, memo_id)
        if not memo:
            print(f"找不到 memo「{memo_id}」（不存在或已软删）。")
            return 1
        capture_text = (memo.get("effective_text") or "").strip()
        if not capture_text:
            print(f"memo「{memo_id}」的捕获文本为空，无可提取内容。")
            return 0

        # Imported lazily so importing this CLI module never pulls the (heavy)
        # quark extractor + prompt files unless the command actually runs.
        from plugins.realityos_quark import extract_and_aggregate

        result = extract_and_aggregate(
            store, user_id=founder, capture_text=capture_text,
            source_text=capture_text)
        _print_extract_result(memo_id, result)
        # exit 0 whenever the command ran (a 0-quark result is honest data, not
        # an operational failure); a memo lookup miss is the only exit-1 path.
        return 0
    finally:
        try:
            store.close()
        except Exception:  # noqa: BLE001 — CLI teardown (C7)
            pass


def _print_extract_result(memo_id: str, result: dict) -> None:
    extracted = int(result.get("extracted", 0))
    aggregated = int(result.get("aggregated", 0))
    counts = result.get("counts") or {}
    if extracted == 0:
        # Honest empty — could be a memo with no Identity/Meaning/Feeling
        # content OR a bad LLM batch (which already landed in DLQ via C7).
        print(f"memo「{memo_id}」未抽出 quark（内容无 I/M/F 信号或批次入 DLQ）。")
        return
    print(f"=== Quark 抽取 · memo {memo_id} ===")
    print(f"抽出 {extracted} 个 quark · 聚合 {aggregated} 个主原子")
    by_kind = counts.get("by_kind") or {}
    if by_kind:
        print("  按类：" + "、".join(f"{k} {v}" for k, v in by_kind.items()))
    skipped = counts.get("skipped") or 0
    if skipped:
        print(f"  跳过（非 PRIMARY/重复）：{skipped}")

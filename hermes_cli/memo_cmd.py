"""``hermes memo`` handler (ADR-V6-047 / A4).

I/O adapter for the source-text correction + re-extraction loop. The pure
loop logic lives in ``plugins.memory.ptg.correction.re_extract_memo``; this
module opens the shared PTGStore, resolves the founder, builds an Atomizer
exactly the way the PTG provider does, and prints. Keeping I/O + Atomizer
construction out of the store is what makes the §6.x contract unit-testable
without a tty or a real LLM (the test monkeypatches ``_build_atomizer``).
"""

from __future__ import annotations

from plugins.memory.ptg.confidence import ConfidenceEngine
from plugins.memory.ptg.founder import resolve_founder
from plugins.memory.ptg.store import PTGStore, load_ptg_config, resolve_db_path


def _build_atomizer(store, user_id, config):
    """Construct the Atomizer the same way the PTG provider does.

    Factored out so tests can monkeypatch it with a deterministic double —
    production runs the real LLM (call_llm resolves the provider from
    config.yaml); the unit test must not make a network call.
    """
    # Imported lazily so importing this CLI module never pulls the (heavy)
    # atomizer + prompt files unless the command actually runs.
    from plugins.memory.ptg.atomizer import Atomizer

    return Atomizer(
        store,
        user_id=user_id,
        confidence_engine=ConfidenceEngine.from_ptg_config(config),
        materialize_graph=bool(config.get("materialize_graph", True)),
    )


def cmd_memo(args) -> int:
    action = getattr(args, "memo_command", None)
    if action == "correct":
        return _cmd_correct(args)
    # no subcommand → nothing to do; tell the user how to use it
    print("`hermes memo correct <memo_id> --text \"纠正后的文本\"`")
    return 0


def _cmd_correct(args) -> int:
    config = load_ptg_config()
    store = PTGStore(db_path=resolve_db_path(config))
    try:
        founder = resolve_founder(store)
        if not founder:
            print("未找到创始人：先发一条消息建立数据，再用 `hermes memo correct`。")
            return 0
        from plugins.memory.ptg.correction import re_extract_memo

        atomizer = _build_atomizer(store, founder, config)
        result = re_extract_memo(
            store, atomizer,
            user_id=founder, memo_id=getattr(args, "memo_id", ""),
            corrected_text=getattr(args, "text", ""),
            actor="user", reason="hermes_memo_correct",
            expected_version=getattr(args, "expected_version", None))
        print(result.get("message", "操作完成。"))
        return 0 if result.get("ok") else 1
    finally:
        try:
            store.close()
        except Exception:  # noqa: BLE001 — CLI teardown (C7)
            pass

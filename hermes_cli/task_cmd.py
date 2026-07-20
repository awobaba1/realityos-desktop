"""``hermes task`` handler (ADR-V6-046 / A3).

I/O adapter for the R12 explicit task-outcome pathway. The pure resolution +
mutation logic lives in ``PTGStore.mark_task_outcome`` / ``list_open_tasks``;
this module opens the shared PTGStore, resolves the founder, and prints. Keeping
I/O out of the store is what makes the §6.x contract unit-testable without a tty.
"""

from __future__ import annotations

from plugins.memory.ptg.founder import resolve_founder
from plugins.memory.ptg.store import PTGStore, load_ptg_config, resolve_db_path

_VERB_OUTCOME = {"done": "completed", "failed": "failed", "delayed": "delayed"}


def cmd_task(args) -> int:
    action = getattr(args, "task_command", None)
    if action == "list":
        return _cmd_list()
    if action in _VERB_OUTCOME:
        return _cmd_mark(action, args)
    # no subcommand → print help equivalent (list)
    return _cmd_list()


def _open_store():
    store = PTGStore(db_path=resolve_db_path(load_ptg_config()))
    founder = resolve_founder(store)
    return store, founder


def _cmd_list() -> int:
    store = PTGStore(db_path=resolve_db_path(load_ptg_config()))
    try:
        founder = resolve_founder(store)
        if not founder:
            print("未找到创始人：先发一条消息建立数据，再用 `hermes task`。")
            return 0
        rows = store.list_open_tasks(founder)
        if not rows:
            print("当前没有待办任务。继续聊聊，等任务抽取后再来 `hermes task`。")
            return 0
        print(f"=== 待办任务（{len(rows)} 条，#N 是编号）===")
        for i, r in enumerate(rows, 1):
            flag = " ⚠逾期" if r.get("is_overdue") else ""
            desc = r.get("task_description") or "(无描述)"
            print(f"  #{i}  {desc}{flag}")
        print("\n用 `hermes task done #N` 标记完成，`failed`/`delayed` 同理。")
        return 0
    finally:
        try:
            store.close()
        except Exception:  # noqa: BLE001 — CLI teardown (C7)
            pass


def _cmd_mark(verb: str, args) -> int:
    store = PTGStore(db_path=resolve_db_path(load_ptg_config()))
    try:
        founder = resolve_founder(store)
        if not founder:
            print("未找到创始人：先发一条消息建立数据，再用 `hermes task`。")
            return 0
        ref = getattr(args, "ref", None)
        note = getattr(args, "note", None)
        result = store.mark_task_outcome(
            user_id=founder, ref=ref or "", outcome=_VERB_OUTCOME[verb],
            actor="user", resolution_note=note)
        print(result.get("message", "操作完成。"))
        if not result.get("ok"):
            print("用 `hermes task list` 查看当前待办编号。")
            return 1
        return 0
    finally:
        try:
            store.close()
        except Exception:  # noqa: BLE001 — CLI teardown (C7)
            pass

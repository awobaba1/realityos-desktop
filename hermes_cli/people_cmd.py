"""``hermes people`` handler (ADR-V6-048 / A5).

I/O adapter for the M-domain people roster + profile. The pure aggregation
logic lives in ``PTGStore.list_people`` / ``person_profile``; this module
opens the shared PTGStore, resolves the founder, resolves a name→entity_id,
and prints. Keeping I/O out of the store is what makes the §6.x contract
unit-testable without a tty.
"""

from __future__ import annotations

from plugins.memory.ptg.founder import resolve_founder
from plugins.memory.ptg.store import PTGStore, load_ptg_config, resolve_db_path


def cmd_people(args) -> int:
    action = getattr(args, "people_command", None)
    if action == "show":
        return _cmd_show(args)
    # default (None or 'list') → list
    return _cmd_list()


def _open():
    store = PTGStore(db_path=resolve_db_path(load_ptg_config()))
    return store, resolve_founder(store)


def _cmd_list() -> int:
    store = PTGStore(db_path=resolve_db_path(load_ptg_config()))
    try:
        founder = resolve_founder(store)
        if not founder:
            print("未找到创始人：先发一条消息建立数据，再用 `hermes people`。")
            return 0
        rows = store.list_people(founder)
        if not rows:
            print("还没有识别到的人物。继续聊聊，等人名被抽取后再来 `hermes people`。")
            return 0
        print(f"=== 人物（{len(rows)} 位，按提及频次排序）===")
        for i, r in enumerate(rows, 1):
            aliases = r.get("aliases") or []
            tail = f"（别名：{'、'.join(aliases)}）" if aliases else ""
            print(f"  #{i}  {r['entity_name']}  · 提及 {r['mention_count']} 次{tail}")
        print("\n用 `hermes people show <名字>` 查看某人的完整画像。")
        return 0
    finally:
        try:
            store.close()
        except Exception:  # noqa: BLE001 — CLI teardown (C7)
            pass


def _resolve_entity_id(store, user_id: str, ref: str) -> str | None:
    """Turn a CLI ref into a person entity_id.

    Accepts a raw entity_id directly, else falls back to ``search_entities``
    narrowed to people. Returns the first person match or None.
    """
    ref = (ref or "").strip()
    if not ref:
        return None
    # Direct id hit?
    hits = [e for e in store.list_people(user_id, limit=500)
            if e["entity_id"] == ref]
    if hits:
        return ref
    # Name match via search_entities, filtered to people.
    found = store.search_entities(user_id, ref, limit=20)
    person_ids = {e["id"] for e in found if e.get("entity_type") == "person"}
    if person_ids:
        return next(iter(person_ids))
    # Fallback: exact name in list_people (handles 2-char CJK search_entities
    # may have missed).
    for p in store.list_people(user_id, limit=500):
        if p["entity_name"] == ref or ref in (p.get("aliases") or []):
            return p["entity_id"]
    return None


def _cmd_show(args) -> int:
    store = PTGStore(db_path=resolve_db_path(load_ptg_config()))
    try:
        founder = resolve_founder(store)
        if not founder:
            print("未找到创始人：先发一条消息建立数据，再用 `hermes people show`。")
            return 0
        ref = getattr(args, "ref", "")
        entity_id = _resolve_entity_id(store, founder, ref)
        if not entity_id:
            print(f"找不到人物「{ref}」。先用 `hermes people list` 看看有哪些人。")
            return 1
        p = store.person_profile(founder, entity_id)
        if not p.get("found"):
            print(f"找不到人物「{ref}」（{p.get('reason', 'not_found')}）。")
            return 1
        _print_profile(p)
        return 0
    finally:
        try:
            store.close()
        except Exception:  # noqa: BLE001 — CLI teardown (C7)
            pass


def _print_profile(p) -> None:
    name = p["entity_name"]
    print(f"=== 人物画像 · {name} ===")
    aliases = p.get("aliases") or []
    if aliases:
        print(f"别名：{'、'.join(aliases)}")
    print(f"提及 {p['mention_count']} 次 · 首见 {p['first_seen_at']} · 最近 {p['last_seen_at']}")

    brk = p.get("interaction_breakdown") or {}
    print(f"\n— 互动（共 {brk.get('total', 0)} 次）—")
    by_type = brk.get("by_type") or {}
    by_sent = brk.get("by_sentiment") or {}
    if by_type:
        print("  类型：" + "、".join(f"{k} {v}" for k, v in by_type.items()))
    if by_sent:
        print("  情绪：" + "、".join(f"{k} {v}" for k, v in by_sent.items()))

    ctx = p.get("recent_contexts") or []
    if ctx:
        print(f"\n— 最近互动（{len(ctx)} 条）—")
        for c in ctx:
            ctx_txt = (c.get("context") or "(无上下文)").strip()
            print(f"  · [{c.get('timestamp', '')}] {ctx_txt}")

    rels = p.get("relations") or []
    if rels:
        print(f"\n— 关系（{len(rels)} 条）—")
        for r in rels:
            subj = r.get("subject_name") or "?"
            obj = r.get("object_name") or "?"
            rtype = r.get("relation_type") or "关联"
            val = r.get("value")
            label = f"{rtype}({val})" if val else rtype
            print(f"  · {subj} —[{label}]→ {obj}")

    emo = p.get("emotions") or {}
    if emo.get("count"):
        print(f"\n— 情绪关联（{emo['count']} 次）—")
        for t in emo.get("triggers") or []:
            trig = (t.get("trigger") or "").strip()
            print(f"  · [{t.get('timestamp', '')}] {t.get('state_type', '')}/"
                  f"{t.get('intensity', '')} {trig}")

    print("\n（以上为原始事件聚合，未经 LLM 合成 — ADR-V6-048 诚实边界）")

"""``hermes purge`` handler (ADR-V6-067).

The §6.2 阶段2 physical-purge surface — and the SOLE production caller of
``purge_soft_deleted`` (sovereignty.py). Until ADR-V6-067 that primitive was
ORPHAN CODE: zero non-test callers, while comments in sovereignty.py +
web_server.py falsely claimed a "nightly cron" ran it (documentation
fake-green + 做了没发, ADR-V6-037's most-fatal class). This CLI closes the loop.

Safety: hard-DELETE is the single legitimate C2 exception (grace-window-
expired rows only). DRY-RUN IS THE DEFAULT — counts eligible rows per table,
deletes nothing. ``--confirm`` executes. ``--older-than-days`` sets the grace
window (default 30, conservative — the primitive's own default is 1 day for
test convenience).
"""

from __future__ import annotations

import json

from plugins.memory.ptg.store import PTGStore, load_ptg_config, resolve_db_path
from plugins.realityos_sovereignty.sovereignty import purge_soft_deleted

# Conservative CLI default grace window. The primitive defaults to 1 day (test
# convenience); a founder CLI must not nuke rows soft-deleted yesterday.
_DEFAULT_OLDER_THAN_DAYS = 30


def cmd_purge(args) -> int:
    config = load_ptg_config()
    store = PTGStore(db_path=resolve_db_path(config))
    try:
        confirmed = getattr(args, "confirm", False)
        older = getattr(args, "older_than_days", _DEFAULT_OLDER_THAN_DAYS)
        tables = _parse_tables(getattr(args, "tables", None))
        result = purge_soft_deleted(
            store, older_than_days=older, tables=tables, dry_run=not confirmed)
    finally:
        try:
            store.close()
        except Exception:  # noqa: BLE001 — CLI teardown (C7)
            pass
    if getattr(args, "as_json", False):
        print(json.dumps(
            {"dry_run": not confirmed, "older_than_days": older,
             "counts": result, "total": sum(result.values())},
            ensure_ascii=False))
        return 0
    _print_result(result, confirmed, older)
    return 0


def _parse_tables(raw):
    if not raw:
        return None
    return [t.strip() for t in str(raw).split(",") if t.strip()]


def _print_result(result, confirmed, older) -> None:
    mode = "硬删执行" if confirmed else "干跑（DRY-RUN，未删除）"
    total = sum(result.values())
    if not result:
        print(f"=== §6.2 阶段2 purge · {mode} · 0 条符合（>{older} 天）===")
        print("无软删行超过宽限窗口——无需 purge。")
        if not confirmed:
            print("（dry-run 默认；加 --confirm 执行硬删。）")
        return
    print(f"=== §6.2 阶段2 purge · {mode} · 共 {total} 条（>{older} 天）===")
    for t, n in sorted(result.items(), key=lambda x: -x[1]):
        print(f"  {t}: {n}")
    if not confirmed:
        print("⚠️ 以上为 dry-run 预览，未实际删除。加 --confirm 执行硬删（C2 唯一例外）。")
    else:
        print("✅ 已物理删除上述软删行（grace window 已过，§6.2 阶段2 C2 合规）。")

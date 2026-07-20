"""``hermes purge`` subcommand parser (ADR-V6-067).

The §6.2 阶段2 physical-purge surface — sole production caller of
``purge_soft_deleted`` (sovereignty.py). Until this CLI, that primitive was
ORPHAN CODE (zero non-test callers) while comments falsely claimed a nightly
cron ran it — documentation fake-green + 做了没发 (ADR-V6-037's most-fatal
class). DRY-RUN IS THE DEFAULT (hard-DELETE is the single C2 exception);
``--confirm`` executes. Handler injected (``cmd_purge``) to avoid importing
``main``.
"""

from __future__ import annotations

from typing import Callable


def build_purge_parser(subparsers, *, cmd_purge: Callable) -> None:
    """Attach the ``purge`` subcommand to ``subparsers``.

    ``hermes purge`` previews (dry-run default) or executes (``--confirm``) the
    §6.2 阶段2 physical purge of soft-deleted rows past the grace window — the
    one legitimate hard-DELETE surface in V6 (C2 exception; every row already
    soft-deleted + grace expired). Conservative 30-day default; the primitive
    itself is never auto-scheduled (no nightly cron wired — ADR-V6-067).
    """
    p = subparsers.add_parser(
        "purge",
        help="§6.2 阶段2 软删行物理清除（dry-run 默认，--confirm 硬删）",
        description=(
            "§6.2 阶段2 physical purge (ADR-V6-067) — the sole production caller "
            "of purge_soft_deleted (sovereignty.py). Until this CLI that primitive "
            "was orphan code (zero non-test callers) while comments falsely "
            "claimed a nightly cron ran it (documentation fake-green, 做了没发 "
            "ADR-V6-037). Hard-DELETE is the single C2 exception — every row is "
            "already soft-deleted with an expired grace window. DRY-RUN DEFAULT; "
            "--confirm executes. No automatic scheduler is wired."
        ),
    )
    p.add_argument(
        "--confirm", action="store_true",
        help="执行硬删（默认 dry-run 仅预览计数）。")
    p.add_argument(
        "--older-than-days", type=int, default=30,
        help="宽限窗口天数（默认 30；超过此天数的软删行才被清除）。")
    p.add_argument(
        "--tables", default=None,
        help="限定表（逗号分隔，如 memos,identity_events）；默认全 C2 用户表。")
    p.add_argument(
        "--json", action="store_true", dest="as_json",
        help="机器可读 JSON。")
    p.set_defaults(func=cmd_purge)

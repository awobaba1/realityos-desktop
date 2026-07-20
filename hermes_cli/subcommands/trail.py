"""``hermes trail`` subcommand parser (ADR-V6-070).

The READ surface for the write-only-no-consumer triplet (deletion_log /
tool_events / quality_metrics). The fourth-round audit (ADR-037 维度) found all
three have real producers + an ADR-added read API + C4 tests — but ZERO CLI/API/
UI consumer (做了没发, ADR-V6-037's most-fatal fake-green class). ``hermes trail``
is the consumer that closes the loop for all three. Handler injected
(``cmd_trail``) to avoid importing ``main``.
"""

from __future__ import annotations

from typing import Callable


def build_trail_parser(subparsers, *, cmd_trail: Callable) -> None:
    """Attach the ``trail`` subcommand to ``subparsers``.

    ``hermes trail`` reads the three observation tables that were write-only
    until this CLI. Default = overview (a small window from each); ``--type``
    selects one table for a detailed listing. Read-only (架构 §4.7): never
    writes, never mutates.
    """
    p = subparsers.add_parser(
        "trail",
        help="观察面三件套读面（软删审计/工具捕获/质量时序）",
        description=(
            "Observation-trail read surface (ADR-V6-070). Reads the three tables "
            "that were write-only-no-consumer until this CLI: deletion_log "
            "(soft-delete audit, ADR-V6-045), tool_events (post_tool_call capture), "
            "quality_metrics (calibration time-series, ADR-V6-028). The consumer "
            "that closes 做了没发 (ADR-V6-037) for the triplet — the same gap "
            "ADR-V6-063 closed for citation counters and ADR-V6-065 for dlq_messages. "
            "Default = overview; --type <X> = detailed listing."
        ),
    )
    p.add_argument(
        "--type", dest="trail_type", default=None,
        choices=["deletion", "tool", "quality"],
        help="选一张表读明细：deletion(软删审计)/tool(工具捕获)/quality(质量时序)。"
             "缺省=三表概览。")
    p.add_argument(
        "--limit", type=int, default=20,
        help="明细条数上限（默认 20，夹到 [0,200]；概览固定 3/表）。")
    p.add_argument(
        "--table", dest="table_filter", default=None,
        help="仅 --type deletion：按源表过滤（如 meaning_events/feedback）。")
    p.add_argument(
        "--tool", dest="tool_filter", default=None,
        help="仅 --type tool：按工具名过滤。")
    p.add_argument(
        "--metric", dest="metric_filter", default=None,
        help="仅 --type quality：按指标类型过滤。")
    p.add_argument(
        "--json", action="store_true", dest="as_json",
        help="机器可读 JSON。")
    p.set_defaults(func=cmd_trail)

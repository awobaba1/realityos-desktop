"""``hermes dlq`` subcommand parser (ADR-V6-065).

The C7 DLQ read/ack surface. Every failure path (atomize/quark/theory/insights/
provider) writes a ``dlq_messages`` row under C7 (never-silent-failure); until
this CLI (+ ``PTGStore.dlq_stats/dlq_list/dlq_resolve``), that table was
write-only-no-consumer — the C7 Phase-Gate Checklist's 'DLQ backlog < 5/week'
KR was unverifiable (做了没发, ADR-V6-037's most-fatal fake-green). ``hermes dlq``
is the consumer that closes the loop. Handler injected (``cmd_dlq``) to avoid
importing ``main``.
"""

from __future__ import annotations

from typing import Callable


def build_dlq_parser(subparsers, *, cmd_dlq: Callable) -> None:
    """Attach the ``dlq`` subcommand to ``subparsers``.

    ``hermes dlq`` reads DLQ backlog stats + recent rows (default: unresolved,
    most actionable) and optionally acks one row via ``--resolve``. Read-mostly
    (架构 §4.7); ``--resolve`` flips the status-metadata ``resolved`` flag only
    (the failure payload is never mutated — append-only compliant, ADR-V6-065
    D3). ``--stats`` is observation-only (the hard alerting gate is a future
    ops layer, not this CLI).
    """
    p = subparsers.add_parser(
        "dlq",
        help="C7 DLQ 积压读面（未解决失败记录统计+明细+ack）",
        description=(
            "C7 DLQ backlog surface (ADR-V6-065). Reads dlq_messages — the table "
            "every failure path writes under C7 (never-silent-failure). The read "
            "surface that makes the Phase-Gate 'DLQ backlog < 5/week' KR "
            "observable — without it the table is write-only (做了没发, "
            "ADR-V6-037). Default lists unresolved rows; --resolve acks one "
            "(status-metadata flip, append-only compliant)."
        ),
    )
    p.add_argument(
        "--stats", action="store_true", dest="stats_only",
        help="仅输出聚合统计（total/unresolved/resolved/by_source/by_error_type）。")
    p.add_argument(
        "--resolved", action="store_true", dest="only_resolved",
        help="列出已解决（默认仅未解决，最可操作）。")
    p.add_argument(
        "--all", action="store_true", dest="show_all",
        help="列出全部（含已解决）。")
    p.add_argument(
        "--source", default=None,
        help="按来源过滤（如 atomize/quark/theory/insights/provider）。")
    p.add_argument(
        "--limit", type=int, default=20,
        help="明细条数上限（默认 20，夹到 [0,200]）。")
    p.add_argument(
        "--resolve", dest="resolve_id", default=None,
        help="标记单条 DLQ 为 resolved（创始人 ack；状态元数据翻转）。")
    p.add_argument(
        "--json", action="store_true", dest="as_json",
        help="机器可读 JSON。")
    p.set_defaults(func=cmd_dlq)

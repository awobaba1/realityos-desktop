"""``hermes citation`` subcommand parser (ADR-V6-063).

The G1 citation-credibility READ surface. ``PTGProvider._observe_citation_quality``
bumps grounded/ungrounded turn counters to ``ptg_meta`` every turn (ADR-V6-043);
until this CLI (+ ``PTGStore.citation_stats``), those counters were
write-only-no-consumer — ADR-V6-043's "计数器可追溯 ✅ 跨重启可查" promise was
unfulfilled (做了没发, ADR-V6-037's most-fatal fake-green). ``hermes citation``
is the consumer that closes the loop. Handler injected (``cmd_citation``) to
avoid importing ``main``.
"""

from __future__ import annotations

from typing import Callable


def build_citation_parser(subparsers, *, cmd_citation: Callable) -> None:
    """Attach the ``citation`` subcommand to ``subparsers``.

    ``hermes citation`` reads the G1 grounded/ungrounded turn counters and
    renders them (human table by default, ``--json`` for machine-readable).
    Read-only (架构 §4.7): reads ``ptg_meta``, writes nothing. Observation-only
    — the hard refuse-to-render gate is a future agent-loop answer hook
    (ADR-V6-043 next iteration), not this CLI.
    """
    p = subparsers.add_parser(
        "citation",
        help="G1 引用可信度统计（grounded/ungrounded 计数读面）",
        description=(
            "G1 citation credibility (ADR-V6-043 / ADR-V6-063). Reads the "
            "grounded/ungrounded turn counters PTGProvider bumps each turn. "
            "The read surface that fulfills ADR-V6-043's '计数器可追溯 · 跨重启"
            "可查' promise — without it the counters are write-only "
            "(做了没发, ADR-V6-037). Observation-only; hard-enforcement gate is "
            "a future agent-loop hook."
        ),
    )
    p.add_argument(
        "--json", action="store_true", dest="as_json",
        help="Emit machine-readable JSON instead of the human table.")
    p.set_defaults(func=cmd_citation)

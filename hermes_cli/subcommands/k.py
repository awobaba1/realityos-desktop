"""``hermes k`` subcommand parser (ADR-V6-056).

The K-domain correlation pathway (Phase 2-A / ADR-V6-044): compute R9
feeling-event → trigger-entity valence co-occurrence edges and persist them as
``K_Correlation`` relations, then read them back. Handler injected (``cmd_k``)
to avoid importing ``main``.

Until ADR-V6-056 (A1 audit 2026-07-20), ``compute_k_correlations`` was
production-zero-reachable — implemented by ADR-V6-044 but with no CLI, no
scheduler, no hook (做了没发). ``hermes k compute`` arms it; ``hermes k show``
is the consumer so its output is not write-only.
"""

from __future__ import annotations

from typing import Callable


def build_k_parser(subparsers, *, cmd_k: Callable) -> None:
    """Attach the ``k`` subcommand to ``subparsers``.

    ``hermes k compute`` runs ``compute_k_correlations`` (pure statistics: R9
    feeling_events → P(negative|entity)/P(negative) lift, sample-gated by F6
    ≥10, zero LLM) and writes K_Correlation edges to the relations graph.
    ``hermes k show`` renders the current edges. Single-direction (架构 §4.7):
    reads feeling_events/entities, writes only the relations graph.
    correlation != causation (PRD 01:93).
    """
    k_parser = subparsers.add_parser(
        "k",
        help="K-domain correlation compute + show (R9 valence co-occurrence)",
        description=(
            "K-domain correlation (ADR-V6-044 / ADR-V6-056). Compute R9 "
            "feeling-event → trigger-entity valence co-occurrence (pure "
            "statistics: P(negative|entity)/P(negative) lift, sample-gated by "
            "F6 ≥10, zero LLM) and persist K_Correlation edges to the relations "
            "graph, then read them back. correlation != causation (PRD 01:93)."
        ),
    )
    k_sub = k_parser.add_subparsers(dest="k_command")

    compute = k_sub.add_parser(
        "compute", help="Compute K_Correlation edges from R9 feeling_events")
    compute.add_argument(
        "--user-id", default=None,
        help="Override the founder user id (defaults to the resolved founder)")

    show = k_sub.add_parser(
        "show", help="Show current K_Correlation edges (correlation != causation)")
    show.add_argument(
        "--user-id", default=None,
        help="Override the founder user id (defaults to the resolved founder)")

    k_parser.set_defaults(func=cmd_k)

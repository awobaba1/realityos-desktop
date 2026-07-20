"""``hermes theory`` subcommand parser (ADR-V6-051 / B3).

The Theory derivation pathway (Phase 2-B): derive the 7-PC / 5-FR skeletons from
the materialized atom + relation graph and persist each to insight_aggregation.
Handler injected (``cmd_theory``) to avoid importing ``main``.
"""

from __future__ import annotations

from typing import Callable


def build_theory_parser(subparsers, *, cmd_theory: Callable) -> None:
    """Attach the ``theory`` subcommand to ``subparsers``.

    Until ADR-V6-051 the Theory layer (ADR-V6-050) was only reachable in tests.
    ``theory derive`` is the explicit CLI entry: gather the user's atoms
    (``recent_atoms``) + relations (``relations_for_user``), run the Theory
    engine, and persist every derivation (PC→constraint_state, FR→fr_snapshot)
    to insight_aggregation with its honest ``degraded`` flag + ``basis``.
    Single-direction data flow (架构 §4.7): reads atoms/relations, writes only
    insight_aggregation.
    """
    theory_parser = subparsers.add_parser(
        "theory",
        help="Derive PC/FR skeletons from the atom graph and persist insights",
        description=(
            "Theory derivation + persist (ADR-V6-051 B3 / ADR-V6-050). Derive "
            "the 7 Personal-Constraint (PC) + 5 Life-Framework (FR) skeleton "
            "scores from the materialized atom + relation graph via an LLM "
            "approximation, then deterministically stamp each derivation's "
            "honest degraded flag + basis (the engine, not the LLM, decides). "
            "Writes only insight_aggregation (PC→constraint_state, "
            "FR→fr_snapshot) — never back to the atom layer."
        ),
    )
    theory_sub = theory_parser.add_subparsers(dest="theory_command")

    derive = theory_sub.add_parser(
        "derive", help="Derive PC/FR for the current period and persist")
    derive.add_argument(
        "--user-id", default=None,
        help="Override the founder user id (defaults to the resolved founder)")
    derive.add_argument(
        "--period-key", default=None,
        help="Override the period key (defaults to today's Beijing date)")

    theory_parser.set_defaults(func=cmd_theory)

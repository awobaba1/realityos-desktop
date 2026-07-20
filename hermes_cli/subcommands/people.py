"""``hermes people`` subcommand parser (ADR-V6-048 / A5).

The M-domain people roster + profile surface. Handler injected
(``cmd_people``) to avoid importing ``main``.
"""

from __future__ import annotations

from typing import Callable


def build_people_parser(subparsers, *, cmd_people: Callable) -> None:
    """Attach the ``people`` subcommand to ``subparsers``.

    A read-only view of the founder's people graph: ``list`` the people the
    founder has talked about (ordered by mention frequency), ``show`` one
    person's full M-domain profile (interactions / contexts / relations /
    emotions). Pure SQL under the hood — no LLM synthesis (honest boundary).
    """
    people_parser = subparsers.add_parser(
        "people",
        help="List / inspect people in your memory (M-domain)",
        description=(
            "People roster + profile (ADR-V6-048). `people list` shows everyone "
            "you've talked about, ordered by how often they come up. "
            "`people show <name>` aggregates one person's interactions, recent "
            "contexts, relations, and emotions — straight from the event "
            "tables, no LLM synthesis."
        ),
    )
    people_sub = people_parser.add_subparsers(dest="people_command")

    people_sub.add_parser("list", help="List people (ordered by mention_count)")

    show = people_sub.add_parser("show", help="Show one person's full profile")
    show.add_argument(
        "ref", help="Person name (or entity id) to look up")

    people_parser.set_defaults(func=cmd_people)

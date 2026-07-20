"""``hermes quark`` subcommand parser (ADR-V6-051 / B3).

The Quark extraction pathway (Phase 2-B): extract primitive quarks from a
single memo's capture text and aggregate them to the primary atoms. Handler
injected (``cmd_quark``) to avoid importing ``main``.
"""

from __future__ import annotations

from typing import Callable


def build_quark_parser(subparsers, *, cmd_quark: Callable) -> None:
    """Attach the ``quark`` subcommand to ``subparsers``.

    Until ADR-V6-051 the Quark layer (ADR-V6-049) was only reachable in tests.
    ``quark extract`` is the explicit CLI entry: load a memo, tokenize its
    effective capture text (corrected_text ?? source_text), run the quark
    extractor, and aggregate the PRIMARY kind per quark to its atom (Identity→R3,
    Meaning→R7, Feeling→R9). Failures degrade to DLQ (C7); the CLI reports the
    closed-loop outcome.
    """
    quark_parser = subparsers.add_parser(
        "quark",
        help="Extract quarks from a memo and aggregate to primary atoms",
        description=(
            "Quark extraction + aggregation (ADR-V6-051 B3 / ADR-V6-049). "
            "Extract the primitive quarks (Identity / Meaning / Feeling) from a "
            "memo's capture text, then map the PRIMARY quark of each kind to "
            "its atom (Identity→R3, Meaning→R7, Feeling→R9). C5 schema-gated, "
            "C6 logged, C7 DLQ — a bad batch degrades to [], never raises."
        ),
    )
    quark_sub = quark_parser.add_subparsers(dest="quark_command")

    extract = quark_sub.add_parser(
        "extract", help="Extract quarks from one memo and aggregate to atoms")
    extract.add_argument(
        "memo_id", help="The memo id whose capture text to extract quarks from")
    extract.add_argument(
        "--user-id", default=None,
        help="Override the founder user id (defaults to the resolved founder)")

    quark_parser.set_defaults(func=cmd_quark)

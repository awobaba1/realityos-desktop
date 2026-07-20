"""``hermes memo`` subcommand parser (ADR-V6-047 / A4).

The source-text correction + re-extraction pathway. Handler injected
(``cmd_memo``) to avoid importing ``main``.
"""

from __future__ import annotations

from typing import Callable


def build_memo_parser(subparsers, *, cmd_memo: Callable) -> None:
    """Attach the ``memo`` subcommand to ``subparsers``.

    Until ADR-V6-047 the only way to fix an ASR/typo error in a memo was to
    live with the wrong atoms it produced. ``memo correct`` records the
    corrected text (``memos.corrected_text``, source NEVER mutated — C2),
    re-runs extraction on it, retires the OLD wrong atoms only on success
    (写后删), and invalidates the affected insights.
    """
    memo_parser = subparsers.add_parser(
        "memo",
        help="Correct a memo's source text and re-extract its atoms",
        description=(
            "Source-text correction + re-extraction (ADR-V6-047). Correct an "
            "ASR/typo error in a memo; the corrected text is recorded (the "
            "original is never modified — C2), extraction re-runs on the "
            "corrected text, and the OLD wrong atoms are retired only on "
            "success (写后删 — a failed re-extraction leaves them live)."
        ),
    )
    memo_sub = memo_parser.add_subparsers(dest="memo_command")

    correct = memo_sub.add_parser(
        "correct", help="Correct a memo's text and re-extract its atoms")
    correct.add_argument("memo_id", help="The memo id to correct")
    correct.add_argument(
        "--text", required=True,
        help="The corrected source text (re-extraction runs on this)")
    correct.add_argument(
        "--expected-version", type=int, default=None,
        help="Optimistic-concurrency version; reject if the memo changed")

    memo_parser.set_defaults(func=cmd_memo)

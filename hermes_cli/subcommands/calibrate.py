"""``hermes calibrate`` subcommand parser (ADR-V6-028).

Extracted alongside the other ``hermes_cli/subcommands/*`` builders. Handler
injected (``cmd_calibrate``) to avoid importing ``main``.
"""

from __future__ import annotations

from typing import Callable


def build_calibrate_parser(subparsers, *, cmd_calibrate: Callable) -> None:
    """Attach the ``calibrate`` subcommand to ``subparsers``.

    Founder daily calibration (§11.4/§11.5): sample today's extracted atoms,
    rate each 准/不准/惊喜, and the verdicts land in ``feedback`` + "不准" atoms
    are confidence-demoted + a ``correction_rate`` quality_metric is recorded.
    """
    calibrate_parser = subparsers.add_parser(
        "calibrate",
        help="Founder daily atom calibration (准/不准/惊喜)",
        description=(
            "Sample the day's extracted atoms and rate each one. '不准' atoms are "
            "confidence-demoted (not deleted); verdicts + a correction_rate metric "
            "are recorded. The §11.5 closed-loop quality channel."
        ),
    )
    calibrate_parser.add_argument(
        "--date",
        help="Calibrate a specific day (YYYY-MM-DD); defaults to today (Asia/Shanghai).",
    )
    calibrate_parser.add_argument(
        "--limit", type=int, default=50,
        help="Max atoms to sample for the session (default: 50).",
    )
    calibrate_parser.set_defaults(func=cmd_calibrate)

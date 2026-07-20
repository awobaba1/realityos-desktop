"""``hermes task`` subcommand parser (ADR-V6-046 / A3).

The R12 explicit user pathway — the V6 analogue of danao14's ``/完成`` slash
command. Handler injected (``cmd_task``) to avoid importing ``main``.
"""

from __future__ import annotations

from typing import Callable


def build_task_parser(subparsers, *, cmd_task: Callable) -> None:
    """Attach the ``task`` subcommand to ``subparsers``.

    Until ADR-V6-046 the only way an R12 task-outcome atom existed was the LLM
    extracting one from chat. This surface lets the founder explicitly mark a
    task completed / failed / delayed (promoting the R2 row to R12 in place).
    """
    task_parser = subparsers.add_parser(
        "task",
        help="Mark a task done / failed / delayed (R12 explicit pathway)",
        description=(
            "The user-sovereign explicit task-outcome pathway (ADR-V6-046). "
            "List open tasks, then mark one completed / failed / delayed by its "
            "#N index (from `task list`) or a name fragment. The R2 row is "
            "promoted to R12 in place — no second row, no delete (C2)."
        ),
    )
    task_sub = task_parser.add_subparsers(dest="task_command")

    task_sub.add_parser("list", help="Show open tasks (pending / in_progress)")

    done = task_sub.add_parser("done", help="Mark a task completed")
    done.add_argument("ref", help="Task #N (from `task list`) or name fragment or atom id")
    done.add_argument("--note", default=None, help="Optional resolution note")

    failed = task_sub.add_parser("failed", help="Mark a task as not accomplished")
    failed.add_argument("ref", help="Task #N or name fragment or atom id")
    failed.add_argument("--note", default=None, help="Optional resolution note")

    delayed = task_sub.add_parser("delayed", help="Mark a task delayed (stays open, overdue)")
    delayed.add_argument("ref", help="Task #N or name fragment or atom id")
    delayed.add_argument("--note", default=None, help="Optional resolution note")

    task_parser.set_defaults(func=cmd_task)

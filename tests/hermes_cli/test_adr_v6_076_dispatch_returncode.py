"""ADR-V6-076 P0-1 regression: ``hermes`` dispatch must propagate handler return codes.

The 10-agent audit (subagent #4 — hermes_cli wiring) found that
``hermes_cli/main.py`` dispatched with a bare ``args.func(args)`` — the
handler's return value was discarded. ``main()`` then fell through to an
implicit ``return None``, and the entry point's ``sys.exit(main())`` turned
that into ``sys.exit(None)`` → exit 0. Every V6 handler carefully returned
``1`` on failure (memo-not-found, person-not-found, task-not-found, …) but
the shell saw exit 0, so any ``hermes <cmd> && next_step`` CI/cron chain
merrily advanced on failure. This is the canonical 永远 exit 0 骗 CI
anti-fake-green defect.

These static guards pin the fix (``return args.func(args) or 0`` +
``sys.exit(main())``) using the same whitespace-normalized anchoring as
ADR-V6-075's publish-contract guards — a reformat can't slip a regression
past them. The pre-existing ``test_adr_v6_0*`` tests all call ``cmd_X(args)``
directly and never cross the entry point, which is exactly the blind spot
that let this defect ship; these guards close it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_MAIN = Path("hermes_cli/main.py")


@pytest.fixture(scope="module")
def normalized():
    return " ".join(_MAIN.read_text(encoding="utf-8").split())


class TestDispatchPropagatesReturnCode:
    def test_dispatch_returns_handler_value(self, normalized):
        """The core fix: ``return args.func(args) or 0``. Without the
        ``return``, the handler's ``return 1`` is discarded and main() returns
        None → exit 0 on failure."""
        assert "return args.func(args) or 0" in normalized, (
            "hermes_cli/main.py dispatch must `return args.func(args) or 0`. "
            "The bare `args.func(args)` form discards the handler's return "
            "code → every command exits 0 on failure (ADR-V6-076 P0-1, the "
            "永远 exit 0 骗 CI defect surfaced by the 10-agent audit)."
        )

    def test_main_module_calls_sys_exit(self, normalized):
        """``if __name__ == '__main__'`` must wrap main() in sys.exit() so the
        return code reaches the shell when invoked via ``python -m
        hermes_cli.main``. A bare ``main()`` call discards it."""
        assert "sys.exit(main())" in normalized or "_sys.exit(main())" in normalized, (
            "__main__ must call sys.exit(main()) — a bare main() call discards "
            "the return code under `python -m hermes_cli.main` (ADR-V6-076 P0-1)."
        )

    def test_no_bare_args_func_call_remains(self, normalized):
        """Defense against a partial revert: the bare ``args.func(args)``
        without ``return`` must not be present. (Whitespace-normalized, so a
        stray ``args.func(args)`` inside a comment or the ``or 0`` line won't
        false-positive — the bare form is the exact 5-word sequence.)"""
        # The fixed form is `return args.func(args) or 0`. The buggy form is
        # `args.func(args)` on its own (no `return`, no `or 0`). After
        # whitespace normalization the fixed line reads exactly that token
        # sequence preceded by `return`, so the bare buggy token sequence
        # `args.func(args)` followed by a newline->space then `else:` must
        # not appear.
        assert "args.func(args) else" not in normalized, (
            "Found bare `args.func(args)` followed by `else:` — the dispatch "
            "regressed to discarding the handler return code (ADR-V6-076 P0-1)."
        )

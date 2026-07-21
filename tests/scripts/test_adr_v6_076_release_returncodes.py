"""ADR-V6-076 P0-2 regression: ``scripts/release.py`` must propagate failure.

The 10-agent audit (subagent #5 — release pipeline) found that
``scripts/release.py`` swallowed every failure:

* ``main()`` had bare ``return`` (no value) on each git/release failure
  path → ``sys.exit(None)`` → exit 0.
* The push-failure branch printed "Continue manually…" and then **fell
  through** to ``build_release_artifacts`` / ``gh release create``, so a
  dead push still printed "🎉 Release published!".
* ``if __name__ == "__main__": main()`` discarded the return code.

Together this is the canonical 做了没发 / 假绿 root cause of the
v2026.7.18 and v2026.7.19 releases: the publish job exited 0 while the
tag/asset was never actually pushed or uploaded.

These static guards pin the fix using the same whitespace-anchored
discipline as ADR-V6-075 / P0-1 — a reformat can't slip a regression
past them. The release script is invoked by the ``gh release`` flow and
by CI cron, so any ``release.py && downstream`` chain must see the real
exit code.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_SRC = Path("scripts/release.py")


@pytest.fixture(scope="module")
def src():
    return _SRC.read_text(encoding="utf-8")


class TestReleasePropagatesFailure:
    def test_entry_point_uses_sys_exit(self, src):
        """``__main__`` must wrap ``main()`` in ``sys.exit()`` so the return
        code reaches the shell. A bare ``main()`` call discards it."""
        m = re.search(r'if __name__ == "__main__":\s*\n(.*?)(?=\n\S|\Z)', src, re.S)
        assert m, "no __main__ block in scripts/release.py"
        block = m.group(1)
        assert re.search(r"sys\.exit\(main\(\)\)", block), (
            "scripts/release.py __main__ must call sys.exit(main()) — a bare "
            "main() call discards the failure return code (ADR-V6-076 P0-2, "
            "the 做了没发 root cause of v2026.7.18/19)."
        )

    def test_push_failure_short_circuits(self, src):
        """A dead push must ABORT. The first ``return`` keyword after the
        push-failure print must be ``return 1`` — historically the script
        fell through to build/``gh release create`` and printed
        "🎉 Release published!" on a failed push."""
        push_idx = src.find('print("    git push origin HEAD --tags")')
        assert push_idx != -1, "push-failure print not found — release.py shape changed"
        tail = src[push_idx:]
        ret_m = re.search(r"\breturn\b", tail)
        assert ret_m is not None, "no return after push-failure print — does not abort"
        snippet = tail[ret_m.start():ret_m.start() + 12]
        assert snippet.startswith("return 1"), (
            f"first return after push-failure is {snippet.strip()!r}, must be "
            f"'return 1' — a dead push must short-circuit before "
            f"build/gh-release, not fall through to "
            f"'🎉 Release published!' (ADR-V6-076 P0-2)."
        )

    @pytest.mark.parametrize(
        "marker",
        [
            "Failed to stage version files",
            "Failed to commit version bump",
            "Failed to create tag",
            "Release artifacts prepared for manual publish",
        ],
    )
    def test_failure_path_returns_nonzero(self, src, marker):
        """Each git/release failure print must be followed by ``return 1``,
        not a bare ``return`` (which yields None → exit 0)."""
        m = re.search(re.escape(marker) + r'.*\n\s*(return[^\n]*)', src)
        assert m, f"{marker!r} failure path not found in scripts/release.py"
        ret = m.group(1).strip()
        assert re.match(r"return 1\b", ret), (
            f"failure path after {marker!r} returns {ret!r} — must be "
            f"'return 1' so CI/cron see the failure (ADR-V6-076 P0-2)."
        )

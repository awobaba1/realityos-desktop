"""Regression: install.sh commit-pin must fail loud, not silently skip (C7).

The install-stamp pins the managed clone to a specific commit so the running
agent's version matches the built desktop shell. A ``--depth 1`` shallow clone
only has the branch tip; if the pinned commit is older than the tip it is not
present locally. The original pin block did::

    git fetch origin "$INSTALL_COMMIT" || true   # <-- swallows fetch failure
    git checkout --detach "$INSTALL_COMMIT"      # <-- then fails silently

On a shallow public clone the bare-SHA fetch is refused, ``|| true`` swallows
it, and the checkout fails — but under ``--stage`` mode the repository stage
still reported ``{"ok": true}``, leaving the agent at an unknown version
(version skew, the very hazard the stamp exists to prevent). That violates C7
(no silent failure).

The hardened block must:
  1. attempt a direct SHA fetch, and on refusal fall back to ``--unshallow``;
  2. guard the final checkout and ``exit 1`` (log_error) if the commit stays
     unresolvable — never report success on a skipped pin.

See ADR-V6-010 (install.sh commit-pin 留治) + RealityOS C7.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"


def _extract_pin_block() -> str:
    text = INSTALL_SH.read_text()
    match = re.search(
        r'(?P<block>if \[ -n "\$INSTALL_COMMIT" \]; then.*?Pinned checkout to commit \$INSTALL_COMMIT)',
        text,
        re.DOTALL,
    )
    assert match is not None, "commit-pin block not found in install.sh"
    return match["block"]


def test_pin_does_not_silently_swallow_fetch_failure() -> None:
    block = _extract_pin_block()
    # The old swallow-on-the-pinned-commit-fetch must be gone: the bare
    # `git fetch origin "$INSTALL_COMMIT" || true` that hid the failure.
    assert 'git fetch origin "$INSTALL_COMMIT" || true' not in block, (
        "pin fetch must not swallow failure with bare `|| true` (C7 silent failure)"
    )
    # Instead the SHA fetch is attempted inside an `if !` so refusal is acted on.
    assert 'if ! git fetch origin "$INSTALL_COMMIT"' in block


def test_pin_unshallows_when_sha_fetch_refused() -> None:
    block = _extract_pin_block()
    assert "git fetch --unshallow" in block, (
        "pin must fall back to --unshallow when a bare-SHA fetch is refused "
        "(shallow clones don't have the pinned commit)"
    )
    # unshallow fallback must come AFTER the direct SHA fetch attempt.
    sha_idx = block.find('if ! git fetch origin "$INSTALL_COMMIT"')
    unshallow_idx = block.find("git fetch --unshallow")
    assert sha_idx != -1 and unshallow_idx != -1
    assert sha_idx < unshallow_idx, "direct SHA fetch must be tried before unshallow"


def test_pin_fails_loud_when_commit_unresolvable() -> None:
    block = _extract_pin_block()
    # The final checkout must be guarded; on failure it must exit non-zero
    # (log_error + exit 1), not fall through to "Repository ready".
    assert 'if ! git checkout --detach "$INSTALL_COMMIT"' in block
    checkout_idx = block.find('if ! git checkout --detach "$INSTALL_COMMIT"')
    exit_idx = block.find("exit 1", checkout_idx)
    assert exit_idx != -1, (
        "guarded checkout must be followed by `exit 1` (C7: never report ok on a skipped pin)"
    )
    # And the failure path must log loudly (not a bare exit).
    assert "log_error" in block[checkout_idx:exit_idx]


def test_pin_logs_success_when_checkout_succeeds() -> None:
    block = _extract_pin_block()
    # Positive path: a resolvable commit logs a success line (not just silence).
    assert "Pinned checkout to commit $INSTALL_COMMIT" in block

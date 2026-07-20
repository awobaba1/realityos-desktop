"""C4 regression: conftest .pyc defense (ADR-V6-041 / F1).

Bug ID: F1 (strategy-02 T-0 — absence of sys.dont_write_bytecode is a single
point of failure for the entire credibility system).

Reproduction:
  Before the fix, tests/conftest.py did NOT set sys.dont_write_bytecode, so
  pytest imports wrote __pycache__/*.pyc into the source tree. Stale .pyc
  can be imported instead of edited source — masking the fact that a code
  change never took effect — a silent failure (C7) that lets "tests pass
  but production runs old code".

Expected (after fix):
  1. sys.dont_write_bytecode is True in the test process (conftest sets it).
  2. Importing a module does NOT emit a .pyc next to it.
  3. No .pyc/__pycache__ is TRACKED in git (the durable cross-clone invariant;
     runtime pyc in a temp CI checkout is ephemeral and gitignored).

Guarded by tests/test_conftest_pyc_defense.py — DO NOT delete without an ADR.
"""

import importlib.util
import sys
from pathlib import Path


def test_conftest_sets_dont_write_bytecode():
    """F1 reproduction: conftest must set sys.dont_write_bytecode = True."""
    assert sys.dont_write_bytecode is True, (
        "tests/conftest.py must set sys.dont_write_bytecode = True at import "
        "time. Without it, pytest imports emit __pycache__/*.pyc into the "
        "source tree, and stale bytecode can mask the fact that a code "
        "change never took effect (C7 silent failure / strategy-02 T-0)."
    )


def test_importing_module_emits_no_pyc(tmp_path):
    """F1 functional proof: importing a fresh module writes no .pyc.

    sys.dont_write_bytecode is already True in this process (conftest set it),
    so importing anything from a temp dir must not create __pycache__/.
    """
    mod_path = tmp_path / "probe_module_adr_v6_041.py"
    mod_path.write_text(
        "VALUE = 42\n",  # trivial module; would normally get a .pyc
        encoding="utf-8",
    )
    spec = importlib.util.spec_from_file_location("probe_adr_v6_041", mod_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.VALUE == 42

    # The .pyc would land in tmp_path/__pycache__/probe_module_adr_v6_041.*.pyc
    pycache = tmp_path / "__pycache__"
    assert not pycache.exists(), (
        f".pyc was emitted at {pycache} despite sys.dont_write_bytecode. "
        "F1 regression: conftest must keep sys.dont_write_bytecode = True."
    )


def test_no_pycache_committed_to_git():
    """F1 real invariant: no ``.pyc`` / ``__pycache__`` is TRACKED in git.

    This is the actual shadow risk — committed bytecode ships to every clone
    and can be imported instead of edited source, masking a code change that
    never took effect (C7 silent failure / strategy-02 T-0). The conftest
    ``sys.dont_write_bytecode`` flag (tested above) prevents the CURRENT
    process from writing pyc; this guard ensures none was ever COMMITTED.

    CI-stable by design: the earlier ``rglob("__pycache__")`` snapshot variant
    was a false-alarm factory under pytest-xdist — 8 sibling workers each
    import ``plugins/`` and write ephemeral ``__pycache__`` into the shared
    checkout, so the snapshot always found litter that isn't tracked and
    self-cleans next checkout. Tracking (``git ls-files``) is the durable,
    cross-clone invariant.
    """
    import subprocess
    repo_root = Path(__file__).resolve().parent.parent
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files"],
            capture_output=True, text=True, timeout=15, check=True,
        ).stdout
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return  # not a git checkout / git unavailable — can't enforce; skip
    tracked = [ln for ln in out.splitlines() if ln]
    bad = sorted(p for p in tracked
                 if p.endswith(".pyc") or "/__pycache__/" in p
                 or p.endswith("__pycache__") or p.startswith("__pycache__/"))
    assert not bad, (
        "Tracked .pyc/__pycache__ in git — committed bytecode can shadow live "
        f"source across every clone (F1 / C7). Remove from git with: "
        f"git rm -r --cached <paths>. Found: {bad}"
    )

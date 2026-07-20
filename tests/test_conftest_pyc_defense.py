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


def test_no_stale_pycache_in_source_tree():
    """F1 environmental guard: the plugins/ source tree under test has no
    __pycache__ dirs that would let stale bytecode shadow live source.

    This is a snapshot guard — if a prior undisciplined run littered the tree,
    this test fails loudly so it gets cleaned (find . -name __pycache__ -prune
    -exec rm -rf {} +) rather than silently shadowing edits.
    """
    root = Path(__file__).resolve().parent.parent / "plugins"
    if not root.exists():
        return  # plugins/ absent in this checkout — nothing to guard
    stale = sorted(p for p in root.rglob("__pycache__"))
    assert not stale, (
        "Found __pycache__ dirs under plugins/ — these can shadow live source "
        f"with stale bytecode (F1 / C7). Clean with: "
        f"find plugins -name __pycache__ -prune -exec rm -rf {{}} +. "
        f"Found: {[str(p.relative_to(root.parent)) for p in stale]}"
    )

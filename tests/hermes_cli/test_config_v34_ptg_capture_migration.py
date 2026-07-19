"""Regression tests for ADR-V6-024 — the V6 capture-surface observer
(``observability/ptg_capture``) must be enabled-by-default.

WHY THIS TEST EXISTS (C4 — every defect becomes a test case)
-------------------------------------------------------------
The 10-lens audit (ADR-V6-022, lens 4) found that the ``post_tool_call`` /
``pre_gateway_dispatch`` / ``on_session_end`` hooks that turn agent actions
into personal-timeline assets live in a BUNDLED plugin
(``plugins/observability/ptg_capture``). The v20→v21 opt-in migration
grandfathered only USER-installed plugins — bundled plugins ship OFF — so the
advertised "操作电脑 capture surface" was silently dead on every install
(R6/R12/R4 atoms never captured). ADR-V6-024 fixes it two ways:

  1. ``DEFAULT_CONFIG`` seeds ``plugins.enabled = ["observability/ptg_capture"]``
     so FRESH installs are honest from first write (covered by deep-merge at
     ``load_config()`` time).
  2. A v33→v34 migration adds the canonical key to ``plugins.enabled`` for
     EXISTING installs, respecting explicit-disable-wins.

These tests pin both paths so the capture surface can never silently regress
to OFF.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from hermes_cli.config import (
    DEFAULT_CONFIG,
    load_config,
    migrate_config,
    read_raw_config,
)

CAPTURE_KEY = "observability/ptg_capture"
CAPTURE_LEAF = "ptg_capture"


def _write_config(home, text):
    (home / "config.yaml").write_text(text)


@pytest.fixture
def hermes_home(tmp_path):
    with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
        yield tmp_path


class TestDefaultSeed:
    def test_default_config_version_bumped_to_34(self):
        assert DEFAULT_CONFIG.get("_config_version") == 34

    def test_default_seeds_capture_observer_enabled(self):
        # The DEFAULT must seed the canonical path-derived key (matching what
        # `hermes plugins enable observability/ptg_capture` writes), so a fresh
        # install's deep-merged config has the capture surface on.
        enabled = DEFAULT_CONFIG.get("plugins", {}).get("enabled", [])
        assert CAPTURE_KEY in enabled

    def test_fresh_install_loads_capture_observer(self, hermes_home):
        # No config.yaml at all → load_config() deep-merges DEFAULT_CONFIG →
        # the capture observer is enabled out of the box.
        assert not (hermes_home / "config.yaml").exists()
        merged = load_config()
        enabled = set(merged.get("plugins", {}).get("enabled", []))
        assert CAPTURE_KEY in enabled


class TestV33ToV34Migration:
    def test_enables_capture_observer_for_empty_enabled_list(self, hermes_home):
        # The most common real-world install: v21 migration left an explicit
        # empty enabled list (user has no user-installed plugins).
        _write_config(hermes_home, "_config_version: 33\nplugins:\n  enabled: []\n  disabled: []\n")
        migrate_config(quiet=True)
        merged = load_config()
        assert CAPTURE_KEY in set(merged.get("plugins", {}).get("enabled", []))
        assert merged.get("_config_version") == 34

    def test_adds_capture_observer_alongside_user_plugins(self, hermes_home):
        _write_config(
            hermes_home,
            "_config_version: 33\nplugins:\n  enabled:\n    - some_user_plugin\n",
        )
        migrate_config(quiet=True)
        enabled = set(load_config().get("plugins", {}).get("enabled", []))
        assert CAPTURE_KEY in enabled
        assert "some_user_plugin" in enabled  # existing plugins preserved

    def test_creates_plugins_section_when_absent(self, hermes_home):
        # An install with no plugins key at all still ends up with the capture
        # observer enabled (DEFAULT merge supplies it).
        _write_config(hermes_home, "_config_version: 33\n")
        migrate_config(quiet=True)
        assert CAPTURE_KEY in set(load_config().get("plugins", {}).get("enabled", []))

    def test_respects_explicit_disable_canonical_key(self, hermes_home):
        # Explicit-disable wins: the migration must NOT add the key to the
        # persisted enabled list when the user disabled it by canonical key.
        _write_config(
            hermes_home,
            "_config_version: 33\nplugins:\n  enabled: []\n  disabled:\n    - "
            + CAPTURE_KEY
            + "\n",
        )
        migrate_config(quiet=True)
        raw = read_raw_config()
        persisted_enabled = set(raw.get("plugins", {}).get("enabled", []))
        assert CAPTURE_KEY not in persisted_enabled

    def test_respects_explicit_disable_bare_leaf(self, hermes_home):
        # Same, but disabled via the legacy bare leaf name.
        _write_config(
            hermes_home,
            "_config_version: 33\nplugins:\n  enabled: []\n  disabled:\n    - "
            + CAPTURE_LEAF
            + "\n",
        )
        migrate_config(quiet=True)
        raw = read_raw_config()
        persisted_enabled = set(raw.get("plugins", {}).get("enabled", []))
        assert CAPTURE_KEY not in persisted_enabled

    def test_migration_is_idempotent(self, hermes_home):
        _write_config(hermes_home, "_config_version: 33\nplugins:\n  enabled: []\n")
        migrate_config(quiet=True)
        migrate_config(quiet=True)  # second run must not duplicate or error
        enabled = load_config().get("plugins", {}).get("enabled", [])
        assert enabled.count(CAPTURE_KEY) == 1
        assert load_config().get("_config_version") == 34

    def test_already_v34_is_left_unchanged(self, hermes_home):
        # An install already at v34 with the key present is not touched.
        _write_config(
            hermes_home,
            "_config_version: 34\nplugins:\n  enabled:\n    - " + CAPTURE_KEY + "\n",
        )
        migrate_config(quiet=True)
        enabled = load_config().get("plugins", {}).get("enabled", [])
        assert enabled.count(CAPTURE_KEY) == 1

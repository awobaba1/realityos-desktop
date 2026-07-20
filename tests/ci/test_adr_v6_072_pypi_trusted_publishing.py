"""C4 regression: PyPI workflow stays OIDC trusted-publishing (ADR-V6-072).

The fourth-round audit found ``upload_to_pypi.yml`` failing with
``invalid-publisher`` — pypi.org has no matching Trusted Publisher registered
(a USER-SIDE config gap, not a code bug). The workflow itself is correctly
configured for OIDC trusted publishing (no API token).

This static guard prevents the WRONG fix: someone "making CI green" by injecting
a ``PYPI_API_TOKEN`` secret (which would drag a long-lived credential into CI,
violating ADR-403's key-hygiene rule). The correct fix is registering the
publisher on pypi.org (ADR-V6-072 D1). Red-on-OIDC-misconfig is honest; green-
via-token-injection is a real regression.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_WORKFLOW = Path(".github/workflows/upload_to_pypi.yml")


@pytest.fixture(scope="module")
def yaml_text():
    return _WORKFLOW.read_text(encoding="utf-8")


class TestPypiWorkflowStaysOidc:
    def test_publish_job_uses_id_token_write(self, yaml_text):
        """OIDC trusted publishing requires ``id-token: write`` on the publish
        job. Its absence means the workflow can no longer do trusted publishing
        at all (the ADR-V6-072 contract)."""
        assert "id-token: write" in yaml_text

    def test_publish_job_targets_pypi_environment(self, yaml_text):
        """The registered Trusted Publisher is scoped to the ``pypi``
        environment (ADR-V6-072 D1). Renaming it would break the publisher
        match even after the user registers."""
        assert "environment:" in yaml_text
        assert "name: pypi" in yaml_text

    @pytest.mark.parametrize("forbidden", [
        "PYPI_API_TOKEN", "pypi_token", "API_TOKEN", "password:",
    ])
    def test_no_long_lived_token_injection(self, yaml_text, forbidden):
        """The WRONG fix for the red is injecting a long-lived API token.
        That violates ADR-403 (key hygiene) and defeats trusted publishing.
        Whitespace-normalized so a reformatted regression can't slip past."""
        normalized = " ".join(yaml_text.split()).lower()
        assert forbidden.lower() not in normalized, (
            f"upload_to_pypi.yml references {forbidden!r} — long-lived token "
            f"injection violates ADR-403. Fix via pypi.org Trusted Publisher "
            f"registration (ADR-V6-072 D1), NOT a CI secret.")

    def test_uses_pypa_trusted_publish_action(self, yaml_text):
        """Pins the canonical trusted-publishing action (not a fork that
        might silently accept a token fallback)."""
        assert "pypa/gh-action-pypi-publish" in yaml_text

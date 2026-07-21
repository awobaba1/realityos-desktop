"""C4 regression: desktop-build publish job contract (ADR-V6-075 / ADR-V6-037).

The fifth-round C4 audit (``fix()`` commit → regression-test coverage) found
every CODE-level bug-fix either co-commits a test or is covered by the
``test_startup_plugin_gating`` bidirectional invariant. The one test_files=0
case was ``f9dad30ac`` — a CI fix adding ``GH_REPO: ${{ github.repository }}``
to the desktop-build publish job (without it ``gh`` can't infer the repo from
a git remote → "not a git repository" → the publish job silently fails to
attach artifacts → ADR-V6-037's most-fatal fake-green: work done but never
shipped to the download page).

That fix had NO regression test. This static guard is it: it pins the
autopublish contract so a future workflow refactor can't silently drop
``GH_REPO`` or the upload step and reintroduce 做了没发. Whitespace-normalized
so a YAML reformat can't slip a regression past it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_WORKFLOW = Path(".github/workflows/desktop-build.yml")


@pytest.fixture(scope="module")
def yaml_text():
    return _WORKFLOW.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def normalized(yaml_text):
    # Whitespace-normalize so a reformatted workflow can't defeat substring guards.
    return " ".join(yaml_text.split())


class TestPublishJobContract:
    def test_publish_job_exists_and_tag_gated(self, normalized):
        """ADR-V6-037: a ``publish`` job that runs ONLY on release tags. If it
        ran on every push it'd either no-op or pollute; if the tag gate is
        dropped, manual dispatch builds could mis-publish."""
        assert "publish:" in normalized
        assert "startsWith(github.ref, 'refs/tags/v')" in normalized

    def test_publish_job_has_contents_write(self, normalized):
        """Creating/uploading a release requires ``contents: write``. Dropping
        it makes gh release upload 403 — another silent 做了没发 path."""
        assert "contents: write" in normalized

    def test_publish_job_sets_gh_repo(self, normalized):
        """The f9dad30ac fix: the publish job has NO checkout, so ``gh`` can't
        infer the repo from a git remote. ``GH_REPO: ${{ github.repository }}``
        is the explicit pointer. Without it → 'not a git repository' → upload
        fails → installers never reach the Release (做了没发). This is the
        single assertion that locks the original defect as a regression."""
        assert "GH_REPO:" in normalized
        assert "${{ github.repository }}" in normalized

    def test_publish_job_attaches_installers(self, normalized):
        """The actual ship step: ``gh release upload`` of the dmg + exe. If this
        is removed/renamed, the build succeeds and artifacts upload-as-CI-
        artifacts but never reach the Release download page (ADR-V6-037)."""
        assert "gh release upload" in normalized
        assert ".dmg" in normalized
        assert ".exe" in normalized

    def test_publish_job_creates_release_idempotently(self, normalized):
        """``gh release create`` guarded by ``gh release view`` → idempotent:
        re-runs (retried tag push, manual re-trigger) attach rather than crash.
        Removing the create branch means a tag without a pre-existing Release
        can't be published at all."""
        assert "gh release create" in normalized
        assert "gh release view" in normalized


class TestNoTokenInjectionInPublish:
    """The publish job uses the built-in ``GITHUB_TOKEN`` (short-lived,
    per-run). Guard against someone 'fixing' a permission issue by injecting a
    long-lived PAT — that violates ADR-403 key hygiene, same risk class as the
    PyPI token guard in ADR-V6-072.

    Anchored on the actual risk (a ``${{ secrets.X }}`` reference where X is
    not GITHUB_TOKEN) rather than a bare substring blacklist — ``PAT`` appears
    inside PATCH/PATH/COMPAT in legitimate comments, so a substring guard
    would false-positive (the wrap-lucky-green failure mode in reverse)."""

    def test_every_secret_reference_is_github_token(self, yaml_text):
        import re
        refs = re.findall(r"\$\{\{\s*secrets\.([A-Za-z_][A-Za-z0-9_]*)", yaml_text)
        assert refs, (
            "desktop-build.yml references no secrets at all — expected at least "
            "secrets.GITHUB_TOKEN for the publish job (ADR-V6-037).")
        non_default = [r for r in refs if r != "GITHUB_TOKEN"]
        assert not non_default, (
            f"desktop-build.yml references non-default secrets: {non_default}. "
            f"A long-lived PAT/RELEASE_TOKEN violates ADR-403. The publish job "
            f"must use only the built-in GITHUB_TOKEN (ADR-V6-037).")

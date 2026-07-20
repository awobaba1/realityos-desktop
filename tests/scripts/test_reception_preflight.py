"""Unit tests for scripts/reception_preflight.py (ADR-V6-038 D1).

Covers the four CHECK surfaces + main exit-code, with gh/urllib mocked so the
suite is hermetic (no network, no real gh). C7: every red path must surface a
reason, never silently pass.
"""

from __future__ import annotations

import http.client
import io
import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

import scripts.reception_preflight as preflight


# ---------- check_release_assets ----------


def test_release_assets_ok():
    payload = {
        "tagName": "v2026.7.21",
        "targetCommitish": "main",
        "assets": [
            {"name": "RealityOS-0.17.0-mac-arm64.dmg", "size": 138453804},
            {"name": "RealityOS-0.17.0-win-x64.exe", "size": 118769468},
        ],
    }
    with patch.object(preflight, "_gh_json", return_value=(True, payload, "")):
        r = preflight.check_release_assets("v2026.7.21")
    assert r.ok, r.detail
    assert "dmg+exe" in r.name


def test_release_assets_missing_exe_is_red():
    payload = {"assets": [{"name": "RealityOS-0.17.0-mac-arm64.dmg", "size": 138453804}]}
    with patch.object(preflight, "_gh_json", return_value=(True, payload, "")):
        r = preflight.check_release_assets("vX")
    assert not r.ok
    assert "缺" in r.detail or "≥2" in r.detail


def test_release_assets_gh_failure_is_red():
    with patch.object(preflight, "_gh_json", return_value=(False, None, "gh failed")):
        r = preflight.check_release_assets("vX")
    assert not r.ok
    assert "gh failed" in r.detail


# ---------- check_asset_sizes ----------


def test_asset_sizes_all_above_floor_ok():
    payload = {
        "assets": [
            {"name": "a.dmg", "size": preflight.MIN_ASSET_BYTES},
            {"name": "a.exe", "size": preflight.MIN_ASSET_BYTES + 1},
        ]
    }
    with patch.object(preflight, "_gh_json", return_value=(True, payload, "")):
        r = preflight.check_asset_sizes("vX")
    assert r.ok, r.detail


def test_asset_sizes_tiny_file_is_red():
    """反 20B 假备份教训:空/畸资产必须红。"""
    payload = {"assets": [{"name": "a.dmg", "size": 20}, {"name": "a.exe", "size": 118769468}]}
    with patch.object(preflight, "_gh_json", return_value=(True, payload, "")):
        r = preflight.check_asset_sizes("vX")
    assert not r.ok
    assert "a.dmg" in r.detail


# ---------- check_install_sh_reachable ----------


def _mock_urlopen(status: int, body_len: int = 4096) -> MagicMock:
    cm = MagicMock()
    cm.__enter__.return_value.status = status
    cm.__enter__.return_value.read.return_value = b"x" * body_len
    return cm


def test_install_sh_reachable_ok():
    with patch.object(preflight, "_release_commitish", return_value="abc123def456"), \
         patch("urllib.request.urlopen", return_value=_mock_urlopen(200)):
        r = preflight.check_install_sh_reachable("vX")
    assert r.ok, r.detail
    assert "abc123de" in r.name  # short commit in name


def test_install_sh_404_is_red():
    with patch.object(preflight, "_release_commitish", return_value="abc123"), \
         patch("urllib.request.urlopen", side_effect=urllib.error.HTTPError("u", 404, "nf", {}, io.BytesIO(b""))):
        r = preflight.check_install_sh_reachable("vX")
    assert not r.ok
    assert "404" in r.detail


def test_install_sh_no_commit_is_red():
    with patch.object(preflight, "_release_commitish", return_value=None):
        r = preflight.check_install_sh_reachable("vX")
    assert not r.ok


def test_install_sh_remote_disconnected_is_red_not_crash():
    """C4 回归:RemoteDisconnected(ConnectionError/OSError)逃逸 URLError 曾致崩。"""
    exc = http.client.RemoteDisconnected("Remote end closed connection without response")
    with patch.object(preflight, "_release_commitish", return_value="abc123"), \
         patch("urllib.request.urlopen", side_effect=exc):
        r = preflight.check_install_sh_reachable("vX")
    assert not r.ok
    assert "RemoteDisconnected" in r.detail


def test_public_download_remote_disconnected_is_red_not_crash():
    """C4 回归:check_public_download 同样不能因断连崩。"""
    exc = http.client.RemoteDisconnected("closed")
    payload = {"assets": [{"name": "a.dmg", "size": 138453804, "url": "https://x/a.dmg"}]}
    with patch.object(preflight, "_gh_json", return_value=(True, payload, "")), \
         patch("urllib.request.urlopen", side_effect=exc):
        r = preflight.check_public_download("vX")
    assert not r.ok
    assert "RemoteDisconnected" in r.detail


# ---------- check_local_readiness ----------


def test_local_readiness_ok_with_key(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / ".env").write_text("DEEPSEEK_API_KEY=sk-test\n", encoding="utf-8")
    (tmp_path / "config.yaml").write_text("model: glm-5.2\n", encoding="utf-8")
    r = preflight.check_local_readiness()
    assert r.ok, r.detail


def test_local_readiness_red_without_key(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("model: glm-5.2\n", encoding="utf-8")
    r = preflight.check_local_readiness()
    assert not r.ok
    assert "provider key" in r.detail or "LLM" in r.detail


def test_local_readiness_invalid_config_is_red(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / ".env").write_text("DEEPSEEK_API_KEY=sk-test\n", encoding="utf-8")
    (tmp_path / "config.yaml").write_text("- not-a-mapping\n", encoding="utf-8")
    r = preflight.check_local_readiness()
    assert not r.ok
    assert "config" in r.detail


# ---------- main exit-code ----------


def test_main_all_green_exits_zero(capsys):
    with patch.object(preflight, "resolve_tag", return_value=(True, "v2026.7.21", "")), \
         patch.object(preflight, "check_release_assets", return_value=preflight.CheckResult("a", True, "d")), \
         patch.object(preflight, "check_asset_sizes", return_value=preflight.CheckResult("b", True, "d")), \
         patch.object(preflight, "check_install_sh_reachable", return_value=preflight.CheckResult("c", True, "d")), \
         patch.object(preflight, "check_local_readiness", return_value=preflight.CheckResult("e", True, "d")):
        rc = preflight.main([])
    assert rc == 0


def test_main_one_red_exits_one():
    with patch.object(preflight, "resolve_tag", return_value=(True, "vX", "")), \
         patch.object(preflight, "check_release_assets", return_value=preflight.CheckResult("a", True, "")), \
         patch.object(preflight, "check_asset_sizes", return_value=preflight.CheckResult("b", False, "tiny")), \
         patch.object(preflight, "check_install_sh_reachable", return_value=preflight.CheckResult("c", True, "")), \
         patch.object(preflight, "check_local_readiness", return_value=preflight.CheckResult("e", True, "")):
        rc = preflight.main([])
    assert rc == 1


def test_main_tag_unresolvable_exits_one():
    with patch.object(preflight, "resolve_tag", return_value=(False, None, "no gh")):
        rc = preflight.main([])
    assert rc == 1

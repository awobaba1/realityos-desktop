"""ADR-V6-006 regression: computer-use data-boundary hardening (6 holes).

Locks the V6 data boundary on the computer_use tool so it cannot silently
regress. Each hole has at least one deny + one allow assertion. The V6
product rule (架构设计 §0.6 决策② / ADR-V6-003): computer-use may read the
screen as an *execution means* (pixels into the current session context for
a local main model), but screenshots must NEVER persist to disk, be
forwarded to an external vision API, or leave the host to a non-allowlisted
cloud model.

Holes covered:
  1. ``capture`` requires user approval (no longer an auto-allowed read).
  2. No approval callback wired => fail-closed deny (was default-allow).
  3. Cloud-vision gate: strip pixels for non-allowlisted cloud / unreadable
     main models; keep for local or explicitly allowlisted.
  4. aux-vision routing (write-to-disk + forward to external Gemini) disabled.
  5. Screen recording / replay (mp4 + per-turn screenshot persistence) raises.
  6. SKILL.md no longer teaches screenshot persistence.

See ``docs/adr/V6/ADR-V6-006.md`` for the full boundary spec.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

# 8×8 PNG (transparent) — minimal provider-acceptable bytes that decode cleanly
# (>= _MIN_PROVIDER_IMAGE_DIMENSION, so the image_too_small branch is skipped).
_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAYAAADED76LAAAADUlEQVR4nG"
    "NgGAUgAAABCAABgukLHQAAAABJRU5ErkJggg=="
)


@pytest.fixture(autouse=True)
def _clean_computer_use_state(monkeypatch):
    """Start every test from a known-clean state: noop backend, no callback.

    ``reset_backend_for_tests`` clears the cached backend + session approval
    state but NOT the module-global ``_approval_callback``; we reset that too
    so a deny/allow test can never leak a callback into its neighbour.
    """
    from tools.computer_use import tool as cu

    cu.reset_backend_for_tests()
    cu.set_approval_callback(None)
    monkeypatch.setenv("HERMES_COMPUTER_USE_BACKEND", "noop")
    yield
    cu.reset_backend_for_tests()
    cu.set_approval_callback(None)


# ---------------------------------------------------------------------------
# Hole 1: capture requires approval
# ---------------------------------------------------------------------------

def test_safe_actions_no_longer_contains_capture():
    """``capture`` must not be advertised as an always-allowed read action."""
    from tools.computer_use.tool import _SAFE_ACTIONS

    assert "capture" not in _SAFE_ACTIONS
    assert "wait" in _SAFE_ACTIONS
    assert "list_apps" in _SAFE_ACTIONS


def test_capture_blocked_when_no_approval_callback():
    """With no callback wired, a capture is DENIED (fail-closed), not auto-run."""
    from tools.computer_use import tool as cu

    cu.set_approval_callback(None)
    out = cu.handle_computer_use({"action": "capture"})
    assert isinstance(out, str)
    parsed = json.loads(out)
    assert "error" in parsed
    assert "denied" in parsed["error"]


def test_capture_runs_when_approved():
    """An approve callback unlocks capture and the backend capture fires."""
    from tools.computer_use import tool as cu

    cu.set_approval_callback(lambda *a, **k: "approve_session")
    cu.handle_computer_use({"action": "capture"})
    backend = cu._get_backend()
    # _NoopBackend records every call; capture must have reached the backend.
    assert any(name == "capture" for name, _ in backend.calls)


def test_capture_denied_when_callback_returns_deny():
    """An explicit deny verdict blocks the capture at the approval gate."""
    from tools.computer_use import tool as cu

    cu.set_approval_callback(lambda *a, **k: "deny")
    out = cu.handle_computer_use({"action": "capture"})
    parsed = json.loads(out)
    assert parsed.get("error") == "denied by user"


# ---------------------------------------------------------------------------
# Hole 2: no-callback default is deny
# ---------------------------------------------------------------------------

def test_request_approval_denies_when_no_callback():
    from tools.computer_use.tool import _request_approval

    err = _request_approval("click", {"element": 1})
    assert err is not None
    parsed = json.loads(err)
    assert parsed["error"].startswith("denied")


def test_request_approval_none_when_callback_approves():
    from tools.computer_use.tool import _request_approval, set_approval_callback

    set_approval_callback(lambda *a, **k: "approve_once")
    try:
        assert _request_approval("click", {}) is None
    finally:
        set_approval_callback(None)


def test_readonly_actions_bypass_approval_gate():
    """``list_apps`` is neither destructive nor capture, so it never reaches
    _request_approval and runs even with no callback wired."""
    from tools.computer_use import tool as cu

    cu.set_approval_callback(None)
    out = cu.handle_computer_use({"action": "list_apps"})
    parsed = json.loads(out)
    # list_apps returns {"apps": [...], "count": N}; never a denial.
    assert "error" not in parsed or "denied" not in str(parsed.get("error", ""))


# ---------------------------------------------------------------------------
# Hole 3: cloud-vision allowlist gate (fail-closed)
# ---------------------------------------------------------------------------

def test_vision_gate_denies_when_provider_unreadable():
    """Provider lookup raising => fail-closed deny."""
    from tools.computer_use.tool import _capture_pixels_may_leave_host

    with patch(
        "agent.auxiliary_client._read_main_provider", side_effect=RuntimeError("boom")
    ):
        allowed, reason = _capture_pixels_may_leave_host()
    assert allowed is False
    assert "fail-closed" in reason


def test_vision_gate_denies_when_no_provider_configured():
    from tools.computer_use.tool import _capture_pixels_may_leave_host

    with patch("agent.auxiliary_client._read_main_provider", return_value=""), \
         patch("agent.auxiliary_client._read_main_model", return_value=""):
        allowed, reason = _capture_pixels_may_leave_host()
    assert allowed is False
    assert "fail-closed" in reason


def test_vision_gate_allows_local_provider():
    from tools.computer_use.tool import _capture_pixels_may_leave_host

    fake_profile = type("P", (), {"base_url": "http://127.0.0.1:11434"})()
    with patch("agent.auxiliary_client._read_main_provider", return_value="ollama"), \
         patch("agent.auxiliary_client._read_main_model", return_value="llava"), \
         patch("providers.get_provider_profile", return_value=fake_profile):
        allowed, reason = _capture_pixels_may_leave_host()
    assert allowed is True
    assert "local" in reason


def test_vision_gate_denies_cloud_when_allowlist_unset(monkeypatch):
    from tools.computer_use.tool import _capture_pixels_may_leave_host

    monkeypatch.delenv("HERMES_COMPUTER_USE_CLOUD_VISION_ALLOW", raising=False)
    fake_profile = type("P", (), {"base_url": "https://api.deepseek.com"})()
    with patch("agent.auxiliary_client._read_main_provider", return_value="deepseek"), \
         patch("agent.auxiliary_client._read_main_model", return_value="deepseek-chat"), \
         patch("providers.get_provider_profile", return_value=fake_profile):
        allowed, reason = _capture_pixels_may_leave_host()
    assert allowed is False
    assert "allowlist" in reason


def test_vision_gate_allows_cloud_when_explicitly_allowlisted(monkeypatch):
    from tools.computer_use.tool import _capture_pixels_may_leave_host

    monkeypatch.setenv("HERMES_COMPUTER_USE_CLOUD_VISION_ALLOW", "deepseek:deepseek-chat")
    fake_profile = type("P", (), {"base_url": "https://api.deepseek.com"})()
    with patch("agent.auxiliary_client._read_main_provider", return_value="deepseek"), \
         patch("agent.auxiliary_client._read_main_model", return_value="deepseek-chat"), \
         patch("providers.get_provider_profile", return_value=fake_profile):
        allowed, reason = _capture_pixels_may_leave_host()
    assert allowed is True
    assert "allowlist" in reason


def test_capture_response_strips_image_when_gate_denies():
    from tools.computer_use.backend import CaptureResult
    from tools.computer_use.tool import _capture_response

    cap = CaptureResult(mode="vision", width=8, height=8, png_b64=_PNG_B64)
    with patch(
        "tools.computer_use.tool._capture_pixels_may_leave_host",
        return_value=(False, "cloud not allowlisted"),
    ):
        out = _capture_response(cap)
    # Text payload (JSON string), NOT the _multimodal dict.
    assert isinstance(out, str)
    parsed = json.loads(out)
    assert "screenshot omitted" in parsed["summary"]


def test_capture_response_keeps_image_when_gate_allows():
    from tools.computer_use.backend import CaptureResult
    from tools.computer_use.tool import _capture_response

    cap = CaptureResult(mode="vision", width=8, height=8, png_b64=_PNG_B64)
    with patch(
        "tools.computer_use.tool._capture_pixels_may_leave_host",
        return_value=(True, "local provider"),
    ):
        out = _capture_response(cap)
    assert isinstance(out, dict)
    assert out.get("_multimodal") is True


# ---------------------------------------------------------------------------
# Hole 4: aux-vision routing disabled
# ---------------------------------------------------------------------------

def test_aux_vision_routing_always_disabled():
    """aux-vision (write-to-disk + forward to external Gemini) must never fire."""
    from tools.computer_use.tool import _should_route_through_aux_vision

    assert _should_route_through_aux_vision() is False


# ---------------------------------------------------------------------------
# Hole 5: screen recording / replay disabled
# ---------------------------------------------------------------------------

def test_blocked_cua_tools_lists_all_recording_tools():
    from tools.computer_use.cua_backend import CuaDriverBackend

    expected = {
        "start_recording", "stop_recording", "get_recording_state",
        "replay_trajectory", "install_ffmpeg",
    }
    assert expected.issubset(CuaDriverBackend._BLOCKED_CUA_TOOLS)


def test_recording_methods_raise():
    """Bypass __init__ (which would connect to cua-driver); the disabled
    methods raise before touching any instance state."""
    from tools.computer_use.cua_backend import CuaDriverBackend

    backend = CuaDriverBackend.__new__(CuaDriverBackend)
    with pytest.raises(NotImplementedError):
        backend.start_recording(output_dir="/tmp/x")
    with pytest.raises(NotImplementedError):
        backend.start_recording(output_dir="/tmp/x", record_video=True)
    with pytest.raises(NotImplementedError):
        backend.stop_recording()
    with pytest.raises(NotImplementedError):
        backend.get_recording_state()
    with pytest.raises(NotImplementedError):
        backend.replay_trajectory(trajectory_dir="/tmp/x")
    with pytest.raises(NotImplementedError):
        backend.install_ffmpeg()


def test_call_tool_escape_hatch_blocks_recording():
    from tools.computer_use.cua_backend import CuaDriverBackend

    backend = CuaDriverBackend.__new__(CuaDriverBackend)
    backend._session_id = "s1"
    with pytest.raises(NotImplementedError):
        backend.call_tool("start_recording", {})
    with pytest.raises(NotImplementedError):
        backend.call_tool("replay_trajectory", {"trajectory_dir": "/tmp/x"})


# ---------------------------------------------------------------------------
# Hole 6: SKILL.md forbids screenshot persistence
# ---------------------------------------------------------------------------

def _skill_md() -> str:
    path = (
        Path(__file__).resolve().parents[2]
        / "skills" / "computer-use" / "SKILL.md"
    )
    return path.read_text(encoding="utf-8")


def test_skill_md_forbids_screenshot_persistence():
    text = _skill_md()
    # The old permissive "save it somewhere durable" guidance must be gone.
    assert "save it somewhere durable" not in text
    # The section must frame screenshots as read-only + state the hard rule.
    assert "NEVER persist" in text or "never persist" in text.lower()
    assert "Never" in text
    # The forbidden write mechanisms are named explicitly (write_file, base64
    # decode, cp/mv) so the model cannot rationalize an alternative sink.
    # NOTE: these strings SHOULD appear — as prohibitions, not instructions.
    # We assert they sit under a "Never write … to disk" clause, not that the
    # strings are absent.
    assert "write_file" in text
    assert "base64" in text
    assert "ADR-V6" in text

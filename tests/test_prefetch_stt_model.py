"""ADR-V6-031: prefetch_stt_model.py fail-open contract + opt-in wiring.

Pins the C7 honesty guarantee: the STT model pre-fetch NEVER aborts the installer
and NEVER fails silently — on any error it exits 0 AND prints an explicit stderr
note naming the lazy-load fallback. No real download occurs (faster_whisper is
faked via sys.modules). Also asserts the installer wiring is OPT-IN: the
``stt-model`` stage exists as a dispatch case but is NOT in the default manifest
run-sequence (so default install size/time is unchanged).
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO / "scripts" / "prefetch_stt_model.py"


def _load_prefetch_module():
    spec = importlib.util.spec_from_file_location("_prefetch_under_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fake_faster_whisper(*, whisper_side_effect=None):
    fw = types.ModuleType("faster_whisper")

    def WhisperModel(model, **kwargs):
        if whisper_side_effect is not None:
            raise whisper_side_effect
        return object()

    fw.WhisperModel = WhisperModel
    return fw


class TestPrefetchFailOpen:
    def test_success_returns_zero_and_announces(self, capsys, monkeypatch):
        monkeypatch.setitem(sys.modules, "faster_whisper",
                            _fake_faster_whisper(whisper_side_effect=None))
        mod = _load_prefetch_module()
        assert mod.main() == 0
        assert "pre-fetched" in capsys.readouterr().out

    def test_whispermodel_failure_is_fail_open_with_fallback_note(self, capsys, monkeypatch):
        # Network/disk failure during model load → exit 0 + explicit stderr note.
        monkeypatch.setitem(sys.modules, "faster_whisper",
                            _fake_faster_whisper(whisper_side_effect=RuntimeError("network down")))
        mod = _load_prefetch_module()
        assert mod.main() == 0
        err = capsys.readouterr().err
        assert "did not complete" in err
        assert "lazy-download" in err or "lazy-load" in err  # names the fallback

    def test_lazy_ensure_failure_is_fail_open(self, capsys, monkeypatch):
        # faster_whisper absent + lazy install blows up → still exit 0 + note.
        monkeypatch.setitem(sys.modules, "faster_whisper", None)  # ImportError on import
        mod = _load_prefetch_module()
        with patch("tools.lazy_deps.ensure", side_effect=RuntimeError("pip network fail")):
            assert mod.main() == 0
        assert "did not complete" in capsys.readouterr().err


class TestOptInWiring:
    """The stt-model stage must be OPT-IN: dispatchable but not in the default
    manifest (default install must not gain ~500MB of optional STT deps)."""

    def _install_sh(self) -> str:
        return (_REPO / "scripts" / "install.sh").read_text()

    def test_install_sh_has_stt_model_dispatch_case(self):
        sh = self._install_sh()
        # The stage dispatch case exists so `hermes install --stage stt-model` works.
        assert "stt-model)" in sh, "install.sh missing stt-model stage dispatch case"
        assert "prefetch_stt_model" in sh, "install.sh missing prefetch_stt_model helper call"

    def test_install_sh_manifest_excludes_stt_model(self):
        sh = self._install_sh()
        # The --manifest JSON (default desktop bootstrap run-sequence) must NOT
        # list stt-model — it's opt-in only. Find the manifest printf line.
        manifest_lines = [ln for ln in sh.splitlines() if '"name":"prerequisites"' in ln]
        assert manifest_lines, "could not locate install.sh manifest printf"
        manifest = "".join(manifest_lines)
        assert '"name":"stt-model"' not in manifest, (
            "stt-model must NOT be in the default manifest (opt-in only); "
            "adding it would bloat every install with optional STT deps"
        )

    def _install_ps1(self) -> str:
        return (_REPO / "scripts" / "install.ps1").read_text()

    def test_install_ps1_stt_model_is_opt_in(self):
        ps1 = self._install_ps1()
        # stt-model lives in $OptInStages (dispatchable via -Stage), NOT in the
        # default $InstallStages run-sequence that drives -Manifest / Invoke-AllStages.
        assert "Stage-SttModel" in ps1, "install.ps1 missing Stage-SttModel worker"
        assert "$OptInStages = @(" in ps1, "install.ps1 missing OptInStages array"
        assert '"stt-model"' in ps1, "install.ps1 missing stt-model stage name"
        # Get-InstallStage must consult $OptInStages so -Stage stt-model resolves.
        gis = ps1.split("function Get-InstallStage", 1)[1].split("function ", 1)[0]
        assert "OptInStages" in gis, "Get-InstallStage must check OptInStages"
        # The manifest pipeline maps $InstallStages only (not $OptInStages) →
        # stt-model is absent from the default manifest.
        assert "$InstallStages | ForEach-Object" in ps1, "manifest must map InstallStages"

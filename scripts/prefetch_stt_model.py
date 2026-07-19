#!/usr/bin/env python3
"""ADR-V6-031: best-effort pre-fetch of the local STT model (faster-whisper base).

Called by the installer's OPT-IN ``stt-model`` stage (``hermes install --stage
stt-model``) so users who know they want local STT can pre-download the ~150MB
model + heavy ``stt.faster_whisper`` extra at a time of their choosing, instead
of hitting a surprise download on first transcription.

Why opt-in (not default): ``stt.faster_whisper`` pulls faster-whisper +
ctranslate2 + tokenizers + onnxruntime + huggingface_hub (~500MB+ of packages)
plus the 150MB model. Forcing that on every install would bloat installs for
users who never use local STT and undo the deliberate lazy-install design
(``_try_lazy_install_stt`` / ``lazy_deps.ensure``). The default first-use path
already logs clearly (transcription_tools.py "first load downloads the model").

Fail-open contract (C7 — no silent failure): on ANY failure (network, disk,
missing build tools) this exits 0 AND prints an explicit stderr note describing
what happened + the lazy-load fallback. It never aborts the installer stage,
and it never fails silently. The transcription path lazy-loads on first use as
the guaranteed fallback.
"""
from __future__ import annotations

import os
import sys

# Make ``tools`` importable when run as ``python scripts/prefetch_stt_model.py``
# from the repo root (sys.path[0] would otherwise be scripts/, not the root).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Mirrors tools/transcription_tools.py DEFAULT_LOCAL_MODEL ("base", ~150MB).
_MODEL = os.environ.get("HERMES_STT_PREFETCH_MODEL", "base")


def _ensure_faster_whisper() -> "object":
    """Import WhisperModel, lazy-installing the stt.faster_whisper extra first."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        # Package is a lazy extra; install it non-interactively, then import.
        # ``prompt=False`` so this never blocks waiting for user input.
        from tools.lazy_deps import ensure
        ensure("stt.faster_whisper", prompt=False)
        from faster_whisper import WhisperModel
    return WhisperModel


def main() -> int:
    try:
        WhisperModel = _ensure_faster_whisper()
        # Construction triggers the HuggingFace download into the cache. CPU +
        # int8 avoids any CUDA-runtime dependency just to fetch the weights — we
        # only need the download, not GPU inference at prefetch time.
        WhisperModel(_MODEL, device="cpu", compute_type="int8")
    except Exception as exc:  # best-effort: NEVER fail the install (C7 fail-open)
        print(
            f"ADR-V6-031 STT model pre-fetch did not complete ({exc!r}). "
            f"First transcription will lazy-download the '{_MODEL}' model "
            "(~150MB). Non-fatal — the lazy-load path covers it.",
            file=sys.stderr,
        )
        return 0
    print(f"ADR-V6-031 STT model pre-fetched (faster-whisper '{_MODEL}').")
    return 0


if __name__ == "__main__":
    sys.exit(main())

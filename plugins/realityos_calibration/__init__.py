"""RealityOS founder daily calibration plugin (ADR-V6-028 §11.4/§11.5).

Pure-logic closed loop: sample atoms → founder verdict (准/不准/惊喜) → feedback +
confidence demotion + correction_rate telemetry. See ``calibration.py``.
"""

from .calibration import (
    CALIBRATION_FLOOR,
    CalibrationResult,
    Rater,
    VERDICT_CORRECT,
    VERDICT_SKIP,
    VERDICT_SURPRISE,
    VERDICT_WRONG,
    format_atom_for_display,
    run_calibration,
)

__all__ = [
    "CALIBRATION_FLOOR",
    "CalibrationResult",
    "Rater",
    "VERDICT_CORRECT",
    "VERDICT_SKIP",
    "VERDICT_SURPRISE",
    "VERDICT_WRONG",
    "format_atom_for_display",
    "run_calibration",
]

"""RealityOS V6 sovereignty layer (§6.1–§6.8, PIPL §31/§45/§47).

The user's control surface over their life-graph: two-mode cascade deletion,
one-click JSON export, minor-mode age gate, and the consent_tag exercise face.
See ``sovereignty.py`` for the Phase-1 scope (primitives live + tested; the
desktop UI to call them is the next step).
"""

from __future__ import annotations

from .sovereignty import (
    MODE_A,
    MODE_B,
    cascade_soft_delete,
    export_user_data,
    export_user_data_json,
    get_consent_summary,
    is_minor,
    purge_soft_deleted,
    register,
    set_consent_tag,
    set_minor_mode,
)

__all__ = [
    "MODE_A",
    "MODE_B",
    "cascade_soft_delete",
    "export_user_data",
    "export_user_data_json",
    "get_consent_summary",
    "is_minor",
    "purge_soft_deleted",
    "register",
    "set_consent_tag",
    "set_minor_mode",
]

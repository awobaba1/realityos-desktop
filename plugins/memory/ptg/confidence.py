"""RealityOS V6 — Confidence gate (C5: agent output must pass validation).

Faithful port of V5 ``danao13/backend/app/services/validation_service.py``
``validate_and_filter`` + ``danao13/backend/app/core/config.py`` thresholds
(R3=0.8, R2=0.7, R1=0.5, R7=0.5, R0=0.7, comparison ``>=``, single
``atom.confidence`` field). The R1 neutral-mood exemption
(``state_type=='mood' and direction=='stable' and intensity=='low'`` bypasses
the 0.5 gate) is preserved verbatim — dropping it silently loses neutral moods.

ONE deliberate V6 refinement (documented in ADR-V6-011): V5 parsed the whole
``ExtractionResult`` via pydantic's discriminated Union, so a single malformed
atom rejected the ENTIRE memo (all siblings lost). V6 validates each atom
against its own model individually → one bad atom goes to its own DLQ entry,
its valid siblings still land. Strictly more faithful to C2 (nothing lost);
the top-level structure check (must be a dict with ``summary`` + ``atoms``
list) still rejects a wholly-broken output, matching V5.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from pydantic import ValidationError

from .atom_schemas import (
    ATOM_MODELS,
    R0EntityAtom,
    R1SelfStateAtom,
    R2TaskAtom,
    R3PersonAtom,
    R7ExpressionAtom,
)

logger = logging.getLogger(__name__)

# V5 defaults — danao13 backend/app/core/config.py:80-84. Overridable via
# plugins.ptg.confidence_threshold.{person,task,state,expression,entity}.
DEFAULT_THRESHOLDS = {
    "person": 0.8,      # R3 Person
    "task": 0.7,        # R2 Task
    "state": 0.5,       # R1 SelfState
    "expression": 0.5,  # R7 Expression
    "entity": 0.7,      # R0 Entity (ADR-048)
}


class ValidationResult:
    """Outcome of the C5 gate: schema validation + confidence filtering.

    Three buckets:
      * ``valid_atoms``    — passed schema + confidence; written to event tables.
      * ``filtered_atoms`` — schema-valid but below the type confidence gate;
                             recorded to DLQ (never silently dropped, C7).
      * ``invalid_atoms``  — schema-broken (bad enum / missing field / unknown
                             type); recorded to DLQ individually (V6 granular).
    """

    def __init__(self) -> None:
        self.valid_atoms: list[Any] = []
        self.filtered_atoms: list[dict] = []
        self.invalid_atoms: list[dict] = []
        self.summary: str = ""
        self.schema_valid: bool = True
        self.errors: list[str] = []

    @property
    def is_valid(self) -> bool:
        return self.schema_valid and not self.errors


class ConfidenceEngine:
    """Per-relation confidence gate. Thresholds mirror V5; injectable for tests."""

    def __init__(self, thresholds: Optional[dict] = None) -> None:
        src = dict(DEFAULT_THRESHOLDS)
        if thresholds:
            for k in src:
                if k in thresholds and thresholds[k] is not None:
                    src[k] = float(thresholds[k])
        self.person = src["person"]
        self.task = src["task"]
        self.state = src["state"]
        self.expression = src["expression"]
        self.entity = src["entity"]

    @classmethod
    def from_ptg_config(cls, ptg_config: Optional[dict]) -> "ConfidenceEngine":
        """Build from the ``plugins.ptg`` config dict (load_ptg_config() output)."""
        cfg = ptg_config or {}
        return cls(cfg.get("confidence_threshold"))

    # -- per-type gate -----------------------------------------------------

    def _passes(self, atom: Any) -> bool:
        """Confidence gate for one typed atom, with the R1 neutral-mood exemption."""
        if isinstance(atom, R3PersonAtom):
            return atom.confidence >= self.person
        if isinstance(atom, R2TaskAtom):
            return atom.confidence >= self.task
        if isinstance(atom, R7ExpressionAtom):
            return atom.confidence >= self.expression
        if isinstance(atom, R0EntityAtom):
            return atom.confidence >= self.entity
        if isinstance(atom, R1SelfStateAtom):
            is_neutral_mood = (
                atom.state_type == "mood"
                and atom.direction == "stable"
                and atom.intensity == "low"
            )
            return atom.confidence >= self.state or is_neutral_mood
        return False  # unknown typed atom — shouldn't happen; treat as filtered

    def _threshold_for(self, atom: Any) -> float:
        if isinstance(atom, R3PersonAtom):
            return self.person
        if isinstance(atom, R2TaskAtom):
            return self.task
        if isinstance(atom, R7ExpressionAtom):
            return self.expression
        if isinstance(atom, R0EntityAtom):
            return self.entity
        return self.state  # R1SelfStateAtom (and fallback)

    # -- main entry --------------------------------------------------------

    def validate(self, raw_output: Any) -> ValidationResult:
        """Validate + confidence-filter an LLM extraction output.

        Top-level structure errors (not a dict / no summary / atoms not a list)
        reject the whole output (V5 behaviour). Per-atom errors are isolated
        to ``invalid_atoms`` so siblings survive (V6 refinement).
        """
        result = ValidationResult()

        # Step 1 — top-level structure gate.
        if not isinstance(raw_output, dict):
            result.schema_valid = False
            result.errors.append("LLM output is not a JSON object")
            return result
        summary = raw_output.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            result.schema_valid = False
            result.errors.append("missing/empty 'summary' string")
            return result
        atoms = raw_output.get("atoms")
        if not isinstance(atoms, list):
            result.schema_valid = False
            result.errors.append("'atoms' is not a list")
            return result
        result.summary = summary.strip()[:50]

        # Step 2 — per-atom validate + confidence gate.
        for item in atoms:
            if not isinstance(item, dict):
                result.invalid_atoms.append({"atom": item, "error": "atom is not an object"})
                continue
            atom_type = item.get("type")
            model = ATOM_MODELS.get(atom_type) if isinstance(atom_type, str) else None
            if model is None:
                result.invalid_atoms.append(
                    {"atom": item, "error": f"unknown atom type: {atom_type!r}"})
                continue
            try:
                atom = model(**item)
            except (ValidationError, TypeError) as exc:
                result.invalid_atoms.append({"atom": item, "error": str(exc)})
                continue

            if self._passes(atom):
                result.valid_atoms.append(atom)
            else:
                thr = self._threshold_for(atom)
                d = atom.model_dump()
                d["_filter_reason"] = (
                    f"confidence {atom.confidence} < threshold {thr}")
                result.filtered_atoms.append(d)

        return result

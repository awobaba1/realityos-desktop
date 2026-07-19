"""ADR-V6-032: pin the Phase-2 Quark/Theory interface contracts (IDL).

These tests do NOT exercise Phase-2 logic (there is none — Phase 2 is deferred
per ADR-V6-022 "不做"). They pin the CONTRACT: the data shapes, the fixed
enumerations, the Protocol interfaces, and — crucially for the anti-fake-green
mandate — the guarantee that NO concrete extractor/engine ships in this module
(a concrete class would be 假绿: code that looks like Phase 2 but isn't).

Design fidelity is asserted against ``danao13/RealityOS-V6-架构设计.md`` §4.3E
(Quark→atom map, 7 Quark / Phase-2 subset of 3, 7 PC / 5 FR enumerations).
"""
from __future__ import annotations

import inspect

import pytest
from pydantic import ValidationError

from plugins.memory.ptg.phase2_contracts import (
    FR_DIMENSIONS,
    PC_CONSTRAINTS,
    PHASE2_CONTRACT_VERSION,
    PHASE2_QUARK_KINDS,
    QUARK_KINDS,
    QUARK_TO_ATOM_MAP,
    THEORY_DERIVATIONS,
    QuarkExtractor,
    QuarkRecord,
    TheoryDerivation,
    TheoryEngine,
)


class TestImportIsContractOnly:
    """Importing the module executes no Phase-2 logic and ships no concrete
    extractor/engine (the anti-fake-green guarantee)."""

    def test_module_imports_cleanly(self):
        # Already imported at top — reaching here means import had no side effect
        # that aborted collection. Belt-and-suspenders: re-import is a no-op.
        import importlib

        import plugins.memory.ptg.phase2_contracts as mod

        importlib.reload(mod)  # must not raise / must not execute Phase-2 work

    def test_only_protocols_and_pydantic_models_ship(self):
        """The anti-fake-green guarantee: this module ships CONTRACTS only. Every
        class defined here must be either a ``typing.Protocol`` (the extractor/
        engine interface shapes) or a pydantic ``BaseModel`` (the data shapes). A
        *concrete* implementer — a class that actually extracts Quarks or derives
        theory — must NOT ship here (Phase 2 is not implemented). If a future
        commit adds a concrete logic class, this test fails to force an explicit
        ADR (C1) + the acknowledgment that Phase 2 has started."""
        import plugins.memory.ptg.phase2_contracts as mod

        from pydantic import BaseModel
        from typing import Protocol

        classes = {
            name: val
            for name, val in vars(mod).items()
            if inspect.isclass(val) and val.__module__ == mod.__name__
        }
        assert classes, "expected at least the Protocols + BaseModels to be defined"
        for name, val in classes.items():
            is_protocol = Protocol in getattr(val, "__mro__", ())
            is_model = issubclass(val, BaseModel)
            assert is_protocol or is_model, (
                f"{name} is neither a Protocol nor a pydantic BaseModel — a concrete "
                "Phase-2 implementer must not ship in the contracts module (anti-fake-green)"
            )

    @pytest.mark.parametrize("protocol", ["QuarkExtractor", "TheoryEngine"])
    def test_extractor_engine_are_protocols_not_concrete(self, protocol):
        """The two interface names are Protocols (contract shapes), never
        concrete classes with extraction/derivation behaviour."""
        from typing import Protocol

        import plugins.memory.ptg.phase2_contracts as mod

        obj = getattr(mod, protocol)
        assert Protocol in getattr(obj, "__mro__", ()), (
            f"{protocol} must remain a Protocol (contract shape), not a concrete class"
        )


class TestQuarkContractFidelity:
    """Quark enumerations + Quark→atom map match doc §4.3E exactly."""

    def test_seven_quark_kinds_after_deleting_exception(self):
        # doc §4.3E line 371: delete Exception (== R11 duplicate) → 7 Quark.
        assert QUARK_KINDS == (
            "Identity", "Meaning", "Feeling", "Time", "Behavior", "Context", "Network",
        )
        assert len(QUARK_KINDS) == 7
        assert "Exception" not in QUARK_KINDS  # the deleted duplicate

    def test_phase2_subset_is_identity_meaning_feeling(self):
        # doc §4.3E line 374: Phase 2 实际交付 = Identity + Meaning + Feeling (text).
        assert PHASE2_QUARK_KINDS == ("Identity", "Meaning", "Feeling")
        # The deferred kinds depend on cut Layer B/C continuous audio (line 373).
        assert set(PHASE2_QUARK_KINDS) < set(QUARK_KINDS)

    def test_quark_to_atom_map_covers_all_kinds(self):
        assert set(QUARK_TO_ATOM_MAP) == set(QUARK_KINDS)

    @pytest.mark.parametrize(
        "kind, atoms",
        [
            ("Identity", ("R3_Person", "R1_SelfState")),       # line 381 主 R3 / 辅 R1
            ("Meaning", ("R7_Expression", "R2_Task", "R8_Cognition")),  # line 384
            ("Feeling", ("R9_Emotion", "R1_SelfState")),       # line 385 主 R9 / 辅 R1
            ("Time", ("R10_Rhythm", "R11_StateChange")),       # line 387 规则引擎
            ("Behavior", ("R6_Behavior",)),                    # line 383
            ("Context", ("R5_Context",)),                      # line 382
            ("Network", ("R3_Person",)),                       # line 386 R3 关系维度
        ],
    )
    def test_quark_to_atom_map_matches_doc(self, kind, atoms):
        assert QUARK_TO_ATOM_MAP[kind] == atoms


class TestQuarkRecordValidation:
    """QuarkRecord pydantic constraints (the C5-gate shape)."""

    def test_valid_record(self):
        rec = QuarkRecord(kind="Identity", value="张三", source_id="memo_1")
        assert rec.occurrence_count == 1
        assert rec.confidence == 0.0
        assert rec.evidence == {}

    def test_rejects_unknown_kind(self):
        with pytest.raises(ValidationError):
            QuarkRecord(kind="NotAQuark", value="x", source_id="m")

    def test_confidence_bounds(self):
        with pytest.raises(ValidationError):
            QuarkRecord(kind="Identity", value="x", source_id="m", confidence=1.5)
        with pytest.raises(ValidationError):
            QuarkRecord(kind="Identity", value="x", source_id="m", confidence=-0.1)

    def test_occurrence_count_at_least_one(self):
        with pytest.raises(ValidationError):
            QuarkRecord(kind="Meaning", value="x", source_id="m", occurrence_count=0)


class TestTheoryContractFidelity:
    """PC / FR / derivation enumerations match doc line 116 / §4.3E line 389."""

    def test_seven_pc_constraints(self):
        # doc line 116: 7 PC 约束.
        assert PC_CONSTRAINTS == (
            "Time", "Energy", "Cognition", "Emotion", "Social", "Execution", "Environment",
        )
        assert len(PC_CONSTRAINTS) == 7

    def test_five_fr_dimensions(self):
        # doc line 116: 5 维 FR (职业/人际/身心/学习/财务).
        assert FR_DIMENSIONS == ("Career", "Interpersonal", "BodyMind", "Learning", "Finance")
        assert len(FR_DIMENSIONS) == 5

    def test_derivation_kinds(self):
        # doc line 116/389: 相变 / PASCR 4 链 / 守恒检查.
        assert THEORY_DERIVATIONS == ("PhaseTransition", "PASCR", "Conservation")


class TestTheoryDerivationValidation:
    def test_pc_writes_constraint_state(self):
        d = TheoryDerivation(
            kind="PC", name="Energy", score=0.6, aggregation_type="constraint_state"
        )
        assert d.rationale == ""

    def test_fr_writes_fr_snapshot(self):
        TheoryDerivation(
            kind="FR", name="Career", score=0.7, aggregation_type="fr_snapshot"
        )

    def test_rejects_detected_pattern_for_theory(self):
        # insight_aggregation.type also allows 'detected_pattern' (doc line 288),
        # but that is the Pattern-Discovery sink, NOT a Theory output. Theory only
        # writes constraint_state / fr_snapshot (doc §4.3E line 389).
        with pytest.raises(ValidationError):
            TheoryDerivation(
                kind="PC", name="Energy", score=0.5, aggregation_type="detected_pattern"
            )

    def test_score_bounds(self):
        with pytest.raises(ValidationError):
            TheoryDerivation(
                kind="FR", name="Career", score=2.0, aggregation_type="fr_snapshot"
            )


class TestProtocolsAreStructural:
    """A Phase-2 implementer satisfies the Protocols structurally (duck-typed)."""

    def test_quark_extractor_protocol_is_runtime_checkable(self):
        class _Impl:
            def extract(self, quark_evidence_rows, capture_text):
                return []

        assert isinstance(_Impl(), QuarkExtractor)

    def test_theory_engine_protocol_is_runtime_checkable(self):
        class _Impl:
            def derive(self, user_id, atoms, relations):
                return []

        assert isinstance(_Impl(), TheoryEngine)

    def test_contract_version_is_pinned(self):
        # Bumped on any Phase-2-visible contract edit (C1: contract change = decision).
        assert isinstance(PHASE2_CONTRACT_VERSION, int)
        assert PHASE2_CONTRACT_VERSION >= 1

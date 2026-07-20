"""RealityOS V6 — Phase 2 interface contracts (IDL): QuarkExtractor + TheoryEngine.

ADR-V6-032. The design doc (``danao13/RealityOS-V6-架构设计.md``) committed in
Phase 0 to **pin** the Quark + Theory interface contracts BEFORE Phase 2 so that
Phase-2 implementation never forces a PTG schema migration:

  - doc §9 line 785: "Quark 接口 IDL + 理论层接口契约（Phase 2 实现，接口现在
    定死，避免 Phase 2 改 PTG Schema）"
  - doc §7.3 line 746: Phase 0 建 ``realityos_*`` 骨架 + "Quark 接口 IDL（即使
    stub）"
  - doc §4.3E lines 368-389: the authoritative Quark/Theory spec.
  - doc §10 line 798: "QuarkExtractor/TheoryEngine 具体算法（Phase 2 设计，接口
    Phase 0 定）"

The audit ADR-V6-022 (桶 C item C4 / action 16) found this pinning was NEVER done
— only the ``tool_events.quark_evidence`` column was reserved
(``schema.py`` ~line 410), with no contract defining what fills it or consumes it.
This module closes that gap.

It defines the Phase-2 contracts as IDL (Interface Definition Language): pydantic
data shapes + ``Protocol`` interfaces + the fixed contract constants (Quark→atom
aggregation map, PC/FR enumerations). It contains **no Phase-2 implementation** —
no concrete extractor/engine class. Phase 2 will instantiate
``realityos_quark`` / ``realityos_theory`` plugins (doc §7.3 module list) that
satisfy these contracts. Importing this module executes no Phase-2 logic.

Why pydantic models (not just TypedDict): to match the C5-gate idiom of
``atom_schemas.py`` — Phase-2 Quark/Theory outputs flow through the same
schema-validation gate, so the contract shapes are pydantic from the start.

All design intent below is sourced verbatim from the doc (§4.3E / §0.9 / §13) —
this is contract pinning, not invention.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

__all__ = [
    "QUARK_KINDS",
    "PHASE2_QUARK_KINDS",
    "QUARK_TO_ATOM_MAP",
    "QuarkRecord",
    "QuarkExtractor",
    "PC_CONSTRAINTS",
    "FR_DIMENSIONS",
    "THEORY_DERIVATIONS",
    "TheoryDerivation",
    "TheoryEngine",
    "PHASE2_CONTRACT_VERSION",
]

# Bump when the contract shape changes in a Phase-2-visible way. Phase 2 pins
# against a version so a contract edit is an explicit, reviewed event (C1).
#
# v2 (ADR-V6-050): TheoryDerivation gains ``basis`` + ``degraded``. ADR-V6-040
# D4's honest-degradation iron rule demands that every PC-dimension derivation
# carry a machine-readable degradation flag + the data basis — burying it in
# ``rationale`` prose is exactly the fake-green the rule warns against (a
# renderer could silently ignore prose and render a None/unsupported dim as
# "平稳"). Both fields are optional with defaults, so v1 consumers reading v2
# records are unaffected; the version bump is the C1 signal (ADR-V6-039 rule 4).
PHASE2_CONTRACT_VERSION = 2

# ── Quark layer (data-syntax primitives; doc §4.3E, §6) ─────────────────────

# The 7 Meta Quark after deleting Exception (== R11 duplicate; doc §4.3E line
# 371). Phase 2 implements only the text-reachable subset (line 374); the rest
# are pinned here so their shapes are fixed for Phase 2.5 (acoustic) / Phase 3
# (thin client) but are NOT produced by the Phase-2 QuarkExtractor.
QUARK_KINDS: tuple[str, ...] = (
    "Identity",   # Phase 2 (text) → R3(main)/R1(aux)
    "Meaning",    # Phase 2 → R7(main)/R2/R8
    "Feeling",    # Phase 2 (text weak) → R9(main)/R1
    "Time",       # R10 pipeline (rule engine, not a separate extractor; line 372)
    "Behavior",   # Phase 2 (post_tool_call hook) / Phase 3 (SED) → R6
    "Context",    # Phase 3 (acoustic scene) → R5
    "Network",    # Phase 2.5+ (multi-person) → R3 relation dimension
)

# Phase-2-implemented subset (doc §4.3E line 374: "Identity（文本版）+ Meaning +
# Feeling（文本弱版）三个"). Time/Behavior/Context/Network are pinned for later
# phases (line 373: V6 source-limited — depend on cut Layer B/C continuous
# audio); Phase 2 does NOT implement them (防空跑).
PHASE2_QUARK_KINDS: tuple[str, ...] = ("Identity", "Meaning", "Feeling")


class QuarkRecord(BaseModel):
    """A single extracted Quark — the "what signal was seen" evidence layer.

    Quarks are data-syntax primitives that aggregate INTO atoms (R1-R12). A
    Quark is "saw the name 张三 3 times"; the R3 atom is "张三 is a person with
    interaction_count=3". The Quark layer's independent value (doc §4.3E line
    375): ① signal dedup, ② cross-atom entity disambiguation ("张总/老张/张三"
    → one entity), ③ confidence evidence carrier for Stage-3 cross-validation.
    """

    kind: str = Field(pattern="^(Identity|Meaning|Feeling|Time|Behavior|Context|Network)$")
    # The raw signal value (entity name / intent label / emotion token / ...).
    value: str = Field(max_length=200)
    # Provenance: which capture produced this Quark (memo_id / tool_event id).
    source_id: str
    # How many times this signal was seen in the source window (dedup count).
    occurrence_count: int = Field(default=1, ge=1)
    # Phase-2 text confidence; Phase 2.5 replaces with acoustic-derived score.
    confidence: float = Field(default=0.0, ge=0, le=1)
    # Free-form evidence payload (mirrors tool_events.quark_evidence JSON).
    evidence: dict = Field(default_factory=dict)


# Quark → atom aggregation map (doc §4.3E lines 377-388, "REV-3 补全").
# Encoded as a constant so Phase-2 aggregation has ONE fixed target — changing
# it is a reviewed contract edit (bump PHASE2_CONTRACT_VERSION), not a silent
# behavior drift. Atom type names match atom_schemas.py R*-prefix convention.
QUARK_TO_ATOM_MAP: dict[str, tuple[str, ...]] = {
    "Identity": ("R3_Person", "R1_SelfState"),                   # 主 R3 / 辅 R1 (line 381)
    "Context": ("R5_Context",),                                  # Phase 3 (line 382)
    "Behavior": ("R6_Behavior",),                                # Phase 2 hook / 3 SED (line 383)
    "Meaning": ("R7_Expression", "R2_Task", "R8_Cognition"),     # intent 分类 (line 384)
    "Feeling": ("R9_Emotion", "R1_SelfState"),                   # 主 R9 / 辅 R1 (line 385)
    "Network": ("R3_Person",),                                   # R3 关系维度, Phase 2.5+ (line 386)
    "Time": ("R10_Rhythm", "R11_StateChange"),                   # 规则引擎 (line 387)
}


@runtime_checkable
class QuarkExtractor(Protocol):
    """Phase-2 contract: extract Quark records from captured evidence.

    Input: ``tool_events.quark_evidence`` rows (the Phase-0/1-reserved column,
    schema.py ~line 410) + raw capture text. Output: ``QuarkRecord`` list,
    consumed by the aggregation step that materializes R-atoms via
    ``QUARK_TO_ATOM_MAP``.

    Phase 2 implements ONLY ``Identity`` / ``Meaning`` / ``Feeling`` (text);
    the other Quark kinds are pinned for later phases but NOT produced here
    (doc §4.3E lines 373-374). This Protocol defines the shape only — Phase 2
    provides a concrete implementer; no implementer ships in this module.
    """

    def extract(
        self, quark_evidence_rows: list[dict], capture_text: str
    ) -> list[QuarkRecord]:
        """Contract shape — a Phase-2 implementer satisfies this structurally."""
        ...


# ── Theory layer (derivation; doc §4.3E line 389, §0.9/§13 line 116) ────────

# 7 Personal Constraints (PC) — doc line 116.
PC_CONSTRAINTS: tuple[str, ...] = (
    "Time", "Energy", "Cognition", "Emotion", "Social", "Execution", "Environment",
)
# 5-dimensional Life Framework (FR) — doc line 116.
FR_DIMENSIONS: tuple[str, ...] = ("Career", "Interpersonal", "BodyMind", "Learning", "Finance")
# Phase transition + PASCR 4-chain + conservation check (doc line 116 / 389).
THEORY_DERIVATIONS: tuple[str, ...] = ("PhaseTransition", "PASCR", "Conservation")


class TheoryDerivation(BaseModel):
    """A single Theory-engine derivation, persisted to ``insight_aggregation``.

    doc §4.3E line 389: TheoryEngine runs on PTG + insight_aggregation producing
    7 PC / 5 FR / 相变 / PASCR / 守恒. Phase 2 = LLM approximation of the
    derivation skeletons (e.g. ``PC-Energy = σ(R1.fatigue_score,
    R10.sleep_deviation)``); Phase 2.5+ swaps in statistical formulas. The
    interface contract is pinned NOW so the swap needs no schema change.
    """

    kind: str = Field(pattern="^(PC|FR|PhaseTransition|PASCR|Conservation)$")
    # PC → one of PC_CONSTRAINTS; FR → one of FR_DIMENSIONS; else the derivation name.
    name: str = Field(max_length=50)
    # Derived score 0..1 (Phase 2 LLM-approx; Phase 2.5 statistical).
    score: float = Field(ge=0, le=1)
    # Human-readable derivation trace (which atoms fed this — for auditability).
    rationale: str = Field(default="", max_length=500)
    # insight_aggregation.type target (schema.py: PC/相变/PASCR/守恒→constraint_state,
    # FR→fr_snapshot). Matches doc line 288 enum.
    aggregation_type: str = Field(pattern="^(constraint_state|fr_snapshot)$")
    confidence: float = Field(default=0.0, ge=0, le=1)
    # ADR-V6-050 (contract v2): the data basis for this derivation — which atom
    # kind / source fed it, OR why it could not be derived ("需 R10 sleep 连续值
    # (Phase 2.5)，文本无据"). Machine-readable provenance so a consumer never
    # mistakes an unsupported dim for a measured one.
    basis: str = Field(default="", max_length=300)
    # ADR-V6-050 (contract v2): True when this derivation rests on a missing /
    # severely-degraded data source (no acoustic / multi-person / sleep chain).
    # The honest-degradation iron rule (ADR-V6-040 D4): a degraded dim MUST be
    # rendered as "数据不足/降级", NEVER as a real score or "平稳". The engine
    # stamps this deterministically (not the LLM) — the LLM cannot know it
    # lacks data, so the engine enforces the contract.
    degraded: bool = Field(default=False)


@runtime_checkable
class TheoryEngine(Protocol):
    """Phase-2 contract: derive theory-layer insights from materialized atoms.

    Input: the PTG atom + relation graph (READ-ONLY — doc §4.7 invariant: the
    theory/insight layer READS the atom layer, never writes back; single-direction
    data flow). Output: ``TheoryDerivation`` list, persisted to
    ``insight_aggregation`` (PC/相变/PASCR/守恒 → ``constraint_state``; FR →
    ``fr_snapshot``).

    Phase 2 = LLM approximation of the derivation skeletons (doc §4.3E line 389);
    Phase 2.5+ replaces with statistical formulas behind the SAME interface.
    This Protocol defines the shape only — no implementer ships in this module.
    """

    def derive(
        self, user_id: str, atoms: list[dict], relations: list[dict]
    ) -> list[TheoryDerivation]:
        """Contract shape — a Phase-2 implementer satisfies this structurally."""
        ...

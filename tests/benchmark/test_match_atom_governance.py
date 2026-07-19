"""ADR-V6-035 (action 20): governance of the match_atom equivalence policy.

The equivalence groups (R3 kinship synonyms, R1 state pairs, R7 intent pairs,
text-overlap threshold) ARE the eval's matching policy — they decide which
predicted/expected atom pairs count as the same, so they directly move the
recall/precision numbers. ADR-V6-022 桶 C / action 20 flagged them as "偏松" with
NO governance surface (inline literals, no version, no audit).

These tests pin the policy as a governed, versioned, auditable surface — WITHOUT
changing the matching behaviour (the existing ``test_match_atom.py`` C4 lock
guards behaviour; these tests guard the POLICY MEMBERSHIP). Any future change to
a group or threshold must: (a) update these expectations, (b) bump
``MATCH_ATOM_VERSION``, (c) be a deliberate reviewed decision (C1).

The permissiveness is INTENTIONAL (retained, not tightened): tightening regresses
recall without helping precision — precision⑦ proved the threshold path dead
(TP/FP confidence non-separable, structural ceiling ~70 %, ADR-V6-026).
"""
from __future__ import annotations

from tests.benchmark.match_atom import (
    MATCH_ATOM_VERSION,
    R1_STATE_EQUIVALENT_GROUPS,
    R7_INTENT_EQUIVALENT_GROUPS,
    TEXT_OVERLAP_THRESHOLD,
    _PERSON_SYNONYM_GROUPS,
    describe_equivalences,
)


class TestPolicyIsGoverned:
    """The policy is a versioned, named, module-level constant set — not buried
    inline literals."""

    def test_version_is_pinned(self):
        # Bump on ANY policy change. The matching policy is a decision (C1).
        assert isinstance(MATCH_ATOM_VERSION, int)
        assert MATCH_ATOM_VERSION >= 1

    def test_equivalences_are_module_level_immutable(self):
        # frozenset → runtime-immutable; a group can't be silently mutated.
        assert isinstance(R1_STATE_EQUIVALENT_GROUPS, frozenset)
        assert isinstance(R7_INTENT_EQUIVALENT_GROUPS, frozenset)
        for pair in R1_STATE_EQUIVALENT_GROUPS | R7_INTENT_EQUIVALENT_GROUPS:
            assert isinstance(pair, frozenset)


class TestPolicyMembershipPinned:
    """Exact membership — a change here is a policy edit (bump version + review)."""

    def test_r3_six_kinship_synonym_groups(self):
        # 6 kinship groups (妈妈/爸爸/孩子/老婆/老公/老板 families).
        assert len(_PERSON_SYNONYM_GROUPS) == 6
        assert {"妈妈", "老妈", "妈", "母亲", "妈咪"} in _PERSON_SYNONYM_GROUPS
        assert {"老板", "领导", "上司"} in _PERSON_SYNONYM_GROUPS

    def test_r1_three_state_equivalent_pairs(self):
        assert R1_STATE_EQUIVALENT_GROUPS == frozenset({
            frozenset({"energy", "mood"}),
            frozenset({"energy", "fatigue"}),
            frozenset({"stress", "mood"}),
        })

    def test_r7_four_intent_equivalent_pairs(self):
        assert R7_INTENT_EQUIVALENT_GROUPS == frozenset({
            frozenset({"Consumption", "Evaluation"}),
            frozenset({"Consumption", "Complaint"}),
            frozenset({"Health", "Complaint"}),
            frozenset({"Need_To_Do", "Help"}),
        })

    def test_text_overlap_threshold(self):
        assert TEXT_OVERLAP_THRESHOLD == 0.4


class TestDescribeEquivalences:
    """The structured policy snapshot is stable + complete (eval-report surface)."""

    def test_snapshot_keys(self):
        snap = describe_equivalences()
        assert set(snap) == {
            "version",
            "r3_person_synonym_groups",
            "r1_state_equivalent_pairs",
            "r7_intent_equivalent_pairs",
            "text_overlap_threshold",
        }

    def test_snapshot_matches_constants(self):
        snap = describe_equivalences()
        assert snap["version"] == MATCH_ATOM_VERSION
        assert snap["text_overlap_threshold"] == TEXT_OVERLAP_THRESHOLD
        assert len(snap["r3_person_synonym_groups"]) == len(_PERSON_SYNONYM_GROUPS)
        assert len(snap["r1_state_equivalent_pairs"]) == len(R1_STATE_EQUIVALENT_GROUPS)
        assert len(snap["r7_intent_equivalent_pairs"]) == len(R7_INTENT_EQUIVALENT_GROUPS)

    def test_snapshot_is_json_serializable(self):
        # eval reports serialize this (sorted lists, not sets/frozensets).
        import json

        json.dumps(describe_equivalences())  # must not raise

"""C4 unit tests: sample-size graded confidence gate (ADR-V6-042 / F6).

Bug ID: F6 — insight_aggregation.confidence was graded by coarse cold-start
metrics (memo total + registration days), so an LLM could write a strong,
specific conclusion on n=2 backing atoms and have it cached as 0.8
"sufficient". This is V6's single biggest fake-green source (Agent ③ /
strategy-02). The gate floors confidence by the thinnest backing sample.

These tests pin the PRD §8.6.7 thresholds (10 / 30 / 0.6 / 0.9) and the
wiring semantics (cap = min(base, weakest-sample-grade)).
"""

import pytest

from plugins.memory.ptg.confidence_gate import (
    CONF_FLOOR,
    CONF_HIGH,
    CONF_MID,
    MID_SAMPLE,
    MIN_SAMPLE,
    cap_confidence_by_atom_samples,
    grade_confidence_by_sample,
    is_valid_sufficiency_label,
    merge_sufficiency,
    sample_sufficiency,
)


# ── grade_confidence_by_sample ──────────────────────────────────────────────

class TestGradeConfidenceBySample:
    def test_below_min_sample_floors(self):
        """F6 core: <10 → CONF_FLOOR (not None — column is CHECK 0..1)."""
        for n in [0, 1, 5, 9]:
            assert grade_confidence_by_sample(n) == CONF_FLOOR

    def test_min_sample_boundary_mid(self):
        """10 → mid (0.6); 29 → still mid."""
        assert grade_confidence_by_sample(MIN_SAMPLE) == CONF_MID
        assert grade_confidence_by_sample(MID_SAMPLE - 1) == CONF_MID

    def test_mid_sample_boundary_high(self):
        """30 → high (0.9)."""
        assert grade_confidence_by_sample(MID_SAMPLE) == CONF_HIGH
        assert grade_confidence_by_sample(1000) == CONF_HIGH

    def test_negative_clamped_to_floor(self):
        """Defensive: a buggy negative count floors, never crashes."""
        assert grade_confidence_by_sample(-5) == CONF_FLOOR

    def test_thresholds_match_prd(self):
        """Pin PRD §8.6.7 exact values — change requires an ADR."""
        assert MIN_SAMPLE == 10
        assert MID_SAMPLE == 30
        assert CONF_MID == 0.6
        assert CONF_HIGH == 0.9
        assert CONF_FLOOR == 0.1


# ── sample_sufficiency ──────────────────────────────────────────────────────

class TestSampleSufficiency:
    def test_labels_at_boundaries(self):
        assert sample_sufficiency(0) == "insufficient"
        assert sample_sufficiency(9) == "insufficient"
        assert sample_sufficiency(10) == "partial"
        assert sample_sufficiency(29) == "partial"
        assert sample_sufficiency(30) == "sufficient"
        assert sample_sufficiency(500) == "sufficient"

    def test_negative_clamped(self):
        assert sample_sufficiency(-1) == "insufficient"


# ── cap_confidence_by_atom_samples ──────────────────────────────────────────

class TestCapConfidenceByAtomSamples:
    def test_all_kinds_sufficient_keeps_base(self):
        """If every kind has ≥30, no cap — base confidence stands."""
        counts = {"emotions": 40, "tasks": 50, "people": 35}
        capped, label, kind = cap_confidence_by_atom_samples(0.8, counts,
                                                              ["emotions", "tasks", "people"])
        assert capped == 0.8
        assert label == "sufficient"
        # weakest_kind is the first kind at the min grade (all tied at sufficient)
        assert kind in ("emotions", "tasks", "people")

    def test_one_kind_thin_caps_to_floor(self):
        """F6: one weak kind drags the whole report to CONF_FLOOR."""
        counts = {"emotions": 2, "tasks": 50}  # emotions n=2 → floor
        capped, label, kind = cap_confidence_by_atom_samples(0.8, counts,
                                                              ["emotions", "tasks"])
        assert capped == CONF_FLOOR
        assert label == "insufficient"
        assert kind == "emotions"

    def test_one_kind_partial_caps_to_mid(self):
        counts = {"emotions": 15, "tasks": 50}  # emotions n=15 → mid
        capped, label, kind = cap_confidence_by_atom_samples(0.8, counts,
                                                              ["emotions", "tasks"])
        assert capped == CONF_MID
        assert label == "partial"
        assert kind == "emotions"

    def test_base_already_below_sample_grade_keeps_base(self):
        """min() semantics: if cold-start confidence is already lower, keep it."""
        counts = {"emotions": 50}  # sample grade 0.9
        capped, _, _ = cap_confidence_by_atom_samples(0.3, counts, ["emotions"])
        assert capped == 0.3

    def test_missing_kind_skipped_by_default(self):
        """skip_absent=True (default): absent kind drives no conclusion →
        skipped, not floored. A week with 0 emotions but 40 tasks is graded
        by the 40 tasks (the report omits the empty emotion section)."""
        counts = {"tasks": 50}
        capped, label, kind = cap_confidence_by_atom_samples(0.8, counts,
                                                              ["emotions", "tasks"])
        assert capped == 0.8
        assert label == "sufficient"
        assert kind == "tasks"

    def test_missing_kind_floored_when_strict(self):
        """skip_absent=False: strict per-kind enforcement — absent kind →
        n=0 → floor. Use only when every kind MUST be present."""
        counts = {"tasks": 50}
        capped, label, kind = cap_confidence_by_atom_samples(
            0.8, counts, ["emotions", "tasks"], skip_absent=False)
        assert capped == CONF_FLOOR
        assert label == "insufficient"
        assert kind == "emotions"

    def test_all_kinds_absent_floors_defensively(self):
        """Every kind absent (skip_absent=True) → no data at all → floor."""
        counts: dict = {}
        capped, label, kind = cap_confidence_by_atom_samples(
            0.8, counts, ["emotions", "tasks"])
        assert capped == CONF_FLOOR
        assert label == "insufficient"
        assert kind is None

    def test_empty_kinds_floors_defensively(self):
        """No kinds declared → cannot prove sufficiency → floor."""
        capped, label, kind = cap_confidence_by_atom_samples(0.8, {}, [])
        assert capped == CONF_FLOOR
        assert label == "insufficient"
        assert kind is None


# ── merge_sufficiency ───────────────────────────────────────────────────────

class TestMergeSufficiency:
    def test_most_pessimistic_wins(self):
        assert merge_sufficiency("sufficient", "insufficient") == "insufficient"
        assert merge_sufficiency("partial", "sufficient") == "partial"
        assert merge_sufficiency("sufficient", "sufficient") == "sufficient"

    def test_unknown_label_treated_as_insufficient(self):
        """Defensive: a missing/garbage verdict cannot improve trust."""
        assert merge_sufficiency("sufficient", "") == "insufficient"
        assert merge_sufficiency("sufficient", "garbage") == "insufficient"

    def test_no_labels_is_sufficient(self):
        """Vacuous truth — no verdicts to contradict optimism. (Callers should
        pass at least one real verdict; this just pins the degenerate path.)"""
        assert merge_sufficiency() == "sufficient"


# ── is_valid_sufficiency_label ──────────────────────────────────────────────

class TestIsValidSufficiencyLabel:
    @pytest.mark.parametrize("label", ["insufficient", "partial", "sufficient"])
    def test_valid(self, label):
        assert is_valid_sufficiency_label(label)

    @pytest.mark.parametrize("label", ["", "unknown", "Sufficient", None])
    def test_invalid(self, label):
        assert not is_valid_sufficiency_label(label)

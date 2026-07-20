"""RealityOS V6 — Phase 1b atom expansion regression tests (C4, ADR-V6-016).

Locks the three atoms Phase 1a lacked — R8_Cognition, R9_Emotion, R12_Outcome —
end to end across every layer they touch:

  1. Schema (C5 gate): the three pydantic atoms validate good input and reject
     bad enum / missing-required / unknown-type output → DLQ, never silent write.
  2. ConfidenceEngine Stage2: R1 intensity_weight (ADR-024, high/med/low =
     1.0/0.8/0.5), the three new type thresholds (cognition 0.5 / emotion 0.3 /
     outcome 0.4), the asr_quality_factor multiplier, and the preserved R1
     neutral-mood exemption.
  3. Atomizer dispatch: R8/R12 → meaning_events with the right atom_kind +
     serialized payload; R9 → feeling_events with atom_kind + emotion_vad.
  4. Recall reconstruction: recent_atoms round-trips R8/R9/R12 from the event
     tables back to typed atom dicts (atom_kind-driven dispatch).
  5. Graph materialization: R8 → topic node + self-learns edge; R12 → task node
     + self-has_task edge; R9 → no node (an emotion is not an entity).

The LLM caller and clock are injected (mock) so every test runs offline and
deterministically. No real founder content — synthetic atoms only (ADR-403).
"""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from plugins.memory.ptg.atomizer import Atomizer
from plugins.memory.ptg.atom_schemas import (
    R8CognitionAtom, R9EmotionAtom, R12OutcomeAtom,
)
from plugins.memory.ptg.confidence import ConfidenceEngine
from plugins.memory.ptg.store import PTGStore

_FIXED_NOW = datetime(2026, 7, 19, 14, 30)


# ---------------------------------------------------------------------------
# Helpers (mirror test_ptg_atomizer.py conventions)
# ---------------------------------------------------------------------------

def _resp(content_obj, *, model="glm-5.2", in_t=120, out_t=60):
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps(content_obj, ensure_ascii=False)))],
        model=model,
        usage=SimpleNamespace(prompt_tokens=in_t, completion_tokens=out_t),
    )


def _callerReturning(response=None, supplement=None):
    """Pass-aware mock for two-pass extraction (ADR-V6-016). Detection: the v12
    system prompt contains 'R8_Cognition' (v11 does not). For Phase 1b tests the
    R8/R9/R12 payload is the SUPPLEMENT (pass 2); the primary pass (v11) returns
    an empty R0-R7 response (these tests exercise the supplement pass only)."""
    primary = response if response is not None else _resp({"summary": "无主原子",
                                                            "atoms": []})
    supp = _resp(supplement) if supplement is not None else _resp(
        {"summary": "无补充原子", "atoms": []})

    def _call(**kwargs):
        sys_msg = ""
        msgs = kwargs.get("messages") or []
        if msgs and isinstance(msgs[0], dict):
            sys_msg = msgs[0].get("content", "") or ""
        return supp if "R8_Cognition" in sys_msg else primary
    return _call


@pytest.fixture
def store(tmp_path):
    s = PTGStore(db_path=str(tmp_path / "ptg.db"))
    s.ensure_founder("user-1", "founder@realityos.local")
    yield s
    s.close()


def _atomizer(store, caller):
    return Atomizer(
        store, user_id="user-1", llm_caller=caller,
        now_fn=lambda: _FIXED_NOW,
        confidence_engine=ConfidenceEngine(),
    )


def _memo(store, text="今天搞懂了React的diff算法，被领导表扬后很开心，季度方案终于搞定了"):
    return store.insert_memo(user_id="user-1", source_text=text, input_mode="text")


def _rows(store, sql, params=()):
    return [dict(r) for r in store._conn.execute(sql, params).fetchall()]


# A valid extraction exercising all three new atoms, each above its gate:
#   R8 cognition 0.8 ≥ 0.5, R9 emotion 0.7 ≥ 0.3, R12 outcome 0.85 ≥ 0.4.
_PHASE1B_OUTPUT = {
    "summary": "学到diff算法被表扬方案搞定",
    "atoms": [
        {"type": "R8_Cognition", "topic": "React diff 算法",
         "knowledge_tags": ["Hooks", "diff", "虚拟 DOM"], "engagement": "high",
         "is_question": False, "confidence": 0.8},
        {"type": "R9_Emotion", "emotion_label": "开心", "valence": "positive",
         "arousal": "high", "trigger": "被领导表扬", "intensity": "high",
         "confidence": 0.7},
        {"type": "R12_Outcome", "task_ref": "季度方案", "outcome": "completed",
         "resolution_note": "评审一次过", "confidence": 0.85},
    ],
}


# ===========================================================================
# 1. Schema (C5 gate) — the three atoms validate / reject correctly
# ===========================================================================

class TestR8Schema:
    def test_valid_r8(self):
        a = R8CognitionAtom(topic="k8s 调度", knowledge_tags=["调度器"],
                            engagement="high", confidence=0.8)
        assert a.type == "R8_Cognition"
        assert a.is_question is False  # default

    @pytest.mark.parametrize("bad", [
        {"topic": "x", "engagement": "bogus", "confidence": 0.5},        # bad enum
        {"engagement": "high", "confidence": 0.5},                       # missing topic
        {"topic": "x", "engagement": "high", "confidence": 1.5},         # conf > 1
    ])
    def test_invalid_r8_rejected(self, bad):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            R8CognitionAtom(**bad)

    def test_too_many_tags_rejected(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            R8CognitionAtom(topic="x", engagement="high", confidence=0.5,
                            knowledge_tags=[f"t{i}" for i in range(9)])  # max 8


class TestR9Schema:
    def test_valid_r9(self):
        a = R9EmotionAtom(emotion_label="焦虑", valence="negative", arousal="high",
                          trigger="体检报告", intensity="medium", confidence=0.6)
        assert a.valence == "negative"

    @pytest.mark.parametrize("bad", [
        {"emotion_label": "x", "valence": "up", "arousal": "high",    # bad valence
         "intensity": "high", "confidence": 0.5},
        {"emotion_label": "x", "valence": "positive", "arousal": "medium",  # bad arousal
         "intensity": "high", "confidence": 0.5},
        {"emotion_label": "x", "valence": "positive", "arousal": "high",    # missing intensity
         "confidence": 0.5},
    ])
    def test_invalid_r9_rejected(self, bad):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            R9EmotionAtom(**bad)


class TestR12Schema:
    def test_valid_r12(self):
        a = R12OutcomeAtom(task_ref="竞标", outcome="failed",
                           resolution_note="报价偏高", confidence=0.7)
        assert a.outcome == "failed"
        assert a.resolution_note is not None

    @pytest.mark.parametrize("bad", [
        {"task_ref": "x", "outcome": "won", "confidence": 0.5},     # bad outcome enum
        {"outcome": "completed", "confidence": 0.5},                # missing task_ref
        {"task_ref": "x", "outcome": "completed", "confidence": -0.1},  # conf < 0
    ])
    def test_invalid_r12_rejected(self, bad):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            R12OutcomeAtom(**bad)


# ===========================================================================
# 2. ConfidenceEngine Stage2 — type_adjustment + asr_quality + thresholds
# ===========================================================================

class TestStage2TypeAdjustment:
    """R1 intensity_weight (ADR-024): low mood/stress is a weaker signal.

    Same base confidence 0.7, state_type='stress' (no neutral-mood exemption),
    threshold 0.5 → effective = base × weight: high 0.70 (pass), medium 0.56
    (pass), low 0.35 (filtered).
    """

    @pytest.mark.parametrize("intensity,passes", [
        ("high", True), ("medium", True), ("low", False),
    ])
    def test_r1_intensity_weight(self, intensity, passes):
        eng = ConfidenceEngine()
        from plugins.memory.ptg.atom_schemas import R1SelfStateAtom
        atom = R1SelfStateAtom(state_type="stress", direction="up",
                               intensity=intensity, confidence=0.7)
        assert eng._passes(atom) is passes

    def test_low_intensity_weight_value(self):
        """The three weights are exactly ADR-024's spec (1.0 / 0.8 / 0.5)."""
        from plugins.memory.ptg.atom_schemas import R1SelfStateAtom
        eng = ConfidenceEngine()
        for intensity, want in [("high", 1.0), ("medium", 0.8), ("low", 0.5)]:
            a = R1SelfStateAtom(state_type="stress", direction="up",
                                intensity=intensity, confidence=0.7)
            assert eng._type_adjustment(a) == want

    def test_other_atoms_have_unit_adjustment(self):
        """Stage2 Phase 1b: only R1 carries a non-1.0 adjustment."""
        eng = ConfidenceEngine()
        assert eng._type_adjustment(R8CognitionAtom(
            topic="x", engagement="high", confidence=0.8)) == 1.0
        assert eng._type_adjustment(R12OutcomeAtom(
            task_ref="x", outcome="completed", confidence=0.8)) == 1.0


class TestStage2Thresholds:
    """The three new type thresholds: cognition 0.5 / emotion 0.3 / outcome 0.4."""

    def test_r8_cognition_threshold(self):
        eng = ConfidenceEngine()
        below = R8CognitionAtom(topic="x", engagement="high", confidence=0.49)
        above = R8CognitionAtom(topic="x", engagement="high", confidence=0.5)
        assert eng._passes(below) is False
        assert eng._passes(above) is True

    def test_r9_emotion_threshold(self):
        eng = ConfidenceEngine()
        below = R9EmotionAtom(emotion_label="x", valence="positive",
                              arousal="high", intensity="high", confidence=0.29)
        above = R9EmotionAtom(emotion_label="x", valence="positive",
                              arousal="high", intensity="high", confidence=0.3)
        assert eng._passes(below) is False
        assert eng._passes(above) is True

    def test_r12_outcome_threshold(self):
        eng = ConfidenceEngine()
        below = R12OutcomeAtom(task_ref="x", outcome="completed", confidence=0.39)
        above = R12OutcomeAtom(task_ref="x", outcome="completed", confidence=0.4)
        assert eng._passes(below) is False
        assert eng._passes(above) is True


class TestStage2AsrQuality:
    """asr_quality_factor multiplies the effective confidence (voice path)."""

    def test_asr_quality_discounts_below_gate(self):
        """R8 conf 0.8 normally passes cognition 0.5; at asr_quality 0.5 the
        effective 0.4 drops below the gate → filtered."""
        eng = ConfidenceEngine()
        atom = R8CognitionAtom(topic="x", engagement="high", confidence=0.8)
        assert eng._passes(atom) is True            # text origin
        eng.set_asr_quality(0.5)
        assert eng._passes(atom) is False            # voice, poor ASR

    def test_asr_quality_clamped(self):
        eng = ConfidenceEngine()
        eng.set_asr_quality(1.5)
        assert eng._asr_quality == 1.0              # clamped, no boost
        eng.set_asr_quality(-0.2)
        assert eng._asr_quality == 0.0

    def test_text_origin_is_unit(self):
        eng = ConfidenceEngine()
        assert eng._asr_quality == 1.0              # default text origin


class TestR1NeutralMoodExemptionPreserved:
    """The V5 neutral-mood exemption (mood + stable + low bypasses the 0.5 gate)
    survives Stage2 — applied via the exemption, not by zeroing the discount."""

    def test_neutral_mood_low_passes_despite_low_weight(self):
        from plugins.memory.ptg.atom_schemas import R1SelfStateAtom
        eng = ConfidenceEngine()
        # low intensity → weight 0.5 → effective would be 0.3 < 0.5, BUT the
        # neutral-mood exemption bypasses the gate entirely.
        atom = R1SelfStateAtom(state_type="mood", direction="stable",
                               intensity="low", confidence=0.6)
        assert eng._passes(atom) is True


# ===========================================================================
# 3. Atomizer dispatch — R8/R12 → meaning_events, R9 → feeling_events
# ===========================================================================

class TestAtomizerDispatch:
    def test_three_new_atoms_written_to_correct_tables(self, store):
        az = _atomizer(store, _callerReturning(supplement=_PHASE1B_OUTPUT))
        out = az.atomize(memo_id=_memo(store), source_text="...")
        assert out["ok"] is True
        assert out["written"] == 3
        assert out["filtered"] == 0 and out["invalid"] == 0
        assert store.count_rows("meaning_events") == 2   # R8 + R12
        assert store.count_rows("feeling_events") == 1   # R9
        assert store.count_rows("dlq_messages") == 0

    def test_r8_meaning_event_payload(self, store):
        az = _atomizer(store, _callerReturning(supplement=_PHASE1B_OUTPUT))
        az.atomize(memo_id=_memo(store), source_text="x")
        r8 = _rows(store,
                   "SELECT * FROM meaning_events WHERE atom_kind='R8'")[0]
        assert r8["intent_class"] == "Other"            # no 'learning' enum
        assert r8["task_description"] == "React diff 算法"
        assert json.loads(r8["topic_tags"]) == ["Hooks", "diff", "虚拟 DOM"]
        cn = json.loads(r8["completion_note"])
        assert cn == {"engagement": "high", "is_question": False}

    def test_r12_meaning_event_payload(self, store):
        az = _atomizer(store, _callerReturning(supplement=_PHASE1B_OUTPUT))
        az.atomize(memo_id=_memo(store), source_text="x")
        r12 = _rows(store,
                    "SELECT * FROM meaning_events WHERE atom_kind='R12'")[0]
        assert r12["intent_class"] == "Need_To_Do"
        assert r12["task_description"] == "季度方案"
        assert r12["task_status"] == "completed"        # outcome→status map
        cn = json.loads(r12["completion_note"])
        assert cn == {"outcome": "completed", "resolution_note": "评审一次过"}

    @pytest.mark.parametrize("outcome,status,overdue", [
        ("completed", "completed", 0),
        ("failed", "dismissed", 0),
        ("delayed", "pending", 1),
    ])
    def test_r12_outcome_to_status_mapping(self, store, outcome, status, overdue):
        payload = {"summary": "x", "atoms": [
            {"type": "R12_Outcome", "task_ref": "T", "outcome": outcome,
             "confidence": 0.85}]}
        az = _atomizer(store, _callerReturning(supplement=payload))
        az.atomize(memo_id=_memo(store), source_text="x")
        r12 = _rows(store,
                    "SELECT task_status, is_overdue FROM meaning_events "
                    "WHERE atom_kind='R12'")[0]
        assert r12["task_status"] == status
        assert r12["is_overdue"] == overdue

    def test_r9_feeling_event_payload(self, store):
        az = _atomizer(store, _callerReturning(supplement=_PHASE1B_OUTPUT))
        az.atomize(memo_id=_memo(store), source_text="x")
        r9 = _rows(store,
                   "SELECT * FROM feeling_events WHERE atom_kind='R9'")[0]
        assert r9["state_type"] == "mood"               # only fitting CHECK enum
        assert r9["direction"] == "up"                  # valence positive → up
        assert r9["intensity"] == "high"
        vad = json.loads(r9["emotion_vad"])
        assert vad == {"valence": "positive", "arousal": "high", "label": "开心"}
        trg = json.loads(r9["trigger_source"])
        # F3 (ADR-V6-044): R9 trigger_source now carries an ``entity`` key — the
        # post-hoc-resolved entity the emotion attaches to, or "" when the trigger
        # is a situation (here: 被领导表扬 → no known person) rather than an entity.
        assert trg == {"trigger": "被领导表扬", "entity": "", "atom": "R9_Emotion"}

    @pytest.mark.parametrize("valence,direction", [
        ("positive", "up"), ("negative", "down"), ("neutral", "stable"),
    ])
    def test_r9_valence_to_direction(self, store, valence, direction):
        payload = {"summary": "x", "atoms": [
            {"type": "R9_Emotion", "emotion_label": "e", "valence": valence,
             "arousal": "low", "trigger": "t", "intensity": "medium",
             "confidence": 0.7}]}
        az = _atomizer(store, _callerReturning(supplement=payload))
        az.atomize(memo_id=_memo(store), source_text="x")
        r9 = _rows(store,
                   "SELECT direction FROM feeling_events WHERE atom_kind='R9'")[0]
        assert r9["direction"] == direction


# ===========================================================================
# 4. Recall reconstruction — recent_atoms round-trips R8/R9/R12
# ===========================================================================

class TestRecallReconstruction:
    def test_recent_atoms_roundtrips_three_new_atoms(self, store):
        az = _atomizer(store, _callerReturning(supplement=_PHASE1B_OUTPUT))
        mid = _memo(store)
        az.atomize(memo_id=mid, source_text="x")
        atoms = store.recent_atoms(user_id="user-1", memo_id=mid)
        types = sorted(a["type"] for a in atoms)
        assert types == ["R12_Outcome", "R8_Cognition", "R9_Emotion"]

        r8 = next(a for a in atoms if a["type"] == "R8_Cognition")
        assert r8["topic"] == "React diff 算法"
        assert r8["knowledge_tags"] == ["Hooks", "diff", "虚拟 DOM"]
        assert r8["engagement"] == "high" and r8["is_question"] is False

        r9 = next(a for a in atoms if a["type"] == "R9_Emotion")
        assert r9["emotion_label"] == "开心"
        assert r9["valence"] == "positive" and r9["arousal"] == "high"
        assert r9["trigger"] == "被领导表扬"
        assert r9["intensity"] == "high"

        r12 = next(a for a in atoms if a["type"] == "R12_Outcome")
        assert r12["task_ref"] == "季度方案"
        assert r12["outcome"] == "completed"
        assert r12["resolution_note"] == "评审一次过"

    def test_r2_dispatch_is_purely_atom_kind(self, store):
        """A meaning_event with intent_class='Need_To_Do' but atom_kind='R7'
        reconstructs as R7 (intent is an R7 sub-classification), proving the
        dispatch is atom_kind-driven, not intent_class-driven."""
        mid = store.insert_memo(user_id="user-1", source_text="x", input_mode="text")
        store.insert_meaning_event(
            user_id="user-1", source_text="x", intent_class="Need_To_Do",
            task_description="should-be-R7", atom_kind="R7",
            confidence_base=0.8, relation_confidence=0.8, memo_id=mid)
        atoms = store.recent_atoms(user_id="user-1", memo_id=mid)
        assert any(a["type"] == "R7_Expression" and a["content_summary"] == "should-be-R7"
                   for a in atoms)
        assert not any(a["type"] == "R2_Task" for a in atoms)


# ===========================================================================
# 5. Graph materialization — R8/R12 become nodes, R9 does not
# ===========================================================================

class TestGraphMaterialization:
    def test_r8_creates_topic_node_and_learns_edge(self, store):
        az = _atomizer(store, _callerReturning(supplement=_PHASE1B_OUTPUT))
        az.atomize(memo_id=_memo(store), source_text="x")
        rels = store.relations_for_user("user-1")
        learns = [r for r in rels if r["relation_type"] == "learns"]
        assert len(learns) == 1
        assert learns[0]["object_name"] == "React diff 算法"
        assert learns[0]["object_type"] == "topic"
        assert learns[0]["value"] == "high"             # engagement

    def test_r12_creates_task_node_and_has_task_edge(self, store):
        az = _atomizer(store, _callerReturning(supplement=_PHASE1B_OUTPUT))
        az.atomize(memo_id=_memo(store), source_text="x")
        rels = store.relations_for_user("user-1")
        task_edges = [r for r in rels if r["relation_type"] == "has_task"
                      and r["object_name"] == "季度方案"]
        assert len(task_edges) == 1
        assert task_edges[0]["object_type"] == "task"
        assert task_edges[0]["value"] == "completed"   # outcome

    def test_r9_creates_no_graph_node(self, store):
        """An emotion is not an entity — R9 materializes no node/edge."""
        az = _atomizer(store, _callerReturning(supplement=_PHASE1B_OUTPUT))
        az.atomize(memo_id=_memo(store), source_text="x")
        rels = store.relations_for_user("user-1")
        # Only R8 (learns) + R12 (has_task) edges exist; nothing for R9.
        assert all(r["relation_type"] in ("learns", "has_task") for r in rels)
        assert not any("开心" in (r["object_name"] or "") for r in rels)


# ===========================================================================
# C7 — a malformed new atom goes to DLQ, valid siblings still land (V6 granular)
# ===========================================================================

class TestGranularSchemaRejection:
    def test_one_bad_new_atom_goes_to_dlq_siblings_survive(self, store):
        """A bogus R8 (bad engagement enum) is routed to DLQ; the valid R9/R12
        in the same memo still land (V6 granular gate, C2 nothing lost)."""
        payload = {"summary": "x", "atoms": [
            {"type": "R8_Cognition", "topic": "ok", "engagement": "bogus",
             "confidence": 0.8},                            # invalid → DLQ
            {"type": "R9_Emotion", "emotion_label": "开心", "valence": "positive",
             "arousal": "high", "trigger": "t", "intensity": "high",
             "confidence": 0.7},                            # valid → lands
            {"type": "R12_Outcome", "task_ref": "T", "outcome": "completed",
             "confidence": 0.85},                           # valid → lands
        ]}
        az = _atomizer(store, _callerReturning(supplement=payload))
        out = az.atomize(memo_id=_memo(store), source_text="x")
        assert out["ok"] is True
        assert out["written"] == 2                          # R9 + R12
        assert out["invalid"] == 1                          # R8
        assert store.count_rows("dlq_messages") == 1
        assert store.count_rows("feeling_events") == 1      # R9 survived
        assert store.count_rows("meaning_events") == 1      # R12 survived

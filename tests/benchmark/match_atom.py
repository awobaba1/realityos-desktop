"""Atom matcher for the HL-12 extraction eval — ported verbatim from V5.

V5 source: ``danao13/backend/tests/benchmark/run_eval.py::match_atom`` (ADR-201).
The matching logic is the evaluator's load-bearing component, so it lives in its
own module (not buried in run_eval) and has its own C4 regression test
(``test_match_atom.py``). Type-aware:

  * **R3_Person** — normalized name match, kinship synonym groups (老妈↔妈妈,
    娃↔孩子), plural-strip (同事们→同事), and bidirectional alias matching.
  * **R2_Task** — Chinese char-overlap ≥ ``TEXT_OVERLAP_THRESHOLD`` (no word
    boundaries).
  * **R1_SelfState** — state_type exact OR a semantic-equivalence group
    (energy/mood, energy/fatigue, stress/mood) when direction matches.
  * **R7_Expression** — intent_class exact OR a semantic-equivalence group.

These equivalences encode "the model got the semantics right, the label differs
in surface form" — without them recall is falsely depressed (V5 found R3 recall
artificially low before the synonym groups, ADR-201).

## Governance (ADR-V6-035, action 20)

The equivalence groups are the eval's **matching policy** — they decide which
predicted/expected pairs count as the same atom, so they directly move the
recall/precision numbers. ADR-V6-022 桶 C / action 20 flagged them as "偏松" with
**no governance surface** (inline literals, no version, no audit). This module
closes that gap WITHOUT changing the matching behaviour:

- All equivalences are now **module-level named constants** (lifted out of the
  ``match_atom`` body) — one auditable location, not buried inline.
- ``MATCH_ATOM_VERSION`` pins the policy; ANY change to a group or threshold is a
  deliberate, reviewed, version-bumped decision (C1: the matching policy is a
  decision).
- ``describe_equivalences()`` returns a structured snapshot so eval reports can
  log exactly which equivalences are active (transparency).

The "偏松" permissiveness is **intentional and retained** (zero behaviour change):
tightening these groups would regress recall without helping precision —
precision⑦ proved the threshold path dead (TP/FP confidence overlap is
non-separable; structural ceiling ~70 %, ADR-V6-026). So governance here =
visibility + versioning + honest documentation, NOT tightening. The existing
``test_match_atom.py`` C4 lock guarantees no behaviour drift; the new
``test_match_atom_governance.py`` pins the policy membership + version.
"""

from __future__ import annotations

import re

# ── Matching-policy version (ADR-V6-035 governance) ─────────────────────────
# Bump on ANY change to an equivalence group or threshold below. The matching
# policy is a decision (C1); a version makes every change a reviewed event.
MATCH_ATOM_VERSION = 1

# ── R3 Person kinship synonym groups ─────────────────────────────────────────
_PERSON_SYNONYM_GROUPS = [
    {"妈妈", "老妈", "妈", "母亲", "妈咪"},
    {"爸爸", "老爸", "爸", "父亲"},
    {"孩子", "娃", "娃娃", "宝宝", "儿子", "女儿", "小孩", "娃儿"},
    {"老婆", "妻子", "媳妇", "太太", "夫人"},
    {"老公", "丈夫", "先生"},
    {"老板", "领导", "上司"},
]
_PERSON_GROUP: dict[str, int] = {n: i for i, g in enumerate(_PERSON_SYNONYM_GROUPS) for n in g}

# ── R1 SelfState semantic-equivalence pairs (honored only when direction matches) ──
R1_STATE_EQUIVALENT_GROUPS: frozenset[frozenset[str]] = frozenset({
    frozenset({"energy", "mood"}),
    frozenset({"energy", "fatigue"}),
    frozenset({"stress", "mood"}),
})

# ── R7 Expression intent-class equivalence pairs ─────────────────────────────
R7_INTENT_EQUIVALENT_GROUPS: frozenset[frozenset[str]] = frozenset({
    frozenset({"Consumption", "Evaluation"}),
    frozenset({"Consumption", "Complaint"}),
    frozenset({"Health", "Complaint"}),
    frozenset({"Need_To_Do", "Help"}),
})

# ── Chinese char-overlap threshold for text atoms (R2/R8/R12) ────────────────
# Permissive by design (no word boundaries; surface phrasing varies). See module
# docstring governance note — tightening regresses recall (⑦, ADR-V6-026).
TEXT_OVERLAP_THRESHOLD = 0.4


def describe_equivalences() -> dict:
    """Structured snapshot of the current matching policy (governance).

    Use from run_eval / eval reports to log exactly which equivalences are
    active, so the matching policy behind a recall/precision number is visible
    (not a hidden inline literal). The policy is versioned
    (``MATCH_ATOM_VERSION``); any change is a deliberate, tested, version-bumped
    decision (C1). See module docstring for the "偏松 is intentional" rationale.
    """
    return {
        "version": MATCH_ATOM_VERSION,
        "r3_person_synonym_groups": [sorted(g) for g in _PERSON_SYNONYM_GROUPS],
        "r1_state_equivalent_pairs": [sorted(p) for p in R1_STATE_EQUIVALENT_GROUPS],
        "r7_intent_equivalent_pairs": [sorted(p) for p in R7_INTENT_EQUIVALENT_GROUPS],
        "text_overlap_threshold": TEXT_OVERLAP_THRESHOLD,
    }


def _norm_person(name: str) -> str:
    """归一人物称呼：strip 空白/复数'们'。中文无大小写，lower() 为占位。"""
    n = (name or "").strip().lower()
    if n.endswith("们"):  # 同事们 → 同事
        n = n[:-1]
    return n


def _person_names_match(pred_name: str, exp_name: str, pred_aliases, exp_aliases) -> bool:
    """R3 人物匹配：精确(归一后) → 同义等价组 → alias 双向。"""
    pn, en = _norm_person(pred_name), _norm_person(exp_name)
    if pn and pn == en:
        return True
    if pn in _PERSON_GROUP and en in _PERSON_GROUP and _PERSON_GROUP[pn] == _PERSON_GROUP[en]:
        return True
    pa = {_norm_person(a) for a in (pred_aliases or [])}
    ea = {_norm_person(a) for a in (exp_aliases or [])}
    if (en and en in pa) or (pn and pn in ea):
        return True
    return False


def _text_overlap(pred: str, exp: str, *, threshold: float = TEXT_OVERLAP_THRESHOLD) -> bool:
    """Chinese char-overlap (no word boundaries). Used by the Phase 1b atom
    matchers (R8 topic, R12 task_ref) — same logic R2_Task inlines below."""
    p = re.sub(r'[，。、！？\s,\.!?]', '', (pred or "").lower())
    e = re.sub(r'[，。、！？\s,\.!?]', '', (exp or "").lower())
    if not e:
        return False
    common = sum(1 for c in e if c in p)
    return common / len(e) >= threshold


def match_atom(predicted: dict, expected: dict) -> bool:
    """Check if a predicted atom matches an expected atom (type-aware)."""
    if predicted.get("type") != expected.get("type"):
        return False

    atom_type = expected.get("type")

    if atom_type == "R3_Person":
        return _person_names_match(
            predicted.get("person_name", ""),
            expected.get("person_name", ""),
            predicted.get("aliases"),
            expected.get("aliases"),
        )

    elif atom_type == "R2_Task":
        pred_text = predicted.get("task_description", "").lower()
        exp_text = expected.get("task_description", "").lower()
        if not pred_text or not exp_text:
            return False
        pred_clean = re.sub(r'[，。、！？\s,\.!?]', '', pred_text)
        exp_clean = re.sub(r'[，。、！？\s,\.!?]', '', exp_text)
        if not exp_clean:
            return False
        common = sum(1 for c in exp_clean if c in pred_clean)
        return common / len(exp_clean) >= TEXT_OVERLAP_THRESHOLD

    elif atom_type == "R1_SelfState":
        if (predicted.get("state_type") == expected.get("state_type")
                and predicted.get("direction") == expected.get("direction")):
            return True
        pred_state = predicted.get("state_type", "")
        exp_state = expected.get("state_type", "")
        if (frozenset({pred_state, exp_state}) in R1_STATE_EQUIVALENT_GROUPS
                and predicted.get("direction") == expected.get("direction")):
            return True
        return False

    elif atom_type == "R7_Expression":
        pred_class = predicted.get("intent_class", "")
        exp_class = expected.get("intent_class", "")
        if pred_class == exp_class:
            return True
        return frozenset({pred_class, exp_class}) in R7_INTENT_EQUIVALENT_GROUPS

    # ── Phase 1b atoms (ADR-V6-016) ──────────────────────────────────────
    # R8_Cognition: topic is text → char-overlap ≥ threshold (same philosophy as
    # R2_Task; knowledge_tags phrasing varies, so topic is load-bearing).
    elif atom_type == "R8_Cognition":
        return _text_overlap(predicted.get("topic", ""),
                             expected.get("topic", ""))

    # R9_Emotion: the emotion POLARITY (valence) is the load-bearing semantic —
    # two extracts that both read the same input as positive match even if the
    # surface label differs (开心 vs 高兴). Phase 1b label vocab isn't standardized.
    elif atom_type == "R9_Emotion":
        return predicted.get("valence") == expected.get("valence") \
            and bool(predicted.get("valence"))

    # R12_Outcome: outcome enum is small + exact-matchable; task_ref is text →
    # char-overlap ≥ threshold (a 'completed 述职报告' matches 'completed 季度述职').
    elif atom_type == "R12_Outcome":
        if predicted.get("outcome") != expected.get("outcome"):
            return False
        return _text_overlap(predicted.get("task_ref", ""),
                             expected.get("task_ref", ""))

    return False

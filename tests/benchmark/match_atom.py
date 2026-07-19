"""Atom matcher for the HL-12 extraction eval — ported verbatim from V5.

V5 source: ``danao13/backend/tests/benchmark/run_eval.py::match_atom`` (ADR-201).
The matching logic is the evaluator's load-bearing component, so it lives in its
own module (not buried in run_eval) and has its own C4 regression test
(``test_match_atom.py``). Type-aware:

  * **R3_Person** — normalized name match, kinship synonym groups (老妈↔妈妈,
    娃↔孩子), plural-strip (同事们→同事), and bidirectional alias matching.
  * **R2_Task** — Chinese char-overlap ≥ 0.4 (no word boundaries).
  * **R1_SelfState** — state_type exact OR a semantic-equivalence group
    (energy/mood, energy/fatigue, stress/mood) when direction matches.
  * **R7_Expression** — intent_class exact OR a semantic-equivalence group.

These equivalences encode "the model got the semantics right, the label differs
in surface form" — without them recall is falsely depressed (V5 found R3 recall
artificially low before the synonym groups, ADR-201).
"""

from __future__ import annotations

import re

_PERSON_SYNONYM_GROUPS = [
    {"妈妈", "老妈", "妈", "母亲", "妈咪"},
    {"爸爸", "老爸", "爸", "父亲"},
    {"孩子", "娃", "娃娃", "宝宝", "儿子", "女儿", "小孩", "娃儿"},
    {"老婆", "妻子", "媳妇", "太太", "夫人"},
    {"老公", "丈夫", "先生"},
    {"老板", "领导", "上司"},
]
_PERSON_GROUP: dict[str, int] = {n: i for i, g in enumerate(_PERSON_SYNONYM_GROUPS) for n in g}


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
        return common / len(exp_clean) >= 0.4

    elif atom_type == "R1_SelfState":
        if (predicted.get("state_type") == expected.get("state_type")
                and predicted.get("direction") == expected.get("direction")):
            return True
        _STATE_EQUIVALENT = {
            frozenset({"energy", "mood"}),
            frozenset({"energy", "fatigue"}),
            frozenset({"stress", "mood"}),
        }
        pred_state = predicted.get("state_type", "")
        exp_state = expected.get("state_type", "")
        if (frozenset({pred_state, exp_state}) in _STATE_EQUIVALENT
                and predicted.get("direction") == expected.get("direction")):
            return True
        return False

    elif atom_type == "R7_Expression":
        pred_class = predicted.get("intent_class", "")
        exp_class = expected.get("intent_class", "")
        if pred_class == exp_class:
            return True
        _R7_EQUIVALENT = {
            frozenset({"Consumption", "Evaluation"}),
            frozenset({"Consumption", "Complaint"}),
            frozenset({"Health", "Complaint"}),
            frozenset({"Need_To_Do", "Help"}),
        }
        return frozenset({pred_class, exp_class}) in _R7_EQUIVALENT

    return False

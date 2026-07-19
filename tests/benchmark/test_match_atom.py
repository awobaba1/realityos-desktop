"""C4 regression: match_atom R3 synonym normalization + per-type matching.

Ported from V5 (danao13/backend/tests/benchmark/test_match_atom.py). Locks the
matching logic the eval depends on — a regression here silently inflates/deflates
the precision/recall numbers (the anti-fake-green gate).
"""

from tests.benchmark.match_atom import match_atom


def _r3(pred_name, exp_name, pred_aliases=None, exp_aliases=None):
    return match_atom(
        {"type": "R3_Person", "person_name": pred_name, "aliases": pred_aliases or []},
        {"type": "R3_Person", "person_name": exp_name, "aliases": exp_aliases or []},
    )


# ── R3 同义称呼归一 ──
def test_r3_exact_match():
    assert _r3("妈妈", "妈妈")
    assert _r3("张三", "张三")


def test_r3_synonym_kinship():
    assert _r3("老妈", "妈妈")
    assert _r3("妈妈", "母亲")
    assert _r3("老爸", "爸爸")


def test_r3_synonym_offspring():
    assert _r3("娃", "孩子")
    assert _r3("宝宝", "孩子")
    assert _r3("儿子", "孩子")


def test_r3_plural_strip():
    assert _r3("同事们", "同事")
    assert _r3("朋友们", "朋友")


def test_r3_alias_bidirectional():
    assert _r3("小红红", "小红", pred_aliases=["小红"])
    assert _r3("老张", "张总", exp_aliases=["老张"])


def test_r3_distinct_names_do_not_match():
    assert not _r3("张三", "李四")
    assert not _r3("妈妈", "爸爸")
    assert not _r3("同事", "客户")


def test_r3_empty_safe():
    assert not _r3("", "妈妈")
    assert not _r3("妈妈", "")


# ── 其他类型 ──
def test_r2_char_overlap():
    assert match_atom(
        {"type": "R2_Task", "task_description": "提交季度报告给王总"},
        {"type": "R2_Task", "task_description": "提交季度报告"},
    )


def test_r1_state_direction():
    assert match_atom(
        {"type": "R1_SelfState", "state_type": "mood", "direction": "up"},
        {"type": "R1_SelfState", "state_type": "energy", "direction": "up"},
    )
    assert not match_atom(
        {"type": "R1_SelfState", "state_type": "mood", "direction": "stable"},
        {"type": "R1_SelfState", "state_type": "mood", "direction": "up"},
    )


def test_r7_intent_class():
    assert match_atom(
        {"type": "R7_Expression", "intent_class": "Consumption"},
        {"type": "R7_Expression", "intent_class": "Evaluation"},
    )


def test_cross_type_never_matches():
    assert not match_atom(
        {"type": "R3_Person", "person_name": "妈妈"},
        {"type": "R2_Task", "task_description": "妈妈"},
    )

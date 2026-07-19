"""RealityOS V6 — read/recall surface regression tests (ADR-V6-012).

Locks the three new read APIs (recent_atoms, search_entities, relations_for_user)
and the prefetch renderer (§4.3A). These are the read half of the brain — the
Atomizer writes atoms nothing could read back until these existed (Explore recon
finding #3). The eval harness depends on recent_atoms; prefetch depends on the
renderer; both must not regress.
"""

from __future__ import annotations

import pytest

from plugins.memory.ptg.recall import _estimate_tokens, render_relations_block
from plugins.memory.ptg.store import PTGStore


@pytest.fixture
def store(tmp_path):
    s = PTGStore(db_path=str(tmp_path / "ptg.db"))
    s.ensure_founder("u1", "founder@realityos.local")
    yield s
    s.close()


# ── recent_atoms: reconstruct post-gate atoms from the event tables ──────────

def test_recent_atoms_reconstructs_all_types(store):
    mid = store.insert_memo(user_id="u1", source_text="x", input_mode="text")
    store.insert_identity_event(
        user_id="u1", source_text="x", person_name="张三", mention_context="开会",
        sentiment="positive", interaction_type="meeting",
        confidence_base=0.9, relation_confidence=0.95, memo_id=mid)
    store.insert_meaning_event(
        user_id="u1", source_text="x", intent_class="Need_To_Do",
        task_description="交报告", urgency="high",
        confidence_base=0.8, relation_confidence=0.85, memo_id=mid)
    store.insert_meaning_event(
        user_id="u1", source_text="x", intent_class="Complaint",
        task_description="服务太差",
        confidence_base=0.7, relation_confidence=0.7, memo_id=mid)
    store.insert_feeling_event(
        user_id="u1", source_text="x", state_type="stress", direction="up",
        intensity="medium", confidence_base=0.7, relation_confidence=0.75, memo_id=mid)
    store.insert_entity_event(
        user_id="u1", source_text="x", entity_name="厦门", entity_category="place",
        confidence_base=0.8, relation_confidence=0.8, memo_id=mid)

    atoms = store.recent_atoms(user_id="u1", memo_id=mid)
    types = sorted(a["type"] for a in atoms)
    assert types == ["R0_Entity", "R1_SelfState", "R2_Task", "R3_Person", "R7_Expression"]
    r3 = next(a for a in atoms if a["type"] == "R3_Person")
    assert r3["person_name"] == "张三"
    assert r3["confidence"] == 0.95  # prefers relation_confidence over confidence_base
    r2 = next(a for a in atoms if a["type"] == "R2_Task")
    assert r2["task_description"] == "交报告" and r2["urgency"] == "high"
    r7 = next(a for a in atoms if a["type"] == "R7_Expression")
    assert r7["intent_class"] == "Complaint"  # non-Need_To_Do → R7


def test_recent_atoms_respects_soft_delete(store):
    mid = store.insert_memo(user_id="u1", source_text="x", input_mode="text")
    eid = store.insert_identity_event(
        user_id="u1", source_text="x", person_name="张三",
        confidence_base=0.9, relation_confidence=0.9, memo_id=mid)
    store.soft_delete("identity_events", eid)
    atoms = store.recent_atoms(user_id="u1", memo_id=mid)
    assert all(a["type"] != "R3_Person" for a in atoms)


def test_recent_atoms_without_memo_id_returns_all(store):
    store.insert_identity_event(
        user_id="u1", source_text="x", person_name="张三",
        confidence_base=0.9, relation_confidence=0.9)
    store.insert_identity_event(
        user_id="u1", source_text="y", person_name="李四",
        confidence_base=0.9, relation_confidence=0.9)
    atoms = store.recent_atoms(user_id="u1")
    assert len(atoms) == 2


# ── search_entities + relations_for_user ─────────────────────────────────────

def _seed_graph(store):
    self_id = store.upsert_entity(user_id="u1", entity_name="我", entity_type="person",
                                  properties={"is_self": True})
    zhang = store.upsert_entity(user_id="u1", entity_name="张三", entity_type="person",
                                properties={"sentiment": "positive"})
    store.upsert_entity(user_id="u1", entity_name="北京", entity_type="context")
    store.upsert_relation(user_id="u1", subject_id=self_id, object_id=zhang,
                          relation_type="interacts_with", value="meeting", confidence=0.95)
    return self_id, zhang


def test_search_entities_finds_by_name(store):
    _seed_graph(store)
    hits = store.search_entities("u1", "张三")
    assert len(hits) == 1
    assert hits[0]["entity_name"] == "张三"
    assert hits[0]["entity_type"] == "person"


def test_search_entities_empty_query(store):
    _seed_graph(store)
    assert store.search_entities("u1", "") == []
    assert store.search_entities("u1", "   ") == []


def test_relations_for_user_joins_names(store):
    _, zhang = _seed_graph(store)
    rels = store.relations_for_user("u1")
    assert len(rels) == 1
    r = rels[0]
    assert r["relation_type"] == "interacts_with"
    assert r["subject_name"] == "我"
    assert r["object_name"] == "张三"
    assert r["confidence"] == 0.95
    # Filtered by entity
    rels_f = store.relations_for_user("u1", entity_id=zhang)
    assert len(rels_f) == 1


def test_relations_ordered_confidence_desc(store):
    self_id = store.upsert_entity(user_id="u1", entity_name="我", entity_type="person")
    a = store.upsert_entity(user_id="u1", entity_name="任务A", entity_type="task")
    b = store.upsert_entity(user_id="u1", entity_name="任务B", entity_type="task")
    store.upsert_relation(user_id="u1", subject_id=self_id, object_id=a,
                          relation_type="has_task", confidence=0.6)
    store.upsert_relation(user_id="u1", subject_id=self_id, object_id=b,
                          relation_type="has_task", confidence=0.9)
    rels = store.relations_for_user("u1")
    assert rels[0]["object_name"] == "任务B"  # higher confidence first


# ── render_relations_block (§4.3A) ───────────────────────────────────────────

def test_render_empty_when_no_match(store):
    _seed_graph(store)
    assert render_relations_block(store, "u1", "不存在的人") == ""


def test_render_empty_on_empty_query(store):
    _seed_graph(store)
    assert render_relations_block(store, "u1", "") == ""


def test_render_produces_block_with_self_edge(store):
    _seed_graph(store)
    block = render_relations_block(store, "u1", "张三")
    assert "RealityOS 图谱" in block
    assert "张三" in block
    assert "互动" in block  # interacts_with verb
    assert "置信0.95" in block


def test_render_type_weighting(store):
    """A person tie outranks a mention even at lower confidence (§4.3A)."""
    self_id = store.upsert_entity(user_id="u1", entity_name="我", entity_type="person")
    # Two entities BOTH matching the query token "项目"
    person = store.upsert_entity(user_id="u1", entity_name="项目负责人", entity_type="person")
    ctx = store.upsert_entity(user_id="u1", entity_name="项目地点", entity_type="context")
    store.upsert_relation(user_id="u1", subject_id=self_id, object_id=ctx,
                          relation_type="mentions", confidence=0.99)  # high conf mention
    store.upsert_relation(user_id="u1", subject_id=self_id, object_id=person,
                          relation_type="interacts_with", confidence=0.5)  # low conf person
    block = render_relations_block(store, "u1", "项目")
    # person (interacts_with) should appear before context (mentions) despite lower conf
    pos_person = block.find("项目负责人")
    pos_ctx = block.find("项目地点")
    assert 0 <= pos_person < pos_ctx


def test_render_respects_token_budget(store):
    """Hard cap — the block never blows past the budget."""
    self_id = store.upsert_entity(user_id="u1", entity_name="我", entity_type="person")
    for i in range(30):
        e = store.upsert_entity(user_id="u1", entity_name=f"人物{i}", entity_type="person")
        store.upsert_relation(user_id="u1", subject_id=self_id, object_id=e,
                              relation_type="interacts_with", confidence=0.9 - i * 0.001)
    block = render_relations_block(store, "u1", "人物", token_budget=80)
    assert _estimate_tokens(block) <= 80
    assert block.count("\n- ") >= 1  # at least one line fit


def test_render_no_self_loop_noise(store):
    """The self node alone (no edges to others) renders nothing — no '我与我自己互动'."""
    store.upsert_entity(user_id="u1", entity_name="我", entity_type="person",
                        properties={"is_self": True})
    assert render_relations_block(store, "u1", "我") == ""

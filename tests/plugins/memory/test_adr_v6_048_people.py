"""C4 regression: M-domain people roster + profile (ADR-V6-048 / A5).

Locks the pure-SQL aggregators: ``list_people`` (roster ordering + self-exclusion
+ soft-delete) and ``person_profile`` (header / interactions / contexts /
relations / emotions). Pure-logic coverage; the CLI smoke is in
``test_people_cmd.py``.
"""

from __future__ import annotations

import json

import pytest

from plugins.memory.ptg.store import PTGStore


USER = "u1"


@pytest.fixture
def store(tmp_path):
    s = PTGStore(db_path=str(tmp_path / "ptg.db"))
    s.ensure_founder(USER, "founder@realityos.local")
    yield s
    s.close()


def _person(store, name="张三", aliases=None, mention_count=None):
    props = {}
    if aliases:
        props["aliases"] = aliases
    eid = store.upsert_entity(
        user_id=USER, entity_name=name, entity_type="person", properties=props)
    if mention_count:
        store._conn.execute(
            "UPDATE entities SET mention_count=? WHERE id=?", (mention_count, eid))
        store._conn.commit()
    return eid


# ===========================================================================
# list_people
# ===========================================================================

class TestListPeople:
    def test_orders_by_mention_count_excludes_self(self, store):
        store.ensure_self_entity(USER)  # the "我" self-node
        _person(store, "甲", mention_count=3)
        _person(store, "乙", mention_count=7)
        _person(store, "丙", mention_count=1)
        rows = store.list_people(USER)
        names = [r["entity_name"] for r in rows]
        assert names == ["乙", "甲", "丙"]
        assert "我" not in names  # self-node excluded
        for r in rows:
            assert {"entity_id", "entity_name", "mention_count",
                    "first_seen_at", "last_seen_at", "aliases"} <= set(r)

    def test_excludes_non_person_entities(self, store):
        _person(store, "张三")
        store.upsert_entity(user_id=USER, entity_name="写周报",
                            entity_type="task")  # not a person
        rows = store.list_people(USER)
        assert [r["entity_name"] for r in rows] == ["张三"]

    def test_respects_soft_delete(self, store):
        eid = _person(store, "李四")
        store.soft_delete("entities", eid)
        assert store.list_people(USER) == []

    def test_aliases_surface(self, store):
        _person(store, "王五", aliases=["老王", "Wang"])
        rows = store.list_people(USER)
        assert rows[0]["aliases"] == ["老王", "Wang"]


# ===========================================================================
# person_profile
# ===========================================================================

class TestPersonProfile:
    def test_not_found_and_not_a_person(self, store):
        assert store.person_profile(USER, "nope")["found"] is False
        eid = store.upsert_entity(user_id=USER, entity_name="写周报", entity_type="task")
        r = store.person_profile(USER, eid)
        assert r["found"] is False and r["reason"].startswith("not_a_person")

    def test_header_minimal(self, store):
        eid = _person(store, "张三", aliases=["老张"])
        p = store.person_profile(USER, eid)
        assert p["found"] and p["entity_name"] == "张三"
        assert p["aliases"] == ["老张"]
        assert p["entity_id"] == eid

    def test_interaction_breakdown_by_name_and_alias(self, store):
        eid = _person(store, "张三", aliases=["老张"])
        for _ in range(2):
            store.insert_identity_event(
                user_id=USER, source_text="x", person_name="张三",
                sentiment="positive", interaction_type="meeting",
                confidence_base=0.9, relation_confidence=0.9)
        store.insert_identity_event(
            user_id=USER, source_text="x", person_name="老张",  # alias
            sentiment="negative", interaction_type="conflict",
            confidence_base=0.9, relation_confidence=0.9)
        store.insert_identity_event(
            user_id=USER, source_text="x", person_name="其他人",  # different person
            sentiment="neutral", interaction_type="casual",
            confidence_base=0.9, relation_confidence=0.9)
        p = store.person_profile(USER, eid)
        brk = p["interaction_breakdown"]
        assert brk["total"] == 3  # 2 张三 + 1 老张 (alias); 其他 excluded
        assert brk["by_type"]["meeting"] == 2 and brk["by_type"]["conflict"] == 1
        assert brk["by_sentiment"]["positive"] == 2 and brk["by_sentiment"]["negative"] == 1

    def test_recent_contexts_ordered_desc(self, store):
        eid = _person(store, "张三")
        store.insert_identity_event(
            user_id=USER, source_text="x", person_name="张三", mention_context="早",
            confidence_base=0.9, relation_confidence=0.9, timestamp="2026-07-01T00:00:00+00:00")
        store.insert_identity_event(
            user_id=USER, source_text="x", person_name="张三", mention_context="晚",
            confidence_base=0.9, relation_confidence=0.9, timestamp="2026-07-20T00:00:00+00:00")
        p = store.person_profile(USER, eid, recent_limit=10)
        ctxs = [c["context"] for c in p["recent_contexts"]]
        assert ctxs == ["晚", "早"]  # newest first

    def test_relations_neighbourhood(self, store):
        a = _person(store, "张三")
        b = _person(store, "李四")
        store.upsert_relation(user_id=USER, subject_id=a, object_id=b,
                              relation_type="colleague", value="同事", confidence=0.8)
        p = store.person_profile(USER, a)
        assert len(p["relations"]) == 1
        rel = p["relations"][0]
        assert rel["relation_type"] == "colleague"
        assert {rel["subject_name"], rel["object_name"]} == {"张三", "李四"}

    def test_emotions_match_r9_entity(self, store):
        eid = _person(store, "张三")
        # R9 emotion about 张三
        store.insert_feeling_event(
            user_id=USER, source_text="x", confidence_base=0.8, relation_confidence=0.8,
            state_type="mood", direction="up", intensity="high", atom_kind="R9",
            trigger_source=json.dumps({"trigger": "被表扬", "entity": "张三"}))
        # R9 emotion about someone else
        store.insert_feeling_event(
            user_id=USER, source_text="x", confidence_base=0.8, relation_confidence=0.8,
            state_type="mood", direction="down", intensity="low", atom_kind="R9",
            trigger_source=json.dumps({"trigger": "x", "entity": "李四"}))
        # R1 self-state (atom_kind R1) — must be excluded
        store.insert_feeling_event(
            user_id=USER, source_text="x", confidence_base=0.8, relation_confidence=0.8,
            state_type="stress", direction="up", intensity="medium", atom_kind="R1",
            trigger_source=json.dumps({"trigger": "x", "entity": "张三"}))
        p = store.person_profile(USER, eid)
        assert p["emotions"]["count"] == 1
        assert p["emotions"]["triggers"][0]["trigger"] == "被表扬"

    def test_full_profile_shape(self, store):
        eid = _person(store, "张三")
        p = store.person_profile(USER, eid)
        for key in ("found", "entity_id", "entity_name", "mention_count",
                    "first_seen_at", "last_seen_at", "aliases",
                    "interaction_breakdown", "recent_contexts", "relations", "emotions"):
            assert key in p

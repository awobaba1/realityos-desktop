"""C4 regression: K-domain (ADR-V6-044) — F3 + F4 + F7.

F3 (R9→entity): the atomizer resolves each R9 emotion's trigger against the
user's known entities and writes ``trigger_source.entity``, so K-correlation
can group emotions by entity (Phase 1 deliberately gave R9 no graph node;
Phase 2 K-domain breaks that boundary).

F4 (store infra): ``relations.stale_at`` column + ``upsert_relation(delta=)``
+ ``transaction()`` atomic context + ``mark_k_correlation_stale()`` pure-UPDATE
invalidation (C2 append-only) + ``ensure_self_entity()``.

F7 (compute_k_correlations): pure-statistics port of danao14, adapted to V6's
CATEGORICAL valence (negative = ``valence == "negative"``), F6 sample-gated
confidence, real entity-id FKs, and atomic recompute+revive+invalidate.
correlation != causation (PRD 01:93).
"""

from __future__ import annotations

import json

import pytest

from plugins.memory.ptg.atomizer import Atomizer
from plugins.memory.ptg.confidence import ConfidenceEngine
from plugins.memory.ptg.confidence_gate import CONF_MID, MIN_SAMPLE
from plugins.memory.ptg.k_correlation import K_RELATION_TYPE, compute_k_correlations
from plugins.memory.ptg.store import PTGStore

USER = "user-1"
TS = "2026-07-08T10:00:00+00:00"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    s = PTGStore(db_path=str(tmp_path / "ptg.db"))
    s.ensure_founder(USER, "founder@realityos.local")
    yield s
    s.close()


def _atomizer(store, **kw):
    return Atomizer(store, user_id=USER, confidence_engine=ConfidenceEngine(),
                    materialize_graph=False, **kw)


def _add_entity(store, name):
    return store.upsert_entity(user_id=USER, entity_name=name, entity_type="person")


def _add_r9(store, entity, valence, n=1, *, trigger="x"):
    """Seed n R9 feeling_events tied to an entity name with a categorical valence."""
    for _ in range(n):
        store.insert_feeling_event(
            user_id=USER, source_text="x", state_type="mood",
            direction={"positive": "up", "negative": "down", "neutral": "stable"}[valence],
            intensity="high", atom_kind="R9",
            emotion_vad=json.dumps({"valence": valence, "arousal": "high"}),
            trigger_source=json.dumps(
                {"trigger": trigger, "entity": entity, "atom": "R9_Emotion"}),
            confidence_base=0.9, relation_confidence=0.9, timestamp=TS)


def _k_edges(store):
    return store._conn.execute(
        "SELECT object_id, value, confidence, stale_at, delta "
        "FROM relations WHERE relation_type = 'K_Correlation'").fetchall()


# ===========================================================================
# F4: store infrastructure
# ===========================================================================

class TestF4Transaction:
    def test_transaction_commits(self, store):
        sid = store.ensure_self_entity(USER)
        eid = _add_entity(store, "alice")
        rid = store.upsert_relation(user_id=USER, subject_id=sid, object_id=eid,
                                    relation_type=K_RELATION_TYPE, value="negative",
                                    confidence=0.6)
        with store.transaction():
            store._conn.execute("UPDATE relations SET confidence=0.8 WHERE id=?", (rid,))
        got = store._conn.execute("SELECT confidence FROM relations WHERE id=?", (rid,)).fetchone()[0]
        assert got == 0.8

    def test_transaction_rolls_back_on_exception(self, store):
        sid = store.ensure_self_entity(USER)
        eid = _add_entity(store, "alice")
        rid = store.upsert_relation(user_id=USER, subject_id=sid, object_id=eid,
                                    relation_type=K_RELATION_TYPE, value="negative",
                                    confidence=0.6)
        with pytest.raises(RuntimeError):
            with store.transaction():
                store._conn.execute("UPDATE relations SET confidence=0.99 WHERE id=?", (rid,))
                raise RuntimeError("boom")
        # rolled back → stays 0.6 (no orphan half-state)
        got = store._conn.execute("SELECT confidence FROM relations WHERE id=?", (rid,)).fetchone()[0]
        assert got == 0.6


class TestF4UpsertRelationDelta:
    def test_delta_written_on_insert(self, store):
        sid = store.ensure_self_entity(USER)
        eid = _add_entity(store, "alice")
        rid = store.upsert_relation(
            user_id=USER, subject_id=sid, object_id=eid,
            relation_type=K_RELATION_TYPE, value="negative", confidence=0.6,
            delta={"sample_size": 12, "lift": 2.0})
        row = store._conn.execute("SELECT delta FROM relations WHERE id=?", (rid,)).fetchone()
        assert json.loads(row[0]) == {"sample_size": 12, "lift": 2.0}

    def test_delta_overwritten_on_revidence(self, store):
        sid = store.ensure_self_entity(USER)
        eid = _add_entity(store, "alice")
        rid = store.upsert_relation(
            user_id=USER, subject_id=sid, object_id=eid,
            relation_type=K_RELATION_TYPE, value="negative", confidence=0.6,
            delta={"sample_size": 12, "lift": 2.0})
        # re-evidence with a fresh delta snapshot (latest wins)
        store.upsert_relation(
            user_id=USER, subject_id=sid, object_id=eid,
            relation_type=K_RELATION_TYPE, value="negative", confidence=0.9,
            delta={"sample_size": 42, "lift": 1.3})
        row = store._conn.execute("SELECT delta, confidence FROM relations WHERE id=?", (rid,)).fetchone()
        assert json.loads(row[0])["sample_size"] == 42
        assert row[1] == 0.9  # max(old,new)


class TestF4MarkStale:
    def test_pure_update_not_delete(self, store):
        """C2: mark_stale only sets stale_at; value/delta/evidence preserved."""
        sid = store.ensure_self_entity(USER)
        alice = _add_entity(store, "alice")
        bob = _add_entity(store, "bob")
        store.upsert_relation(user_id=USER, subject_id=sid, object_id=alice,
                              relation_type=K_RELATION_TYPE, value="negative",
                              confidence=0.6, delta={"sample_size": 12})
        store.upsert_relation(user_id=USER, subject_id=sid, object_id=bob,
                              relation_type=K_RELATION_TYPE, value="positive",
                              confidence=0.6, delta={"sample_size": 12})
        n = store.mark_k_correlation_stale(user_id=USER, subject_id=sid,
                                           keep_object_ids={alice}, now_ts=1753000000.0)
        assert n == 1  # bob invalidated, alice kept
        rows = {r[0]: r for r in _k_edges(store)}
        assert rows[alice][3] is None       # alice active
        assert rows[bob][3] is not None     # bob stale
        # value/delta PRESERVED on the stale row (not deleted)
        assert rows[bob][1] == "positive"
        assert json.loads(rows[bob][4])["sample_size"] == 12

    def test_idempotent_only_null_stale(self, store):
        """Re-marking does not re-stamp an already-stale edge (rowcount 0)."""
        sid = store.ensure_self_entity(USER)
        bob = _add_entity(store, "bob")
        store.upsert_relation(user_id=USER, subject_id=sid, object_id=bob,
                              relation_type=K_RELATION_TYPE, value="positive", confidence=0.6)
        store.mark_k_correlation_stale(user_id=USER, subject_id=sid,
                                       keep_object_ids=set(), now_ts=1753000000.0)
        # second mark — bob already stale → 0 newly invalidated
        n = store.mark_k_correlation_stale(user_id=USER, subject_id=sid,
                                           keep_object_ids=set(), now_ts=1753000001.0)
        assert n == 0


class TestF4EnsureSelfEntity:
    def test_find_or_create_idempotent(self, store):
        a = store.ensure_self_entity(USER)
        b = store.ensure_self_entity(USER)
        assert a == b, "ensure_self_entity must return the SAME id on repeat calls"


# ===========================================================================
# F3: R9 → entity resolution
# ===========================================================================

class TestF3ResolveTriggerEntity:
    def test_match(self):
        assert Atomizer._resolve_trigger_entity("和张三吵架", ["张三", "李四"]) == "张三"

    def test_longest_match_wins(self):
        assert Atomizer._resolve_trigger_entity("和张三丰聊", ["张三", "张三丰"]) == "张三丰"

    def test_no_match_returns_empty(self):
        assert Atomizer._resolve_trigger_entity("甲方改需求", ["张三"]) == ""

    def test_empty_trigger(self):
        assert Atomizer._resolve_trigger_entity(None, ["张三"]) == ""
        assert Atomizer._resolve_trigger_entity("", ["张三"]) == ""

    def test_empty_names(self):
        assert Atomizer._resolve_trigger_entity("anything", []) == ""


class TestF3TriggerEntityNames:
    def test_loads_user_entities(self, store):
        _add_entity(store, "alice")
        _add_entity(store, "bob")
        az = _atomizer(store)
        az.atomize  # ensure constructable
        names = az._trigger_entity_names()
        assert "alice" in names and "bob" in names

    def test_dedupes_and_drops_short(self, store):
        _add_entity(store, "alice")
        _add_entity(store, "alice")  # dup name → same normalized node, no dup
        _add_entity(store, "a")       # 1-char → dropped (too noisy)
        az = _atomizer(store)
        names = az._trigger_entity_names()
        assert names.count("alice") == 1
        assert "a" not in names


class TestF3WriteAtomPopulatesEntity:
    def test_r9_write_resolves_entity_into_trigger_source(self, store):
        """F3 end-to-end: _write_atom on an R9 atom populates trigger_source.entity
        from the user's entities (Option B post-hoc resolution)."""
        _add_entity(store, "alice")
        memo_id = store.insert_memo(user_id=USER, source_text="和alice吵架很烦",
                                    input_mode="text")
        az = _atomizer(store)
        from plugins.memory.ptg.atom_schemas import R9EmotionAtom
        az._write_atom(
            R9EmotionAtom(emotion_label="愤怒", valence="negative", arousal="high",
                          trigger="和alice吵架", intensity="high", confidence=0.8),
            memo_id=memo_id, source_text="和alice吵架很烦",
            input_mode="text", llm_call_id="llm-1")
        row = store._conn.execute(
            "SELECT trigger_source FROM feeling_events WHERE atom_kind='R9'").fetchone()
        trig = json.loads(row[0])
        assert trig["entity"] == "alice", (
            "F3: R9 trigger_source must carry the resolved entity for K-grouping.")
        assert trig["trigger"] == "和alice吵架"

    def test_r9_write_empty_entity_when_no_match(self, store):
        _add_entity(store, "alice")  # only alice known
        memo_id = store.insert_memo(user_id=USER, source_text="甲方改需求烦", input_mode="text")
        az = _atomizer(store)
        from plugins.memory.ptg.atom_schemas import R9EmotionAtom
        az._write_atom(
            R9EmotionAtom(emotion_label="烦", valence="negative", arousal="high",
                          trigger="甲方改需求", intensity="high", confidence=0.8),
            memo_id=memo_id, source_text="甲方改需求烦",
            input_mode="text", llm_call_id="llm-2")
        row = store._conn.execute(
            "SELECT trigger_source FROM feeling_events WHERE atom_kind='R9'").fetchone()
        assert json.loads(row[0])["entity"] == ""  # situation, not a known entity


# ===========================================================================
# F7: compute_k_correlations
# ===========================================================================

class TestF7Correlation:
    def test_negative_edge_for_skew_negative_entity(self, store):
        """Entity over-represented in negative emotions → negative K-edge."""
        _add_entity(store, "alice")  # 12 neg
        _add_entity(store, "bob")    # 12 pos (baseline ballast)
        _add_r9(store, "alice", "negative", n=12)
        _add_r9(store, "bob", "positive", n=12)
        n = compute_k_correlations(store, USER, now_ts=1753000000.0)
        assert n == 2
        rows = {store._conn.execute("SELECT entity_name FROM entities WHERE id=?",
                                    (r[0],)).fetchone()[0]: r for r in _k_edges(store)}
        # alice: P(neg|alice)=1.0, baseline=0.5 → lift 2.0 ≥ 1.2 → negative
        assert rows["alice"][1] == "negative"
        assert rows["alice"][3] is None  # active
        d = json.loads(rows["alice"][4])
        assert d["method"] == "conditional_probability"
        assert d["correlation_not_causation"] is True  # PRD 01:93 honesty tag
        assert d["sample_size"] == 12
        assert d["polarity"] == "negative"

    def test_positive_edge_for_skew_non_negative_entity(self, store):
        _add_entity(store, "alice")
        _add_entity(store, "bob")  # all positive
        _add_r9(store, "alice", "negative", n=12)
        _add_r9(store, "bob", "positive", n=12)
        compute_k_correlations(store, USER, now_ts=1753000000.0)
        rows = {store._conn.execute("SELECT entity_name FROM entities WHERE id=?",
                                    (r[0],)).fetchone()[0]: r for r in _k_edges(store)}
        # bob: P(neg|bob)=0, baseline=0.5 → lift 0 ≤ 0.83 → positive
        assert rows["bob"][1] == "positive"

    def test_sample_below_min_no_edge(self, store):
        """PRD §8.6.7: <10 samples for an entity → no K-edge for it (even if the
        raw skew looks significant). Carol provides positive ballast so the
        qualifying entities (bob) actually produce edges."""
        _add_entity(store, "alice")
        _add_entity(store, "bob")
        _add_entity(store, "carol")
        _add_r9(store, "alice", "negative", n=MIN_SAMPLE - 1)  # 9 → below gate
        _add_r9(store, "bob", "negative", n=12)                # passes gate
        _add_r9(store, "carol", "positive", n=12)              # positive ballast
        compute_k_correlations(store, USER, now_ts=1753000000.0)
        rows = {store._conn.execute("SELECT entity_name FROM entities WHERE id=?",
                                    (r[0],)).fetchone()[0]: r for r in _k_edges(store)
                if r[3] is None}
        assert "alice" not in rows, "alice (<10 samples) must get no edge"

    def test_confidence_mid_band(self, store):
        """10-29 samples → CONF_MID (0.6)."""
        _add_entity(store, "alice")
        _add_entity(store, "bob")
        _add_r9(store, "alice", "negative", n=15)
        _add_r9(store, "bob", "positive", n=15)
        compute_k_correlations(store, USER, now_ts=1753000000.0)
        rows = {store._conn.execute("SELECT entity_name FROM entities WHERE id=?",
                                    (r[0],)).fetchone()[0]: r for r in _k_edges(store)}
        assert rows["alice"][2] == CONF_MID

    def test_no_data_marks_all_existing_k_edges_stale(self, store):
        """total==0 → every existing K edge loses backing → stale (pure UPDATE)."""
        sid = store.ensure_self_entity(USER)
        alice = _add_entity(store, "alice")
        store.upsert_relation(user_id=USER, subject_id=sid, object_id=alice,
                              relation_type=K_RELATION_TYPE, value="negative",
                              confidence=0.6, delta={"sample_size": 12})
        # No R9 feeling_events at all
        n = compute_k_correlations(store, USER, now_ts=1753000000.0)
        assert n == 0
        row = _k_edges(store)[0]
        assert row[3] is not None  # stale_at set
        assert row[1] == "negative"  # value preserved (not deleted)

    def test_recompute_invalidates_edge_that_falls_below_gate(self, store):
        """Edge created, then on recompute the entity's lift enters the neutral
        band (0.83..1.2 → no edge) because the baseline rose → the edge is
        invalidated atomically; a still-qualifying edge stays active.

        Phase 1: alice 12 neg, bob 12 pos → baseline 0.5; alice lift 2.0
        (negative edge), bob lift 0 (positive edge).
        Phase 2: add carol 50 neg → baseline rises to ~0.84; alice (100% neg)
        lift ~1.19 (<1.2, >0.83) → NO edge → invalidated. bob (100% pos)
        lift 0 → stays positive (active). Carol ~1.19 → no edge.
        """
        _add_entity(store, "alice")
        _add_entity(store, "bob")
        _add_entity(store, "carol")
        _add_r9(store, "alice", "negative", n=12)
        _add_r9(store, "bob", "positive", n=12)
        compute_k_correlations(store, USER, now_ts=1753000000.0)
        rows1 = {store._conn.execute("SELECT entity_name FROM entities WHERE id=?",
                                     (r[0],)).fetchone()[0]: r
                 for r in _k_edges(store) if r[3] is None}
        assert "alice" in rows1 and "bob" in rows1  # both active after phase 1

        _add_r9(store, "carol", "negative", n=50)   # raise the baseline
        compute_k_correlations(store, USER, now_ts=1753000001.0)
        rows2 = {store._conn.execute("SELECT entity_name FROM entities WHERE id=?",
                                     (r[0],)).fetchone()[0]: r
                 for r in _k_edges(store)}
        assert rows2["alice"][3] is not None, "alice fell below gate → must be stale"
        assert rows2["bob"][3] is None, "bob still qualifies → must stay active"

    def test_correlation_not_causation_in_delta(self, store):
        _add_entity(store, "alice")
        _add_entity(store, "bob")
        _add_r9(store, "alice", "negative", n=12)
        _add_r9(store, "bob", "positive", n=12)
        compute_k_correlations(store, USER, now_ts=1753000000.0)
        rows = {r[0]: r for r in _k_edges(store)}
        for r in rows.values():
            assert json.loads(r[4])["correlation_not_causation"] is True

    def test_object_id_is_real_entity_fk(self, store):
        """V6 relations.object_id FKs entities(id) — the edge must use the entity
        id, not the name (danao14 stored names; V6 can't)."""
        alice_id = _add_entity(store, "alice")
        _add_entity(store, "bob")
        _add_r9(store, "alice", "negative", n=12)
        _add_r9(store, "bob", "positive", n=12)
        compute_k_correlations(store, USER, now_ts=1753000000.0)
        object_ids = [r[0] for r in _k_edges(store)]
        assert alice_id in object_ids, "K-edge object_id must be the real entity id (FK)"

    def test_never_raises_on_store_error(self):
        """C7: a broken store read must not propagate — compute returns 0."""
        class _BrokenConn:
            def execute(self, *a, **k):
                raise RuntimeError("store broken")

        class _BrokenStore:
            _conn = _BrokenConn()

            def ensure_self_entity(self, u):
                raise RuntimeError("x")

        assert compute_k_correlations(_BrokenStore(), USER) == 0

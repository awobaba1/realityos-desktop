"""RealityOS V6 Atomizer — Phase 1a heart regression tests (C4).

Locks ADR-V6-011: the HL-12 extraction pipeline over the PTG store. Covers the
full happy path (5 atom types → 4 event tables), the C5 confidence gate (+ R1
neutral-mood exemption), the V6 granular per-atom schema rejection (one bad atom
no longer sinks its siblings — the honest C2 improvement over V5), every C7 DLQ
failure mode (llm_error / json_parse_error / schema_invalid / write_error /
below_confidence_threshold), the C6 llm_call_logs substrate, and the sync_turn →
background-atomize wiring.

The LLM caller and clock are injected (mock), so these run offline with zero
network and are fully deterministic.
"""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from plugins.memory.ptg.atomizer import Atomizer, _estimate_cost, _format_user_prompt
from plugins.memory.ptg.confidence import ConfidenceEngine
from plugins.memory.ptg.provider import PTGProvider
from plugins.memory.ptg.store import PTGStore

_FIXED_NOW = datetime(2026, 7, 19, 14, 30)  # 2026-07-19 Sunday 14:30 Beijing


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resp(content_obj, *, model="glm-5.2", in_t=120, out_t=60):
    """Build an OpenAI-shape response carrying ``content_obj`` as JSON text."""
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps(content_obj, ensure_ascii=False)))],
        model=model,
        usage=SimpleNamespace(prompt_tokens=in_t, completion_tokens=out_t),
    )


def _callerReturning(response, supplement=None):
    """Pass-aware mock for two-pass extraction (ADR-V6-016).

    Returns ``response`` for the v11 primary pass (R0-R7) and ``supplement``
    (default: an empty-atom response) for the v12 supplement pass (R8/R9/R12).
    Detection: the v12 system prompt contains 'R8_Cognition' (v11 does not).
    Tests that don't care about R8/R9/R12 get unchanged primary-pass behaviour;
    tests that do pass an explicit ``supplement`` payload.
    """
    supp = supplement if supplement is not None else {
        "summary": "无补充原子", "atoms": []}
    supp_resp = _resp(supp)

    def _call(**kwargs):
        sys_msg = ""
        msgs = kwargs.get("messages") or []
        if msgs and isinstance(msgs[0], dict):
            sys_msg = msgs[0].get("content", "") or ""
        return supp_resp if "R8_Cognition" in sys_msg else response
    return _call


def _callerRaising(exc):
    def _call(**kwargs):
        raise exc
    return _call


@pytest.fixture
def store(tmp_path):
    s = PTGStore(db_path=str(tmp_path / "ptg.db"))
    s.ensure_founder("user-1", "founder@realityos.local")
    yield s
    s.close()


def _atomizer(store, caller, *, thresholds=None):
    return Atomizer(
        store, user_id="user-1", llm_caller=caller,
        now_fn=lambda: _FIXED_NOW,
        confidence_engine=ConfidenceEngine(thresholds),
    )


def _memo(store, text="今天和张三开会讨论了厦门国贸的项目，烦死了"):
    return store.insert_memo(user_id="user-1", source_text=text, input_mode="text")


def _row(store, table, where=""):
    q = f"SELECT * FROM {table} {where}".strip()
    return store._conn.execute(q).fetchone()


# A representative valid extraction: one of each Phase-1 atom type, all above
# their thresholds (R3≥0.8, R2≥0.7, R7≥0.5, R1≥0.5, R0≥0.7).
_VALID_OUTPUT = {
    "summary": "开会讨论项目心烦",
    "atoms": [
        {"type": "R3_Person", "person_name": "张三", "sentiment": "neutral",
         "interaction_type": "meeting", "segment_id": 0, "confidence": 0.9},
        {"type": "R2_Task", "task_description": "推进厦门国贸项目",
         "urgency": "medium", "deadline": None, "confidence": 0.8},
        {"type": "R7_Expression", "intent_class": "Complaint",
         "content_summary": "会议让人心烦", "confidence": 0.7},
        {"type": "R1_SelfState", "state_type": "stress", "direction": "up",
         "intensity": "high", "evidence": "烦死了", "confidence": 0.85},
        {"type": "R0_Entity", "entity_name": "厦门国贸", "entity_category": "organization",
         "mention_context": "项目相关", "confidence": 0.85},
    ],
}


# ---------------------------------------------------------------------------
# Happy path — 5 atoms → 4 event tables, C6 log, no DLQ
# ---------------------------------------------------------------------------

def test_happy_path_writes_all_atoms_to_correct_tables(store):
    az = _atomizer(store, _callerReturning(_resp(_VALID_OUTPUT)))
    memo_id = _memo(store)

    out = az.atomize(memo_id=memo_id, source_text="今天和张三开会...")

    assert out["ok"] is True
    assert out["written"] == 5
    assert out["filtered"] == 0
    assert out["invalid"] == 0
    # 1:1 routing.
    assert store.count_rows("identity_events") == 1   # R3
    assert store.count_rows("meaning_events") == 2    # R2 + R7
    assert store.count_rows("feeling_events") == 1    # R1
    assert store.count_rows("entity_events") == 1     # R0
    # C6: two passes → two log rows (v11 primary + v12 supplement), both
    # successful and schema-valid.
    assert store.count_rows("llm_call_logs") == 2
    logs = [dict(r) for r in store._conn.execute(
        "SELECT prompt_template_version, success, schema_valid, model, cost_cny "
        "FROM llm_call_logs")]
    versions = {lg["prompt_template_version"] for lg in logs}
    assert versions == {"v11", "v12"}        # primary + supplement
    assert all(lg["success"] == 1 for lg in logs)
    assert all(lg["schema_valid"] == 1 for lg in logs)   # V6 fills it (V5 left NULL)
    v11 = next(lg for lg in logs if lg["prompt_template_version"] == "v11")
    assert v11["model"] == "glm-5.2"
    assert v11["cost_cny"] and v11["cost_cny"] > 0
    # C7: nothing dropped.
    assert store.count_rows("dlq_messages") == 0


def test_happy_path_event_columns_match_schema(store):
    az = _atomizer(store, _callerReturning(_resp(_VALID_OUTPUT)))
    memo_id = _memo(store)
    az.atomize(memo_id=memo_id, source_text="x")

    ident = _row(store, "identity_events")
    assert ident["person_name"] == "张三"
    assert ident["interaction_type"] == "meeting"
    assert ident["memo_id"] == memo_id
    assert abs(ident["confidence_base"] - 0.9) < 1e-9
    # C2: written rows carry the soft-delete + version invariant.
    assert ident["deleted_at"] is None
    assert ident["version"] == 1

    meanings = store._conn.execute(
        "SELECT intent_class, task_description FROM meaning_events ORDER BY intent_class"
    ).fetchall()
    classes = {m["intent_class"] for m in meanings}
    assert classes == {"Need_To_Do", "Complaint"}  # R2→Need_To_Do, R7→Complaint


def test_llm_call_id_threaded_through_events_and_log(store):
    az = _atomizer(store, _callerReturning(_resp(_VALID_OUTPUT)))
    memo_id = _memo(store)
    out = az.atomize(memo_id=memo_id, source_text="x")
    llm_call_id = out["llm_call_id"]

    ident = _row(store, "identity_events")
    assert ident["llm_call_id"] == llm_call_id  # C6 traceability
    log = _row(store, "llm_call_logs")
    assert log["id"] == llm_call_id


# ---------------------------------------------------------------------------
# C5 confidence gate (+ R1 neutral-mood exemption)
# ---------------------------------------------------------------------------

def test_below_threshold_atom_goes_to_dlq_not_table(store):
    output = {"summary": "s", "atoms": [
        {"type": "R3_Person", "person_name": "李四", "confidence": 0.5}]}  # < 0.8
    az = _atomizer(store, _callerReturning(_resp(output)))
    az.atomize(memo_id=_memo(store), source_text="x")

    assert store.count_rows("identity_events") == 0
    assert store.count_rows("dlq_messages") == 1
    dlq = _row(store, "dlq_messages")
    assert dlq["source"] == "confidence_filter"
    assert dlq["error_type"] == "below_confidence_threshold"
    assert "0.5" in dlq["error_msg"] and "0.8" in dlq["error_msg"]


def test_r1_neutral_mood_exempt_from_threshold(store):
    # mood/stable/low at 0.3 (< 0.5) still passes — V5 exemption preserved.
    output = {"summary": "s", "atoms": [
        {"type": "R1_SelfState", "state_type": "mood", "direction": "stable",
         "intensity": "low", "confidence": 0.3}]}
    az = _atomizer(store, _callerReturning(_resp(output)))
    az.atomize(memo_id=_memo(store), source_text="x")

    assert store.count_rows("feeling_events") == 1   # exempt → written
    assert store.count_rows("dlq_messages") == 0


def test_thresholds_configurable(store):
    # Lower the person threshold to 0.5 → a 0.6 R3 now passes.
    output = {"summary": "s", "atoms": [
        {"type": "R3_Person", "person_name": "王五", "confidence": 0.6}]}
    az = _atomizer(store, _callerReturning(_resp(output)),
                   thresholds={"person": 0.5})
    az.atomize(memo_id=_memo(store), source_text="x")
    assert store.count_rows("identity_events") == 1


# ---------------------------------------------------------------------------
# V6 granular per-atom schema rejection (C2 improvement over V5)
# ---------------------------------------------------------------------------

def test_one_bad_atom_does_not_sink_siblings(store):
    output = {"summary": "s", "atoms": [
        {"type": "R3_Person", "person_name": "张三", "confidence": 0.9},   # good
        {"type": "R3_Person", "person_name": "李四", "sentiment": "angry",  # bad enum
         "confidence": 0.9},
    ]}
    az = _atomizer(store, _callerReturning(_resp(output)))
    az.atomize(memo_id=_memo(store), source_text="x")

    assert store.count_rows("identity_events") == 1   # good sibling survived
    assert store.count_rows("dlq_messages") == 1
    dlq = _row(store, "dlq_messages")
    assert dlq["source"] == "schema_validate"
    assert dlq["error_type"] == "schema_invalid"


def test_unknown_atom_type_routed_to_dlq(store):
    output = {"summary": "s", "atoms": [
        {"type": "R9_Unicorn", "foo": "bar"},
        {"type": "R2_Task", "task_description": "ok", "confidence": 0.8},
    ]}
    az = _atomizer(store, _callerReturning(_resp(output)))
    out = az.atomize(memo_id=_memo(store), source_text="x")
    assert out["written"] == 1
    assert out["invalid"] == 1


def test_top_level_structurally_invalid_rejects_whole_output(store):
    # Missing 'summary' → whole output to DLQ (V5 behaviour for top-level errs).
    output = {"atoms": []}
    az = _atomizer(store, _callerReturning(_resp(output)))
    az.atomize(memo_id=_memo(store), source_text="x")

    assert store.count_rows("meaning_events") == 0
    assert store.count_rows("dlq_messages") == 1
    assert _row(store, "dlq_messages")["source"] == "schema_validate"
    # LLM call itself succeeded → success=1, but schema_valid backfilled to 0.
    log = _row(store, "llm_call_logs")
    assert log["success"] == 1
    assert log["schema_valid"] == 0


# ---------------------------------------------------------------------------
# C7 DLQ failure modes
# ---------------------------------------------------------------------------

def test_llm_call_failure_dlq_and_log(store):
    az = _atomizer(store, _callerRaising(RuntimeError("No LLM provider configured")))
    az.atomize(memo_id=_memo(store), source_text="x")

    # Two passes both fail → 2 DLQ + 2 failed log rows (fail-isolated per pass).
    assert store.count_rows("dlq_messages") == 2
    dlq = _row(store, "dlq_messages")
    assert dlq["source"] == "llm_extract"
    assert dlq["error_type"] == "llm_error"          # V5 DLQ taxonomy
    assert "No LLM provider" in dlq["error_msg"]
    logs = [dict(r) for r in store._conn.execute(
        "SELECT success, error_type FROM llm_call_logs")]
    assert len(logs) == 2
    assert all(lg["success"] == 0 for lg in logs)
    assert all(lg["error_type"] == "RuntimeError" for lg in logs)  # exc class (V5)


def test_json_parse_failure_dlq(store):
    resp = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="not json {"))],
        model="glm-5.2", usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5))
    az = _atomizer(store, _callerReturning(resp))
    az.atomize(memo_id=_memo(store), source_text="x")

    assert store.count_rows("dlq_messages") == 1
    dlq = _row(store, "dlq_messages")
    assert dlq["source"] == "llm_extract"
    assert dlq["error_type"] == "json_parse_error"
    log = _row(store, "llm_call_logs")
    assert log["success"] == 0 and log["schema_valid"] == 0


def test_atom_write_failure_isolated_to_one_atom(store, monkeypatch):
    # Make only the identity write blow up; the other 4 atoms must still land.
    def boom(self, **kw):
        raise sqlite_fail("simulated write failure")
    import plugins.memory.ptg.store as store_mod
    monkeypatch.setattr(store_mod.PTGStore, "insert_identity_event", boom)

    az = _atomizer(store, _callerReturning(_resp(_VALID_OUTPUT)))
    out = az.atomize(memo_id=_memo(store), source_text="x")

    assert out["written"] == 4          # R2/R7/R1/R0 survived
    assert store.count_rows("dlq_messages") == 1
    dlq = _row(store, "dlq_messages")
    assert dlq["source"] == "atom_write"
    assert dlq["error_type"] == "write_error"
    assert "R3_Person" in dlq["error_msg"]


class sqlite_fail(Exception):
    """Stand-in for a sqlite write error."""


# ---------------------------------------------------------------------------
# C6 log content (replay substrate)
# ---------------------------------------------------------------------------

def test_llm_log_captures_full_prompt_and_response(store):
    az = _atomizer(store, _callerReturning(_resp(_VALID_OUTPUT)))
    az.atomize(memo_id=_memo(store), source_text="原始用户文本")
    # _row returns the first-inserted log = the v11 primary pass.
    log = _row(store, "llm_call_logs")
    prompt_input = json.loads(log["prompt_input"])
    assert prompt_input["engine"] == "hl12_extract"
    assert prompt_input["prompt_version"] == "v11"        # primary pass baseline
    assert "原始用户文本" in prompt_input["full_prompt"]      # C6 replay
    assert prompt_input["system_prompt_hash"]
    response = json.loads(log["response"])
    assert response["content"]["summary"] == "开会讨论项目心烦"


# ---------------------------------------------------------------------------
# prompt assembly
# ---------------------------------------------------------------------------

def test_user_prompt_includes_beijing_time_and_weekday_and_suffix():
    txt = _format_user_prompt("你好", _FIXED_NOW, None)
    assert "2026年7月19日" in txt
    assert "星期日" in txt           # 2026-07-19 is a Sunday
    assert "北京时间" in txt
    assert "你好" in txt
    assert txt.rstrip().endswith("输出严格 JSON 格式。")


def test_user_prompt_includes_location_when_present():
    txt = _format_user_prompt("hi", _FIXED_NOW, {"name": "厦门"})
    assert "地点：厦门" in txt


def test_cost_estimate_matches_v5_pricing():
    # zhipu ¥2/M in, ¥8/M out.
    cost = _estimate_cost(1_000_000, 1_000_000, "zhipu")
    assert abs(cost - 10.0) < 1e-9
    assert abs(_estimate_cost(0, 0, "zhipu")) < 1e-9


# ---------------------------------------------------------------------------
# sync_turn wiring (provider)
# ---------------------------------------------------------------------------

def test_sync_turn_spawns_atomize_when_enabled(tmp_path, monkeypatch):
    spawned = []
    p = PTGProvider(config={"db_path": str(tmp_path / "ptg.db"), "atomize": True})
    p.initialize("s", hermes_home=str(tmp_path), agent_context="primary")
    assert p._atomizer is not None                  # heart built

    # Run the spawn inline (deterministic) and assert it's invoked with the
    # captured memo_id + source_text.
    def _inline_spawn(*, memo_id, source_text):
        spawned.append((memo_id, source_text))
    monkeypatch.setattr(p, "_spawn_atomize", _inline_spawn)

    p.sync_turn("a real user turn", "reply")
    assert spawned and spawned[0][1] == "a real user turn"
    assert p._store.count_rows("memos") == 1        # memo still captured sync
    p.shutdown()


def test_sync_turn_no_atomize_when_disabled(tmp_path, monkeypatch):
    spawned = []
    p = PTGProvider(config={"db_path": str(tmp_path / "ptg.db"), "atomize": False})
    p.initialize("s", hermes_home=str(tmp_path), agent_context="primary")
    assert p._atomizer is None                       # heart not built (opt-out)
    monkeypatch.setattr(p, "_spawn_atomize",
                        lambda **kw: spawned.append(kw))
    p.sync_turn("a turn", "reply")
    assert spawned == []                             # no spawn
    assert p._store.count_rows("memos") == 1         # capture unaffected
    p.shutdown()


def test_sync_turn_end_to_end_writes_atoms_via_mock_llm(tmp_path):
    """Full stack: provider → background thread → Atomizer (mock LLM) → events.

    Patches ``threading.Thread`` on the provider module to run the target
    inline, so the atomization is synchronous and the assertion is race-free.
    """
    p = PTGProvider(config={"db_path": str(tmp_path / "ptg.db"), "atomize": True})
    p.initialize("s", hermes_home=str(tmp_path), agent_context="primary")
    # Swap in a real Atomizer with a mock caller.
    p._atomizer = Atomizer(
        p._store, user_id=p._user_id, llm_caller=_callerReturning(_resp(_VALID_OUTPUT)),
        now_fn=lambda: _FIXED_NOW)

    import plugins.memory.ptg.provider as prov_mod
    real_thread = prov_mod.threading.Thread

    class _InlineThread:
        def __init__(self, target, **kw):
            self._target = target

        def start(self):
            self._target()  # run synchronously instead of spawning

        # The work runs inline in start(), so by the time the thread is tracked
        # it is already complete. Implement the Thread interface the shutdown
        # drain (ADR-V6-012) queries so this double stays faithful.
        def is_alive(self):
            return False

        def join(self, timeout=None):
            return None

    prov_mod.threading.Thread = _InlineThread
    try:
        p.sync_turn("今天和张三开会讨论厦门国贸的项目", "assistant reply")
    finally:
        prov_mod.threading.Thread = real_thread

    assert p._store.count_rows("memos") == 1
    assert p._store.count_rows("identity_events") == 1
    assert p._store.count_rows("meaning_events") == 2
    assert p._store.count_rows("llm_call_logs") == 2   # v11 primary + v12 supplement
    p.shutdown()


# ---------------------------------------------------------------------------
# Graph materialization — entities/relations upsert (ADR-V6-011 决策6)
# ---------------------------------------------------------------------------

def test_upsert_entity_creates_then_bumps_mention_count(store):
    eid1 = store.upsert_entity(user_id="user-1", entity_name="张三", entity_type="person")
    eid2 = store.upsert_entity(user_id="user-1", entity_name="张三", entity_type="person")
    assert eid1 == eid2                                       # idempotent
    row = store._conn.execute(
        "SELECT mention_count, version FROM entities WHERE id = ?", (eid1,)).fetchone()
    assert row["mention_count"] == 2
    assert row["version"] == 2


def test_upsert_entity_normalizes_whitespace(store):
    a = store.upsert_entity(user_id="user-1", entity_name="张三", entity_type="person")
    b = store.upsert_entity(user_id="user-1", entity_name="  张三  ", entity_type="person")
    assert a == b
    assert store.count_rows("entities") == 1                 # not duplicated


def test_upsert_relation_evidence_count_and_confidence_max(store):
    s = store.upsert_entity(user_id="user-1", entity_name="我", entity_type="person")
    o = store.upsert_entity(user_id="user-1", entity_name="李四", entity_type="person")
    r1 = store.upsert_relation(user_id="user-1", subject_id=s, object_id=o,
                               relation_type="interacts_with", confidence=0.5)
    r2 = store.upsert_relation(user_id="user-1", subject_id=s, object_id=o,
                               relation_type="interacts_with", confidence=0.9)
    assert r1 == r2
    row = store._conn.execute(
        "SELECT evidence_count, confidence FROM relations WHERE id = ?", (r1,)).fetchone()
    assert row["evidence_count"] == 2
    assert abs(row["confidence"] - 0.9) < 1e-9               # max — a low mention never dilutes


def test_atomize_materializes_graph_nodes_and_self_edges(store):
    az = _atomizer(store, _callerReturning(_resp(_VALID_OUTPUT)))
    az.atomize(memo_id=_memo(store), source_text="x")
    # self + person(张三) + task + context(厦门国贸) = 4 nodes (R1/R7 make none)
    assert store.count_rows("entities") == 4
    # self→person / self→task / self→context = 3 edges
    assert store.count_rows("relations") == 3
    self_row = store._conn.execute(
        "SELECT properties FROM entities WHERE entity_name = '我'").fetchone()
    assert json.loads(self_row["properties"])["is_self"] is True
    person = store._conn.execute(
        "SELECT entity_type FROM entities WHERE entity_name = '张三'").fetchone()
    assert person["entity_type"] == "person"
    edge = store._conn.execute(
        "SELECT r.relation_type FROM relations r "
        "JOIN entities s ON r.subject_id = s.id "
        "JOIN entities o ON r.object_id = o.id "
        "WHERE s.entity_name = '我' AND o.entity_name = '张三'").fetchone()
    assert edge["relation_type"] == "interacts_with"


def test_materialize_graph_can_be_disabled(store):
    az = Atomizer(store, user_id="user-1",
                  llm_caller=_callerReturning(_resp(_VALID_OUTPUT)),
                  now_fn=lambda: _FIXED_NOW,
                  confidence_engine=ConfidenceEngine(),
                  materialize_graph=False)
    az.atomize(memo_id=_memo(store), source_text="x")
    assert store.count_rows("entities") == 0
    assert store.count_rows("relations") == 0
    assert store.count_rows("meaning_events") == 2          # events still captured


def test_materialize_failure_isolated_from_event_write(store, monkeypatch):
    # upsert_entity blows up → graph materialize DLQs, but every event still lands.
    def boom(self, **kw):
        raise RuntimeError("graph DB busy")
    import plugins.memory.ptg.store as store_mod
    monkeypatch.setattr(store_mod.PTGStore, "upsert_entity", boom)

    az = _atomizer(store, _callerReturning(_resp(_VALID_OUTPUT)))
    out = az.atomize(memo_id=_memo(store), source_text="x")

    assert out["written"] == 5                               # all events captured
    assert store.count_rows("identity_events") == 1
    assert store.count_rows("meaning_events") == 2
    # 3 eligible atoms (R3/R2/R0) each DLQ a graph_materialize error; R1/R7 skip.
    dlqs = [dict(r) for r in store._conn.execute(
        "SELECT source, error_type FROM dlq_messages")]
    assert len(dlqs) == 3
    assert all(d["source"] == "graph_materialize" for d in dlqs)
    assert all(d["error_type"] == "materialize_error" for d in dlqs)


def test_materialize_skips_r1_r7_creates_no_nodes(store):
    output = {"summary": "s", "atoms": [
        {"type": "R1_SelfState", "state_type": "stress", "direction": "up",
         "intensity": "high", "confidence": 0.8},
        {"type": "R7_Expression", "intent_class": "Complaint",
         "content_summary": "烦", "confidence": 0.8},
    ]}
    az = _atomizer(store, _callerReturning(_resp(output)))
    az.atomize(memo_id=_memo(store), source_text="x")
    assert store.count_rows("entities") == 0                # no self node either
    assert store.count_rows("relations") == 0
    assert store.count_rows("feeling_events") == 1
    assert store.count_rows("meaning_events") == 1


def test_re_atomize_same_atoms_bumps_mention_not_duplicates(store):
    az = _atomizer(store, _callerReturning(_resp(_VALID_OUTPUT)))
    az.atomize(memo_id=_memo(store), source_text="x")
    az.atomize(memo_id=_memo(store), source_text="x")       # same atoms again
    person = store._conn.execute(
        "SELECT mention_count FROM entities WHERE entity_name = '张三'").fetchone()
    assert person["mention_count"] == 2
    assert store.count_rows("entities") == 4                # not duplicated
    edge = store._conn.execute(
        "SELECT r.evidence_count FROM relations r "
        "JOIN entities o ON r.object_id = o.id "
        "WHERE o.entity_name = '张三'").fetchone()
    assert edge["evidence_count"] == 2


# ---------------------------------------------------------------------------
# ADR-V6-013 (ADR-049 port): entity-vocabulary injection regression (C4)
# ---------------------------------------------------------------------------

def test_format_entity_vocab_none_when_empty():
    """First-run (no entities) → None → section omitted (no token cost, no
    behaviour change vs pre-vocab)."""
    from plugins.memory.ptg.atomizer import _format_entity_vocab
    assert _format_entity_vocab([]) is None


def test_format_entity_vocab_renders_buckets_with_aliases():
    from plugins.memory.ptg.atomizer import _format_entity_vocab
    out = _format_entity_vocab([
        {"entity_name": "张三", "entity_type": "person", "aliases": ["老张", "张总"]},
        {"entity_name": "李四", "entity_type": "person", "aliases": []},
        {"entity_name": "厦门国贸", "entity_type": "context", "aliases": []},
    ])
    assert out and out.startswith("## 已知实体词汇")
    assert "ASR 同音误识别" in out          # the self-describing instruction
    assert "[人物] 张三（老张、张总）; 李四" in out
    assert "[情境] 厦门国贸" in out


def test_format_user_prompt_omits_vocab_section_when_none():
    from plugins.memory.ptg.atomizer import _format_user_prompt
    out = _format_user_prompt("hello", _FIXED_NOW, None, entity_vocab=None)
    assert "已知实体词汇" not in out        # first-run: no vocab section


def test_format_user_prompt_injects_vocab_section_when_present():
    from plugins.memory.ptg.atomizer import _format_user_prompt
    out = _format_user_prompt("hello", _FIXED_NOW, None,
                              entity_vocab="## 已知实体词汇\n[人物] 张三")
    assert "已知实体词汇" in out
    assert out.index("已知实体词汇") < out.index("hello")  # before source text


def test_atomizer_vocab_section_none_on_first_run(store):
    """Empty store → no entities → vocab None → extraction proceeds vocab-less."""
    az = _atomizer(store, _callerReturning(_resp(_VALID_OUTPUT)))
    assert az._entity_vocab_section() is None


def test_atomizer_vocab_section_built_when_entities_exist(store):
    """Once the user has entities, the section is built from the store."""
    store.upsert_entity(user_id="user-1", entity_name="张三", entity_type="person",
                        properties={"aliases": ["老张"]})
    az = _atomizer(store, _callerReturning(_resp(_VALID_OUTPUT)))
    section = az._entity_vocab_section()
    assert section and "张三" in section and "老张" in section


def test_atomizer_vocab_section_failisolates(store, monkeypatch):
    """C7: a store load failure must NOT break extraction — vocab returns None
    and atomize still runs (enrichment, not a gate)."""
    def boom(*a, **kw):
        raise RuntimeError("db locked")
    az = _atomizer(store, _callerReturning(_resp(_VALID_OUTPUT)))
    monkeypatch.setattr(store, "list_top_entities", boom)
    assert az._entity_vocab_section() is None     # swallowed, not raised
    # And extraction still completes end-to-end.
    out = az.atomize(memo_id=_memo(store), source_text="x")
    assert out["ok"] is True


def test_materialization_persists_r3_aliases(store):
    """ADR-V6-013: an R3 atom carrying aliases lands as an entity whose
    properties.aliases is populated (so the vocab can surface them next turn)."""
    import json
    payload = {"summary": "s", "atoms": [
        {"type": "R3_Person", "person_name": "张三", "aliases": ["老张", "张总"],
         "sentiment": "neutral", "interaction_type": "meeting",
         "segment_id": 0, "confidence": 0.9}]}
    az = _atomizer(store, _callerReturning(_resp(payload)))
    az.atomize(memo_id=_memo(store), source_text="x")
    row = store._conn.execute(
        "SELECT properties FROM entities WHERE entity_name=? AND user_id=?",
        ("张三", "user-1")).fetchone()
    assert row is not None
    assert json.loads(row["properties"])["aliases"] == ["老张", "张总"]


# ---------------------------------------------------------------------------
# §6.7 minor-mode gate (ADR-V6-023) — drop R1SelfState/R9Emotion biometric
# atoms for minor tenants at the materialization boundary; extraction unchanged.
# ---------------------------------------------------------------------------

# A supplement payload with one R9 emotion atom (v12 pass).
_SUPPLEMENT_R9 = {
    "summary": "情绪波动",
    "atoms": [
        {"type": "R9_Emotion", "emotion_label": "焦虑", "valence": "negative",
         "arousal": "high", "trigger": "项目进度", "intensity": "high",
         "confidence": 0.85},
    ],
}


def test_minor_mode_drops_r1_and_r9_biometric_atoms(store):
    from plugins.realityos_sovereignty.sovereignty import set_minor_mode

    set_minor_mode(store, "user-1", True)
    # Primary (_VALID_OUTPUT) carries R3 + R1 (SelfState); supplement carries R9.
    az = _atomizer(
        store, _callerReturning(_resp(_VALID_OUTPUT), _SUPPLEMENT_R9))

    out = az.atomize(memo_id=_memo(store), source_text="今天和张三开会心烦...")

    # Primary has 5 atoms (R3/R2/R7/R1/R0); R1 dropped → 4 written. The R9
    # supplement atom is also dropped → filtered counts both (R1 + R9).
    assert out["written"] == 4
    assert out["filtered"] == 2
    types = {a["type"] for a in store.recent_atoms(user_id="user-1")}
    assert "R3_Person" in types
    assert "R1_SelfState" not in types
    assert "R9_Emotion" not in types


def test_adult_mode_keeps_r1_and_r9_zero_regression(store):
    # Default (adult) mode: the gate is inert — R1 + R9 written as before.
    az = _atomizer(
        store, _callerReturning(_resp(_VALID_OUTPUT), _SUPPLEMENT_R9))

    out = az.atomize(memo_id=_memo(store), source_text="今天和张三开会心烦...")

    assert out["written"] == 6  # 5 primary (incl. R1) + 1 supplement (R9)
    assert out["filtered"] == 0
    types = {a["type"] for a in store.recent_atoms(user_id="user-1")}
    assert "R1_SelfState" in types
    assert "R9_Emotion" in types


def test_minor_mode_keeps_non_biometric_atoms(store):
    from plugins.realityos_sovereignty.sovereignty import set_minor_mode

    set_minor_mode(store, "user-1", True)
    # Only R2 + R3 (no biometric atoms) — the gate must not touch them.
    payload = {"summary": "s", "atoms": [
        {"type": "R3_Person", "person_name": "张三", "sentiment": "neutral",
         "interaction_type": "meeting", "segment_id": 0, "confidence": 0.9},
        {"type": "R2_Task", "task_description": "推进项目", "urgency": "medium",
         "deadline": None, "confidence": 0.8}]}
    az = _atomizer(store, _callerReturning(_resp(payload)))

    out = az.atomize(memo_id=_memo(store), source_text="x")

    assert out["written"] == 2
    assert out["filtered"] == 0  # gate specific to R1/R9; R2/R3 unaffected


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


def _callerReturning(response):
    def _call(**kwargs):
        return response
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
    # C6: exactly one successful, schema-valid log row.
    assert store.count_rows("llm_call_logs") == 1
    log = _row(store, "llm_call_logs")
    assert log["success"] == 1
    assert log["schema_valid"] == 1          # V6 fills it (V5 left NULL)
    assert log["prompt_template_version"] == "v11"
    assert log["model"] == "glm-5.2"
    assert log["cost_cny"] and log["cost_cny"] > 0
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

    assert store.count_rows("dlq_messages") == 1
    dlq = _row(store, "dlq_messages")
    assert dlq["source"] == "llm_extract"
    assert dlq["error_type"] == "llm_error"          # V5 DLQ taxonomy
    assert "No LLM provider" in dlq["error_msg"]
    log = _row(store, "llm_call_logs")
    assert log["success"] == 0
    assert log["error_type"] == "RuntimeError"        # log carries the exc class (V5)


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
    log = _row(store, "llm_call_logs")
    prompt_input = json.loads(log["prompt_input"])
    assert prompt_input["engine"] == "hl12_extract"
    assert prompt_input["prompt_version"] == "v11"
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

    prov_mod.threading.Thread = _InlineThread
    try:
        p.sync_turn("今天和张三开会讨论厦门国贸的项目", "assistant reply")
    finally:
        prov_mod.threading.Thread = real_thread

    assert p._store.count_rows("memos") == 1
    assert p._store.count_rows("identity_events") == 1
    assert p._store.count_rows("meaning_events") == 2
    assert p._store.count_rows("llm_call_logs") == 1
    p.shutdown()

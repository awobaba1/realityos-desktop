"""RealityOS V6 — weekly mirror tests (ADR-V6-017, 架构 §0.5③/§4.4).

Covers the four production-critical properties:
  1. aggregation is window-bounded + type-aware (only in-week atoms, all 8 types).
  2. the §0.5③ cold-start gate: insufficient (reg<14d OR memo<15) ⇒ placeholder
     with NO LLM call; partial (15≤memo<30); sufficient (memo≥30).
  3. the LLM path: success ⇒ C5-validated mirror stored + logged (C6); failure
     and C5-invalid ⇒ DLQ + degrade to placeholder (C7).
  4. storage: upsert into insight_aggregation, regenerate replaces in place.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from plugins.memory.ptg.store import PTGStore
from plugins.realityos_insights.aggregation import aggregate_week
from plugins.realityos_insights.weekly_mirror import (
    MIN_MEMOS,
    MIN_REGISTRATION_DAYS,
    PARTIAL_MEMO_THRESHOLD,
    WeeklyMirrorService,
    _resolve_week,
    _validate_mirror,
)

_BEIJING_TZ = timezone(timedelta(hours=8))
USER = "founder-1"

# Fixed "now" = 2026-07-15 10:00 Beijing (a Wednesday). Default week ⇒ the
# most recently COMPLETED Mon–Sun = 2026-07-06 .. 2026-07-12 (Beijing).
FIXED_NOW = datetime(2026, 7, 15, 10, 0, tzinfo=_BEIJING_TZ)
# Explicit week_start 2026-07-06 Beijing ⇒ UTC window [07-05T16:00, 07-12T16:00).
WEEK_START = "2026-07-06"
IN_WINDOW_TS = "2026-07-08T10:00:00+00:00"   # mid-week UTC
OUT_WINDOW_TS = "2026-07-20T10:00:00+00:00"  # the week after


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    s = PTGStore(db_path=str(tmp_path / "ptg.db"))
    s.ensure_founder(USER, "founder@realityos.local")
    yield s
    s.close()


def _set_registration(store, days_ago: int) -> None:
    """Pin realityos_users.created_at to N days ago (UTC) to control the gate."""
    created = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    with store._lock:
        store._conn.execute(
            "UPDATE realityos_users SET created_at = ? WHERE id = ?", (created, USER))


def _seed_memos(store, n: int) -> None:
    for i in range(n):
        store.insert_memo(user_id=USER, source_text=f"memo {i}", input_mode="text")


def _mock_caller(content="# 本周镜面（2026-07-06 ~ 2026-07-12）\n\n"
                         "这周你和张三推进了述职报告，也搞懂了 React diff 算法。"
                         "情绪上被甲方改需求弄得很烦。整体节奏紧凑，精力偏满。"):
    """A recording mock caller returning a valid mirror. Returns (caller, calls)."""
    calls = []

    def _c(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            model="mock-model", provider="mock",
            usage=SimpleNamespace(prompt_tokens=120, completion_tokens=210))

    return _c, calls


def _service(store, caller=None, now_fn=lambda: FIXED_NOW) -> WeeklyMirrorService:
    return WeeklyMirrorService(store, user_id=USER, caller=caller, now_fn=now_fn)


# ---------------------------------------------------------------------------
# 1. Aggregation: window-bounded + type-aware
# ---------------------------------------------------------------------------

def test_aggregation_only_counts_in_window_atoms(store):
    _seed_week_atoms(store, IN_WINDOW_TS)
    _seed_week_atoms(store, OUT_WINDOW_TS)  # duplicate, but next week → excluded
    agg = aggregate_week(store, user_id=USER,
                         week_start="2026-07-05T16:00:00+00:00",
                         week_end="2026-07-12T16:00:00+00:00")
    # Exactly one of each type in-window (the OUT_WINDOW seed is excluded).
    assert agg["atom_counts"].get("R3_Person") == 1
    assert agg["atom_counts"].get("R2_Task") == 1
    assert agg["atom_counts"].get("R12_Outcome") == 1
    assert agg["atom_counts"].get("R9_Emotion") == 1
    assert agg["atom_counts"].get("R8_Cognition") == 1
    assert agg["atom_counts"].get("R1_SelfState") == 1
    assert agg["atom_counts"].get("R0_Entity") == 1


def test_aggregation_groups_people_and_outcomes(store):
    # Three mentions of 张三, two of 李四; two completed + one delayed outcome.
    for _ in range(3):
        store.insert_identity_event(user_id=USER, source_text="x",
                                    person_name="张三", mention_context="聊述职",
                                    confidence_base=0.9, relation_confidence=0.9,
                                    timestamp=IN_WINDOW_TS)
    for _ in range(2):
        store.insert_identity_event(user_id=USER, source_text="x",
                                    person_name="李四", mention_context="改需求",
                                    confidence_base=0.9, relation_confidence=0.9,
                                    timestamp=IN_WINDOW_TS)
    store.insert_meaning_event(user_id=USER, source_text="x", intent_class="Other",
                               task_description="述职报告", task_status="completed",
                               atom_kind="R12",
                               completion_note='{"outcome":"completed","resolution_note":""}',
                               confidence_base=0.9, relation_confidence=0.9, timestamp=IN_WINDOW_TS)
    store.insert_meaning_event(user_id=USER, source_text="x", intent_class="Other",
                               task_description="竞标", task_status="dismissed",
                               atom_kind="R12",
                               completion_note='{"outcome":"failed","resolution_note":"没中"}',
                               confidence_base=0.9, relation_confidence=0.9, timestamp=IN_WINDOW_TS)
    store.insert_meaning_event(user_id=USER, source_text="x", intent_class="Other",
                               task_description="周报", task_status="pending",
                               is_overdue=1, atom_kind="R12",
                               completion_note='{"outcome":"delayed","resolution_note":"拖到下周"}',
                               confidence_base=0.9, relation_confidence=0.9, timestamp=IN_WINDOW_TS)
    agg = aggregate_week(store, user_id=USER,
                         week_start="2026-07-05T16:00:00+00:00",
                         week_end="2026-07-12T16:00:00+00:00")
    people = {p["name"]: p["count"] for p in agg["people"]}
    assert people == {"张三": 3, "李四": 2}
    assert agg["task_outcomes"] == {"completed": 1, "failed": 1, "delayed": 1}


def test_aggregation_empty_week(store):
    agg = aggregate_week(store, user_id=USER,
                         week_start="2026-07-05T16:00:00+00:00",
                         week_end="2026-07-12T16:00:00+00:00")
    assert agg["atom_counts"] == {}
    assert agg["atom_total"] == 0
    assert agg["people"] == []


def _seed_week_atoms(store, ts: str) -> None:
    """Seed one of each atom type at the given UTC timestamp."""
    store.insert_identity_event(user_id=USER, source_text="x", person_name="张三",
                                mention_context="聊述职报告",
                                confidence_base=0.9, relation_confidence=0.9, timestamp=ts)
    store.insert_meaning_event(user_id=USER, source_text="x", intent_class="Need_To_Do",
                               task_description="写述职报告", urgency="high",
                               atom_kind="R2", confidence_base=0.9, relation_confidence=0.9,
                               timestamp=ts)
    store.insert_meaning_event(user_id=USER, source_text="x", intent_class="Other",
                               task_description="React diff 算法",
                               topic_tags='["diff","虚拟DOM"]', atom_kind="R8",
                               completion_note='{"engagement":"high","is_question":false}',
                               confidence_base=0.9, relation_confidence=0.9, timestamp=ts)
    store.insert_meaning_event(user_id=USER, source_text="x", intent_class="Need_To_Do",
                               task_description="述职报告", task_status="completed",
                               atom_kind="R12",
                               completion_note='{"outcome":"completed","resolution_note":"已交"}',
                               confidence_base=0.9, relation_confidence=0.9, timestamp=ts)
    store.insert_feeling_event(user_id=USER, source_text="x", state_type="mood",
                               direction="down", intensity="high",
                               emotion_vad='{"valence":"negative","arousal":"high","label":"愤怒"}',
                               trigger_source='{"trigger":"甲方改需求","atom":"R9_Emotion"}',
                               ser_source="llm_text", atom_kind="R9",
                               confidence_base=0.9, relation_confidence=0.9, timestamp=ts)
    store.insert_feeling_event(user_id=USER, source_text="x", state_type="stress",
                               direction="up", intensity="medium",
                               atom_kind="R1", confidence_base=0.9, relation_confidence=0.9,
                               timestamp=ts)
    store.insert_meaning_event(user_id=USER, source_text="x", intent_class="Need_To_Do",
                               task_description="下周想早点睡", atom_kind="R7",
                               confidence_base=0.9, relation_confidence=0.9, timestamp=ts)
    store.insert_entity_event(user_id=USER, source_text="x", entity_name="飞书",
                              entity_category="term", mention_context="用飞书沟通",
                              confidence_base=0.9, relation_confidence=0.9, timestamp=ts)


# ---------------------------------------------------------------------------
# 2. Cold-start gate (§0.5③)
# ---------------------------------------------------------------------------

def test_gate_insufficient_via_low_memos(store):
    _set_registration(store, 30)  # registered long ago
    _seed_memos(store, MIN_MEMOS - 1)  # but < 15 memos
    caller, calls = _mock_caller()
    res = _service(store, caller).generate(week_start=WEEK_START)
    assert res["status"] == "placeholder"
    assert res["data_sufficiency"] == "insufficient"
    assert res["llm_call_id"] is None
    assert calls == []  # NO LLM call on the placeholder path
    assert "我还在了解你" in res["content"]


def test_gate_insufficient_via_new_registration(store):
    _set_registration(store, MIN_REGISTRATION_DAYS - 1)  # < 14 days
    _seed_memos(store, MIN_MEMOS + 5)  # enough memos
    caller, calls = _mock_caller()
    res = _service(store, caller).generate(week_start=WEEK_START)
    assert res["status"] == "placeholder"
    assert res["data_sufficiency"] == "insufficient"
    assert calls == []  # registration gate fires even with memos


def test_gate_partial(store):
    _set_registration(store, 30)
    _seed_memos(store, PARTIAL_MEMO_THRESHOLD - 1)  # 15..29 → partial
    caller, calls = _mock_caller()
    res = _service(store, caller).generate(week_start=WEEK_START)
    assert res["status"] == "mirror"
    assert res["data_sufficiency"] == "partial"
    assert len(calls) == 1


def test_gate_sufficient(store):
    _set_registration(store, 30)
    _seed_memos(store, PARTIAL_MEMO_THRESHOLD + 5)  # ≥ 30 → sufficient
    caller, calls = _mock_caller()
    res = _service(store, caller).generate(week_start=WEEK_START)
    assert res["status"] == "mirror"
    assert res["data_sufficiency"] == "sufficient"
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# 3. LLM path: success logs (C6); failure + C5-invalid DLQ + degrade (C7)
# ---------------------------------------------------------------------------

def test_llm_success_logs_and_stores(store):
    _set_registration(store, 30)
    _seed_memos(store, PARTIAL_MEMO_THRESHOLD + 5)
    caller, _ = _mock_caller()
    res = _service(store, caller).generate(week_start=WEEK_START)
    assert res["status"] == "mirror"
    assert res["schema_valid"] is True
    assert res["llm_call_id"] is not None
    # C6: the call is logged with prompt_template_version=v1 + schema_valid=1.
    row = store._conn.execute(
        "SELECT prompt_template_version, success, schema_valid FROM llm_call_logs "
        "WHERE id = ?", [res["llm_call_id"]]).fetchone()
    assert row[0] == "v1"
    assert row[1] == 1
    assert row[2] == 1


def test_llm_failure_degrades_to_placeholder_and_dlqs(store):
    _set_registration(store, 30)
    _seed_memos(store, PARTIAL_MEMO_THRESHOLD + 5)

    def boom(**_kwargs):
        raise RuntimeError("provider timeout")

    res = _service(store, boom).generate(week_start=WEEK_START)
    # Failure ⇒ placeholder content (error variant), but still a stored row.
    assert res["status"] == "mirror"  # gate passed (it's the LLM that failed)
    assert "还没准备好" in res["content"]
    assert res["schema_valid"] is False
    # C7: a DLQ entry was written.
    dlq = store._conn.execute(
        "SELECT source, error_type FROM dlq_messages WHERE user_id = ? "
        "ORDER BY created_at DESC LIMIT 1", [USER]).fetchone()
    assert dlq is not None
    assert dlq[0] == "weekly_mirror"
    # C6: the failed call is logged success=0.
    log = store._conn.execute(
        "SELECT success FROM llm_call_logs WHERE id = ?", [res["llm_call_id"]]).fetchone()
    assert log[0] == 0


def test_c5_invalid_output_degrades_and_dlqs(store):
    _set_registration(store, 30)
    _seed_memos(store, PARTIAL_MEMO_THRESHOLD + 5)
    caller, _ = _mock_caller(content="ok")  # too short → fails _MIN_MIRROR_CHARS
    res = _service(store, caller).generate(week_start=WEEK_START)
    assert res["schema_valid"] is False
    assert "还没准备好" in res["content"]
    dlq = store._conn.execute(
        "SELECT error_type FROM dlq_messages WHERE user_id = ? "
        "AND source='weekly_mirror'", [USER]).fetchone()
    assert dlq is not None
    assert dlq[0] == "mirror_schema_invalid"


def test_validate_mirror_floor():
    assert _validate_mirror("") is False
    assert _validate_mirror("short") is False
    assert _validate_mirror('{"not":"markdown"}' + "x" * 100) is False  # JSON leak
    assert _validate_mirror("# 本周镜面\n" + "具体内容" * 30) is True


# ---------------------------------------------------------------------------
# 4. Storage: upsert into insight_aggregation, regenerate replaces
# ---------------------------------------------------------------------------

def test_mirror_stored_in_insight_aggregation(store):
    _set_registration(store, 30)
    _seed_memos(store, PARTIAL_MEMO_THRESHOLD + 5)
    caller, _ = _mock_caller()
    res = _service(store, caller).generate(week_start=WEEK_START)
    rows = store._conn.execute(
        "SELECT aggregation_type, period_key, data_sufficiency, llm_call_id, "
        "result_data FROM insight_aggregation WHERE user_id = ?", [USER]).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "weekly_mirror"
    assert rows[0][1] == WEEK_START
    assert rows[0][2] == "sufficient"
    assert rows[0][3] == res["llm_call_id"]
    assert "本周镜面" in rows[0][4]


def test_regenerate_same_week_replaces_in_place(store):
    _set_registration(store, 30)
    _seed_memos(store, PARTIAL_MEMO_THRESHOLD + 5)
    caller, _ = _mock_caller()
    svc = _service(store, caller)
    svc.generate(week_start=WEEK_START)
    svc.generate(week_start=WEEK_START)  # regenerate
    # Unique (user, type, period) ⇒ still ONE row, version bumped to 2.
    rows = store._conn.execute(
        "SELECT COUNT(*), MAX(version) FROM insight_aggregation "
        "WHERE user_id = ? AND aggregation_type = 'weekly_mirror'", [USER]).fetchone()
    assert rows[0] == 1
    assert rows[1] == 2


def test_placeholder_path_still_stored(store):
    """The cold-start placeholder is itself a cached insight (so the UI shows it
    consistently + the scheduled job doesn't re-attempt hourly)."""
    _set_registration(store, 2)  # insufficient
    _seed_memos(store, 5)
    caller, calls = _mock_caller()
    res = _service(store, caller).generate(week_start=WEEK_START)
    assert res["status"] == "placeholder"
    rows = store._conn.execute(
        "SELECT data_sufficiency, llm_call_id FROM insight_aggregation "
        "WHERE user_id = ?", [USER]).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "insufficient"
    assert rows[0][1] is None  # no LLM call ⇒ no llm_call_id


# ---------------------------------------------------------------------------
# 5. Window resolution
# ---------------------------------------------------------------------------

def test_resolve_week_default_is_previous_completed_week():
    # Wed 2026-07-15 → previous Mon..Sun = 2026-07-06 .. 2026-07-12.
    win = _resolve_week(FIXED_NOW, None)
    assert win["period_key"] == "2026-07-06"
    assert win["week_start_display"] == "2026-07-06"
    assert win["week_end_display"] == "2026-07-12"


def test_resolve_week_explicit_start():
    win = _resolve_week(FIXED_NOW, "2026-06-01")
    assert win["period_key"] == "2026-06-01"
    assert win["week_end_display"] == "2026-06-07"


# ---------------------------------------------------------------------------
# 6. End-to-end: the mirror reflects real seeded data
# ---------------------------------------------------------------------------

def test_end_to_end_mirror_references_real_week(store):
    _set_registration(store, 30)
    _seed_memos(store, PARTIAL_MEMO_THRESHOLD + 5)
    _seed_week_atoms(store, IN_WINDOW_TS)
    # The mock caller is a stand-in for the real LLM, but the service MUST pass
    # the real aggregated data (张三 / 述职 / React diff / 愤怒) into the prompt.
    seen = {}

    def capturing(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(
                content="# 本周镜面\n" + "张三述职React愤怒" * 20))],
            model="m", provider="p",
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=10))

    res = _service(store, capturing).generate(week_start=WEEK_START)
    assert res["status"] == "mirror"
    # The aggregation fed to the LLM carries the real week's people + topics.
    user_msg = seen["messages"][1]["content"]
    assert "张三" in user_msg
    assert "述职报告" in user_msg or "React diff" in user_msg

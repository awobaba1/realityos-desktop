"""RealityOS V6 — daily report tests (ADR-V6-018, 架构 §4.4/§18.5).

Covers the same four production-critical properties as the weekly mirror, but
for the 1-day window + atom-count gate:
  1. aggregation is window-bounded to one day (only that day's atoms).
  2. the atom-count gate: < 3 atoms that day ⇒ placeholder with NO LLM call;
     3..7 ⇒ partial; ≥ 8 ⇒ sufficient (the §0.5③ memo/registration gate is
     weekly-specific — a day gates on that day's atoms).
  3. the LLM path: success ⇒ C5-validated report stored + logged (C6); failure
     and C5-invalid ⇒ DLQ + degrade to placeholder (C7).
  4. storage: upsert into insight_aggregation (aggregation_type='daily_report'),
     regenerate replaces in place.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from plugins.memory.ptg.store import PTGStore
from plugins.realityos_insights.aggregation import aggregate_window
from plugins.realityos_insights.daily_report import (
    MIN_ATOMS,
    PARTIAL_ATOM_THRESHOLD,
    DailyReportService,
    _resolve_day,
)

_BEIJING_TZ = timezone(timedelta(hours=8))
USER = "founder-1"

# Fixed "now" = 2026-07-15 10:00 Beijing (Wednesday). Default day ⇒ yesterday =
# 2026-07-14 Beijing ⇒ UTC window [07-13T16:00, 07-14T16:00).
FIXED_NOW = datetime(2026, 7, 15, 10, 0, tzinfo=_BEIJING_TZ)
DAY = "2026-07-14"
IN_WINDOW_TS = "2026-07-14T05:00:00+00:00"   # mid-day 14th UTC, inside window
OUT_WINDOW_TS = "2026-07-15T05:00:00+00:00"  # the day after, outside window
DAY_START_UTC = "2026-07-13T16:00:00+00:00"
DAY_END_UTC = "2026-07-14T16:00:00+00:00"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    s = PTGStore(db_path=str(tmp_path / "ptg.db"))
    s.ensure_founder(USER, "founder@realityos.local")
    yield s
    s.close()


def _mock_caller(content="# 今日报告（2026-07-14）\n\n"
                         "今天你和张三推进了述职报告，也被甲方改需求弄得有点烦。"
                         "整体节奏紧凑，下午精力往下走了一些。"):
    """A recording mock caller returning a valid daily report."""
    calls = []

    def _c(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            model="mock-model", provider="mock",
            usage=SimpleNamespace(prompt_tokens=80, completion_tokens=140))

    return _c, calls


def _service(store, caller=None, now_fn=lambda: FIXED_NOW) -> DailyReportService:
    return DailyReportService(store, user_id=USER, caller=caller, now_fn=now_fn)


def _seed_n_atoms(store, ts: str, n: int) -> None:
    """Seed n R3_Person atoms at ts (each its own name ⇒ n atoms)."""
    for i in range(n):
        store.insert_identity_event(
            user_id=USER, source_text="x", person_name=f"人物{i}",
            mention_context=f"今天聊了{i}", confidence_base=0.9,
            relation_confidence=0.9, timestamp=ts)


def _seed_rich_day(store, ts: str) -> None:
    """One of each of the 8 atom types at ts (8 atoms total ⇒ sufficient)."""
    store.insert_identity_event(user_id=USER, source_text="x", person_name="张三",
                                mention_context="今天聊述职报告",
                                confidence_base=0.9, relation_confidence=0.9, timestamp=ts)
    store.insert_meaning_event(user_id=USER, source_text="x", intent_class="Need_To_Do",
                               task_description="写述职报告", urgency="high", atom_kind="R2",
                               confidence_base=0.9, relation_confidence=0.9, timestamp=ts)
    store.insert_meaning_event(user_id=USER, source_text="x", intent_class="Other",
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
    store.insert_feeling_event(user_id=USER, source_text="x", state_type="fatigue",
                               direction="up", intensity="medium", atom_kind="R1",
                               confidence_base=0.9, relation_confidence=0.9, timestamp=ts)
    store.insert_meaning_event(user_id=USER, source_text="x", intent_class="Other",
                               task_description="React diff 算法", topic_tags='["diff"]',
                               atom_kind="R8",
                               completion_note='{"engagement":"high","is_question":false}',
                               confidence_base=0.9, relation_confidence=0.9, timestamp=ts)
    store.insert_meaning_event(user_id=USER, source_text="x", intent_class="Need_To_Do",
                               task_description="明天想早点睡", atom_kind="R7",
                               confidence_base=0.9, relation_confidence=0.9, timestamp=ts)
    store.insert_entity_event(user_id=USER, source_text="x", entity_name="飞书",
                              entity_category="term", mention_context="用飞书沟通",
                              confidence_base=0.9, relation_confidence=0.9, timestamp=ts)


# ---------------------------------------------------------------------------
# 1. Aggregation: window-bounded to one day
# ---------------------------------------------------------------------------

def test_aggregation_only_counts_in_day_window(store):
    _seed_n_atoms(store, IN_WINDOW_TS, 3)
    _seed_n_atoms(store, OUT_WINDOW_TS, 3)  # next day → excluded
    agg = aggregate_window(store, user_id=USER,
                           week_start=DAY_START_UTC, week_end=DAY_END_UTC)
    assert agg["atom_total"] == 3
    assert agg["atom_counts"].get("R3_Person") == 3


def test_aggregation_empty_day(store):
    agg = aggregate_window(store, user_id=USER,
                           week_start=DAY_START_UTC, week_end=DAY_END_UTC)
    assert agg["atom_total"] == 0
    assert agg["people"] == []


# ---------------------------------------------------------------------------
# 2. Atom-count gate (daily §0.5③ analogue)
# ---------------------------------------------------------------------------

def test_gate_insufficient_low_atoms(store):
    _seed_n_atoms(store, IN_WINDOW_TS, MIN_ATOMS - 1)  # 2 atoms → insufficient
    caller, calls = _mock_caller()
    res = _service(store, caller).generate(day=DAY)
    assert res["status"] == "placeholder"
    assert res["data_sufficiency"] == "insufficient"
    assert res["llm_call_id"] is None
    assert calls == []  # NO LLM call on the placeholder path
    assert "聊得不多" in res["content"]


def test_gate_boundary_min_atoms_is_partial(store):
    # Exactly MIN_ATOMS (3) is the first non-placeholder band → partial.
    _seed_n_atoms(store, IN_WINDOW_TS, MIN_ATOMS)
    caller, calls = _mock_caller()
    res = _service(store, caller).generate(day=DAY)
    assert res["status"] == "report"
    assert res["data_sufficiency"] == "partial"
    assert len(calls) == 1


def test_gate_partial(store):
    _seed_n_atoms(store, IN_WINDOW_TS, PARTIAL_ATOM_THRESHOLD - 1)  # 7 → partial
    caller, calls = _mock_caller()
    res = _service(store, caller).generate(day=DAY)
    assert res["status"] == "report"
    assert res["data_sufficiency"] == "partial"
    assert len(calls) == 1


def test_gate_sufficient(store):
    _seed_rich_day(store, IN_WINDOW_TS)  # 8 atoms → sufficient
    caller, calls = _mock_caller()
    res = _service(store, caller).generate(day=DAY)
    assert res["status"] == "report"
    assert res["data_sufficiency"] == "sufficient"
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# 3. LLM path: success logs (C6); failure + C5-invalid DLQ + degrade (C7)
# ---------------------------------------------------------------------------

def test_llm_success_logs_and_stores(store):
    _seed_rich_day(store, IN_WINDOW_TS)
    caller, _ = _mock_caller()
    res = _service(store, caller).generate(day=DAY)
    assert res["status"] == "report"
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
    _seed_rich_day(store, IN_WINDOW_TS)

    def boom(**_kwargs):
        raise RuntimeError("provider timeout")

    res = _service(store, boom).generate(day=DAY)
    # Failure ⇒ placeholder content (error variant), but still a stored row.
    assert res["status"] == "report"  # gate passed (it's the LLM that failed)
    assert "还没准备好" in res["content"]
    assert res["schema_valid"] is False
    # C7: a DLQ entry was written with source=daily_report.
    dlq = store._conn.execute(
        "SELECT source FROM dlq_messages WHERE user_id = ? "
        "ORDER BY created_at DESC LIMIT 1", [USER]).fetchone()
    assert dlq is not None
    assert dlq[0] == "daily_report"
    # C6: the failed call is logged success=0.
    log = store._conn.execute(
        "SELECT success FROM llm_call_logs WHERE id = ?", [res["llm_call_id"]]).fetchone()
    assert log[0] == 0


def test_c5_invalid_output_degrades_and_dlqs(store):
    _seed_rich_day(store, IN_WINDOW_TS)
    caller, _ = _mock_caller(content="ok")  # too short → fails daily MIN_CHARS (60)
    res = _service(store, caller).generate(day=DAY)
    assert res["schema_valid"] is False
    assert "还没准备好" in res["content"]
    dlq = store._conn.execute(
        "SELECT error_type FROM dlq_messages WHERE user_id = ? "
        "AND source='daily_report'", [USER]).fetchone()
    assert dlq is not None
    assert dlq[0] == "daily_report_schema_invalid"


# ---------------------------------------------------------------------------
# 4. Storage: upsert into insight_aggregation, regenerate replaces
# ---------------------------------------------------------------------------

def test_report_stored_in_insight_aggregation(store):
    _seed_rich_day(store, IN_WINDOW_TS)
    caller, _ = _mock_caller()
    res = _service(store, caller).generate(day=DAY)
    rows = store._conn.execute(
        "SELECT aggregation_type, period_key, data_sufficiency, llm_call_id, "
        "result_data FROM insight_aggregation WHERE user_id = ?", [USER]).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "daily_report"
    assert rows[0][1] == DAY
    assert rows[0][2] == "sufficient"
    assert rows[0][3] == res["llm_call_id"]
    assert "今日报告" in rows[0][4]


def test_regenerate_same_day_replaces_in_place(store):
    _seed_rich_day(store, IN_WINDOW_TS)
    caller, _ = _mock_caller()
    svc = _service(store, caller)
    svc.generate(day=DAY)
    svc.generate(day=DAY)  # regenerate
    # Unique (user, type, period) ⇒ still ONE row, version bumped to 2.
    rows = store._conn.execute(
        "SELECT COUNT(*), MAX(version) FROM insight_aggregation "
        "WHERE user_id = ? AND aggregation_type = 'daily_report'", [USER]).fetchone()
    assert rows[0] == 1
    assert rows[1] == 2


def test_placeholder_path_still_stored(store):
    """The cold-start placeholder is itself a cached insight (so the UI shows it
    consistently + the scheduled job doesn't re-attempt hourly)."""
    _seed_n_atoms(store, IN_WINDOW_TS, 1)  # insufficient
    caller, calls = _mock_caller()
    res = _service(store, caller).generate(day=DAY)
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

def test_resolve_day_default_is_yesterday():
    # Wed 2026-07-15 10:00 Beijing → yesterday = 2026-07-14.
    win = _resolve_day(FIXED_NOW, None)
    assert win["period_key"] == "2026-07-14"
    assert win["start_display"] == "2026-07-14"
    assert win["end_display"] == "2026-07-14"  # single-day report


def test_resolve_day_explicit():
    win = _resolve_day(FIXED_NOW, "2026-06-01")
    assert win["period_key"] == "2026-06-01"
    assert win["start_display"] == "2026-06-01"
    assert win["end_display"] == "2026-06-01"


# ---------------------------------------------------------------------------
# 6. End-to-end: the report reflects real seeded data
# ---------------------------------------------------------------------------

def test_end_to_end_report_references_real_day(store):
    _seed_rich_day(store, IN_WINDOW_TS)
    # The mock caller is a stand-in for the real LLM, but the service MUST pass
    # the real aggregated data (张三 / 述职 / React diff / 愤怒) into the prompt.
    seen = {}

    def capturing(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(
                content="# 今日报告\n" + "张三述职React愤怒" * 15))],
            model="m", provider="p",
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=10))

    res = _service(store, capturing).generate(day=DAY)
    assert res["status"] == "report"
    # The aggregation fed to the LLM carries the real day's people + topics.
    user_msg = seen["messages"][1]["content"]
    assert "张三" in user_msg
    assert "述职报告" in user_msg or "React" in user_msg

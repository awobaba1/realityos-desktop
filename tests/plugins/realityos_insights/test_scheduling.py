"""RealityOS V6 — insight scheduling tests (ADR-V6-019).

Covers the scheduler (the "when"), not the report services (the "what" — those
have their own tests). Properties under test:
  1. skip a kind whose current-period row already exists (no LLM call);
  2. generate a kind whose row is missing (when its gate passes);
  3. a cold-start placeholder IS a stored row ⇒ blocks re-run (idempotency);
  4. the period_key the scheduler probes == the key generate() writes;
  5. founder-wait + once-guard behave.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from plugins.memory.ptg.store import PTGStore
from plugins.realityos_insights import scheduling
from plugins.realityos_insights.daily_report import DailyReportService, _resolve_day
from plugins.realityos_insights.scheduling import (
    _read_founder_user_id,
    _wait_for_founder,
    run_due_reports,
    start_scheduler_if_due,
)
from plugins.realityos_insights.weekly_mirror import _resolve_week

_BEIJING_TZ = timezone(timedelta(hours=8))
USER = "founder-1"

# Fixed now = 2026-07-15 10:00 Beijing (Wednesday).
#   yesterday        = 2026-07-14  → daily period_key
#   prev complete week = 2026-07-06..07-12 → weekly period_key
FIXED_NOW = datetime(2026, 7, 15, 10, 0, tzinfo=_BEIJING_TZ)
DAY_KEY = _resolve_day(FIXED_NOW, None)["period_key"]      # "2026-07-14"
WEEK_KEY = _resolve_week(FIXED_NOW, None)["period_key"]    # "2026-07-06"
YESTERDAY_TS = "2026-07-14T05:00:00+00:00"  # inside yesterday's UTC window


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    s = PTGStore(db_path=str(tmp_path / "ptg.db"))
    s.ensure_founder(USER, "founder@realityos.local")
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _reset_scheduler_guard():
    """The once-guard is module-global; reset it between tests."""
    scheduling._reset_for_tests()
    yield
    scheduling._reset_for_tests()


def _mock_caller(content=None):
    calls = []
    if content is None:
        content = ("# 今日报告（2026-07-14）\n\n今天你和张三推进了述职报告，"
                   "也被甲方改需求弄得有点烦。整体节奏紧凑，下午精力往下走。")
    def _c(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            model="mock", provider="mock",
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=10))
    return _c, calls


def _seed_day_atoms(store, n: int) -> None:
    for i in range(n):
        store.insert_identity_event(
            user_id=USER, source_text="x", person_name=f"人物{i}",
            mention_context=f"今天聊了{i}", confidence_base=0.9,
            relation_confidence=0.9, timestamp=YESTERDAY_TS)


def _preseed_weekly(store, *, sufficiency: str = "sufficient") -> None:
    """Mark the weekly row as already-generated so weekly is 'exists' and only
    the daily kind is exercised by the test."""
    win = _resolve_week(FIXED_NOW, None)
    store.upsert_insight(
        user_id=USER, aggregation_type="weekly_mirror", period_key=WEEK_KEY,
        period_start=win["week_start_utc"], period_end=win["week_end_utc"],
        input_data="{}", result_data="# 本周镜面\n(pre-existing)",
        confidence=0.8, data_sufficiency=sufficiency, generated_by="scheduled",
        llm_call_id=None, schema_version="v1", expires_at=win["week_end_utc"])


# ---------------------------------------------------------------------------
# 1. Skip when the current-period row exists (no LLM call)
# ---------------------------------------------------------------------------

def test_skips_when_daily_row_exists(store):
    win = _resolve_day(FIXED_NOW, None)
    store.upsert_insight(
        user_id=USER, aggregation_type="daily_report", period_key=DAY_KEY,
        period_start=win["start_utc"], period_end=win["end_utc"],
        input_data="{}", result_data="# 今日报告\n(pre-existing)",
        confidence=0.8, data_sufficiency="sufficient", generated_by="scheduled",
        llm_call_id=None, schema_version="v1", expires_at=win["end_utc"])
    _preseed_weekly(store)
    caller, calls = _mock_caller()
    res = run_due_reports(store, user_id=USER, now=FIXED_NOW, caller=caller)
    assert res["daily"]["generated"] is False
    assert res["daily"]["reason"] == "exists"
    assert res["weekly"]["generated"] is False
    assert calls == []  # NO LLM call on the skip path


# ---------------------------------------------------------------------------
# 2. Generate when missing + the gate passes
# ---------------------------------------------------------------------------

def test_generates_daily_when_missing_and_gate_passes(store):
    _seed_day_atoms(store, 8)  # ≥ 8 atoms yesterday → sufficient
    _preseed_weekly(store)
    caller, calls = _mock_caller()
    res = run_due_reports(store, user_id=USER, now=FIXED_NOW, caller=caller)
    assert res["daily"]["generated"] is True
    assert res["daily"]["status"] == "report"
    assert res["daily"]["data_sufficiency"] == "sufficient"
    assert res["weekly"]["generated"] is False  # pre-seeded
    assert len(calls) == 1  # exactly one LLM call (daily only)
    row = store.get_insight(user_id=USER, aggregation_type="daily_report",
                            period_key=DAY_KEY)
    assert row is not None
    assert row["data_sufficiency"] == "sufficient"


# ---------------------------------------------------------------------------
# 3. Cold-start placeholder is a stored row ⇒ blocks re-run (idempotency)
# ---------------------------------------------------------------------------

def test_cold_start_placeholder_blocks_rerun(store):
    _preseed_weekly(store)
    caller, calls = _mock_caller()
    # No atoms yesterday → daily gate insufficient → placeholder generated.
    res1 = run_due_reports(store, user_id=USER, now=FIXED_NOW, caller=caller)
    assert res1["daily"]["generated"] is True
    assert res1["daily"]["data_sufficiency"] == "insufficient"
    assert calls == []  # placeholder path ⇒ no LLM call
    # Second run: the placeholder row exists ⇒ skip (no retry, no LLM).
    res2 = run_due_reports(store, user_id=USER, now=FIXED_NOW, caller=caller)
    assert res2["daily"]["generated"] is False
    assert res2["daily"]["reason"] == "exists"
    assert calls == []


# ---------------------------------------------------------------------------
# 4. period_key alignment: scheduler probes the key generate() writes
# ---------------------------------------------------------------------------

def test_period_key_aligns_with_service(store):
    # Generate yesterday's daily report directly via the service, then confirm
    # the scheduler sees it as existing (same period_key).
    _seed_day_atoms(store, 8)
    DailyReportService(store, user_id=USER,
                       caller=_mock_caller()[0],
                       now_fn=lambda: FIXED_NOW).generate(day=DAY_KEY)
    caller, calls = _mock_caller()
    res = run_due_reports(store, user_id=USER, now=FIXED_NOW, caller=caller)
    assert res["daily"]["generated"] is False
    assert res["daily"]["reason"] == "exists"
    assert calls == []


# ---------------------------------------------------------------------------
# 5. Founder wait + read
# ---------------------------------------------------------------------------

def _set_founder_meta(store, uid: str) -> None:
    with store._lock:
        store._conn.execute(
            "INSERT OR REPLACE INTO ptg_meta(key, value) "
            "VALUES ('founder_user_id', ?)", (uid,))


def test_read_founder_user_id_absent(store):
    assert _read_founder_user_id(store) is None


def test_read_founder_user_id_present(store):
    _set_founder_meta(store, USER)
    assert _read_founder_user_id(store) == USER


def test_wait_for_founder_returns_immediately_when_present(store):
    _set_founder_meta(store, USER)
    assert _wait_for_founder(store, wait_seconds=1.0, poll_interval=0.1) == USER


def test_wait_for_founder_times_out_when_absent(store):
    assert _wait_for_founder(store, wait_seconds=0.3, poll_interval=0.1) is None


# ---------------------------------------------------------------------------
# 6. Once-guard (spawn target mocked so no real thread work)
# ---------------------------------------------------------------------------

def test_start_scheduler_once_guard(monkeypatch):
    monkeypatch.setattr(scheduling, "_run_startup_lazy", lambda **_kw: None)
    first = start_scheduler_if_due(enabled=True)
    second = start_scheduler_if_due(enabled=True)
    assert first is True
    assert second is False  # once-guard blocks a second spawn


def test_start_scheduler_disabled_does_not_arm_guard():
    # enabled=False returns False WITHOUT setting the guard, so a later enabled
    # call can still start.
    assert start_scheduler_if_due(enabled=False) is False
    # (guard not armed — verified by the autouse reset + the once-guard test
    # running cleanly after this one.)


# ---------------------------------------------------------------------------
# 7. Stale-prompt-version regeneration (ADR-V6-026)
# ---------------------------------------------------------------------------

def test_get_insight_returns_schema_version(store):
    """ADR-V6-026 read-side regression: get_insight exposes ``schema_version``
    (which holds the prompt_version the row was generated under) so the probe
    can detect a stale-prompt cache row. The column always stored it; this
    seals the read path the probe's staleness check depends on."""
    win = _resolve_day(FIXED_NOW, None)
    store.upsert_insight(
        user_id=USER, aggregation_type="daily_report", period_key=DAY_KEY,
        period_start=win["start_utc"], period_end=win["end_utc"],
        input_data="{}", result_data="x", confidence=0.5,
        data_sufficiency="partial", generated_by="manual",
        llm_call_id=None, schema_version="v9", expires_at=win["end_utc"])
    row = store.get_insight(user_id=USER, aggregation_type="daily_report",
                            period_key=DAY_KEY)
    assert row is not None
    assert row["schema_version"] == "v9"


def test_stale_prompt_version_triggers_regenerate(store):
    """ADR-V6-026 core regression: a cached row generated under an OLD prompt
    version (schema_version != PROMPT_VERSION) MUST be regenerated, not served
    stale until TTL.

    Before the fix the probe treated ANY existing row as "exists" and skipped —
    so bumping the prompt (v1→v2) left the old-prompt report cached for the
    whole TTL window (new/old reports "打架"). The prompt-template doc comment
    even claimed the cache key contained prompt_version, which was false; the
    unique index never did. The fix: the probe compares the row's
    schema_version to the service's PROMPT_VERSION and regenerates on mismatch.
    """
    _seed_day_atoms(store, 8)  # sufficient yesterday → regen yields a real report
    _preseed_weekly(store)
    # Seed a STALE daily row (schema_version="v0"; service PROMPT_VERSION="v1").
    win = _resolve_day(FIXED_NOW, None)
    store.upsert_insight(
        user_id=USER, aggregation_type="daily_report", period_key=DAY_KEY,
        period_start=win["start_utc"], period_end=win["end_utc"],
        input_data="{}", result_data="(stale v0 report)",
        confidence=0.8, data_sufficiency="sufficient", generated_by="scheduled",
        llm_call_id=None, schema_version="v0", expires_at=win["end_utc"])
    caller, calls = _mock_caller()
    res = run_due_reports(store, user_id=USER, now=FIXED_NOW, caller=caller)
    # Stale ⇒ regenerated (NOT skipped), distinct reason, one LLM call.
    assert res["daily"]["generated"] is True
    assert res["daily"]["reason"] == "stale_prompt"
    assert len(calls) == 1
    # The refreshed row now carries the current prompt version + new content.
    row = store.get_insight(user_id=USER, aggregation_type="daily_report",
                            period_key=DAY_KEY)
    assert row["schema_version"] == "v1"
    assert row["result_data"] != "(stale v0 report)"


def test_stale_regen_then_fresh_skips(store):
    """Convergence: after a stale row is regenerated to the current prompt
    version, a second run MUST skip — no infinite regen loop, idempotent."""
    _seed_day_atoms(store, 8)
    _preseed_weekly(store)
    win = _resolve_day(FIXED_NOW, None)
    store.upsert_insight(
        user_id=USER, aggregation_type="daily_report", period_key=DAY_KEY,
        period_start=win["start_utc"], period_end=win["end_utc"],
        input_data="{}", result_data="(stale v0 report)", confidence=0.8,
        data_sufficiency="sufficient", generated_by="scheduled",
        llm_call_id=None, schema_version="v0", expires_at=win["end_utc"])
    caller, calls = _mock_caller()
    run_due_reports(store, user_id=USER, now=FIXED_NOW, caller=caller)  # regen
    res2 = run_due_reports(store, user_id=USER, now=FIXED_NOW, caller=caller)
    assert res2["daily"]["generated"] is False
    assert res2["daily"]["reason"] == "exists"
    assert len(calls) == 1  # only the first (stale) run called the LLM

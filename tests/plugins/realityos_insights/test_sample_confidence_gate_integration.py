"""C4 integration: sample-size confidence gate FIRES in the real generate() flow.

Bug ID: F6 (ADR-V6-042) — the single biggest fake-green source in V6.

This test proves the wiring is end-to-end real, not just a pure-function unit
test that nothing calls. Scenario:
  - Cold-start gate PASSES (40 memos, 30 days registration → 'sufficient' →
    LLM path taken, a mirror IS generated and cached).
  - But the in-window atom sample is THIN (only 2 R9_Emotion atoms). The
    generated mirror's emotion narrative rests on n=2.

Before F6: cached confidence = 0.8 ('sufficient') — the cache presents a
strong emotional narrative backed by 2 data points as high-trust insight.
That is the fake-green.

After F6: cached confidence is CAPPED to CONF_FLOOR (0.1), and the sample
verdict ('insufficient') + weakest kind ('R9_Emotion') are recorded in
input_data for traceability. The mirror is still generated (cold-start
passed), but the cache honestly signals "do not trust strongly".

A paired control test seeds abundant atoms (≥30 each) and asserts the cap
does NOT fire — confidence stays at the cold-start 0.8.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from plugins.memory.ptg.store import PTGStore
from plugins.realityos_insights.weekly_mirror import WeeklyMirrorService, _resolve_week
from plugins.memory.ptg.confidence_gate import CONF_FLOOR

_BEIJING_TZ = timezone(timedelta(hours=8))
USER = "founder-1"
FIXED_NOW = datetime(2026, 7, 15, 10, 0, tzinfo=_BEIJING_TZ)
WEEK_START = "2026-07-06"
IN_WINDOW_TS = "2026-07-08T10:00:00+00:00"


@pytest.fixture
def store(tmp_path):
    s = PTGStore(db_path=str(tmp_path / "ptg.db"))
    s.ensure_founder(USER, "founder@realityos.local")
    yield s
    s.close()


def _set_registration(store, days_ago: int) -> None:
    created = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    with store._lock:
        store._conn.execute(
            "UPDATE realityos_users SET created_at = ? WHERE id = ?", (created, USER))


def _seed_memos(store, n: int) -> None:
    for i in range(n):
        store.insert_memo(user_id=USER, source_text=f"memo {i}", input_mode="text")


def _mock_caller():
    """Returns a caller that yields a valid (C5-passing, ≥80-char) mirror."""
    def _c(**kwargs):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(
                content=("# 本周镜面（2026-07-06 ~ 2026-07-12）\n\n"
                         "这周你主要和张三推进了 Q3 述职报告的撰写，反复修改了三版。"
                         "情绪上被甲方临时改需求弄得很烦躁，精力消耗较大。整体节奏紧凑。")))],
            model="mock", provider="mock", usage=None)
    return _c


def _read_input_data(store, period_key: str) -> dict:
    """Read the cached insight_aggregation.input_data JSON directly (get_insight
    doesn't surface input_data — it's the traceability column for sample gate)."""
    import json
    with store._lock:
        row = store._conn.execute(
            "SELECT input_data FROM insight_aggregation "
            "WHERE user_id=? AND aggregation_type='weekly_mirror' AND period_key=?",
            (USER, period_key)).fetchone()
    if row is None or not row["input_data"]:
        return {}
    return json.loads(row["input_data"])


def _seed_thin_emotions(store, n: int) -> None:
    """Seed n R9_Emotion atoms in-window — a thin emotion sample."""
    for _ in range(n):
        store.insert_feeling_event(
            user_id=USER, source_text="x", state_type="mood",
            direction="down", intensity="high",
            emotion_vad='{"valence":"negative","arousal":"high","label":"烦"}',
            trigger_source='{"trigger":"甲方改需求","atom":"R9_Emotion"}',
            ser_source="llm_text", atom_kind="R9",
            confidence_base=0.9, relation_confidence=0.9, timestamp=IN_WINDOW_TS)


def _seed_abundant_all_kinds(store) -> None:
    """Seed ≥30 of each atom kind in-window — abundant samples, cap won't fire."""
    for _ in range(35):
        store.insert_feeling_event(
            user_id=USER, source_text="x", state_type="mood", direction="down",
            intensity="high", atom_kind="R9",
            emotion_vad='{"valence":"negative","arousal":"high"}',
            confidence_base=0.9, relation_confidence=0.9, timestamp=IN_WINDOW_TS)
        store.insert_feeling_event(
            user_id=USER, source_text="x", state_type="stress", direction="up",
            intensity="medium", atom_kind="R1",
            confidence_base=0.9, relation_confidence=0.9, timestamp=IN_WINDOW_TS)
        store.insert_identity_event(
            user_id=USER, source_text="x", person_name="张三", mention_context="x",
            confidence_base=0.9, relation_confidence=0.9, timestamp=IN_WINDOW_TS)
        store.insert_meaning_event(
            user_id=USER, source_text="x", intent_class="Need_To_Do",
            task_description="写报告", atom_kind="R2",
            confidence_base=0.9, relation_confidence=0.9, timestamp=IN_WINDOW_TS)
        store.insert_entity_event(
            user_id=USER, source_text="x", entity_name="飞书", entity_category="term",
            mention_context="x", confidence_base=0.9, relation_confidence=0.9,
            timestamp=IN_WINDOW_TS)


def test_thin_sample_caps_cached_confidence_even_when_cold_start_sufficient(store):
    """F6 end-to-end: cold-start 'sufficient' (LLM mirror generated) BUT emotion
    sample n=2 → cached confidence CAPPED to CONF_FLOOR, sample verdict recorded."""
    _set_registration(store, 30)          # registered 30d ago → reg gate passes
    _seed_memos(store, 40)                # 40 memos → memo gate passes (≥30)
    _seed_thin_emotions(store, 2)         # ONLY 2 R9 atoms in-window (thin)

    svc = WeeklyMirrorService(
        store, user_id=USER, caller=_mock_caller(), now_fn=lambda: FIXED_NOW)
    result = svc.generate(week_start=WEEK_START)
    period_key = _resolve_week(FIXED_NOW, WEEK_START)["period_key"]

    # Cold-start gate passed → LLM mirror generated (not placeholder).
    assert result["status"] == "mirror", "cold-start should pass (40 memos, 30d reg)"
    assert result["llm_call_id"] is not None, "LLM should have been called"

    # F6: the cached confidence must be CAPPED — the only present kind (R9_Emotion)
    # has n=2 < MIN_SAMPLE → CONF_FLOOR.
    cached = store.get_insight(user_id=USER, aggregation_type="weekly_mirror",
                               period_key=period_key)
    assert cached is not None, "mirror must be cached"
    assert cached["confidence"] == pytest.approx(CONF_FLOOR), (
        f"F6 regression: thin sample (R9 n=2) must cap cached confidence to "
        f"CONF_FLOOR ({CONF_FLOOR}), got {cached['confidence']}. The cold-start "
        f"'sufficient' verdict must NOT let a 2-data-point narrative cache as "
        f"high-trust insight (fake-green).")
    # Traceability: sample verdict recorded in input_data (read directly —
    # get_insight surfaces confidence but not input_data).
    input_data = _read_input_data(store, period_key)
    assert input_data.get("sample_sufficiency") == "insufficient"
    assert input_data.get("sample_weakest_kind") == "R9_Emotion"


def test_abundant_samples_keep_cold_start_confidence(store):
    """F6 control: ≥30 of each kind → cap does NOT fire → confidence stays 0.8
    (cold-start 'sufficient'). Proves the gate is not catastrophically
    pessimistic — well-backed reports keep their trust."""
    _set_registration(store, 30)
    _seed_memos(store, 40)
    _seed_abundant_all_kinds(store)

    svc = WeeklyMirrorService(
        store, user_id=USER, caller=_mock_caller(), now_fn=lambda: FIXED_NOW)
    result = svc.generate(week_start=WEEK_START)
    period_key = _resolve_week(FIXED_NOW, WEEK_START)["period_key"]
    assert result["status"] == "mirror"

    cached = store.get_insight(user_id=USER, aggregation_type="weekly_mirror",
                               period_key=period_key)
    assert cached is not None
    # Cold-start sufficient (0.8), all present kinds ≥30 → no cap → stays 0.8.
    assert cached["confidence"] == pytest.approx(0.8), (
        f"F6 over-pessimism: abundant samples (≥30/kind) should NOT cap "
        f"confidence. Expected 0.8 (cold-start sufficient), got {cached['confidence']}.")
    input_data = _read_input_data(store, period_key)
    assert input_data.get("sample_sufficiency") == "sufficient"

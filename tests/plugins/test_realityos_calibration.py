"""Regression tests for the calibration plugin (ADR-V6-028 §11.4/§11.5).

Drives ``run_calibration`` with a deterministic mock rater (no tty) and asserts
the §11.5 contract end-to-end: verdicts land in feedback, "不准" atoms are
confidence-demoted (not deleted), "惊喜" rows accumulate, and a correction_rate
quality_metric is appended. Also locks the early-exit partial-result behavior.
"""

from __future__ import annotations

import pytest

from plugins.memory.ptg.store import PTGStore
from plugins.realityos_calibration import (
    CALIBRATION_FLOOR,
    VERDICT_CORRECT,
    VERDICT_SKIP,
    VERDICT_SURPRISE,
    VERDICT_WRONG,
    format_atom_for_display,
    run_calibration,
)


@pytest.fixture
def store(tmp_path):
    s = PTGStore(db_path=str(tmp_path / "ptg.db"))
    s.ensure_founder("u1", "founder@realityos.local")
    yield s
    s.close()


def _seed_three_atoms(store):
    """Seed R3 (high-conf wrong), R2 (correct), R0 (surprise). Returns atoms."""
    mid = store.insert_memo(user_id="u1", source_text="x", input_mode="text")
    store.insert_identity_event(
        user_id="u1", source_text="x", person_name="错误的人",
        confidence_base=0.95, relation_confidence=0.95, memo_id=mid)
    store.insert_meaning_event(
        user_id="u1", source_text="x", intent_class="Need_To_Do",
        task_description="正确的任务", atom_kind="R2",
        confidence_base=0.85, relation_confidence=0.85, memo_id=mid)
    store.insert_entity_event(
        user_id="u1", source_text="x", entity_name="惊喜地点",
        entity_category="place", confidence_base=0.7, relation_confidence=0.7, memo_id=mid)
    return store.recent_atoms(user_id="u1", memo_id=mid)


def _fb_count(store, target_type):
    return store._conn.execute(
        "SELECT COUNT(*) FROM feedback WHERE user_id=? AND target_type=? "
        "AND deleted_at IS NULL", ("u1", target_type)).fetchone()[0]


def test_run_calibration_full_three_way(store):
    atoms = _seed_three_atoms(store)
    # Content-based dispatch (deterministic regardless of recent_atoms' tie-order):
    # the 错误的人 atom → WRONG (demoted), 正确的任务 → CORRECT, 惊喜地点 → SURPRISE.
    def rater(display, idx, total):
        if "错误的人" in display:
            return VERDICT_WRONG
        if "正确的任务" in display:
            return VERDICT_CORRECT
        if "惊喜地点" in display:
            return VERDICT_SURPRISE
        return VERDICT_SKIP

    result = run_calibration(
        store=store, user_id="u1", atoms=atoms, rater=rater, metric_date="2026-07-20")

    assert result.total_sampled == 3
    assert (result.correct, result.wrong, result.surprise, result.skipped) == (1, 1, 1, 0)
    assert result.judged == 3
    assert result.correction_rate == pytest.approx(1 / 3)
    assert len(result.feedback_ids) == 3
    # Each verdict type wrote exactly one feedback row.
    assert _fb_count(store, "calibration_wrong") == 1
    assert _fb_count(store, "calibration_correct") == 1
    assert _fb_count(store, "calibration_surprise") == 1

    # §11.5: the WRONG atom was demoted to the floor (not deleted).
    r3 = next(a for a in atoms if a["type"] == "R3_Person")
    rc = store._conn.execute(
        "SELECT relation_confidence, deleted_at FROM identity_events WHERE id=?",
        (r3["atom_id"],)).fetchone()
    assert rc["relation_confidence"] == CALIBRATION_FLOOR
    assert rc["deleted_at"] is None  # C2: demoted, not deleted

    # §11.2: a correction_rate quality_metric row landed.
    qm = store._conn.execute(
        "SELECT value, sample_size, note FROM quality_metrics "
        "WHERE user_id=? AND metric_type='correction_rate' AND metric_date=?",
        ("u1", "2026-07-20")).fetchall()
    assert len(qm) == 1
    assert qm[0]["value"] == pytest.approx(1 / 3)
    assert qm[0]["sample_size"] == 3


def test_run_calibration_skip_records_nothing(store):
    atoms = _seed_three_atoms(store)
    verdicts = iter([VERDICT_SKIP, VERDICT_CORRECT, VERDICT_SKIP])
    rater = lambda display, idx, total: next(verdicts)

    result = run_calibration(
        store=store, user_id="u1", atoms=atoms, rater=rater, metric_date="2026-07-20")

    assert (result.correct, result.skipped) == (1, 2)
    assert result.judged == 1
    assert result.correction_rate == 0.0  # 0 wrong / 1 judged
    # Only the one judged atom wrote feedback.
    assert _fb_count(store, "calibration_correct") == 1
    assert _fb_count(store, "calibration_wrong") == 0


def test_run_calibration_no_judged_writes_no_metric(store):
    """An all-skip session records no correction_rate (avoids div-by-zero + noise)."""
    atoms = _seed_three_atoms(store)
    rater = lambda display, idx, total: VERDICT_SKIP
    result = run_calibration(
        store=store, user_id="u1", atoms=atoms, rater=rater, metric_date="2026-07-20")
    assert result.judged == 0 and result.correction_rate == 0.0
    qm = store._conn.execute(
        "SELECT COUNT(*) FROM quality_metrics WHERE metric_type='correction_rate'"
    ).fetchone()[0]
    assert qm == 0


def test_run_calibration_early_exit_keeps_partial(store):
    """A rater that raises (Ctrl-D) ends the loop; the partial result is kept."""
    atoms = _seed_three_atoms(store)

    def rater(display, idx, total):
        if idx == 1:
            return VERDICT_WRONG
        raise EOFError  # founder ends the session after the first atom.

    result = run_calibration(
        store=store, user_id="u1", atoms=atoms, rater=rater, metric_date="2026-07-20")
    assert result.total_sampled == 2  # idx incremented before the raise on atom 2
    assert result.wrong == 1 and result.judged == 1
    assert result.correction_rate == 1.0
    # The one demotion + one feedback row landed.
    assert _fb_count(store, "calibration_wrong") == 1
    assert len(result.feedback_ids) == 1


def test_run_calibration_accepts_chinese_shortforms(store):
    atoms = _seed_three_atoms(store)
    verdicts = iter(["不准", "准", "惊喜"])  # founder-typed short-forms.
    rater = lambda display, idx, total: next(verdicts)
    result = run_calibration(
        store=store, user_id="u1", atoms=atoms, rater=rater, metric_date="2026-07-20")
    assert (result.wrong, result.correct, result.surprise) == (1, 1, 1)


def test_format_atom_for_display_never_raises():
    """A malformed/future atom dict must render a fallback, not break the loop."""
    out = format_atom_for_display({"type": "R99_Unknown", "confidence": 0.5})
    assert "R99_Unknown" in out and "50%" in out
    # Missing confidence → '?' (no TypeError on None).
    out2 = format_atom_for_display({"type": "R3_Person", "person_name": "x"})
    assert "x" in out2 and "?" in out2

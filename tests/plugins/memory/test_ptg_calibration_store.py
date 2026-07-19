"""Regression tests for ADR-V6-028 store methods.

Locks the three additions that the §11.4/§11.5 calibration channel needs:
  * ``recent_atoms`` now returns ``atom_id`` (the event-table row PK) so a verdict
    can locate the exact row to demote — previously the dicts carried no id.
  * ``insert_feedback`` upserts (revives a soft-deleted row per ADR-083 F6 rather
    than UNIQUE-violating) and never raises (C7).
  * ``adjust_atom_confidence`` is the §11.5 contract — the ONLY sanctioned
    human-mutates-confidence channel: it lowers ``relation_confidence`` on the
    specific row (demote, NOT delete — C2 nothing-lost), dispatching by atom type.
"""

from __future__ import annotations

import pytest

from plugins.memory.ptg.store import PTGStore


@pytest.fixture
def store(tmp_path):
    s = PTGStore(db_path=str(tmp_path / "ptg.db"))
    s.ensure_founder("u1", "founder@realityos.local")
    yield s
    s.close()


# ── recent_atoms carries atom_id for every type ─────────────────────────────

def test_recent_atoms_returns_atom_id_for_all_types(store):
    """Every reconstructed atom carries the backing event-table row PK."""
    mid = store.insert_memo(user_id="u1", source_text="x", input_mode="text")
    eid = store.insert_identity_event(
        user_id="u1", source_text="x", person_name="张三",
        confidence_base=0.9, relation_confidence=0.95, memo_id=mid)
    mid2 = store.insert_meaning_event(
        user_id="u1", source_text="x", intent_class="Other",
        task_description="学了一招", atom_kind="R8",
        confidence_base=0.7, relation_confidence=0.72, memo_id=mid)
    fid = store.insert_feeling_event(
        user_id="u1", source_text="x", state_type="mood", direction="up",
        intensity="medium", atom_kind="R9", emotion_vad='{"label":"开心"}',
        confidence_base=0.6, relation_confidence=0.66, memo_id=mid)

    atoms = store.recent_atoms(user_id="u1", memo_id=mid)
    by_type = {a["type"]: a for a in atoms}
    assert "atom_id" in by_type["R3_Person"]
    assert by_type["R3_Person"]["atom_id"] == eid
    assert "atom_id" in by_type["R8_Cognition"]
    assert by_type["R8_Cognition"]["atom_id"] == mid2
    assert "atom_id" in by_type["R9_Emotion"]
    assert by_type["R9_Emotion"]["atom_id"] == fid


# ── insert_feedback: insert, update-existing, revive-after-soft-delete ───────

def _fb(store, **kw):
    return store._conn.execute(
        "SELECT rating, comment, version, deleted_at FROM feedback "
        "WHERE user_id=? AND target_type=? AND target_id=?",
        ("u1", kw["target_type"], kw["target_id"]),
    ).fetchone()


def test_insert_feedback_inserts_then_updates_same_id(store):
    fb1 = store.insert_feedback(
        user_id="u1", target_type="calibration_wrong", target_id="atom-1",
        rating="thumbs_down", comment="was=0.95",
    )
    assert fb1
    row = _fb(store, target_type="calibration_wrong", target_id="atom-1")
    assert row["rating"] == "thumbs_down" and row["version"] == 1
    assert row["deleted_at"] is None

    # Re-submit SAME target → upserts onto the existing row (same id, version++).
    fb2 = store.insert_feedback(
        user_id="u1", target_type="calibration_wrong", target_id="atom-1",
        rating="thumbs_up", comment="re-judged",
    )
    assert fb2 == fb1
    row = _fb(store, target_type="calibration_wrong", target_id="atom-1")
    assert row["rating"] == "thumbs_up" and row["version"] == 2


def test_insert_feedback_revives_soft_deleted_row(store):
    fb1 = store.insert_feedback(
        user_id="u1", target_type="calibration_surprise", target_id="atom-9",
        rating="thumbs_up", comment="惊喜",
    )
    store.soft_delete("feedback", fb1)
    row = _fb(store, target_type="calibration_surprise", target_id="atom-9")
    assert row["deleted_at"] is not None and row["version"] == 1

    # Re-submit after soft-delete → ADR-083 F6 revive (un-delete, version++),
    # NOT a new insert (would UNIQUE-violate on the plain unique index).
    fb2 = store.insert_feedback(
        user_id="u1", target_type="calibration_surprise", target_id="atom-9",
        rating="thumbs_up", comment="再惊喜",
    )
    assert fb2 == fb1
    row = _fb(store, target_type="calibration_surprise", target_id="atom-9")
    assert row["deleted_at"] is None and row["version"] == 2


# ── adjust_atom_confidence: the §11.5 demotion channel ──────────────────────

def test_adjust_atom_confidence_demotes_correct_row(store):
    mid = store.insert_memo(user_id="u1", source_text="x", input_mode="text")
    eid = store.insert_identity_event(
        user_id="u1", source_text="x", person_name="张三",
        confidence_base=0.9, relation_confidence=0.95, memo_id=mid)
    atoms = store.recent_atoms(user_id="u1", memo_id=mid)
    r3 = next(a for a in atoms if a["type"] == "R3_Person")
    assert r3["confidence"] == 0.95

    rc = store.adjust_atom_confidence(
        user_id="u1", atom_type="R3_Person", atom_id=r3["atom_id"],
        new_confidence=0.3, reason="founder_calibration_wrong",
    )
    assert rc == 1
    # The demotion is visible to reads (recent_atoms prefers relation_confidence).
    r3_after = next(
        a for a in store.recent_atoms(user_id="u1", memo_id=mid)
        if a["type"] == "R3_Person")
    assert r3_after["confidence"] == 0.3
    # C2: the row is demoted, NOT deleted.
    cnt = store._conn.execute(
        "SELECT COUNT(*) FROM identity_events WHERE id=? AND deleted_at IS NULL",
        (eid,)).fetchone()[0]
    assert cnt == 1


def test_adjust_atom_confidence_dispatches_all_tables(store):
    """R3→identity, R2/R7/R8/R12→meaning, R0→entity, R1/R9→feeling."""
    mid = store.insert_memo(user_id="u1", source_text="x", input_mode="text")
    eid = store.insert_meaning_event(
        user_id="u1", source_text="x", intent_class="Need_To_Do",
        task_description="交报告", atom_kind="R2",
        confidence_base=0.8, relation_confidence=0.85, memo_id=mid)
    r2 = next(a for a in store.recent_atoms(user_id="u1", memo_id=mid)
              if a["type"] == "R2_Task")
    assert r2["atom_id"] == eid
    rc = store.adjust_atom_confidence(
        user_id="u1", atom_type="R2_Task", atom_id=eid, new_confidence=0.2)
    assert rc == 1
    after = store._conn.execute(
        "SELECT relation_confidence FROM meaning_events WHERE id=?", (eid,)
    ).fetchone()[0]
    assert after == 0.2


def test_adjust_atom_confidence_unknown_type_and_missing_row(store):
    # Unknown atom type → 0, never raises.
    assert store.adjust_atom_confidence(
        user_id="u1", atom_type="R99_Whatever", atom_id="nope",
        new_confidence=0.1) == 0
    # Known type but nonexistent id → 0, never raises.
    assert store.adjust_atom_confidence(
        user_id="u1", atom_type="R3_Person", atom_id="does-not-exist",
        new_confidence=0.1) == 0


def test_adjust_atom_confidence_clamps_to_check_range(store):
    """relation_confidence is CHECK BETWEEN 0 AND 1 — clamp, never violate."""
    mid = store.insert_memo(user_id="u1", source_text="x", input_mode="text")
    eid = store.insert_identity_event(
        user_id="u1", source_text="x", person_name="张三",
        confidence_base=0.9, relation_confidence=0.9, memo_id=mid)
    store.adjust_atom_confidence(
        user_id="u1", atom_type="R3_Person", atom_id=eid, new_confidence=5.0)
    hi = store._conn.execute(
        "SELECT relation_confidence FROM identity_events WHERE id=?", (eid,)
    ).fetchone()[0]
    assert hi == 1.0
    store.adjust_atom_confidence(
        user_id="u1", atom_type="R3_Person", atom_id=eid, new_confidence=-3.0)
    lo = store._conn.execute(
        "SELECT relation_confidence FROM identity_events WHERE id=?", (eid,)
    ).fetchone()[0]
    assert lo == 0.0

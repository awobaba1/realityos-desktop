"""Smoke tests for the ``hermes calibrate`` I/O adapter (ADR-V6-028).

Wires the CLI handler against a temp PTG store + a StringIO stdin so the full
path runs without a tty: founder resolution → today-window sampling → interactive
rater → run_calibration → summary. Also locks the no-founder and empty-day
short-circuits. Pure-logic coverage lives in test_realityos_calibration.py.
"""

from __future__ import annotations

import io
from types import SimpleNamespace

import pytest

from hermes_cli import calibrate_cmd
from plugins.memory.ptg.store import PTGStore


@pytest.fixture
def temp_store(tmp_path, monkeypatch):
    """Point calibrate_cmd at a temp ptg.db and yield the store for seeding."""
    db_path = str(tmp_path / "ptg.db")
    monkeypatch.setattr(calibrate_cmd, "load_ptg_config", lambda: {})
    monkeypatch.setattr(calibrate_cmd, "resolve_db_path", lambda _cfg: db_path)
    s = PTGStore(db_path=db_path)
    s.ensure_founder("u1", "founder@realityos.local")
    # Mimic PTGProvider init writing the founder id into ptg_meta (provider.py:213)
    # — resolve_founder reads this when no explicit config founder_user_id is set.
    s._conn.execute(
        "INSERT OR REPLACE INTO ptg_meta(key, value) VALUES ('founder_user_id', ?)",
        ("u1",))
    yield s
    s.close()


def _seed_today(temp_store):
    mid = temp_store.insert_memo(user_id="u1", source_text="x", input_mode="text")
    temp_store.insert_identity_event(
        user_id="u1", source_text="x", person_name="错的人",
        confidence_base=0.95, relation_confidence=0.95, memo_id=mid)
    temp_store.insert_meaning_event(
        user_id="u1", source_text="x", intent_class="Need_To_Do",
        task_description="对的任务", atom_kind="R2",
        confidence_base=0.85, relation_confidence=0.85, memo_id=mid)
    temp_store.insert_entity_event(
        user_id="u1", source_text="x", entity_name="惊喜地",
        entity_category="place", confidence_base=0.7, relation_confidence=0.7, memo_id=mid)


def test_cmd_calibrate_end_to_end(temp_store, monkeypatch, capsys):
    _seed_today(temp_store)
    # Feed 不准 / 准 / 惊喜 for the 3 atoms (timestamp DESC: R0, R2, R3 order is
    # nondeterministic on ties — but the summary counts are order-independent).
    monkeypatch.setattr("sys.stdin", io.StringIO("0\n1\ns\n"))

    rc = calibrate_cmd.cmd_calibrate(SimpleNamespace(date=None, limit=50))
    assert rc == 0
    out = capsys.readouterr().out
    assert "创始人每日校准" in out
    # 3 verdicts → 3 feedback rows + 1 demotion (the 不准 atom).
    assert "不准 1" in out and "准 1" in out and "惊喜 1" in out
    assert "纠错率" in out
    wrong = temp_store._conn.execute(
        "SELECT COUNT(*) FROM feedback WHERE target_type='calibration_wrong'"
    ).fetchone()[0]
    assert wrong == 1
    qm = temp_store._conn.execute(
        "SELECT COUNT(*) FROM quality_metrics WHERE metric_type='correction_rate'"
    ).fetchone()[0]
    assert qm == 1


def test_cmd_calibrate_no_founder(tmp_path, monkeypatch, capsys):
    db_path = str(tmp_path / "empty.db")
    monkeypatch.setattr(calibrate_cmd, "load_ptg_config", lambda: {})
    monkeypatch.setattr(calibrate_cmd, "resolve_db_path", lambda _cfg: db_path)
    # No ensure_founder → resolve_founder returns "".
    rc = calibrate_cmd.cmd_calibrate(SimpleNamespace(date=None, limit=50))
    assert rc == 0
    out = capsys.readouterr().out
    assert "未找到创始人" in out


def test_cmd_calibrate_empty_day(temp_store, monkeypatch, capsys):
    # Founder exists, but no atoms captured today.
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    rc = calibrate_cmd.cmd_calibrate(SimpleNamespace(date=None, limit=50))
    assert rc == 0
    out = capsys.readouterr().out
    assert "还没有抽取到的原子可校准" in out


def test_today_window_is_half_open_utc():
    """The today window covers the current Beijing day and converts to UTC."""
    from datetime import datetime, timedelta, timezone
    from plugins.realityos_insights._base import _BEIJING_TZ
    # 2026-07-20 09:30 Beijing.
    now = datetime(2026, 7, 20, 9, 30, tzinfo=_BEIJING_TZ)
    day_str, since, until = calibrate_cmd._today_window(now)
    assert day_str == "2026-07-20"
    # 2026-07-20 00:00 Beijing == 2026-07-19 16:00 UTC.
    assert since.startswith("2026-07-19T16:00:00")
    assert until.startswith("2026-07-20T16:00:00")
    # Date override localizes the parsed naive date to Beijing.
    d2, s2, u2 = calibrate_cmd._today_window(now, date_str="2026-07-15")
    assert d2 == "2026-07-15"
    assert s2.startswith("2026-07-14T16:00:00")

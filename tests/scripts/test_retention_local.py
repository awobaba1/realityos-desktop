"""Unit tests for scripts/retention_local.py (ADR-V6-038 D4)."""

from __future__ import annotations

import datetime
import sqlite3

import pytest

from scripts.retention_local import (
    compute_retention,
    format_report,
    main,
    query_started_ats,
)

D0 = datetime.date(2026, 7, 1)


def _ts(d: datetime.date) -> float:
    """Local-noon timestamp so fromtimestamp().date() round-trips to d in any TZ."""
    return datetime.datetime(d.year, d.month, d.day, 12, 0, 0).timestamp()


def _days(n: int) -> datetime.timedelta:
    return datetime.timedelta(days=n)


# ---------- compute_retention ----------


def test_empty_has_no_data():
    r = compute_retention([], D0)
    assert r["has_data"] is False


def test_d1_revisited_d7_missed():
    ats = [_ts(D0), _ts(D0 + _days(1))]
    r = compute_retention(ats, D0 + _days(7))
    assert r["has_data"] is True
    assert r["install_date"] == D0
    assert r["last_date"] == D0 + _days(1)
    assert r["d1"] == "revisited"
    assert r["d7"] == "missed"  # today=D7 but no session on D7


def test_d7_revisited():
    ats = [_ts(D0), _ts(D0 + _days(7))]
    r = compute_retention(ats, D0 + _days(10))
    assert r["d7"] == "revisited"


def test_not_yet_when_today_before_target():
    ats = [_ts(D0)]
    r = compute_retention(ats, D0)  # today == install day, before D1
    assert r["d1"] == "not_yet"
    assert r["d7"] == "not_yet"


def test_active_days_distinct():
    # Two sessions on D0, one on D1 → active_days = 2, total = 3
    ats = [_ts(D0), _ts(D0), _ts(D0 + _days(1))]
    r = compute_retention(ats, D0 + _days(2))
    assert r["total_sessions"] == 3
    assert r["active_days"] == 2


# ---------- query_started_ats ----------


def test_query_reads_sessions(tmp_path):
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE sessions (id TEXT, started_at REAL)")
    con.executemany("INSERT INTO sessions VALUES (?, ?)", [("a", 1.0), ("b", 2.0)])
    con.commit()
    con.close()
    assert query_started_ats(db) == [1.0, 2.0]


def test_query_missing_table_returns_empty(tmp_path):
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE other (a)")
    con.close()
    assert query_started_ats(db) == []


def test_query_missing_db_returns_empty(tmp_path):
    assert query_started_ats(tmp_path / "nonexistent.db") == []


# ---------- format_report / main ----------


def test_format_report_empty():
    assert "无 session" in format_report({"has_data": False})


def test_main_today_override(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE sessions (id TEXT, started_at REAL)")
    con.execute("INSERT INTO sessions VALUES ('a', ?)", (_ts(D0),))
    con.commit()
    con.close()
    rc = main(["--today", "2026-07-08"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "2026-07-01" in out  # install date
    assert "D1" in out


def test_main_no_data_is_clean_exit(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    rc = main(["--today", "2026-07-08"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "无 session" in out

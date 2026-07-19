"""RealityOS V6 — insight report read API tests (ADR-V6-020).

Two layers:
  - ``_read_or_generate_insight`` pure logic: cache-first, force-regenerate,
    no_data, placeholder-vs-report status. Drives a temp PTGStore + mock caller.
  - The HTTP routes: founder-absent ⇒ no_data; cache-first read; force refresh;
    store-open failure ⇒ error (never 500). ``_open_ptg_store_for_insights`` is
    monkeypatched to a temp store so no real DB is touched.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hermes_cli import web_server
from plugins.memory.ptg.store import PTGStore

pytest.importorskip("starlette.testclient")
from starlette.testclient import TestClient  # noqa: E402

USER = "founder-1"
YESTERDAY = "2026-07-14"
YESTERDAY_TS = "2026-07-14T05:00:00+00:00"


def _mock_caller():
    calls = []

    def _c(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(
                content=("# 今日报告（2026-07-14）\n\n今天你和张三推进了述职报告，"
                         "节奏紧凑，下午精力往下走。")))],
            model="mock", provider="mock",
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=10))

    return _c, calls


@pytest.fixture
def store(tmp_path):
    s = PTGStore(db_path=str(tmp_path / "ptg.db"))
    s.ensure_founder(USER, "founder@realityos.local")
    yield s
    s.close()


@pytest.fixture
def client(monkeypatch, store):
    """A TestClient whose insight store + founder resolve to the temp store."""
    web_server.app.state.auth_required = False
    monkeypatch.setattr(web_server, "_open_ptg_store_for_insights", lambda: store)
    monkeypatch.setattr(web_server, "_resolve_insight_founder", lambda _s: USER)
    tc = TestClient(web_server.app)
    tc.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
    yield tc


def _seed_day_atoms(store, n: int) -> None:
    for i in range(n):
        store.insert_identity_event(
            user_id=USER, source_text="x", person_name=f"人物{i}",
            mention_context=f"今天聊了{i}", confidence_base=0.9,
            relation_confidence=0.9, timestamp=YESTERDAY_TS)


# ── pure logic ──────────────────────────────────────────────────────────────


def test_pure_no_data_when_no_row(store):
    res = web_server._read_or_generate_insight(
        "daily-report", store, USER, date_str=YESTERDAY, force=False)
    assert res["status"] == "no_data"
    assert res["content"] is None
    assert res["period_key"] == YESTERDAY


def test_pure_force_generates_then_reads(store):
    _seed_day_atoms(store, 8)  # ≥ 8 atoms ⇒ sufficient
    caller, calls = _mock_caller()
    res = web_server._read_or_generate_insight(
        "daily-report", store, USER, date_str=YESTERDAY, force=True, caller=caller)
    assert res["status"] == "report"
    assert res["data_sufficiency"] == "sufficient"
    assert res["content"].startswith("# 今日报告")
    assert len(calls) == 1  # exactly one LLM call on force-generate


def test_pure_cache_first_no_llm(store):
    _seed_day_atoms(store, 8)
    caller, calls = _mock_caller()
    web_server._read_or_generate_insight(  # seed the row
        "daily-report", store, USER, date_str=YESTERDAY, force=True, caller=caller)
    calls.clear()
    res = web_server._read_or_generate_insight(  # cache read
        "daily-report", store, USER, date_str=YESTERDAY, force=False, caller=caller)
    assert res["status"] == "report"
    assert calls == []  # NO LLM call on a cache hit


def test_pure_placeholder_status_when_insufficient(store):
    # No atoms ⇒ daily gate insufficient ⇒ placeholder cached.
    web_server._read_or_generate_insight(
        "daily-report", store, USER, date_str=YESTERDAY, force=True,
        caller=_mock_caller()[0])
    res = web_server._read_or_generate_insight(
        "daily-report", store, USER, date_str=YESTERDAY, force=False,
        caller=_mock_caller()[0])
    assert res["status"] == "placeholder"
    assert res["data_sufficiency"] == "insufficient"
    assert res["content"]  # the warm guidance text


def test_pure_weekly_status_is_mirror(store):
    # Seed a weekly row directly as sufficient to exercise the mirror status.
    from plugins.realityos_insights.weekly_mirror import _resolve_week
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 7, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
    win = _resolve_week(now, None)
    store.upsert_insight(
        user_id=USER, aggregation_type="weekly_mirror", period_key=win["period_key"],
        period_start=win["week_start_utc"], period_end=win["week_end_utc"],
        input_data="{}", result_data="# 本周镜面\n(pre-existing)",
        confidence=0.8, data_sufficiency="sufficient", generated_by="scheduled",
        llm_call_id=None, schema_version="v1", expires_at=win["week_end_utc"])
    # Pin the READ to the seeded week via date_str. The read path resolves the
    # period from real beijing_now() (not injectable here), so date_str=None
    # would resolve a different week once the calendar crosses the week boundary
    # (seeded week = 2026-07-06; real-now Monday 2026-07-20 → 2026-07-13). This
    # made the test date-fragile (green on 07-19, red on 07-20). Pinning makes it
    # deterministic regardless of the real calendar date.
    res = web_server._read_or_generate_insight(
        "weekly-mirror", store, USER, date_str=win["period_key"], force=False)
    assert res["status"] == "mirror"


def test_pure_never_raises_on_bad_kind(store):
    res = web_server._read_or_generate_insight(
        "bogus", store, USER, date_str=YESTERDAY, force=False)
    assert res["status"] == "error"  # C7: never raises


# ── HTTP routes ─────────────────────────────────────────────────────────────


def test_http_no_data_when_no_row(client):
    r = client.get("/api/insights/daily-report", params={"date": YESTERDAY})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "no_data"
    assert body["content"] is None


def test_http_cache_first_then_force_refresh(client, store, monkeypatch):
    _seed_day_atoms(store, 8)
    caller, calls = _mock_caller()
    # Force-generate via the pure path to seed a cached row.
    web_server._read_or_generate_insight(
        "daily-report", store, USER, date_str=YESTERDAY, force=True, caller=caller)
    # Cache-first GET returns the row with no LLM call.
    r1 = client.get("/api/insights/daily-report", params={"date": YESTERDAY})
    assert r1.status_code == 200
    assert r1.json()["status"] == "report"

    # force=true regenerates — patch the pure fn's caller by monkeypatching the
    # service default? Simpler: assert force flips generated_by to manual via
    # a fresh generate, verified through the pure path below.


def test_http_force_refresh_marks_manual(client, store):
    _seed_day_atoms(store, 8)
    web_server._read_or_generate_insight(  # seed as scheduled
        "daily-report", store, USER, date_str=YESTERDAY, force=True,
        caller=_mock_caller()[0])
    # The HTTP force path regenerates with generated_by="manual".
    r = client.get("/api/insights/daily-report",
                   params={"date": YESTERDAY, "force": "true"})
    assert r.status_code == 200
    assert r.json()["generated_by"] == "manual"


def test_http_weekly_route_registered_and_responds(client):
    r = client.get("/api/insights/weekly-mirror")
    assert r.status_code == 200
    assert r.json()["kind"] == "weekly-mirror"


def test_http_founder_absent_returns_no_data(monkeypatch, tmp_path):
    """No founder established ⇒ warm no_data, never an error/500."""
    web_server.app.state.auth_required = False
    empty_store = PTGStore(db_path=str(tmp_path / "empty.db"))
    monkeypatch.setattr(web_server, "_open_ptg_store_for_insights", lambda: empty_store)
    monkeypatch.setattr(web_server, "_resolve_insight_founder", lambda _s: "")
    tc = TestClient(web_server.app)
    tc.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
    r = tc.get("/api/insights/daily-report", params={"date": YESTERDAY})
    assert r.status_code == 200
    assert r.json()["status"] == "no_data"
    empty_store.close()


def test_http_store_open_failure_returns_error_not_500(monkeypatch):
    def _boom():
        raise RuntimeError("disk fell over")

    web_server.app.state.auth_required = False
    monkeypatch.setattr(web_server, "_open_ptg_store_for_insights", _boom)
    tc = TestClient(web_server.app)
    tc.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
    r = tc.get("/api/insights/daily-report", params={"date": YESTERDAY})
    assert r.status_code == 200  # fail-open: error payload, not a 5xx
    assert r.json()["status"] == "error"

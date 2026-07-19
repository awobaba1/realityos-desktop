"""RealityOS V6 — PTG memory browser read API tests (ADR-V6-021).

Two layers, mirroring ``test_insights_web_api``:
  - ``_read_memory_browse`` pure logic: empty store, atoms typed by kind,
    entity directory, relation edges, never-raises.
  - The HTTP route ``GET /api/memory/browse``: founder-absent ⇒ no_data,
    seeded ⇒ ok payload, limit clamp, store-open failure ⇒ error (never 500).
``_open_ptg_store_for_insights`` is monkeypatched to a temp store so no real
DB is touched (the helper is shared by insights + browse — same shared handle).
"""

from __future__ import annotations

import pytest

from hermes_cli import web_server
from plugins.memory.ptg.store import PTGStore

pytest.importorskip("starlette.testclient")
from starlette.testclient import TestClient  # noqa: E402

USER = "founder-1"


@pytest.fixture
def store(tmp_path):
    s = PTGStore(db_path=str(tmp_path / "ptg.db"))
    s.ensure_founder(USER, "founder@realityos.local")
    yield s
    s.close()


@pytest.fixture
def client(monkeypatch, store):
    """A TestClient whose browse store + founder resolve to the temp store."""
    web_server.app.state.auth_required = False
    monkeypatch.setattr(web_server, "_open_ptg_store_for_insights", lambda: store)
    monkeypatch.setattr(web_server, "_resolve_insight_founder", lambda _s: USER)
    tc = TestClient(web_server.app)
    tc.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
    yield tc


def _seed_one_of_each_atom(store) -> None:
    """One row in each of the four event tables → four typed atoms."""
    store.insert_identity_event(
        user_id=USER, source_text="今天和张三吃了饭", person_name="张三",
        mention_context="晚餐聊了项目", sentiment="positive", interaction_type="meeting",
        confidence_base=0.9, relation_confidence=0.9, timestamp="2026-07-14T05:00:00+00:00")
    store.insert_meaning_event(
        user_id=USER, source_text="明天要交述职报告", intent_class="Need_To_Do",
        task_description="写述职报告", urgency="high", deadline="2026-07-15",
        confidence_base=0.9, relation_confidence=0.9, atom_kind="R2",
        timestamp="2026-07-14T06:00:00+00:00")
    store.insert_feeling_event(
        user_id=USER, source_text="今天有点累", confidence_base=0.9,
        relation_confidence=0.9, state_type="energy", direction="down",
        intensity="medium", timestamp="2026-07-14T07:00:00+00:00")
    store.insert_entity_event(
        user_id=USER, source_text="去了国金证券", entity_name="国金证券",
        entity_category="organization", mention_context="客户拜访",
        confidence_base=0.85, relation_confidence=0.85,
        timestamp="2026-07-14T08:00:00+00:00")


# ── pure logic ───────────────────────────────────────────────────────────────


def test_pure_empty_store_returns_ok_with_empty_lists(store):
    res = web_server._read_memory_browse(store, USER)
    assert res["status"] == "ok"
    assert res["atoms"] == []
    assert res["entities"] == []
    assert res["relations"] == []
    assert res["memo_count"] == 0


def test_pure_returns_atoms_with_expected_types(store):
    _seed_one_of_each_atom(store)
    res = web_server._read_memory_browse(store, USER)
    assert res["status"] == "ok"
    types = {a["type"] for a in res["atoms"]}
    assert types == {"R3_Person", "R2_Task", "R1_SelfState", "R0_Entity"}
    # Each atom carries type/confidence/timestamp + a fields dict.
    person = next(a for a in res["atoms"] if a["type"] == "R3_Person")
    assert person["fields"]["person_name"] == "张三"
    assert person["confidence"] == 0.9
    assert person["timestamp"]


def test_pure_atoms_ordered_most_recent_first(store):
    _seed_one_of_each_atom(store)
    res = web_server._read_memory_browse(store, USER)
    ts = [a["timestamp"] for a in res["atoms"]]
    assert ts == sorted(ts, reverse=True)


def test_pure_returns_entity_directory(store):
    eid = store.upsert_entity(
        user_id=USER, entity_name="张三", entity_type="person",
        properties={"aliases": ["老张"]})
    store.upsert_entity(user_id=USER, entity_name="老张", entity_type="person")  # bump
    res = web_server._read_memory_browse(store, USER)
    names = {e["entity_name"] for e in res["entities"]}
    assert "张三" in names  # normalized re-mention merges into one node
    assert eid  # upsert returned a stable id


def test_pure_returns_relations_with_joined_names(store):
    subj = store.upsert_entity(user_id=USER, entity_name="张三", entity_type="person")
    obj = store.upsert_entity(user_id=USER, entity_name="国金证券", entity_type="context")
    store.upsert_relation(user_id=USER, subject_id=subj, object_id=obj,
                          relation_type="works_at", confidence=0.8)
    res = web_server._read_memory_browse(store, USER)
    assert any(r["subject_name"] == "张三" and r["object_name"] == "国金证券"
               for r in res["relations"])


def test_pure_never_raises_on_store_error():
    class _Boom:
        def __getattr__(self, _):
            raise RuntimeError("disk fell over")
    res = web_server._read_memory_browse(_Boom(), USER)
    assert res["status"] == "error"


def test_pure_limit_caps_atoms(store):
    for i in range(5):
        store.insert_identity_event(
            user_id=USER, source_text=f"x{i}", person_name=f"人物{i}",
            confidence_base=0.9, relation_confidence=0.9,
            timestamp=f"2026-07-0{i+1}T05:00:00+00:00")
    res = web_server._read_memory_browse(store, USER, limit=3)
    assert len(res["atoms"]) == 3


# ── HTTP route ───────────────────────────────────────────────────────────────


def test_http_no_data_when_no_founder(monkeypatch, tmp_path):
    """No founder established ⇒ warm no_data, never an error/500."""
    web_server.app.state.auth_required = False
    empty_store = PTGStore(db_path=str(tmp_path / "empty.db"))
    monkeypatch.setattr(web_server, "_open_ptg_store_for_insights", lambda: empty_store)
    monkeypatch.setattr(web_server, "_resolve_insight_founder", lambda _s: "")
    tc = TestClient(web_server.app)
    tc.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
    r = tc.get("/api/memory/browse")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "no_data"
    assert body["atoms"] == []
    empty_store.close()


def test_http_browse_returns_seeded_payload(client, store):
    _seed_one_of_each_atom(store)
    r = client.get("/api/memory/browse")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert len(body["atoms"]) == 4
    assert body["memo_count"] == 0  # memos table untouched by event seeding


def test_http_limit_clamped_without_error(client, store):
    _seed_one_of_each_atom(store)
    r = client.get("/api/memory/browse", params={"limit": "9999"})
    assert r.status_code == 200
    assert r.json()["status"] == "ok"  # clamped to 500 internally, no 4xx


def test_http_bad_limit_falls_back_to_default(client, store):
    _seed_one_of_each_atom(store)
    r = client.get("/api/memory/browse", params={"limit": "not-a-number"})
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_http_store_open_failure_returns_error_not_500(monkeypatch):
    def _boom():
        raise RuntimeError("disk fell over")

    web_server.app.state.auth_required = False
    monkeypatch.setattr(web_server, "_open_ptg_store_for_insights", _boom)
    tc = TestClient(web_server.app)
    tc.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
    r = tc.get("/api/memory/browse")
    assert r.status_code == 200  # fail-open: error payload, not a 5xx
    assert r.json()["status"] == "error"

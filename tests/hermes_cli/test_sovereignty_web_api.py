"""RealityOS V6 — sovereignty web API tests (ADR-V6-023).

Wires the §6 sovereignty primitives (one-click export / §6.2 cascade delete /
§6.7 minor mode) to HTTP. Mirrors ``test_memory_browse_web_api``: a temp store
seeded with memos + atoms, ``_open_ptg_store_for_insights`` +
``_resolve_insight_founder`` monkeypatched to it. Every path is fail-open (C7):
no founder ⇒ no_data, store-open failure ⇒ error, bad delete mode ⇒ structured
error code ``bad_mode`` — HTTP 200 throughout, never 5xx.

This is the wiring that de-orphans the sovereignty plugin (ADR-V6-022 B1): the
primitives were real + tested but had zero non-test callers before this ADR.
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
    """A TestClient whose sovereignty store + founder resolve to the temp store.

    The store-opener returns a FRESH PTGStore handle to the SAME db path each
    call (registry-shared, refcounted) — mirroring production, where
    ``_open_ptg_store_for_insights`` calls ``PTGStore(db_path=...)`` and the
    handler's ``close()`` only decrements the ref (never tears down the shared
    connection the fixture holds). Returning the fixture's own ``store`` object
    directly would let the handler's ``close()`` drop refs to 0 and close the
    connection mid-test, silently losing writes across requests.
    """
    db_path = store.db_path
    web_server.app.state.auth_required = False
    monkeypatch.setattr(
        web_server, "_open_ptg_store_for_insights",
        lambda: PTGStore(db_path=str(db_path)))
    monkeypatch.setattr(web_server, "_resolve_insight_founder", lambda _s: USER)
    tc = TestClient(web_server.app)
    tc.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
    yield tc


def _seed(store) -> None:
    """One memo + R3 person atom + R0 entity atom + an entity node + a relation."""
    store.insert_memo(user_id=USER, source_text="今天和张三吃了饭")
    store.insert_identity_event(
        user_id=USER, source_text="今天和张三吃了饭", person_name="张三",
        mention_context="晚餐聊了项目", sentiment="positive",
        interaction_type="meeting", confidence_base=0.9, relation_confidence=0.9,
        timestamp="2026-07-14T05:00:00+00:00")
    store.insert_entity_event(
        user_id=USER, source_text="去了国金证券", entity_name="国金证券",
        entity_category="organization", mention_context="客户拜访",
        confidence_base=0.85, relation_confidence=0.85,
        timestamp="2026-07-14T08:00:00+00:00")
    subj = store.upsert_entity(user_id=USER, entity_name="张三", entity_type="person")
    obj = store.upsert_entity(user_id=USER, entity_name="国金证券", entity_type="context")
    store.upsert_relation(user_id=USER, subject_id=subj, object_id=obj,
                          relation_type="works_at", confidence=0.8)


# ── export ───────────────────────────────────────────────────────────────────


def test_export_returns_ok_with_data(client, store):
    _seed(store)
    res = client.get("/api/sovereignty/export")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    data = body["data"]
    assert data["_export_meta"]["user_id"] == USER
    assert data["_export_meta"]["schema_version"]
    assert len(data["memos"]) == 1
    assert len(data["identity_events"]) == 1


def test_export_excludes_soft_deleted_rows(client, store):
    _seed(store)
    # Soft-delete the memo (Mode A) then export — retired rows are excluded.
    client.post("/api/sovereignty/delete", json={"mode": "A"})
    res = client.get("/api/sovereignty/export")
    assert res.json()["data"]["memos"] == []  # soft-deleted → not exported


def test_export_no_founder_returns_no_data(client, monkeypatch):
    monkeypatch.setattr(web_server, "_resolve_insight_founder", lambda _s: "")
    res = client.get("/api/sovereignty/export")
    assert res.status_code == 200
    assert res.json()["status"] == "no_data"


def test_export_store_open_failure_returns_error(client, monkeypatch):
    def _boom():
        raise RuntimeError("store unavailable")
    monkeypatch.setattr(web_server, "_open_ptg_store_for_insights", _boom)
    res = client.get("/api/sovereignty/export")
    assert res.status_code == 200  # fail-open, never 5xx
    assert res.json()["status"] == "error"


# ── cascade delete ───────────────────────────────────────────────────────────


def test_delete_mode_a_only_marks_memos(client, store):
    _seed(store)
    assert store.memo_count(USER) == 1
    res = client.post("/api/sovereignty/delete", json={"mode": "A"})
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["mode"] == "A"
    assert body["marked"] == {"memos": 1}
    # Mode A NEVER touches PTG atoms (§6.2): the R3 person atom survives.
    assert any(a["type"] == "R3_Person" for a in store.recent_atoms(user_id=USER))
    assert store.memo_count(USER) == 0  # memo soft-deleted → excluded


def test_delete_mode_b_cascades_atoms(client, store):
    _seed(store)
    res = client.post("/api/sovereignty/delete", json={"mode": "B"})
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["mode"] == "B"
    marked = body["marked"]
    # Mode B cascade: memos + the four event tables + entities + relations.
    assert "memos" in marked
    assert "identity_events" in marked
    assert "entity_events" in marked
    # Atoms gone after the cascade (recent_atoms filters deleted_at IS NULL).
    assert store.recent_atoms(user_id=USER) == []
    assert store.memo_count(USER) == 0


def test_delete_bad_mode_returns_structured_error(client):
    res = client.post("/api/sovereignty/delete", json={"mode": "X"})
    assert res.status_code == 200  # fail-open, never 5xx
    body = res.json()
    assert body["status"] == "error"
    assert body["code"] == "bad_mode"


def test_delete_missing_mode_returns_structured_error(client):
    # Empty body (no mode) → structured bad_mode, not 500.
    res = client.post("/api/sovereignty/delete", json={})
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "error"
    assert body["code"] == "bad_mode"


def test_delete_no_founder_returns_no_data(client, monkeypatch):
    monkeypatch.setattr(web_server, "_resolve_insight_founder", lambda _s: "")
    res = client.post("/api/sovereignty/delete", json={"mode": "A"})
    assert res.status_code == 200
    assert res.json()["status"] == "no_data"


# ── minor mode ───────────────────────────────────────────────────────────────


def test_minor_default_false(client):
    res = client.get("/api/sovereignty/minor")
    assert res.status_code == 200
    assert res.json() == {"status": "ok", "enabled": False}


def test_minor_toggle_roundtrip(client, store):
    assert client.post(
        "/api/sovereignty/minor", json={"enabled": True}).json() == {
        "status": "ok", "enabled": True}
    assert client.get("/api/sovereignty/minor").json() == {
        "status": "ok", "enabled": True}
    assert client.post(
        "/api/sovereignty/minor", json={"enabled": False}).json() == {
        "status": "ok", "enabled": False}
    assert client.get("/api/sovereignty/minor").json() == {
        "status": "ok", "enabled": False}


def test_minor_no_founder_returns_ok_false(client, monkeypatch):
    # No founder yet → adult default (False), not an error.
    monkeypatch.setattr(web_server, "_resolve_insight_founder", lambda _s: "")
    res = client.get("/api/sovereignty/minor")
    assert res.status_code == 200
    assert res.json() == {"status": "ok", "enabled": False}

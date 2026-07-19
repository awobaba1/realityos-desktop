"""RealityOS V6 PTGProvider — MemoryProvider subclass regression tests.

Locks P0-4c: the recall + turn-capture half of the PTG. Verifies the provider
honours the MemoryProvider ABC contract (name/initialize/prefetch/sync_turn/
tool surface) and that Phase-0 capture is correct (only real-user primary
turns captured, never raises, graceful when the store is unavailable — C7).
"""

from __future__ import annotations

import json

import pytest

from plugins.memory.ptg.provider import PTGProvider


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def provider(tmp_path):
    """A PTGProvider on an isolated temp DB, initialized as primary."""
    p = PTGProvider(config={"db_path": str(tmp_path / "ptg.db")})
    p.initialize("sess-1", hermes_home=str(tmp_path), platform="cli",
                 agent_context="primary")
    yield p
    p.shutdown()


class _FailingStore:
    """A stand-in PTGStore whose __init__ raises — for the disable-gracefully test."""

    def __init__(self, *a, **kw):
        raise RuntimeError("simulated store init failure")


# ---------------------------------------------------------------------------
# ABC contract
# ---------------------------------------------------------------------------

def test_name_and_availability(provider):
    assert provider.name == "ptg"          # @property, not a method
    assert provider.is_available() is True  # sqlite always available


def test_initialize_opens_store_and_records_schema(provider):
    # Founder bootstrapped + schema version present.
    assert provider._store is not None
    assert provider._store.count_rows("realityos_users") == 1
    row = provider._store._conn.execute(
        "SELECT value FROM ptg_meta WHERE key='schema_version'").fetchone()
    assert row is not None


def test_system_prompt_block_reports_status(provider):
    block = provider.system_prompt_block()
    assert "RealityOS Personal Timeline" in block
    assert "Empty" in block  # empty store → empty-state message
    provider._store.insert_memo(user_id=provider._user_id, source_text="hello")
    block2 = provider.system_prompt_block()
    assert "1 captured memo" in block2


# ---------------------------------------------------------------------------
# sync_turn capture (流经即捕获)
# ---------------------------------------------------------------------------

def test_sync_turn_captures_user_message_as_memo(provider):
    provider.sync_turn("meeting about Q3 budget", "ok", session_id="sess-1")
    assert provider._store.count_rows("memos") == 1
    hits = provider._store.search_memos_fts("budget", user_id=provider._user_id)
    assert len(hits) == 1
    assert "budget" in hits[0]["source_text"]


def test_sync_turn_records_assistant_reply_as_summary(provider):
    provider.sync_turn("what is my budget?", "Your Q3 budget is 1M.", session_id="sess-1")
    row = provider._store._conn.execute(
        "SELECT summary FROM memos WHERE user_id=?", (provider._user_id,)).fetchone()
    assert row["summary"] == "Your Q3 budget is 1M."


def test_sync_turn_skips_empty_user_content(provider):
    provider.sync_turn("   ", "some reply", session_id="sess-1")
    assert provider._store.count_rows("memos") == 0


def test_sync_turn_skips_whitespace_only(provider):
    provider.sync_turn("\n\t ", "", session_id="sess-1")
    assert provider._store.count_rows("memos") == 0


def test_sync_turn_skips_non_primary_context(tmp_path):
    """Subagent/cron/flush turns are internal agent flows — never captured."""
    p = PTGProvider(config={"db_path": str(tmp_path / "ptg.db")})
    p.initialize("sess-sub", hermes_home=str(tmp_path), agent_context="subagent")
    try:
        p.sync_turn("delegated subagent prompt", "sub reply")
        assert p._store.count_rows("memos") == 0
    finally:
        p.shutdown()


def test_sync_turn_captures_when_context_unset(tmp_path):
    """agent_context absent defaults to primary → capture happens."""
    p = PTGProvider(config={"db_path": str(tmp_path / "ptg.db")})
    p.initialize("sess-1", hermes_home=str(tmp_path))  # no agent_context
    try:
        p.sync_turn("a real user turn", "reply")
        assert p._store.count_rows("memos") == 1
    finally:
        p.shutdown()


def test_capture_never_raises(provider, monkeypatch):
    """C7: a store failure inside capture must be swallowed, never break the loop."""
    def boom(*a, **kw):
        raise RuntimeError("db locked")
    monkeypatch.setattr(provider._store, "insert_memo", boom)
    # Must not raise.
    provider.sync_turn("turn that triggers capture failure", "reply")
    provider.prefetch("anything")  # also swallows


# ---------------------------------------------------------------------------
# prefetch recall
# ---------------------------------------------------------------------------

def test_prefetch_returns_recall_text(provider):
    provider._store.insert_memo(user_id=provider._user_id, source_text="standup notes budget")
    out = provider.prefetch("budget", session_id="sess-1")
    assert out
    assert "RealityOS recall" in out
    assert "budget" in out


def test_prefetch_empty_when_no_hits(provider):
    assert provider.prefetch("nonexistentword12345") == ""


def test_prefetch_empty_query_noop(provider):
    assert provider.prefetch("") == ""


def test_prefetch_skips_when_store_unavailable(provider):
    provider._store = None
    assert provider.prefetch("budget") == ""
    assert provider.system_prompt_block() == ""


# ---------------------------------------------------------------------------
# tool surface
# ---------------------------------------------------------------------------

def test_get_tool_schemas_exposes_ptg_search(provider):
    schemas = provider.get_tool_schemas()
    assert len(schemas) == 1
    assert schemas[0]["name"] == "ptg_search"
    assert "query" in schemas[0]["parameters"]["properties"]


def test_handle_ptg_search_returns_results(provider):
    provider._store.insert_memo(user_id=provider._user_id, source_text="budget review Q3")
    out = provider.handle_tool_call("ptg_search", {"query": "budget"})
    payload = json.loads(out)
    assert payload["count"] == 1
    assert "budget" in payload["results"][0]["source_text"]


def test_handle_ptg_search_respects_limit(provider):
    for i in range(5):
        provider._store.insert_memo(user_id=provider._user_id, source_text=f"budget item {i}")
    out = provider.handle_tool_call("ptg_search", {"query": "budget", "limit": 2})
    assert json.loads(out)["count"] == 2


def test_handle_ptg_search_empty_query_errors(provider):
    out = provider.handle_tool_call("ptg_search", {"query": ""})
    assert "error" in out or "required" in out


def test_handle_unknown_tool_errors(provider):
    out = provider.handle_tool_call("ptg_not_a_tool", {})
    assert "error" in out or "Unknown" in out


# ---------------------------------------------------------------------------
# founder user_id resolution
# ---------------------------------------------------------------------------

def test_founder_user_id_persisted_across_restart(tmp_path):
    """First init generates + persists a founder id; second init reuses it."""
    db = str(tmp_path / "ptg.db")
    a = PTGProvider(config={"db_path": db})
    a.initialize("s1", hermes_home=str(tmp_path))
    uid_a = a._user_id
    a.shutdown()
    assert uid_a  # generated

    b = PTGProvider(config={"db_path": db})
    b.initialize("s1", hermes_home=str(tmp_path))
    try:
        assert b._user_id == uid_a  # reused from ptg_meta, no new uuid
    finally:
        b.shutdown()


def test_explicit_config_founder_user_id_wins(tmp_path):
    db = str(tmp_path / "ptg.db")
    p = PTGProvider(config={"db_path": db, "founder_user_id": "fixed-uid-123"})
    p.initialize("s1", hermes_home=str(tmp_path), user_id="gateway-uid")
    try:
        assert p._user_id == "fixed-uid-123"  # config beats kwarg beats persisted
    finally:
        p.shutdown()


def test_kwarg_user_id_used_when_no_config(tmp_path):
    db = str(tmp_path / "ptg.db")
    p = PTGProvider(config={"db_path": db})
    p.initialize("s1", hermes_home=str(tmp_path), user_id="gateway-uid-9")
    try:
        assert p._user_id == "gateway-uid-9"
    finally:
        p.shutdown()


# ---------------------------------------------------------------------------
# graceful disable / shutdown
# ---------------------------------------------------------------------------

def test_initialize_store_failure_disables_provider(tmp_path, monkeypatch):
    """If PTGStore can't open, the provider disables itself — never crashes init."""
    monkeypatch.setattr("plugins.memory.ptg.provider.PTGStore", _FailingStore)
    p = PTGProvider(config={"db_path": str(tmp_path / "ptg.db")})
    p.initialize("s1", hermes_home=str(tmp_path))  # must not raise
    try:
        assert p._store is None
        assert p.system_prompt_block() == ""      # disabled → no prompt
        assert p.prefetch("x") == ""              # disabled → no recall
        p.sync_turn("x", "y")                     # disabled → no-op, no raise
    finally:
        p.shutdown()


def test_shutdown_releases_store(provider):
    assert provider._store is not None
    provider.shutdown()
    assert provider._store is None
    # Double shutdown is safe.
    provider.shutdown()


# ---------------------------------------------------------------------------
# V6 default activation (ADR-V6-010)
# ---------------------------------------------------------------------------

def test_v6_default_memory_provider_is_ptg():
    """V6 ships PTG as the DEFAULT memory provider — the data layer must be
    live on every launch, not silent.

    The provider is built (P0-4c) and proven on real founder data (P0-5b
    runtime gate), but it only activates at agent startup if ``memory.provider``
    is non-empty: agent_init.py does ``mem_config.get("provider", "")`` and
    skips loading entirely on an empty string. For 3 phases the default sat at
    "" — meaning the entire PTG data layer was invisible at runtime even though
    every unit test passed (each test constructed the provider explicitly).
    Lock the default so a silent revert can't hide the data brain again.
    """
    from hermes_cli.config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["memory"]["provider"] == "ptg"


# ---------------------------------------------------------------------------
# Shutdown drain (ADR-V6-012) — in-flight atomize threads must be drained
# before the store closes, else their writes hit a closed DB and the atom is
# lost (C2/C7 data loss on shutdown). Two contract sides: drains when it can,
# bounds when it can't.
# ---------------------------------------------------------------------------

def _atom_json(person="张三", conf=0.95):
    return json.dumps({"summary": "x", "atoms": [
        {"type": "R3_Person", "person_name": person, "confidence": conf}]})


def test_shutdown_drains_finishing_atomize_thread(provider):
    """A completing extraction thread is JOINED — its atom lands before close."""
    import time
    from types import SimpleNamespace
    from plugins.memory.ptg.atomizer import Atomizer

    done = []
    def slow_caller(**kwargs):
        time.sleep(0.4)  # simulate LLM latency across the shutdown boundary
        done.append(True)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=_atom_json()))],
            model="test", usage={"prompt_tokens": 10, "completion_tokens": 5}, provider="test")
    db_path = str(provider._store.db_path)
    provider._atomizer = Atomizer(provider._store, user_id=provider._user_id,
                                  llm_caller=slow_caller)
    provider.sync_turn(user_content="提到张三", assistant_content="ok")
    time.sleep(0.05)  # let the daemon thread start + enter the LLM call
    provider.shutdown()  # must drain (join) the in-flight thread, not kill it
    assert done  # the LLM call completed (thread was drained, not abandoned)
    # The atom landed despite shutdown — reopen the store to prove it.
    from plugins.memory.ptg.store import PTGStore
    s = PTGStore(db_path=db_path)
    atoms = s.recent_atoms(user_id=provider._user_id)
    s.close()
    assert any(a["type"] == "R3_Person" and a["person_name"] == "张三" for a in atoms)


def test_shutdown_drain_is_bounded_against_hung_llm(provider):
    """A hung LLM call cannot hang shutdown — the drain timeout bounds it."""
    import threading
    import time
    from types import SimpleNamespace
    from plugins.memory.ptg.atomizer import Atomizer

    release = threading.Event()

    def hung_caller(**kwargs):
        release.wait(timeout=30)  # never resolves during the test
        raise RuntimeError("unreachable")
    provider._atomizer = Atomizer(provider._store, user_id=provider._user_id,
                                  llm_caller=hung_caller)
    provider.sync_turn(user_content="x", assistant_content="y")
    time.sleep(0.05)
    provider._config["shutdown_drain_timeout"] = 0.3  # short bounded drain
    start = time.monotonic()
    provider.shutdown()
    elapsed = time.monotonic() - start
    assert elapsed < 2.0  # did not hang — bounded by the 0.3s drain + margin
    assert provider._store is None  # store closed despite the hung thread
    release.set()  # let the daemon thread finish + die cleanly

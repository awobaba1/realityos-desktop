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


def test_shutdown_drains_backup_thread_before_close(tmp_path, monkeypatch):
    """ADR-V6-015 (C4 regression): shutdown() MUST join the §6.9 backup thread
    BEFORE store.close(). Pre-fix, the backup thread (spawned in _run →
    run_scheduled_protection) touched _conn via _meta_get/_meta_set while the
    main thread closed it → a use-after-close segfault (reproduced 3/3 locally
    via faulthandler, intermittently in CI). This forces the backup thread to be
    mid-flight at shutdown and proves the bounded-join drain runs (no segfault).
    """
    import threading as _t
    entered, release = _t.Event(), _t.Event()

    def _blocking_protection(store, dest, **kw):
        entered.set()
        release.wait(timeout=10)  # hold the thread "mid-backup" until released

    # _run lazy-imports it: `from .backup import run_scheduled_protection`.
    monkeypatch.setattr(
        "plugins.memory.ptg.backup.run_scheduled_protection",
        _blocking_protection)

    p = PTGProvider(config={
        "db_path": str(tmp_path / "ptg.db"),
        "backup": {"enabled": True, "dest_dir": str(tmp_path / "bk")},
    })
    p.initialize("sess-1", hermes_home=str(tmp_path), platform="cli",
                 agent_context="primary")
    try:
        assert entered.wait(timeout=5), "backup thread never entered protection"
        assert p._backup_thread is not None and p._backup_thread.is_alive()
        done, err = _t.Event(), []

        def _shutdown():
            try:
                p.shutdown()
            except Exception as exc:  # noqa: BLE001
                err.append(exc)
            finally:
                done.set()

        _t.Thread(target=_shutdown, name="t-shutdown").start()
        # shutdown is now blocked on bt.join(timeout). Releasing the backup
        # thread lets the join return → shutdown proceeds to close + finishes.
        # If the drain were missing, close() would fire under the live backup
        # thread (the use-after-close segfault).
        release.set()
        assert done.wait(timeout=15), "shutdown did not return (backup not joined)"
        assert not err, f"shutdown raised: {err}"
        assert not p._backup_thread.is_alive()
    finally:
        release.set()  # never leave the backup thread hung
        try:
            p.shutdown()
        except Exception:  # noqa: BLE001
            pass


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


# ---------------------------------------------------------------------------
# ADR-V6-012 / C4 regression: the FULL shipped loop — sync_turn spawns a
# background atomize → atoms land in event tables + graph → provider.prefetch
# renders BOTH the memo-recall block AND the §4.3A graph block. This is the
# contract the real-DeepSeek E2E (2026-07-19) proved end-to-end; locked here
# with a deterministic mock caller so it can't silently regress. The memo
# recall half alone was already covered; the graph-block half (the ① read
# loop) is what this test nails down.
# ---------------------------------------------------------------------------

def test_sync_turn_atomize_then_prefetch_renders_graph_block(provider):
    from types import SimpleNamespace
    from plugins.memory.ptg.atomizer import Atomizer

    # Canned extraction: R3 person (张三) + R0 entity (厦门国贸) + R2 task — all
    # above gate, all materialize into the graph (entities + self→X relations).
    atoms_payload = {
        "summary": "和张三在厦门国贸开会讨论述职报告",
        "atoms": [
            {"type": "R3_Person", "person_name": "张三", "sentiment": "neutral",
             "interaction_type": "meeting", "segment_id": 0, "confidence": 0.95},
            {"type": "R2_Task", "task_description": "周五前给张三述职报告草稿",
             "urgency": "high", "deadline": None, "confidence": 0.9},
            {"type": "R0_Entity", "entity_name": "厦门国贸", "entity_category": "organization",
             "mention_context": "开会地点", "confidence": 0.9},
        ],
    }
    canned_response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(
            content=json.dumps(atoms_payload, ensure_ascii=False)))],
        model="deepseek-v4-flash",
        usage=SimpleNamespace(prompt_tokens=200, completion_tokens=80),
    )

    def mock_caller(**kwargs):
        return canned_response

    # Inject the mock caller into the provider's Atomizer (same seam the drain
    # test uses) so atomization is deterministic and offline.
    provider._atomizer = Atomizer(
        provider._store, user_id=provider._user_id, llm_caller=mock_caller,
    )

    memo_text = "今天和张三在厦门国贸开会，讨论Q3述职报告，他压力很大。"
    provider.sync_turn(user_content=memo_text, assistant_content="好的",
                       session_id="sess-1")

    # Wait for the background atomize daemon thread to finish (mock is instant).
    #
    # CRITICAL: poll until the GRAPH is materialized (relations_for_user non-
    # empty), NOT merely until atoms land (recent_atoms non-empty). The atomizer
    # writes the atom event first (write_atom) and materializes the relation
    # edge afterwards (_materialize_graph); polling recent_atoms can observe the
    # intermediate state where atoms exist but relations do not, then call
    # prefetch expecting a graph block that isn't there yet → flake. Relations
    # transitively imply atoms (relations are only written after their atoms),
    # so waiting on relations closes the write→materialize window and makes this
    # test deterministic. (ADR-V6-050 B2 follow-up: race proven via code reading
    # of atomizer._extract_and_write_pass.)
    import time
    atoms, relations = [], []
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        relations = provider._store.relations_for_user(provider._user_id)
        if relations:
            atoms = provider._store.recent_atoms(
                user_id=provider._user_id, limit=50)
            break
        time.sleep(0.05)
    assert relations, (
        "background atomize did not materialize graph relations within 5s "
        "(write→materialize window not closed)")
    assert atoms, "relations landed but atoms missing (consistency check)"

    # prefetch must render BOTH halves: memo recall + graph block (§4.3A).
    out = provider.prefetch("张三", session_id="sess-1")
    assert out, "prefetch returned empty after atomize"
    assert "张三" in out, "memo recall half missing 张三"
    # Graph block markers (recall.render_relations_block header + a self→X edge).
    assert "图谱" in out or "互动" in out or "有待办" in out, (
        f"graph block half missing (write-only regression?); prefetch=\n{out}")
    # The structured tie must be visible, not just raw memo text.
    assert "张三" in out and ("互动" in out or "interacts_with" in out
                              or "述职" in out), (
        f"structured relation not rendered; prefetch=\n{out}")


def test_spawn_atomize_uncaught_exception_writes_dlq(tmp_path):
    """ADR-V6-052 (C4 regression): an UNCAUGHT exception escaping ``atomize()``
    must land in DLQ — not merely WARN-log. ``atomize()`` DLQs its *expected*
    failures internally (llm_error / json_parse_error / schema_invalid /
    write_error / materialize_error / below_confidence_threshold); the
    ``_spawn_atomize`` outer wrapper is the backstop for a bug that escapes
    those (e.g. ``IndexError`` on a malformed LLM response with empty
    ``choices``). C7 / ADR-V6-011 contract: ANY exception → DLQ + log, never
    silent. Pre-fix the wrapper only WARN-logged, violating that contract.
    """
    import time

    p = PTGProvider(config={"db_path": str(tmp_path / "ptg.db"), "atomize": True})
    p.initialize("s", hermes_home=str(tmp_path), agent_context="primary")
    assert p._user_id, "founder user_id must resolve at initialize"

    class _BoomAtomizer:
        # Mimics a bug that escapes atomize()'s internal try/excepts (raises
        # before any internal DLQ path runs).
        def atomize(self, *, memo_id, source_text, input_mode="text"):
            raise IndexError("malformed response: choices empty")

    p._atomizer = _BoomAtomizer()

    before = p._store.count_rows("dlq_messages")
    p._spawn_atomize(memo_id="m1", source_text="合成文本无 PII")
    # The raise is immediate; bound the join so a regression can't hang the suite.
    for t in list(p._atomize_threads):
        t.join(timeout=5)
        assert not t.is_alive(), "atomize thread hung after uncaught exception"

    after = p._store.count_rows("dlq_messages")
    assert after == before + 1, (
        f"expected exactly 1 new DLQ row for the uncaught exception, "
        f"got {after - before}")
    row = p._store._conn.execute(
        "SELECT source, error_type, error_msg FROM dlq_messages "
        "ORDER BY created_at DESC LIMIT 1").fetchone()
    assert row["source"] == "atomize_thread"
    assert row["error_type"] == "uncaught_exception"
    assert "IndexError" in row["error_msg"] and "choices empty" in row["error_msg"]
    p.shutdown()


def test_spawn_atomize_dlq_write_failure_does_not_crash(tmp_path, monkeypatch):
    """ADR-V6-052 (C4): the outer-wrapper DLQ write is itself fail-safe. If
    ``insert_dlq`` raises (DB locked mid-shutdown / store closed), the daemon
    thread must WARN-and-survive — the last line of defense can never crash the
    capture surface. Verified by making ``insert_dlq`` blow up."""
    p = PTGProvider(config={"db_path": str(tmp_path / "ptg.db"), "atomize": True})
    p.initialize("s", hermes_home=str(tmp_path), agent_context="primary")

    class _BoomAtomizer:
        def atomize(self, *, memo_id, source_text, input_mode="text"):
            raise RuntimeError("uncaught bug")

    p._atomizer = _BoomAtomizer()

    def _boom_dlq(**kw):
        raise sqlite3.OperationalError("database is locked")

    import sqlite3
    monkeypatch.setattr(p._store, "insert_dlq", _boom_dlq)

    # Must not raise even though both atomize AND the DLQ write blow up.
    p._spawn_atomize(memo_id="m2", source_text="合成文本无 PII")
    for t in list(p._atomize_threads):
        t.join(timeout=5)
        assert not t.is_alive(), "atomize thread hung when DLQ write also failed"
    # No DLQ row could be written (insert_dlq blew up) — the contract here is
    # "survive + WARN", observed by the thread simply finishing cleanly.
    p.shutdown()


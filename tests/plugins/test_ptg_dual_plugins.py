"""RealityOS V6 PTG dual-plugin regression tests (ADR-V6-008 decisions 2 & 3).

Locks P0-4d/e:
  * The memory/ptg plugin's register() harvests a PTGProvider (via the
    _ProviderCollector contract — register_memory_provider only).
  * The observability/ptg_capture plugin's register() wires the three Phase-0
    hooks against a real PluginContext (the reason the split exists).
  * BOTH plugins share ONE PTGStore connection + lock (the shared-singleton
    architectural claim) — a memo captured by the provider is visible to the
    capture plugin's store.
  * Hooks are observers: they return None (allow) and never raise (C7).
  * Both plugin.yaml manifests are valid and declare the right hooks.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

import plugins.memory.ptg as ptg_mem
import plugins.observability.ptg_capture as cap
from plugins.memory.ptg.provider import PTGProvider
from plugins.memory.ptg.store import PTGStore


# ---------------------------------------------------------------------------
# Fake plugin contexts (mirror the real registration surface)
# ---------------------------------------------------------------------------

class _FakeMemCtx:
    """Mimics _ProviderCollector: only register_memory_provider is meaningful."""
    def __init__(self):
        self.provider = None

    def register_memory_provider(self, provider):
        self.provider = provider

    def register_hook(self, *a, **kw):
        pass  # no-op, exactly like _ProviderCollector


class _FakeCapCtx:
    """Mimics real PluginContext.register_hook (stores callbacks by name)."""
    def __init__(self):
        self.hooks: dict = {}

    def register_hook(self, name, cb):
        self.hooks[name] = cb


@pytest.fixture(autouse=True)
def _isolate_capture(monkeypatch, tmp_path):
    """Point the capture plugin's config at a temp DB and reset its globals
    before+after every test, so hooks never touch the real <HERMES_HOME>/ptg.db."""
    db = str(tmp_path / "ptg.db")
    monkeypatch.setattr(cap, "load_ptg_config", lambda: {"db_path": db})
    cap._store = None
    cap._user_id = None
    yield
    cap._store = None
    cap._user_id = None


# ---------------------------------------------------------------------------
# register() contracts
# ---------------------------------------------------------------------------

def test_memory_register_harvests_ptg_provider(monkeypatch, tmp_path):
    monkeypatch.setattr(ptg_mem, "load_ptg_config",
                        lambda: {"db_path": str(tmp_path / "ptg.db")})
    ctx = _FakeMemCtx()
    ptg_mem.register(ctx)
    assert isinstance(ctx.provider, PTGProvider)
    assert ctx.provider.name == "ptg"


def test_capture_register_wires_three_hooks():
    ctx = _FakeCapCtx()
    cap.register(ctx)
    assert set(ctx.hooks) == {"post_tool_call", "pre_gateway_dispatch", "on_session_end"}


# ---------------------------------------------------------------------------
# Hooks: observers return None + never raise (C7)
# ---------------------------------------------------------------------------

def test_hooks_return_none_and_never_raise():
    ctx = _FakeCapCtx()
    cap.register(ctx)
    # post_tool_call — full kwarg set from the dispatcher.
    assert ctx.hooks["post_tool_call"](
        tool_name="computer_use", args={}, result="ok", task_id="t1",
        session_id="s1", tool_call_id="tc1", turn_id=1, api_request_id="r1",
        duration_ms=42, status="ok", error_type=None, error_message=None,
        middleware_trace=None) is None
    # pre_gateway_dispatch — must allow (None), never block/rewrite in Phase 0.
    assert ctx.hooks["pre_gateway_dispatch"](
        event=object(), gateway=None, session_store=None) is None
    # on_session_end
    assert ctx.hooks["on_session_end"](messages=[{"role": "user", "content": "hi"}]) is None


def test_hook_survives_bad_kwargs(capsys):
    """C7: even malformed kwargs must not propagate an exception."""
    ctx = _FakeCapCtx()
    cap.register(ctx)
    # No kwargs at all — callbacks tolerate via .get().
    assert ctx.hooks["post_tool_call"]() is None
    assert ctx.hooks["on_session_end"]() is None


# ---------------------------------------------------------------------------
# Shared singleton (decision 3) — the core architectural claim
# ---------------------------------------------------------------------------

def test_provider_and_capture_share_one_connection(tmp_path, monkeypatch):
    """The provider's store and the capture plugin's store are the SAME
    connection + lock, so a memo captured by the provider is visible to the
    capture side (and vice versa)."""
    db = str(tmp_path / "ptg.db")
    cfg = {"db_path": db}
    # Both resolve the SAME config → SAME resolved path → shared registry entry.
    monkeypatch.setattr(cap, "load_ptg_config", lambda: cfg)

    provider = PTGProvider(config=cfg)
    provider.initialize("s1", platform="cli", agent_context="primary")
    try:
        cap_store = cap._get_store()
        assert cap_store is not None
        assert cap_store._conn is provider._store._conn      # SAME sqlite conn
        assert cap_store._lock is provider._store._lock       # SAME RLock

        # Provider captures a memo; capture side sees it (one connection).
        provider.sync_turn("shared singleton budget marker", "ok")
        assert cap_store.count_rows("memos") == 1
        assert len(cap_store.search_memos_fts("budget")) == 1
    finally:
        provider.shutdown()


def test_capture_sees_founder_after_provider_init(tmp_path, monkeypatch):
    """After the provider bootstraps the founder, the capture plugin resolves
    the same user_id from ptg_meta (shared store)."""
    db = str(tmp_path / "ptg.db")
    cfg = {"db_path": db}
    monkeypatch.setattr(cap, "load_ptg_config", lambda: cfg)

    provider = PTGProvider(config=cfg)
    provider.initialize("s1", agent_context="primary")
    try:
        assert cap._get_user_id() == provider._user_id
    finally:
        provider.shutdown()


def test_capture_user_id_none_before_provider_init(tmp_path, monkeypatch):
    """If no founder has been bootstrapped yet, _get_user_id is None (not a crash)."""
    # Fresh tmp DB via _isolate_capture; provider NOT initialized.
    assert cap._get_user_id() is None


# ---------------------------------------------------------------------------
# plugin.yaml manifests are valid + declare the right hooks
# ---------------------------------------------------------------------------

def test_plugin_yamls_are_valid_manifests():
    root = pathlib.Path(__file__).resolve().parents[2]  # repo root
    mem_yaml = root / "plugins" / "memory" / "ptg" / "plugin.yaml"
    cap_yaml = root / "plugins" / "observability" / "ptg_capture" / "plugin.yaml"

    mem = yaml.safe_load(mem_yaml.read_text())
    assert mem["name"] == "ptg"
    assert "description" in mem and mem["description"]
    # Memory provider plugin declares no hooks (its register_hook is a no-op).
    assert "hooks" not in mem or not mem.get("hooks")

    capm = yaml.safe_load(cap_yaml.read_text())
    assert capm["name"] == "ptg_capture"
    assert set(capm.get("hooks") or []) == {
        "post_tool_call", "pre_gateway_dispatch", "on_session_end"}


# ---------------------------------------------------------------------------
# Shared config/path helpers
# ---------------------------------------------------------------------------

def test_resolve_db_path_none_when_unset():
    from plugins.memory.ptg.store import resolve_db_path
    assert resolve_db_path({}) is None
    assert resolve_db_path(None) is None


def test_resolve_db_path_substitutes_hermes_home(monkeypatch, tmp_path):
    # get_hermes_home is imported lazily inside resolve_db_path — patch at source.
    import hermes_constants
    import plugins.memory.ptg.store as s
    fake_home = tmp_path / "fakehome"
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: fake_home)
    resolved = s.resolve_db_path({"db_path": "$HERMES_HOME/ptg.db"})
    assert resolved == (fake_home / "ptg.db")

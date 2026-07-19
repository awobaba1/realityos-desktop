"""PTGProvider — the RealityOS V6 MemoryProvider over PTGStore.

This is the recall + turn-capture half of the PTG (ADR-V6-008). The OTHER
half (tool-execution / outbound-message capture) lives in the
``plugins/observability/ptg_capture/`` plugin; both share the same
``PTGStore`` singleton via the shared-connection registry.

Phase 0 behaviour:
  * ``sync_turn`` captures the user's turn as a memo (the canonical
    "流经即捕获" surface — every user message that flows through the agent
    becomes a searchable asset).
  * ``prefetch`` injects base-tier FTS5 recall for the upcoming query.
  * ``ptg_search`` tool exposes the same recall to the model explicitly.
  * ``system_prompt_block`` reports store status (memo count, vec tier).

All capture is observation-only: a store failure is logged and swallowed so
it can never break the agent loop (C7). Extraction (turn → atoms/events) and
the C5 validation gate arrive in a later phase.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider
from tools.registry import tool_error

from .atomizer import Atomizer
from .confidence import ConfidenceEngine
from .store import PTGStore

logger = logging.getLogger(__name__)

# Caps concurrent background atomizations so a burst of turns can't fan out into
# many simultaneous LLM calls. Single-founder desktop pace → 2 is ample; queued
# atomizations run as slots free (the turn itself returns immediately).
_ATOMIZE_CONCURRENCY = threading.Semaphore(2)


PTG_SEARCH_SCHEMA = {
    "name": "ptg_search",
    "description": (
        "Search the RealityOS personal timeline — your captured memos, "
        "turns, and (later) extracted events. Use before answering any "
        "question about the user's history, preferences, people, or past "
        "tasks. Returns the most relevant captured turns by keyword."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Keywords to recall."},
            "limit": {"type": "integer", "description": "Max results (default 8)."},
        },
        "required": ["query"],
    },
}


class PTGProvider(MemoryProvider):
    """V6 personal-timeline memory provider over the shared PTGStore."""

    def __init__(self, config: Optional[dict] = None):
        self._config = config or {}
        self._store: Optional[PTGStore] = None
        self._session_id = ""
        self._user_id = ""
        self._agent_context = "primary"
        # Phase 1a heart (ADR-V6-011): per-turn HL-12 atomization. Built in
        # initialize() once the store + founder are ready. ``atomize`` defaults
        # True (the data brain must beat on every launch — same spirit as the
        # ``memory.provider="ptg"`` default-activation guard); capture-focused
        # tests pass a bare config with ``atomize: False`` to stay deterministic.
        self._atomizer: Optional[Atomizer] = None
        self._atomize_enabled = bool(self._config.get("atomize", True))

    # -- core lifecycle ---------------------------------------------------

    @property
    def name(self) -> str:
        return "ptg"

    def is_available(self) -> bool:
        # SQLite is always available; vec is a best-effort upgrade.
        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        # Resolve db_path via the shared resolver so this provider and the
        # capture plugin open the SAME file → shared-connection singleton
        # (ADR-V6-008 decision 3). None → PTGStore default <HERMES_HOME>/ptg.db.
        from .store import resolve_db_path
        db_path = resolve_db_path(self._config)
        embedding_dim = int(self._config.get("embedding_dim", 512))

        try:
            self._store = PTGStore(db_path=db_path, embedding_dim=embedding_dim)
        except Exception as exc:  # noqa: BLE001 — never crash agent init
            logger.warning("PTGStore init failed; provider disabled: %s", exc)
            self._store = None
            return

        self._session_id = session_id
        # agent_context gates capture (ABC contract): only "primary" turns are
        # real user-routed data; subagent/cron/flush are internal agent flows
        # that must NOT pollute the personal timeline.
        self._agent_context = kwargs.get("agent_context") or "primary"
        self._user_id = self._resolve_user_id(kwargs)
        try:
            self._store.ensure_founder(
                self._user_id,
                self._config.get("founder_email", "founder@realityos.local"),
                nickname=self._config.get("founder_nickname", ""),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("PTG ensure_founder failed: %s", exc)

        # Phase 1a heart: build the Atomizer over the now-open store. The LLM
        # caller + clock are the Atomizer's defaults (hermes call_llm + Beijing
        # now); ``main_runtime`` stays None here — call_llm resolves the provider
        # from config.yaml. Extraction never blocks sync_turn (it runs in a
        # daemon thread) and never breaks the loop (fully fail-open, C7).
        if self._atomize_enabled:
            try:
                self._atomizer = Atomizer(
                    self._store,
                    user_id=self._user_id,
                    confidence_engine=ConfidenceEngine.from_ptg_config(self._config),
                    materialize_graph=bool(self._config.get("materialize_graph", True)),
                )
            except Exception as exc:  # noqa: BLE001 — extraction is enrichment; never fatal
                logger.warning("PTG Atomizer init failed; atomization disabled: %s", exc)
                self._atomizer = None
                self._atomize_enabled = False

    def _resolve_user_id(self, init_kwargs: dict) -> str:
        """Pick the tenant user_id for this desktop instance.

        Priority: explicit config > init kwarg (gateway user_id) > a founder id
        persisted in ptg_meta (generated once, reused across restarts so the
        captured asset stays under one stable tenant).
        """
        configured = self._config.get("founder_user_id")
        if configured:
            return str(configured)
        kw_user = init_kwargs.get("user_id") or init_kwargs.get("user_id_alt")
        if kw_user:
            return str(kw_user)
        # Persisted founder id — generated once.
        assert self._store is not None
        with self._store._lock:
            row = self._store._conn.execute(
                "SELECT value FROM ptg_meta WHERE key='founder_user_id'"
            ).fetchone()
            if row is not None:
                return row[0]
            import uuid
            new_id = str(uuid.uuid4())
            self._store._conn.execute(
                "INSERT OR REPLACE INTO ptg_meta(key, value) VALUES ('founder_user_id', ?)",
                (new_id,),
            )
            return new_id

    # -- prompt / recall --------------------------------------------------

    def system_prompt_block(self) -> str:
        if not self._store:
            return ""
        try:
            total = self._store.count_rows("memos")
        except Exception:  # noqa: BLE001
            total = 0
        tier = "sqlite-vec + FTS5" if self._store.vec_available else "FTS5 (base tier)"
        if total == 0:
            return (
                "# RealityOS Personal Timeline\n"
                f"Active ({tier}). Empty — every user turn you receive is captured "
                "automatically as a memo. Use ptg_search to recall prior context "
                "before answering questions about the user."
            )
        return (
            "# RealityOS Personal Timeline\n"
            f"Active ({tier}). {total} captured memo(s). Use ptg_search to recall "
            "relevant prior turns before answering questions about the user's "
            "history, people, or tasks."
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not self._store or not query:
            return ""
        try:
            hits = self._store.search_memos_fts(query, user_id=self._user_id, limit=5)
        except Exception as exc:  # noqa: BLE001
            logger.debug("PTG prefetch failed: %s", exc)
            return ""
        if not hits:
            return ""
        lines = ["## RealityOS recall (prior captured turns)"]
        for h in hits:
            text = (h.get("source_text") or "").strip().replace("\n", " ")
            lines.append(f"- {text[:200]}")
        return "\n".join(lines)

    # -- capture ----------------------------------------------------------

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Capture the user's turn as a memo (流经即捕获) + atomize it (Phase 1a).

        The user message is the canonical capture surface. The assistant reply
        is recorded as the memo's summary so the turn pair is reconstructable.
        Empty / whitespace-only user turns are skipped (no asset, no noise).

        After the memo lands, the Atomizer runs HL-12 extraction in a daemon
        thread (turn → R-atoms → event tables), so atomization never blocks the
        conversation and never breaks the loop (C7). Capture itself remains
        synchronous: even if extraction is disabled or fails, the memo is safe.
        """
        if not self._store or not self._user_id:
            return
        # Only primary-context turns are real user-routed data worth capturing
        # as a personal asset. Subagent/cron/flush flows are internal and would
        # pollute the timeline (ABC agent_context contract).
        if self._agent_context not in ("primary", None):
            return
        text = (user_content or "").strip()
        if not text:
            return
        summary = None
        a = (assistant_content or "").strip()
        if a:
            summary = a[:500]
        try:
            memo_id = self._store.insert_memo(
                user_id=self._user_id,
                source_text=text,
                input_mode="text",
                summary=summary,
            )
        except Exception as exc:  # noqa: BLE001 — capture must never break the loop
            logger.warning("PTG sync_turn capture failed: %s", exc)
            return
        # Phase 1a heart: best-effort background atomization of the captured
        # memo. Disabled in capture-focused tests via ``atomize: False``.
        if self._atomize_enabled and self._atomizer is not None:
            self._spawn_atomize(memo_id=memo_id, source_text=text)

    def _spawn_atomize(self, *, memo_id: str, source_text: str) -> None:
        """Run atomization off the conversation thread, fully fail-open (C7)."""
        atomizer = self._atomizer
        if atomizer is None:
            return

        def _run() -> None:
            try:
                with _ATOMIZE_CONCURRENCY:
                    atomizer.atomize(
                        memo_id=memo_id, source_text=source_text, input_mode="text")
            except Exception as exc:  # noqa: BLE001 — observer surface: never escape
                logger.warning("PTG background atomize failed for memo %s: %s", memo_id, exc)

        threading.Thread(target=_run, name="ptg-atomize", daemon=True).start()

    # -- tools ------------------------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [PTG_SEARCH_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if tool_name == "ptg_search":
            return self._handle_search(args)
        return tool_error(f"Unknown PTG tool: {tool_name}")

    def _handle_search(self, args: dict) -> str:
        if not self._store:
            return tool_error("PTG store unavailable")
        query = args.get("query", "").strip()
        if not query:
            return tool_error("query is required")
        limit = int(args.get("limit", 8) or 8)
        try:
            hits = self._store.search_memos_fts(query, user_id=self._user_id, limit=limit)
        except Exception as exc:  # noqa: BLE001
            return tool_error(f"search failed: {exc}")
        return json.dumps({"results": hits, "count": len(hits)}, ensure_ascii=False)

    # -- shutdown ---------------------------------------------------------

    def shutdown(self) -> None:
        # Stop scheduling new atomizations first; in-flight daemon threads are
        # fail-open and die with the process.
        self._atomizer = None
        self._atomize_enabled = False
        if self._store is not None:
            try:
                self._store.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("PTG shutdown close() failed: %s", exc)
        self._store = None

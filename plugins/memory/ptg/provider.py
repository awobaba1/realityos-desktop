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
from .citation import CITATION_INSTRUCTION, ground_answer, looks_like_history_claim, number_chunks
from .confidence import ConfidenceEngine
from .store import PTGStore

logger = logging.getLogger(__name__)

# Caps concurrent background atomizations so a burst of turns can't fan out into
# many simultaneous LLM calls. Single-founder desktop pace → 2 is ample; queued
# atomizations run as slots free (the turn itself returns immediately).
_ATOMIZE_CONCURRENCY = threading.Semaphore(2)


def _query_terms(query: str) -> List[str]:
    """Split a recall query into ≥2-char term tokens for the history-claim
    heuristic. Splits on whitespace and common CJK/ASCII punctuation; for a
    space-less CJK query, the whole string is returned as one term too (so an
    answer echoing the query still matches via substring)."""
    import re
    q = (query or "").strip()
    if not q:
        return []
    parts = [p for p in re.split(r"[\s,，、;；。./]+", q) if p]
    terms = [p for p in parts if len(p) >= 2]
    if q not in terms and len(q) >= 2:
        terms.append(q)
    return terms


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
        # Shutdown drain (ADR-V6-012): track in-flight atomize daemon threads so
        # shutdown() can JOIN them before closing the store. Without this, a
        # thread mid-extraction when close() fires writes to a closed DB → the
        # atom is lost (C2/C7 data loss on shutdown). Bounded join; we never
        # block forever on a hung LLM call.
        self._atomize_threads: List["threading.Thread"] = []
        self._atomize_threads_lock = threading.Lock()
        # §6.9 scheduled-protection thread (ADR-V6-014). Tracked separately so
        # shutdown() can join it BEFORE store.close() — run_scheduled_protection
        # touches _conn via _meta_get/_meta_set, so an unjoined backup thread +
        # close() is a use-after-close segfault (caught by faulthandler; the
        # ADR-V6-015 fix).
        self._backup_thread = None
        # G1 citation gate (ADR-V6-043 / F2): the recall hits + query from the
        # LAST prefetch / ptg_search, kept so sync_turn can validate the agent's
        # answer against the chunks actually in scope. The agent loop is
        # sequential (prefetch → [tool calls] → answer → sync_turn) on a
        # single-founder desktop, so a plain instance slot suffices (no lock).
        self._last_recall_hits: List[Dict[str, Any]] = []
        self._last_query: str = ""

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

        # §6.9 scheduled protection (ADR-V6-014): startup-lazy daily backup +
        # monthly restore drill. The desktop brain may not be open at 04:00, so
        # we run on launch IF overdue (ptg_meta timestamps), in a daemon thread
        # so init never blocks on a backup. Fail-open + opt-out
        # (plugins.ptg.backup.enabled=false). Honours "data never leaves device"
        # — dest_dir defaults to a pure-local <HERMES_HOME>/backups/ptg.
        backup_cfg = self._config.get("backup") or {}
        if backup_cfg.get("enabled", True):
            self._spawn_scheduled_protection(backup_cfg)

    def _spawn_scheduled_protection(self, backup_cfg: dict) -> None:
        """Run §6.9 backup + drill off the init thread, fully fail-open (C7)."""
        store = self._store
        if store is None:
            return

        def _run() -> None:
            try:
                from .backup import run_scheduled_protection
                dest = backup_cfg.get("dest_dir")
                if not dest:
                    from hermes_constants import get_hermes_home
                    dest = str(get_hermes_home() / "backups" / "ptg")
                run_scheduled_protection(
                    store, dest,
                    backup_interval_hours=float(backup_cfg.get("backup_interval_hours", 24)),
                    drill_interval_days=float(backup_cfg.get("drill_interval_days", 30)),
                    keep=int(backup_cfg.get("keep", 30)),
                )
            except Exception as exc:  # noqa: BLE001 — observer surface: never escape
                logger.warning("PTG scheduled protection failed: %s", exc)

        t = threading.Thread(target=_run, name="ptg-backup", daemon=True)
        # Track for the shutdown() drain (ADR-V6-015): this thread touches _conn
        # via _meta_get/_meta_set, so it MUST be joined before store.close() —
        # closing the store under a running backup is a use-after-close segfault.
        self._backup_thread = t
        t.start()

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
            "history, people, or tasks.\n\n"
            + CITATION_INSTRUCTION
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not self._store or not query:
            return ""
        blocks: list[str] = []
        try:
            hits = self._store.search_memos_fts(query, user_id=self._user_id, limit=5)
        except Exception as exc:  # noqa: BLE001
            logger.debug("PTG prefetch failed: %s", exc)
            hits = []
        # G1 (ADR-V6-043 / F2): render recall as NUMBERED chunks `[N] date: text`
        # so the agent has a citation handle, and stash the hits + query for
        # sync_turn to validate the answer against. The previous `- {text[:200]}`
        # form gave the agent context but no way to cite a source, and nothing
        # could check an ungrounded claim — the credibility root failure.
        self._last_recall_hits = hits or []
        self._last_query = query
        if hits:
            chunk_text, _index_map = number_chunks(hits)
            blocks.append(
                "## RealityOS recall (prior captured turns)\n"
                "以下片段按 [N] 编号；引用用户过去时必须用该编号标注来源。\n\n"
                + chunk_text
            )
        # §4.3A: fold the entity/relation graph into context so the model sees
        # structured ties ("你与张三互动过 3 次"), not just raw memo text. The
        # Atomizer materialized this graph; without rendering it back, it was
        # write-only (Explore recon finding #3). Empty when no graph hits.
        try:
            from .recall import render_relations_block
            graph_block = render_relations_block(
                self._store, self._user_id, query, token_budget=800)
            if graph_block:
                blocks.append(graph_block)
        except Exception as exc:  # noqa: BLE001 — recall never breaks the loop
            logger.debug("PTG prefetch graph render failed: %s", exc)
        return "\n\n".join(blocks)

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
        # G1 (ADR-V6-043 / F2): observe the answer's grounding — did the agent
        # cite the chunks it was given, or assert the user's past ungrounded?
        # Observation-only here (never breaks the loop, C7); the hard
        # refuse-to-render gate needs an agent-loop answer hook (documented as
        # the next iteration). Fully fail-open: a store/log glitch is swallowed.
        self._observe_citation_quality(assistant_content)
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
                # C7 / ADR-V6-011 contract: ANY exception → DLQ + log, never
                # silent. ``atomize()`` DLQs its *expected* failures internally
                # (llm_error / json_parse_error / schema_invalid / write_error /
                # materialize_error / below_confidence_threshold); this outer
                # wrapper is the backstop for an UNCAUGHT exception (a bug —
                # e.g. ``IndexError`` on a malformed LLM response) that escapes
                # those handlers. The DLQ write is itself fail-safe: if it
                # raises (DB locked / shutting down) the daemon thread still
                # survives WARN-only — the last line must never crash capture.
                logger.warning("PTG background atomize failed for memo %s: %s", memo_id, exc)
                try:
                    self._store.insert_dlq(
                        user_id=self._user_id,
                        source="atomize_thread",
                        error_type="uncaught_exception",
                        error_msg=f"{type(exc).__name__}: {exc}",
                        original_data={"memo_id": memo_id, "source_text": source_text},
                    )
                except Exception:  # noqa: BLE001 — last-line DLQ must not crash observer
                    logger.warning(
                        "PTG atomize DLQ write also failed for memo %s: %s", memo_id, exc)

        t = threading.Thread(target=_run, name="ptg-atomize", daemon=True)
        with self._atomize_threads_lock:
            # Prune dead threads so the list can't grow without bound.
            self._atomize_threads = [x for x in self._atomize_threads if x.is_alive()]
            self._atomize_threads.append(t)
        t.start()

    # -- G1 citation observation (ADR-V6-043 / F2) -----------------------

    def _observe_citation_quality(self, assistant_content: str) -> None:
        """Validate the answer's grounding against the last recall + record it.

        Three outcomes, all observation-only (C7 — never breaks the loop):
          * ``grounded``   — answer cited ≥1 valid recalled chunk. Bump the
            grounded counter (a healthy credibility signal).
          * ``ungrounded`` — answer made a history-like claim (referenced the
            user's past / a recalled term) but cited nothing valid. This is a
            credibility incident: an ungrounded assertion about the user's
            past reached them. WARN-logged + bump the ungrounded counter. The
            hard refuse-to-render gate needs an agent-loop answer hook
            (documented as the next iteration); this is the observable foundation.
          * ``neutral``    — generic reply (no history claim) with recall in
            scope, or no recall was in scope at all. Not counted either way.

        Counters persist to ``ptg_meta`` (citation_grounded_turns /
        citation_ungrounded_turns) so credibility drift is queryable across
        restarts. Fully fail-open: a store/log glitch is swallowed.
        """
        if not self._store:
            return
        hits = self._last_recall_hits
        if not hits:
            return  # nothing recalled this turn — nothing to ground against
        try:
            grounding = ground_answer(assistant_content or "", hits)
            if grounding["has_valid_citation"]:
                self._bump_meta("citation_grounded_turns")
                logger.debug(
                    "PTG citation: grounded (%d source(s), %d dropped)",
                    len(grounding["sources"]), len(grounding["dropped"]))
                return
            # No valid citation — is the answer even about the user's past?
            # The recalled terms are the query tokens (what the agent was
            # asked to look up); if the answer echoes them or uses past-tense
            # markers, it's a history claim that SHOULD have been grounded.
            terms = _query_terms(self._last_query)
            if looks_like_history_claim(assistant_content or "", terms):
                self._bump_meta("citation_ungrounded_turns")
                logger.warning(
                    "PTG citation: UNGROUNDED history claim — answer asserted the "
                    "user's past with %d recalled chunk(s) in scope but cited none "
                    "valid (dropped/hallucinated=%d, query=%r). This is a G1 "
                    "credibility incident (ADR-V6-043); hard-enforcement pending.",
                    len(hits), len(grounding["dropped"]), self._last_query[:80])
        except Exception as exc:  # noqa: BLE001 — observer surface: never escape
            logger.debug("PTG citation observation failed: %s", exc)

    def _bump_meta(self, key: str) -> None:
        """Increment an integer counter in ptg_meta, fail-open (C7)."""
        store = self._store
        if store is None:
            return
        try:
            with store._lock:
                row = store._conn.execute(
                    "SELECT value FROM ptg_meta WHERE key=?", (key,)).fetchone()
                cur = int(row[0]) if row is not None and str(row[0]).isdigit() else 0
                store._conn.execute(
                    "INSERT OR REPLACE INTO ptg_meta(key, value) VALUES (?, ?)",
                    (key, str(cur + 1)))
        except Exception as exc:  # noqa: BLE001
            logger.debug("PTG ptg_meta bump(%s) failed: %s", key, exc)

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
        # G1 (ADR-V6-043 / F2): return NUMBERED chunks so the agent can cite
        # [N], and stash the hits + query for sync_turn validation. The
        # ``numbered`` field is the cite-aware rendering; ``results`` keeps the
        # raw rows for any structured use.
        self._last_recall_hits = hits or []
        self._last_query = query
        chunk_text, _index_map = number_chunks(hits)
        return json.dumps(
            {
                "numbered": chunk_text,
                "results": hits,
                "count": len(hits),
                "citation_rule": "引用用户过去时必须用 [N] 编号标注来源片段。",
            },
            ensure_ascii=False,
        )

    # -- shutdown ---------------------------------------------------------

    def shutdown(self) -> None:
        # Stop scheduling new atomizations first.
        self._atomizer = None
        self._atomize_enabled = False
        # DRAIN in-flight extraction threads BEFORE closing the store. A thread
        # mid-extraction (LLM call done, writing atoms) would otherwise hit a
        # closed DB and lose the atom (C2/C7). Bounded join: timeout is the
        # LLM call budget + a margin, so a hung provider can't hang shutdown.
        drain_timeout = float(self._config.get("shutdown_drain_timeout", 35.0))
        with self._atomize_threads_lock:
            live = [t for t in self._atomize_threads if t.is_alive()]
        for t in live:
            t.join(timeout=drain_timeout)
        still_alive = [t for t in live if t.is_alive()]
        if still_alive:
            logger.warning(
                "PTG shutdown: %d atomize thread(s) still alive after %.1fs drain "
                "(hung LLM call?); proceeding to close — their atoms may be lost.",
                len(still_alive), drain_timeout)
        # DRAIN the §6.9 backup thread too (ADR-V6-015): run_scheduled_protection
        # is one-shot but touches _conn (_meta_get/_meta_set) mid-flight; closing
        # the store under it is a use-after-close segfault. One-shot ⇒ the join
        # virtually always returns immediately; the bound only covers a backup
        # that happens to be mid-write at shutdown.
        bt = self._backup_thread
        if bt is not None and bt.is_alive():
            bt.join(timeout=drain_timeout)
            if bt.is_alive():
                logger.warning(
                    "PTG shutdown: backup thread still alive after %.1fs drain; "
                    "proceeding to close.", drain_timeout)
        if self._store is not None:
            try:
                self._store.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("PTG shutdown close() failed: %s", exc)
        self._store = None

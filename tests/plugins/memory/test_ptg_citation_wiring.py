"""C4 integration: G1 citation gate wired into PTGProvider (ADR-V6-043 / F2).

Bug ID: F2 — the credibility root failure. Before the gate, prefetch rendered
recall as `- {text[:200]}` with no citation handle, and nothing checked the
answer's grounding. These tests prove the wiring is end-to-end real, not just
a pure-function unit test that nothing calls:

  * prefetch renders NUMBERED chunks (`[1] date: text`) and stashes the hits
    for sync_turn validation.
  * ptg_search returns a `numbered` cite-aware field + citation rule.
  * sync_turn observes grounding: a grounded answer bumps the grounded
    counter; an ungrounded history-claim bumps the ungrounded counter (the G1
    credibility incident); a neutral reply bumps neither.
  * empty recall → no observation; a store/glitch → fail-open (C7).

The observation is intentionally non-blocking (hard refuse-to-render needs an
agent-loop answer hook — documented in the ADR as the next iteration). What's
proven here is the observable foundation: cite-aware recall + a real
validator + queryable counters in ptg_meta.
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
    """PTGProvider on an isolated temp DB, atomize+backup OFF for determinism."""
    p = PTGProvider(config={
        "db_path": str(tmp_path / "ptg.db"),
        "atomize": False,
        "backup": {"enabled": False},
    })
    p.initialize("sess-1", hermes_home=str(tmp_path), platform="cli",
                 agent_context="primary")
    yield p
    p.shutdown()


def _insert_memo(p, text):
    return p._store.insert_memo(
        user_id=p._user_id, source_text=text, input_mode="text")


def _meta(p, key: str) -> int:
    """Read an integer ptg_meta counter."""
    with p._store._lock:
        row = p._store._conn.execute(
            "SELECT value FROM ptg_meta WHERE key=?", (key,)).fetchone()
    return int(row[0]) if row is not None and str(row[0]).isdigit() else 0


# ---------------------------------------------------------------------------
# prefetch renders numbered chunks + stashes hits
# ---------------------------------------------------------------------------

class TestPrefetchNumbered:
    def test_prefetch_renders_numbered_chunks_with_date(self, provider):
        _insert_memo(provider, "和小王开了会，讨论了述职报告")
        block = provider.prefetch("小王")
        assert "[1]" in block, "prefetch must render recall as [N] numbered chunks"
        # G1: real memo_id never appears; only the synthetic [N] handle.
        assert "和小王开了会" in block
        # hits stashed for sync_turn validation
        assert len(provider._last_recall_hits) == 1
        assert provider._last_query == "小王"

    def test_prefetch_empty_query_returns_empty(self, provider):
        assert provider.prefetch("") == ""
        assert provider._last_recall_hits == []

    def test_prefetch_no_hits_empties_slot(self, provider):
        _insert_memo(provider, "和小王开了会")
        provider.prefetch("完全不匹配的关键词zzz")
        assert provider._last_recall_hits == []


# ---------------------------------------------------------------------------
# ptg_search returns a cite-aware numbered field
# ---------------------------------------------------------------------------

class TestPtgSearchNumbered:
    def test_search_returns_numbered_field_and_rule(self, provider):
        _insert_memo(provider, "和小王开了会，讨论了述职报告")
        raw = provider.handle_tool_call("ptg_search", {"query": "小王"})
        data = json.loads(raw)
        assert "numbered" in data, "ptg_search must return a cite-aware 'numbered' field"
        assert "[1]" in data["numbered"]
        assert "citation_rule" in data
        assert data["count"] == 1
        assert len(provider._last_recall_hits) == 1


# ---------------------------------------------------------------------------
# sync_turn observes grounding — the G1 credibility signal
# ---------------------------------------------------------------------------

class TestSyncTurnCitationObservation:
    def test_grounded_answer_bumps_grounded_counter(self, provider):
        _insert_memo(provider, "和小王开了会，讨论了述职报告")
        provider.prefetch("小王")  # recall in scope
        assert _meta(provider, "citation_grounded_turns") == 0

        provider.sync_turn(
            user_content="小王上周说了什么？",
            assistant_content="你上周和小王讨论了述职报告 [1]。")
        assert _meta(provider, "citation_grounded_turns") == 1, (
            "A grounded answer (valid [1] citation) must bump the grounded counter.")
        assert _meta(provider, "citation_ungrounded_turns") == 0

    def test_ungrounded_history_claim_bumps_ungrounded_counter(self, provider):
        """F2 core: the agent asserts the user's past (recalled term 小王 +
        past marker 上周) with recall in scope but cites NOTHING valid → a G1
        credibility incident, recorded in the ungrounded counter."""
        _insert_memo(provider, "和小王开了会，讨论了述职报告")
        provider.prefetch("小王")  # recall IS in scope
        assert _meta(provider, "citation_ungrounded_turns") == 0

        provider.sync_turn(
            user_content="小王上周说了什么？",
            assistant_content="你上周和小王说想辞职，情绪很低落。")  # claim, no [N]
        assert _meta(provider, "citation_ungrounded_turns") == 1, (
            "An ungrounded history claim (recall in scope, no valid citation) "
            "is a G1 credibility incident — must bump the ungrounded counter.")
        assert _meta(provider, "citation_grounded_turns") == 0

    def test_hallucinated_citation_is_ungrounded(self, provider):
        """Agent cites [9] which was never provided → dropped, no valid source
        → ungrounded (the agent invented a reference)."""
        _insert_memo(provider, "和小王开了会，讨论了述职报告")
        provider.prefetch("小王")
        provider.sync_turn(
            user_content="小王上周说了什么？",
            assistant_content="你上周和小王说想辞职 [9]。")  # [9] out of range
        assert _meta(provider, "citation_ungrounded_turns") == 1
        assert _meta(provider, "citation_grounded_turns") == 0

    def test_neutral_reply_bumps_neither_counter(self, provider):
        """A generic reply with no history claim is NOT a credibility incident
        even with recall in scope — not every turn references the past."""
        _insert_memo(provider, "和小王开了会，讨论了述职报告")
        provider.prefetch("小王")
        provider.sync_turn(
            user_content="记一下明天和小李开会",
            assistant_content="好的，已记下。")
        assert _meta(provider, "citation_grounded_turns") == 0
        assert _meta(provider, "citation_ungrounded_turns") == 0

    def test_empty_recall_no_observation(self, provider):
        """No recall in scope this turn → nothing to ground against → no bump,
        regardless of what the answer says."""
        provider.sync_turn(
            user_content="随便聊聊",
            assistant_content="你上周和小王说想辞职")  # history-like, but no recall
        assert _meta(provider, "citation_grounded_turns") == 0
        assert _meta(provider, "citation_ungrounded_turns") == 0


# ---------------------------------------------------------------------------
# C7: observation never breaks the loop
# ---------------------------------------------------------------------------

class TestCitationObservationFailOpen:
    def test_garbage_recall_hits_do_not_raise(self, provider):
        """C7: even if _last_recall_hits is corrupted (e.g. a non-dict slipped
        in), the observer must swallow it and never break sync_turn."""
        provider._last_recall_hits = ["not a dict"]  # type: ignore[assignment]
        # Must not raise.
        provider.sync_turn(
            user_content="问点什么",
            assistant_content="你上周说了些啥 [1]")
        # No counter changed (the broken state prevented a clean grading).
        assert _meta(provider, "citation_grounded_turns") == 0
        assert _meta(provider, "citation_ungrounded_turns") == 0

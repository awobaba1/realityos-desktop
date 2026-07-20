"""C4 unit tests: grounded-answer citation gate (ADR-V6-043 / F2).

Bug ID: F2 — V6 prefetch rendered recall as `- {text[:200]}` with no memo_id
and no citation handle, and nothing validated that an answer's claims about
the user's past were backed by actually-recalled memos. An ungrounded
assertion ("你上周说想辞职" with no such memo) reached the user
indistinguishable from a grounded one. This is the G1 credibility root
failure (strategy-02). The citation gate ports danao13's production pattern
(``rag_service.py:711-731``) — synthetic 1-based indices → cite [N] →
bounds-check → map to real memo_id — adapted to V6's free-text answers.

These tests pin the pure-function contract of plugins/memory/ptg/citation.py
in isolation. The wiring into PTGProvider is covered by
tests/plugins/memory/ptg/test_citation_wiring.py.
"""

from __future__ import annotations

import pytest

from plugins.memory.ptg.citation import (
    CITATION_INSTRUCTION,
    extract_cited_indices,
    ground_answer,
    looks_like_history_claim,
    number_chunks,
    validate_citations,
)


# ── number_chunks ───────────────────────────────────────────────────────────

class TestNumberChunks:
    def test_empty_hits(self):
        text, idx_map = number_chunks([])
        assert text == ""
        assert idx_map == {}

    def test_renders_numbered_with_date(self):
        hits = [
            {"id": "m1", "source_text": "和小王开了会", "timestamp": "2026-07-08T10:00:00+00:00"},
            {"id": "m2", "source_text": "写了述职报告", "timestamp": "2026-07-09T10:00:00+00:00"},
        ]
        text, idx_map = number_chunks(hits)
        assert text.splitlines()[0].startswith("[1] 2026-07-08: ")
        assert "和小王开了会" in text.splitlines()[0]
        assert text.splitlines()[1].startswith("[2] 2026-07-09: ")
        assert idx_map == {1: "m1", 2: "m2"}

    def test_no_timestamp_omits_date_prefix(self):
        hits = [{"id": "m1", "source_text": "no date here"}]
        text, idx_map = number_chunks(hits)
        # No "YYYY-MM-DD:" — just "[1] no date here"
        assert text == "[1] no date here"
        assert idx_map == {1: "m1"}

    def test_truncates_long_snippet(self):
        long = "x" * 500
        text, _ = number_chunks([{"id": "m1", "source_text": long}])
        # snippet portion capped at 200 chars
        line = text.splitlines()[0]
        assert len(line) < len(long)

    def test_real_memo_id_never_in_prompt(self):
        """G1 root: the agent only sees [idx], never the memo_id — a
        hallucinated out-of-range index can't leak a real id."""
        hits = [{"id": "SECRET-MEMO-ID-123", "source_text": "snippet"}]
        text, _ = number_chunks(hits)
        assert "SECRET-MEMO-ID-123" not in text
        assert "[1]" in text

    def test_supports_snippet_alias_and_memo_id_alias(self):
        """Caller may use 'snippet' or 'memo_id' aliases too."""
        hits = [{"memo_id": "m9", "snippet": "alt fields"}]
        text, idx_map = number_chunks(hits)
        assert idx_map == {1: "m9"}
        assert "alt fields" in text

    def test_collapse_newlines_in_snippet(self):
        hits = [{"id": "m1", "source_text": "line1\nline2\nline3"}]
        text, _ = number_chunks(hits)
        assert text.count("\n") == 0  # newlines within snippet flattened to spaces


# ── extract_cited_indices ───────────────────────────────────────────────────

class TestExtractCitedIndices:
    def test_single_token(self):
        assert extract_cited_indices("你上周开了会 [1]") == [1]

    def test_multiple_tokens(self):
        assert extract_cited_indices("见 [1] 和 [3]") == [1, 3]

    def test_comma_list(self):
        assert extract_cited_indices("见 [1,3,5]") == [1, 3, 5]

    def test_full_width_comma_list(self):
        assert extract_cited_indices("见 [1，3]") == [1, 3]

    def test_range_expanded(self):
        assert extract_cited_indices("见 [1-3]") == [1, 2, 3]

    def test_en_dash_range(self):
        assert extract_cited_indices("见 [2–4]") == [2, 3, 4]

    def test_descending_range_normalized(self):
        assert extract_cited_indices("见 [5-3]") == [3, 4, 5]

    def test_pathological_range_capped(self):
        """A [1-9999] token must not expand to 9999 indices — capped at +50."""
        out = extract_cited_indices("见 [1-9999]")
        assert out == list(range(1, 52))  # 1..51

    def test_dedupe_and_sort(self):
        assert extract_cited_indices("[3][1][3]") == [1, 3]

    def test_no_tokens(self):
        assert extract_cited_indices("普通回复，无引用") == []
        assert extract_cited_indices("") == []

    def test_mixed_in_prose(self):
        text = "根据记录 [1]，你和小王 [2,3] 在上周讨论过 [4-5]。"
        assert extract_cited_indices(text) == [1, 2, 3, 4, 5]


# ── validate_citations ──────────────────────────────────────────────────────

class TestValidateCitations:
    def test_valid_indices_map_to_sources(self):
        index_map = {1: "m1", 2: "m2", 3: "m3"}
        hits = [{"id": "m1", "source_text": "a", "timestamp": "2026-07-08T10:00:00+00:00"},
                {"id": "m2", "source_text": "b", "timestamp": "2026-07-09T10:00:00+00:00"},
                {"id": "m3", "source_text": "c"}]
        sources, dropped = validate_citations([1, 3], index_map, hits)
        assert [s["memo_id"] for s in sources] == ["m1", "m3"]
        assert sources[0]["date"] == "2026-07-08"
        assert dropped == []

    def test_out_of_range_dropped_as_hallucination(self):
        index_map = {1: "m1"}
        sources, dropped = validate_citations([1, 9, 99], index_map)
        assert [s["memo_id"] for s in sources] == ["m1"]
        assert dropped == [9, 99]  # hallucinated — never mapped to a real id

    def test_dedupe_repeated_citation(self):
        index_map = {1: "m1"}
        sources, dropped = validate_citations([1, 1, 1], index_map)
        assert len(sources) == 1
        assert dropped == []

    def test_empty(self):
        sources, dropped = validate_citations([], {1: "m1"})
        assert sources == []
        assert dropped == []


# ── ground_answer ───────────────────────────────────────────────────────────

class TestGroundAnswer:
    def test_grounded_answer(self):
        hits = [{"id": "m1", "source_text": "和小王开了会", "timestamp": "2026-07-08T10:00:00+00:00"}]
        out = ground_answer("你上周和小王开了会 [1]", hits)
        assert out["has_valid_citation"] is True
        assert len(out["sources"]) == 1
        assert out["sources"][0]["memo_id"] == "m1"
        assert out["dropped"] == []
        assert out["cited_indices"] == [1]
        assert out["n_chunks"] == 1

    def test_ungrounded_hallucinated_citation(self):
        hits = [{"id": "m1", "source_text": "和小王开了会"}]
        # Agent cites [9] which doesn't exist → dropped, no valid source.
        out = ground_answer("你上周说想辞职 [9]", hits)
        assert out["has_valid_citation"] is False
        assert out["sources"] == []
        assert out["dropped"] == [9]

    def test_no_citation_tokens(self):
        hits = [{"id": "m1", "source_text": "和小王开了会"}]
        out = ground_answer("好的，我明白了", hits)
        assert out["has_valid_citation"] is False
        assert out["cited_indices"] == []
        # NB: has_valid_citation=False with no history-claim is "neutral", not
        # "ungrounded" — the caller (looks_like_history_claim) decides.

    def test_empty_hits(self):
        out = ground_answer("任意答案 [1]", [])
        assert out["has_valid_citation"] is False
        assert out["n_chunks"] == 0


# ── looks_like_history_claim ────────────────────────────────────────────────

class TestLooksLikeHistoryClaim:
    def test_recalled_term_present(self):
        assert looks_like_history_claim("你和小王讨论过这个", ["小王"]) is True

    def test_past_tense_marker(self):
        assert looks_like_history_claim("你上周提到过这件事", []) is True

    def test_recency_marker_english(self):
        assert looks_like_history_claim("You mentioned this last week", []) is True

    def test_generic_reply_not_a_claim(self):
        assert looks_like_history_claim("好的，已记下", []) is False
        assert looks_like_history_claim("我可以帮你分析这件事", []) is False

    def test_empty_answer(self):
        assert looks_like_history_claim("", ["张三"]) is False

    def test_short_term_ignored(self):
        """A 1-char term is too noisy to count as a recall anchor — single-char
        terms are skipped (len < 2). So an answer with no ≥2-char recalled term
        AND no past-tense marker is NOT a history claim."""
        # "张"/"三" are 1-char → skipped; no past marker → not a claim.
        assert looks_like_history_claim("你和张三聊过", ["张", "三"]) is False
        assert looks_like_history_claim("你好", ["你", "好"]) is False


# ── CITATION_INSTRUCTION surfaced ───────────────────────────────────────────

class TestCitationInstruction:
    def test_instruction_exists_and_is_chinese(self):
        assert "引用" in CITATION_INSTRUCTION
        assert "[N]" in CITATION_INSTRUCTION
        assert "我没有这方面的记录" in CITATION_INSTRUCTION  # the "say you don't know" clause

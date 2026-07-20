"""Grounded-answer citation gate (ADR-V6-043 / F2) — credibility root.

G1 (strategy-02): the entire credibility system rests on the agent's answers
about the user's past being GROUNDED in actually-recalled memos. Before this
module, V6 ``prefetch`` rendered recall as plain ``- {text[:200]}`` bullets
with NO memo_id and NO citation handle — the agent received context but had
no way to cite which memo a claim came from, and nothing validated that a
claim was backed by a real recalled memo. An ungrounded assertion ("你上周
说想辞职" when no such memo exists) would reach the user indistinguishable
from a grounded one. That is the credibility root failure.

This module ports danao13's production citation pattern
(``rag_service.py:711-731``) — synthetic 1-based indices → LLM cites [N] →
bounds-check → map to real memo_id — and adapts it to V6's agent-driven
architecture (the agent generates free-text answers, not a structured JSON
response, so citations are parsed from ``[N]`` tokens in the answer text).

Pure functions, no store/LLM deps — fully unit-testable in isolation. Wired
into PTGProvider.prefetch (numbered chunks), system_prompt_block (citation
instruction), and sync_turn (per-turn validation + observation).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Sequence, Tuple

# ── Citation instruction surfaced to the agent via system_prompt_block ──────
# Tells the model to ground every factual claim about the user's past in a
# recalled chunk, citing by the [N] handle. Without this, the agent has no
# signal that citations are expected.
CITATION_INSTRUCTION = (
    "## 引用铁律（G1 可信度根基）\n"
    "当你基于 RealityOS 召回的片段回答用户关于其过去的人/事/情绪/任务时，"
    "必须在该陈述后用 [N] 标注来源片段编号（如「你上周和小王开了两次会 [1][3]」）。"
    "N 是召回片段前的 [N] 编号。不得引用未在召回中出现过的编号，"
    "也不得在无任何召回片段支撑时断言用户的过去——无依据时如实说「我没有这方面的记录」。"
)

# Matches [1], [2], [1,3], [1-3], [1][3] citation tokens in answer text.
# Captures the digit runs; combined ranges/lists are expanded by the caller.
# The inner separator class covers ASCII hyphen, ASCII + full-width comma, and
# en/em-dash (LLMs occasionally emit [2–4] / [2—4] in ranges).
_CITATION_TOKEN = re.compile(r"\[(\d+(?:\s*[-,，–—]\s*\d+)*)\]")


def number_chunks(
    hits: Sequence[Mapping[str, Any]],
    *,
    max_chars: int = 200,
) -> Tuple[str, Dict[int, str]]:
    """Render recall hits as 1-based numbered chunks + an index→memo_id map.

    Mirrors danao13 ``rag_service.py:713-716``: each chunk is
    ``[idx] YYYY-MM-DD: <snippet>`` (synthetic 1-based index, Beijing-style
    date prefix trimmed to date). The index is the ONLY handle the agent sees
    — the real memo_id never enters the prompt, so a hallucinated citation
    (an index out of range) cannot leak a real id; it just fails the bounds
    check in :func:`validate_citations`.

    Args:
        hits: recall hits, each a mapping with at least ``id`` (memo_id) and
            ``source_text``; ``timestamp`` (ISO-8601) is used for the date
            prefix when present.
        max_chars: per-snippet truncation (keeps the prompt bounded; matches
            the prefetch ``text[:200]`` convention).

    Returns:
        (chunk_text, index_map): ``chunk_text`` joins the numbered lines with
        newlines (empty when ``hits`` is empty); ``index_map`` maps 1-based
        idx → memo_id (str).
    """
    if not hits:
        return "", {}
    lines: List[str] = []
    index_map: Dict[int, str] = {}
    for idx, h in enumerate(hits, start=1):
        memo_id = str(h.get("id") or h.get("memo_id") or "")
        index_map[idx] = memo_id
        snippet = (h.get("source_text") or h.get("snippet") or "").strip().replace("\n", " ")
        snippet = snippet[:max_chars]
        date_str = _date_prefix(h.get("timestamp"))
        prefix = f"[{idx}]"
        line = f"{prefix} {date_str}: {snippet}" if date_str else f"{prefix} {snippet}"
        lines.append(line)
    return "\n".join(lines), index_map


def _date_prefix(ts: Any) -> str:
    """Extract a YYYY-MM-DD date prefix from an ISO-8601 timestamp, or ''."""
    if not ts:
        return ""
    try:
        s = str(ts)
        # ISO-8601 with optional timezone; fromisoformat handles most shapes.
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001 — date is cosmetic; never break recall
        # Fall back to leading 10 chars if they look like a date.
        s = str(ts)
        return s[:10] if len(s) >= 10 and s[4] == "-" else ""


def extract_cited_indices(text: str) -> List[int]:
    """Parse all [N] / [N,M] / [N-M] citation tokens from ``text`` → sorted
    unique 1-based indices.

    Handles mixed punctuation (ASCII comma, full-width comma, hyphen, en-dash).
    Duplicates collapse; order is ascending. Returns [] when ``text`` has no
    citation tokens (which may itself be a credibility signal — see
    :func:`ground_answer`).
    """
    if not text:
        return []
    found: set = set()
    for m in _CITATION_TOKEN.finditer(text):
        group = m.group(1)
        # Split on comma (ASCII + full-width) first; each part may be a range.
        parts = re.split(r"[,，]", group)
        for part in parts:
            part = part.strip()
            if not part:
                continue
            if "-" in part or "–" in part or "—" in part:
                # Range like 1-3 → expand to 1,2,3.
                bounds = re.split(r"[-–—]", part)
                if len(bounds) == 2 and bounds[0].strip().isdigit() and bounds[1].strip().isdigit():
                    lo, hi = int(bounds[0]), int(bounds[1])
                    if lo > hi:
                        lo, hi = hi, lo
                    # Cap expansion to avoid a pathological [1-9999] token.
                    for n in range(lo, min(hi, lo + 50) + 1):
                        found.add(n)
            elif part.isdigit():
                found.add(int(part))
    return sorted(found)


def validate_citations(
    cited_indices: Sequence[int],
    index_map: Mapping[int, str],
    hits: Sequence[Mapping[str, Any]] | None = None,
) -> Tuple[List[Dict[str, Any]], List[int]]:
    """Bounds-check cited indices against the chunks actually provided.

    Mirrors danao13 ``rag_service.py:607-609, 724-731``: an index the LLM
    cited that wasn't in the provided chunks is a HALLUCINATION — dropped,
    never mapped to a real memo_id. Valid indices map to real sources.

    Args:
        cited_indices: the 1-based indices parsed from the answer.
        index_map: the idx → memo_id map from :func:`number_chunks`.
        hits: the original hits (for date/snippet in the source dict). If
            omitted, sources carry only memo_id.

    Returns:
        (sources, dropped): ``sources`` is a list of
        ``{"memo_id", "date", "snippet"}`` for each VALID citation (deduped,
        citation-order); ``dropped`` is the list of hallucinated/out-of-range
        indices (for telemetry — a high dropped count signals the LLM is
        inventing references).
    """
    # Build an idx→hit lookup for date/snippet enrichment.
    hit_by_idx: Dict[int, Mapping[str, Any]] = {}
    if hits:
        for idx, h in enumerate(hits, start=1):
            hit_by_idx[idx] = h

    sources: List[Dict[str, Any]] = []
    seen_ids: set = set()
    dropped: List[int] = []
    for idx in cited_indices:
        memo_id = index_map.get(idx)
        if memo_id is None or memo_id == "":
            dropped.append(idx)  # hallucinated — not in provided chunks
            continue
        if memo_id in seen_ids:
            continue  # dedupe repeated citations of the same chunk
        seen_ids.add(memo_id)
        h = hit_by_idx.get(idx, {})
        sources.append({
            "memo_id": memo_id,
            "date": _date_prefix(h.get("timestamp")) if h else "",
            "snippet": (h.get("source_text") or h.get("snippet") or "").strip().replace("\n", " ")[:200],
            "index": idx,
        })
    return sources, dropped


def ground_answer(
    answer_text: str,
    hits: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    """End-to-end grounding check: did the answer cite real recalled chunks?

    Wires the three steps together (number → extract → validate). Used by
    PTGProvider.sync_turn to record a per-turn credibility observation: a
    factual-sounding answer with zero valid citations is a credibility
    incident (the agent asserted the user's past without grounding).

    Args:
        answer_text: the assistant's answer.
        hits: the recall hits that were in scope for this turn (from the last
            prefetch / ptg_search).

    Returns:
        ``{"sources": [...], "dropped": [...], "cited_indices": [...],
        "has_valid_citation": bool, "n_chunks": int}``.
        ``has_valid_citation`` is True iff ≥1 cited index mapped to a real
        chunk. An answer that cites nothing (``cited_indices`` empty) is
        NOT automatically ungrounded — it may be a generic conversational
        reply with no factual claim about the past; the caller applies the
        "does this answer reference user history?" heuristic before flagging.
    """
    _chunk_text, index_map = number_chunks(hits)
    cited = extract_cited_indices(answer_text)
    sources, dropped = validate_citations(cited, index_map, hits)
    return {
        "sources": sources,
        "dropped": dropped,
        "cited_indices": cited,
        "has_valid_citation": len(sources) > 0,
        "n_chunks": len(hits),
    }


def looks_like_history_claim(answer_text: str, recalled_terms: Sequence[str]) -> bool:
    """Heuristic: does this answer make a claim about the user's past that
    SHOULD be grounded?

    Used by sync_turn to decide whether a zero-citation answer is a credibility
    incident (claim about history, no grounding) vs an acceptable generic reply
    ("好的，已记下" / "我可以帮你..."). A claim is "history-like" if it references
    a recalled entity/person/task term OR contains past-tense / recency markers.

    This is deliberately a permissive heuristic — false positives (flagging a
    grounded answer) are recoverable, false negatives (missing an ungrounded
    claim) defeat the gate. So it errs toward True.
    """
    if not answer_text:
        return False
    text = answer_text
    # Recalled entity/person/task term present → likely a claim about them.
    for term in recalled_terms:
        t = (term or "").strip()
        if t and len(t) >= 2 and t in text:
            return True
    # Past-tense / recency markers (Chinese + English).
    markers = ("上周", "昨天", "之前", "上次", "最近", "前天", "过去",
               "你说过", "你提到", "你告诉", "你表示",
               "last week", "yesterday", "before", "said", "mentioned", "told")
    low = text.lower()
    return any(m in low for m in markers)

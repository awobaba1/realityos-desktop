"""RealityOS V6 — structured recall renderer (架构 §4.3A, ADR-V6-012).

The read loop's presentation layer. ``search_memos_fts`` already recalls raw
captured turns (the base tier). This module folds the GRAPH — the entities and
self→X relations the Atomizer materialized (ADR-V6-011 决策6) — into a compact
markdown block so the model sees structured context ("你与张三互动过 3 次，最近
提及交报告") alongside the raw text. Without it, the graph the Atomizer builds is
write-only (Explore recon finding #3) — captured but never shown back.

Spec (§4.3A):
  * ``render_relations_block(store, user_id, query, token_budget=800)`` — find
    entities whose name matches the query, pull their self→X edges, render as
    markdown. Confidence DESC; type-weighted so people (interacts_with) and tasks
    (has_task) rank above plain mentions.
  * Hard ≤ ``token_budget`` cap (token estimate is a no-deps heuristic; see
    ``_estimate_tokens``). Overflow is truncated, never silently blown past.
  * Empty when no graph hits → the caller appends nothing (no noise on a fresh
    or unrelated turn).

Fail-open (C7): any store error is swallowed → empty string; recall never breaks
the conversation loop.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List

if TYPE_CHECKING:
    from .store import PTGStore

logger = logging.getLogger(__name__)

# §4.3A type weighting — people and tasks surface before plain mentions/emotion.
_RELATION_WEIGHTS = {
    "interacts_with": 3,  # person — the highest-value structured tie
    "has_task": 2,        # task — actionable, time-sensitive
    "mentions": 1,        # context/topic — background
}
_VERBS = {
    "interacts_with": "互动",
    "has_task": "有待办",
    "mentions": "提及",
}
_TYPE_LABEL = {"person": "人物", "task": "任务", "topic": "话题", "context": "地点/机构"}


def _estimate_tokens(text: str) -> int:
    """Rough token estimate with no external deps (no tiktoken in the fork).

    CJK chars ≈ 1 token each (Chinese BPE merges are ~1-2 chars/token); Latin /
    digit / punct ≈ 4 chars/token (the BPE average). Blended for mixed text.
    Used only to cap the prefetch block — never for billing.
    """
    cjk = sum(1 for c in text if "一" <= c <= "鿿")
    other = max(0, len(text) - cjk)
    return cjk + other // 4


def render_relations_block(store: "PTGStore", user_id: str, query: str,
                           *, token_budget: int = 800,
                           self_name: str = "我") -> str:
    """Render a markdown block of self→X relations relevant to ``query``.

    Returns "" when there are no matching entities (fresh/unrelated turn), so the
    caller appends nothing. Never raises (C7).
    """
    if not store or not query or not query.strip():
        return ""
    try:
        matched = store.search_entities(user_id, query, limit=5)
        if not matched:
            return ""
        matched_ids = {e["id"] for e in matched}
        # One round-trip: pull the user's top relations, keep those touching a
        # matched entity. Confidence DESC is already the store's ordering.
        all_rels = store.relations_for_user(user_id, limit=60)
        rels = [r for r in all_rels
                if r.get("subject_id") in matched_ids or r.get("object_id") in matched_ids]
        if not rels:
            return ""
    except Exception as exc:  # noqa: BLE001 — recall never breaks the loop
        logger.debug("render_relations_block query failed: %s", exc)
        return ""

    # Type-weighted, then confidence, then evidence — so a high-value person tie
    # outranks a low-value mention even at slightly lower confidence.
    rels.sort(key=lambda r: (
        -_RELATION_WEIGHTS.get(r.get("relation_type", ""), 0),
        -(r.get("confidence") or 0.0),
        -(r.get("evidence_count") or 0),
    ))

    header = "## RealityOS 图谱（你与…的关系）"
    out_lines: List[str] = [header]
    # Reserve header tokens; stop appending once the budget is consumed.
    if _estimate_tokens("\n".join(out_lines)) >= token_budget:
        return "\n".join(out_lines)

    for r in rels:
        # The non-self endpoint is the focus of each edge.
        if r.get("subject_type") == "person" and r.get("subject_name") == self_name:
            focus_name, focus_type = r.get("object_name", "?"), r.get("object_type", "")
        elif r.get("object_type") == "person" and r.get("object_name") == self_name:
            focus_name, focus_type = r.get("subject_name", "?"), r.get("subject_type", "")
        else:
            focus_name = r.get("object_name") or r.get("subject_name") or "?"
            focus_type = r.get("object_type") or r.get("subject_type") or ""
        verb = _VERBS.get(r.get("relation_type", ""), r.get("relation_type", "关联"))
        detail = r.get("value")
        conf = r.get("confidence")
        seen = r.get("evidence_count")
        parts = [f"- {self_name}与「{focus_name}」{verb}"]
        bits: List[str] = []
        if focus_type:
            bits.append(_TYPE_LABEL.get(focus_type, focus_type))
        if detail:
            bits.append(str(detail))
        if conf is not None:
            bits.append(f"置信{conf:.2f}")
        if seen and int(seen) > 1:
            bits.append(f"记录{seen}次")
        if bits:
            parts.append("（" + "，".join(bits) + "）")
        line = "".join(parts)
        tentative = "\n".join(out_lines + [line])
        if _estimate_tokens(tentative) > token_budget:
            break  # hard cap — truncate rather than blow the budget
        out_lines.append(line)
    return "\n".join(out_lines) if len(out_lines) > 1 else ""

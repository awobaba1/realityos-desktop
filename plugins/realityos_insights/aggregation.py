"""RealityOS V6 — weekly aggregation (ADR-V6-017, 架构 §4.4).

Turns one week of PTG atoms into the structured dict the weekly-mirror prompt
consumes. Pure data assembly — no LLM, no gate decisions. The
``WeeklyMirrorService`` runs the cold-start gate and decides placeholder vs
LLM; this module just reads the week's atoms (``PTGStore.recent_atoms`` with a
``[since, until)`` window, ADR-V6-016) and groups them.

The aggregation is type-aware across all eight Phase-1 atoms (R0/R1/R2/R3/R7
from Phase 1a + R8/R9/R12 from Phase 1b-1): people, tasks, task outcomes,
emotions, cognitions, self-states, expressions, and top entities. Empty
sections are omitted by the prompt, never fabricated (prompt §铁律 #5).
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List

from plugins.memory.ptg.store import PTGStore

# Caps on per-section samples handed to the LLM — keeps the prompt bounded and
# forces the model to pick the salient few rather than enumerate everything.
_MAX_PEOPLE = 6
_MAX_TASKS = 10
_MAX_EMOTIONS = 10
_MAX_COGNITIONS = 10
_MAX_SELF_STATES = 10
_MAX_EXPRESSIONS = 8
_MAX_ENTITIES = 10
_MAX_CONTEXT_LEN = 60  # truncate mention_context snippets so the prompt stays tight


def aggregate_week(
    store: PTGStore,
    *,
    user_id: str,
    week_start: str,
    week_end: str,
) -> Dict[str, Any]:
    """Assemble one week of atoms into the mirror-input dict.

    ``week_start``/``week_end`` are ISO-8601 strings defining the half-open
    window ``[week_start, week_end)``. Reads only non-deleted atoms in window
    (``recent_atoms`` respects soft-delete + the window). Never raises — a read
    failure yields an empty aggregation so the service can still emit a
    placeholder (C7).
    """
    try:
        atoms = store.recent_atoms(
            user_id=user_id, since=week_start, until=week_end, limit=1000)
    except Exception:  # noqa: BLE001 — aggregation must never break the service
        atoms = []

    counts: Counter = Counter(a["type"] for a in atoms)

    people = _top_people(atoms)
    tasks = _tasks(atoms)
    task_outcomes = _task_outcomes(atoms)
    emotions = _emotions(atoms)
    cognitions = _cognitions(atoms)
    self_states = _self_states(atoms)
    expressions = _expressions(atoms)
    top_entities = _top_entities(atoms)

    return {
        "atom_counts": dict(counts),
        "atom_total": int(sum(counts.values())),
        "people": people,
        "tasks": tasks,
        "task_outcomes": task_outcomes,
        "emotions": emotions,
        "cognitions": cognitions,
        "self_states": self_states,
        "expressions": expressions,
        "top_entities": top_entities,
    }


def _top_people(atoms: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """R3_Person grouped by name (count + a representative context snippet)."""
    per_name: Dict[str, Dict[str, Any]] = {}
    for a in atoms:
        if a.get("type") != "R3_Person":
            continue
        name = (a.get("person_name") or "").strip()
        if not name:
            continue
        slot = per_name.setdefault(
            name, {"name": name, "count": 0, "context": ""})
        slot["count"] += 1
        if not slot["context"] and a.get("mention_context"):
            slot["context"] = _clip(a["mention_context"])
    out = sorted(per_name.values(), key=lambda x: x["count"], reverse=True)
    return out[:_MAX_PEOPLE]


def _tasks(atoms: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for a in atoms:
        if a.get("type") != "R2_Task":
            continue
        desc = (a.get("task_description") or "").strip()
        if not desc:
            continue
        out.append({"desc": desc, "urgency": a.get("urgency"),
                    "deadline": a.get("deadline")})
        if len(out) >= _MAX_TASKS:
            break
    return out


def _task_outcomes(atoms: List[Dict[str, Any]]) -> Dict[str, int]:
    """R12_Outcome distribution by outcome (completed/failed/delayed)."""
    counts = Counter(
        (a.get("outcome") or "completed")
        for a in atoms if a.get("type") == "R12_Outcome")
    # Stable key order so the prompt is deterministic.
    return {k: counts.get(k, 0) for k in ("completed", "failed", "delayed")}


def _emotions(atoms: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for a in atoms:
        if a.get("type") != "R9_Emotion":
            continue
        out.append({"label": a.get("emotion_label"), "valence": a.get("valence"),
                    "arousal": a.get("arousal"), "trigger": _clip(a.get("trigger") or ""),
                    "intensity": a.get("intensity")})
        if len(out) >= _MAX_EMOTIONS:
            break
    return out


def _cognitions(atoms: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for a in atoms:
        if a.get("type") != "R8_Cognition":
            continue
        topic = (a.get("topic") or "").strip()
        if not topic:
            continue
        out.append({"topic": topic, "tags": a.get("knowledge_tags") or [],
                    "engagement": a.get("engagement"),
                    "is_question": a.get("is_question")})
        if len(out) >= _MAX_COGNITIONS:
            break
    return out


def _self_states(atoms: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for a in atoms:
        if a.get("type") != "R1_SelfState":
            continue
        out.append({"state": a.get("state_type"), "direction": a.get("direction"),
                    "intensity": a.get("intensity")})
        if len(out) >= _MAX_SELF_STATES:
            break
    return out


def _expressions(atoms: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for a in atoms:
        if a.get("type") != "R7_Expression":
            continue
        out.append({"intent": a.get("intent_class"),
                    "summary": _clip(a.get("content_summary") or "")})
        if len(out) >= _MAX_EXPRESSIONS:
            break
    return out


def _top_entities(atoms: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """R0_Entity grouped by name (count + category)."""
    per_name: Dict[str, Dict[str, Any]] = {}
    for a in atoms:
        if a.get("type") != "R0_Entity":
            continue
        name = (a.get("entity_name") or "").strip()
        if not name:
            continue
        slot = per_name.setdefault(
            name, {"name": name, "category": a.get("entity_category"),
                   "count": 0})
        slot["count"] += 1
    out = sorted(per_name.values(), key=lambda x: x["count"], reverse=True)
    return out[:_MAX_ENTITIES]


def _clip(text: str) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text[:_MAX_CONTEXT_LEN] + ("…" if len(text) > _MAX_CONTEXT_LEN else "")

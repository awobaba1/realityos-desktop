"""RealityOS V6 — capture-event schemas (the post_tool_call → tool_events gate).

This is the §9#4 + §0.6 capture surface. Every tool the agent runs on the user's
behalf is observed by the ``post_tool_call`` hook and sunk into ``tool_events``
as a personal-timeline asset (流经即捕获 — turns AND tool executions). A
``CaptureEvent`` validates the hook payload's shape BEFORE the sink so a
malformed dispatch (a plugin emitting junk kwargs, a shape change in
``model_tools._emit_post_tool_call_hook``) goes to the DLQ, never silently
written as garbage (C7-adjacent — capture is observation, but we still refuse to
persist a structurally-invalid row).

NOT to be confused with the HL-12 atom schemas (``atom_schemas.py``), which are
the C5 gate for LLM *extraction output*. ``CaptureEvent`` gates raw tool I/O;
the LLM-driven semantic extraction from a tool event (Phase 2 quark extractor
filling ``quark_evidence``) is what sits behind the real C5 gate downstream.

Size discipline (PIPL §6 minimization): ``CaptureEvent`` carries ``tool_args``
and ``result_summary`` already capped by the caller — a ``web_fetch`` body is
never stored whole. The caps live here so the sink can't accidentally bypass them.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator

# PIPL §6 minimization caps. tool_args holds the call's input (may include user
# text → L3); result_summary holds a truncated echo of the output. Neither is the
# raw full payload — a multi-MB web page or terminal dump is reduced before sink.
MAX_TOOL_ARGS_CHARS = 4096
MAX_RESULT_SUMMARY_CHARS = 2048


class CaptureEvent(BaseModel):
    """A validated tool-execution capture, ready to sink into ``tool_events``.

    Every field maps 1:1 to a ``tool_events`` column. The hook builds this from
    the ``post_tool_call`` payload; ``CaptureEvent.from_hook_kwargs`` is the
    canonical constructor and applies the size caps + status normalization.
    """

    tool_name: str = Field(min_length=1, max_length=200)
    session_id: Optional[str] = Field(default=None, max_length=200)
    status: str = Field(pattern="^(ok|error)$")
    duration_ms: int = Field(default=0, ge=0)
    error_type: Optional[str] = Field(default=None, max_length=200)
    error_msg: Optional[str] = Field(default=None, max_length=4000)
    tool_args: dict = Field(default_factory=dict)
    result_summary: Optional[dict] = Field(default=None)
    extracted_via: str = Field(default="post_tool_call", max_length=50)
    quark_evidence: list = Field(default_factory=list)

    @field_validator("tool_name")
    @classmethod
    def _nonempty_tool_name(cls, v: str) -> str:
        # The hook may pass "<unknown>" when function_name was absent; that is a
        # real capture we still want to record, so allow it through (min_length=1
        # already rejects the empty string).
        return v

    @staticmethod
    def from_hook_kwargs(kwargs: dict) -> "CaptureEvent":
        """Build a CaptureEvent from a ``post_tool_call`` payload dict.

        Normalizes the hook's ok/error fields (``status`` derived from
        ``error_type`` when the hook omits it — same logic as
        ``model_tools._emit_post_tool_call_hook``), caps oversized payloads
        (PIPL §6), and coerces ``tool_args``/``result`` to JSON-able dicts.
        Raises ``ValidationError`` on a structurally-bad payload — the caller
        routes that to the DLQ (C7), never a silent drop.
        """
        tool_name = str(kwargs.get("tool_name") or kwargs.get("function_name")
                        or "<unknown>")
        status = kwargs.get("status")
        if status is None:
            status = "error" if kwargs.get("error_type") else "ok"
        # result → result_summary (truncated). Keep it a dict so it round-trips
        # through JSON; non-dict results are wrapped so the column stays uniform.
        raw_result = kwargs.get("result")
        result_summary: Optional[dict]
        if raw_result is None:
            result_summary = None
        elif isinstance(raw_result, dict):
            result_summary = _cap_dict(raw_result, MAX_RESULT_SUMMARY_CHARS)
        else:
            # Strings (terminal output, web body) get truncated in-place; other
            # types are stringified under a "value" key so the column is always JSON.
            text = raw_result if isinstance(raw_result, str) else str(raw_result)
            result_summary = {"value": text[:MAX_RESULT_SUMMARY_CHARS]}

        raw_args = kwargs.get("args")
        tool_args = raw_args if isinstance(raw_args, dict) else {}
        tool_args = _cap_dict(tool_args, MAX_TOOL_ARGS_CHARS)

        return CaptureEvent(
            tool_name=tool_name,
            session_id=(str(kwargs.get("session_id"))[:200]
                        if kwargs.get("session_id") else None),
            status=status,
            duration_ms=int(kwargs.get("duration_ms") or 0),
            error_type=(str(kwargs.get("error_type"))[:200]
                        if kwargs.get("error_type") else None),
            error_msg=(str(kwargs.get("error_message")
                          or kwargs.get("error_msg"))[:4000]
                       if (kwargs.get("error_message") or kwargs.get("error_msg"))
                       else None),
            tool_args=tool_args,
            result_summary=result_summary,
            extracted_via="post_tool_call",
            quark_evidence=[],
        )


def _cap_dict(d: dict, max_chars: int) -> dict:
    """Truncate a dict's JSON serialization to ``max_chars`` by dropping the
    last keys (preserving JSON structure). PIPL §6 minimization for tool I/O."""
    import json
    try:
        encoded = json.dumps(d, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return {"_unserializable": True}
    if len(encoded) <= max_chars:
        return d
    # Oversized: keep the leading keys that fit, mark truncation.
    kept: dict[str, Any] = {}
    running = 2  # "{}"
    for k, v in d.items():
        chunk = json.dumps({str(k): v}, ensure_ascii=False, default=str)
        if running + len(chunk) + 1 > max_chars - 30:  # leave room for the marker
            break
        running += len(chunk) + 1
        kept[str(k)] = v
    kept["_truncated"] = True
    return kept

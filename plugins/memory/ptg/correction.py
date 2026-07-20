"""Source-text correction + re-extraction orchestration (ADR-V6-047 / A4).

The closed loop: user corrects a memo's ASR/typo errors → re-run extraction on
the corrected text → retire the old (wrong) atoms only on success → invalidate
insights. Ported from danao13 ADR-056 ``run_re_extraction_pipeline``, adapted to
V6's atomizer (which is extract+write-combined, not split).

The C2 ordering (danao13 ADR-079 "写后删") is preserved: the OLD atoms survive a
FAILED re-extraction. V6 can't hold the SQLite lock across the LLM call (that
would block every other thread for ~seconds), so the V6-honest sequence is:

  1. ``correct_memo_source_text`` (pure-CRUD, updates corrected_text + version).
  2. ``snapshot_memo_atom_ids`` — capture OLD live atom ids (quick SELECT).
  3. ``atomizer.atomize`` on the corrected text — writes NEW atoms (same memo_id).
  4. ONLY if atomize ``ok``: ``soft_delete_atom_ids`` on the OLD ids (by exact id,
     so the new atoms are untouched) + ``invalidate_insights``.

A failed re-extraction leaves the OLD atoms live (corrected_text recorded, no
harm) — the user retries or the original atoms stand. Never raises (C7); returns
a result dict the CLI surfaces.

Self-learning (ASR hotword injection / LLM vocab injection — ADR-056's three
layers) is DEFERRED to Phase 2.5 (声学); the honest A4 minimum is "record
correction → re-extract → retire wrong atoms → refresh insights".
"""

from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


def re_extract_memo(
    store, atomizer, *,
    user_id: str, memo_id: str, corrected_text: str,
    actor: str = "user", reason: str = "user_correction",
    expected_version: int = None,
) -> Dict[str, Any]:
    """Correct a memo's source text and re-extract its atoms (ADR-V6-047 / A4).

    ``atomizer`` is an ``Atomizer`` built the same way the PTG provider builds
    one (user_id-scoped, materialize_graph per the caller). Returns
    ``{ok, status, memo_id, version, written, retired_old, invalidated, message}``.
    Never raises (C7) — every failure path is logged and surfaced in the dict.
    """
    corr = store.correct_memo_source_text(
        user_id=user_id, memo_id=memo_id, corrected_text=corrected_text,
        actor=actor, reason=reason, expected_version=expected_version)
    if not corr.get("ok"):
        return {**corr, "written": 0, "retired_old": 0, "invalidated": 0,
                "message": _status_msg(corr.get("status"))}
    if corr.get("status") == "unchanged":
        return {**corr, "written": 0, "retired_old": 0, "invalidated": 0,
                "message": "文本未变化，无需重新提取。"}

    # 写后删: snapshot OLD atoms before re-writing, so we can retire exactly
    # those (by id) after the new extraction lands — new atoms survive.
    old = store.snapshot_memo_atom_ids(user_id, memo_id)

    try:
        res = atomizer.atomize(
            memo_id=memo_id, source_text=corrected_text, input_mode="text")
    except Exception as exc:  # noqa: BLE001 — atomize itself is C7, but be safe
        logger.warning("re_extract_memo atomize failed (%s): %s", memo_id, exc)
        return {"ok": False, "status": "atomize_error", "memo_id": memo_id,
                "version": corr.get("version"), "written": 0, "retired_old": 0,
                "invalidated": 0, "message": "重新提取失败，旧原子保留，稍后再试。"}

    if not res.get("ok"):
        # OLD atoms deliberately survive (写后删) — extraction failed.
        logger.info("re_extract_memo atomize not-ok (%s): old atoms kept", memo_id)
        return {"ok": False, "status": "atomize_failed", "memo_id": memo_id,
                "version": corr.get("version"), "written": res.get("written", 0),
                "retired_old": 0, "invalidated": 0,
                "message": "重新提取未成功，旧原子保留，稍后再试。"}

    retired = store.soft_delete_atom_ids(
        user_id=user_id, ids_by_table=old, actor=actor,
        reason=f"memo_corrected_re_extract ({reason})")
    invalidated = store.invalidate_insights(user_id)
    logger.info("re_extract_memo ok memo=%s written=%s retired_old=%s inval=%s",
                memo_id, res.get("written", 0), retired, invalidated)
    return {
        "ok": True, "status": "re_extracted", "memo_id": memo_id,
        "version": corr.get("version"), "written": res.get("written", 0),
        "retired_old": retired, "invalidated": invalidated,
        "message": (f"已纠正并重新提取：写入 {res.get('written', 0)} 原子，"
                    f"旧原子软删 {retired} 条。"),
    }


def _status_msg(status: str) -> str:
    return {
        "not_found": "找不到这条 memo。",
        "deleted": "这条 memo 已删除，无法纠正。",
        "version_conflict": "版本冲突：memo 已被改动，刷新后再试。",
        "error": "操作没成功，稍后再试。",
    }.get(status, "操作没成功。")

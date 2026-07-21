"""C4 guard: C6 ``llm_call_logs`` reality scope boundary (ADR-V6-074).

ADR-V6-074 nails down the C6 contract: ``llm_call_logs`` (WORM, replayable
reality) is the scope of LLM calls whose OUTPUT becomes a reality row in the
PTG store. The four reality engines (atomizer / quark / theory / insights)
own that contract — each has a ``_llm()`` that delegates transport to
``auxiliary_client.call_llm`` AND records via ``store.insert_llm_call_log``
AND threads ``llm_call_id`` to its events.

Auxiliary/export paths (``query_rewrite`` / ``teams_pipeline``) do NOT write
PTG reality rows — they're correctly OUT of ``llm_call_logs`` scope (stdlib
``logger.info`` observability only). This module pins BOTH halves of the
boundary so it can't silently rot in either direction:

- **forward**: drop the guard if an engine stops logging (real C6 regression).
- **reverse**: drop the guard RED if someone wires an auxiliary path into PTG
  reality without adding the logging that then becomes mandatory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Reality-affecting engines — output lands in PTG reality tables, so C6
# replayability (llm_call_logs + llm_call_id) is MANDATORY.
REALITY_ENGINES = [
    "plugins/memory/ptg/atomizer.py",
    "plugins/realityos_quark/extractor.py",
    "plugins/realityos_theory/engine.py",
    "plugins/realityos_insights/_base.py",
]

# Auxiliary/export paths — output is a retrieval query / external-sink payload,
# NOT a PTG reality row. Out of llm_call_logs scope by design (ADR-V6-074).
AUXILIARY_NON_REALITY = [
    "plugins/memory/query_rewrite.py",
    "plugins/teams_pipeline/pipeline.py",
]


@pytest.mark.parametrize("engine", REALITY_ENGINES)
class TestRealityEnginesLogC6:
    def _src(self, engine) -> str:
        return Path(engine).read_text(encoding="utf-8")

    def test_engine_records_llm_call_log(self, engine):
        """Every reality engine MUST ``insert_llm_call_log`` — C6 replayable
        reality. A missing call is a real C6 regression, not a style issue."""
        src = self._src(engine)
        assert "self._store.insert_llm_call_log(" in src, (
            f"{engine}: reality engine must record to llm_call_logs (C6). "
            f"If it delegates to another engine's _llm, that's fine — but "
            f"the call must exist somewhere on the reality path.")

    def test_engine_threads_llm_call_id(self, engine):
        """Reality engine output (events/atoms/insights) MUST carry
        ``llm_call_id`` for traceability (C6)."""
        src = self._src(engine)
        assert "llm_call_id" in src, (
            f"{engine}: reality engine must thread llm_call_id to its "
            f"output rows (C6 traceability).")

    def test_engine_uses_auxiliary_transport(self, engine):
        """Reality engines reuse ``auxiliary_client.call_llm`` as the HTTP
        transport (not a bespoke client). The engine's ``_llm`` wrapper adds
        the C6 logging the bare transport doesn't provide."""
        src = self._src(engine)
        assert "auxiliary_client" in src or "call_llm" in src, (
            f"{engine}: reality engine should reuse the shared call_llm "
            f"transport rather than a bespoke HTTP client.")


@pytest.mark.parametrize("path", AUXILIARY_NON_REALITY)
class TestAuxiliaryPathsOutOfRealityScope:
    def _src(self, path) -> str:
        return Path(path).read_text(encoding="utf-8")

    def test_does_not_write_ptg_reality(self, path):
        """query_rewrite / teams_pipeline are auxiliary/export — they MUST NOT
        write PTG reality rows, so they're correctly out of llm_call_logs
        scope (ADR-V6-074). If someone wires these into PTG, C6 logging
        becomes mandatory and this assertion goes RED to force it (never
        silently passes)."""
        src = self._src(path)
        assert "PTGStore" not in src, (
            f"{path} now references PTGStore — if it writes reality rows, "
            f"it MUST log to llm_call_logs + thread llm_call_id (C6). Either "
            f"add that logging or keep it auxiliary (ADR-V6-074).")
        assert "insert_llm_call_log" not in src, (
            f"{path} calls insert_llm_call_log but is classified auxiliary "
            f"(ADR-V6-074) — reclassify or reconcile the boundary.")

    def test_uses_stdlib_observability(self, path):
        """Auxiliary paths use stdlib logging for observability (not the WORM
        llm_call_logs table). Pinning the observability channel so it isn't
        mistaken for a missing C6 record."""
        src = self._src(path)
        assert "logger" in src, (
            f"{path}: auxiliary path should use stdlib logger for "
            f"observability (the auxiliary call_llm transport logs "
            f"task/provider/model via logger.info).")


class TestC6ScopeBoundary:
    def test_auxiliary_set_matches_reality(self):
        """Static integrity: the engine set and auxiliary set are disjoint and
        non-empty — the boundary is real, not a vacuous guard."""
        engines = {Path(e).stem for e in REALITY_ENGINES}
        aux = {Path(a).stem for a in AUXILIARY_NON_REALITY}
        assert engines and aux, "both sets must be populated"
        assert engines.isdisjoint(aux), (
            "a module is in both engine and auxiliary sets — boundary is "
            "contradictory")

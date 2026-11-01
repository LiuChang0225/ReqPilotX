"""Optional pinned real OpenKB agent construction; never runs a paid call.

This is an extension point for an approved host runner, not a mock runtime.
Unavailable dependencies, uncompiled Wiki or changed upstream contracts fail
explicitly. The final proposal is compiled through OpenKBAnalysisService.
"""
from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path

from .errors import AdapterError
from .service import read
from .knowledge_store import digest

UPSTREAM_COMMIT = "ff54396e575ee6feb0113b631a34caa082b441cc"
QUERY_MODULE_SHA256 = "cec427a29b0e9eeaf803fda2de79767ec35143e369fb2086df5f35901b7b048b"


def build_analysis_query_agent(service, session_id, model, *, bundle=None):
    try:
        from agents import function_tool
        from openkb.agent.query import build_query_agent
    except ImportError as exc:
        raise AdapterError("The approved pinned OpenKB/Agents runtime is unavailable; no live analysis was run") from exc
    module = inspect.getsourcefile(build_query_agent)
    if not module or hashlib.sha256(Path(module).read_bytes()).hexdigest() != QUERY_MODULE_SHA256:
        raise AdapterError("OpenKB query contract differs from the audited upstream pin; re-audit required")
    directory, session = service._session(session_id)
    service._assert_current(session)
    binding = service.store.prepare_openkb_workspace(session["generation"])
    wiki = service.store.inside(binding["wiki_relative_path"])
    if not wiki.is_dir() or not (wiki / "index.md").is_file() or any(path.is_symlink() for path in wiki.rglob("*")):
        raise AdapterError("A compiled, host-bound same-generation OpenKB Wiki is required")
    base = build_query_agent(str(wiki), model, language="zh", bundle=bundle)

    @function_tool
    def read_registered_original(document_id: str) -> str:
        """Read actual registered original facts and persist issued evidence IDs."""
        return json.dumps(service.read_original(session_id, document_id), ensure_ascii=False)

    @function_tool
    def read_customer_analysis_units() -> str:
        """Read the frozen customer requirements and complete declared context."""
        _, current = service._session(session_id)
        service._assert_current(current)
        packet = read(directory / "packet/analysis-packet.json")
        if packet["packet_sha256"] != current["packet_sha256"] or digest({k: v for k, v in packet.items() if k != "packet_sha256"}) != current["packet_sha256"]:
            raise AdapterError("Frozen customer packet version changed")
        value = {"packet_sha256": packet["packet_sha256"], "units": packet["units"], "catalog_gaps": packet["catalog_gaps"]}
        encoded = json.dumps(value, ensure_ascii=False)
        if len(encoded.encode()) > 2 * 1024 * 1024:
            raise AdapterError("Customer unit context exceeds explicit tool budget; select a narrower unit scope")
        return encoded

    from reqpilotx.application.agent_workflow.source_item_analysis import ANALYSIS_SKILL
    contract = {"schema_version": "reqpilotx-openkb-assessment/v1", "project_id": session["project_id"],
        "kb": session["kb"], "generation": session["generation"], "packet_sha256": session["packet_sha256"],
        "decisions": [{"unit_id": uid, "decision": "PENDING_EVIDENCE", "reason": "State the actual proposed comparison.",
            "evidence_ids": [], "conditions": [], "conflicts": [], "missing": [], "clarification_question": "State the needed evidence."}
            for uid in session["unit_ids"]]}
    instructions = (base.instructions or "") + "\n\n" + ANALYSIS_SKILL
    instructions += ("\nFor this analysis task, use Wiki only for discovery. Read all selected customer units with "
        "read_customer_analysis_units and read_registered_original for actual product/project evidence. "
        "Declared referenced customer context is not an additional obligation or a new compliance decision. "
        "Return exactly the JSON envelope below with every selected unit once. Only evidence_ids returned "
        "by read_registered_original may be selected. Keep conditions, conflicts and missing coverage explicit. "
        "This extension uses evidence_ids for citation selection; the host maps them to native fact_id/exact quote pairs. "
        "No tool can accept an engineer judgment. Final answer contract: " + json.dumps(contract, ensure_ascii=False))
    instructions += "\nHost original index: " + json.dumps(service.source_index(session_id), ensure_ascii=False)
    return base.clone(tools=[*base.tools, read_registered_original, read_customer_analysis_units], instructions=instructions)

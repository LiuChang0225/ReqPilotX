"""Compatibility wrapper for shared original-read evidence validation."""
import json
from pathlib import Path
from .errors import AdapterError
from reqpilotx.application.openkb_originals import OriginalEvidenceBridge

def build_evidence_query_agent(store, wiki_root, model, *, generation=None, bundle=None):
    """Optional real upstream extension; not exercised against a live model here.

    Uses pinned OpenKB build_query_agent/Agent.clone; no upstream modifications.
    Requires the separately installed, approved OpenKB runtime dependencies.
    """
    bridge = OriginalEvidenceBridge(store, generation)
    binding = store.prepare_openkb_workspace(bridge.generation)
    expected = store.inside(binding["wiki_relative_path"])
    if Path(wiki_root).absolute() != expected or not expected.is_dir():
        raise AdapterError("Only the host-bound generation Wiki directory may be queried")
    if any(path.is_symlink() for path in expected.rglob("*")):
        raise AdapterError("Wiki contains an unsafe symbolic link")
    from agents import function_tool
    from openkb.agent.query import build_query_agent

    base = build_query_agent(str(wiki_root), model, bundle=bundle)

    @function_tool
    def read_registered_original(document_id: str) -> str:
        """Read host-registered original facts and their immutable references.

        Wiki text is discovery only. Cite only evidence_ids returned by this tool.
        """
        return json.dumps(bridge.read_registered_original(document_id), ensure_ascii=False)

    instructions = (base.instructions or "") + "\n\nOriginal evidence index: " + json.dumps(bridge.source_index(), ensure_ascii=False)
    instructions += ("\nUse the Wiki for discovery, then read_registered_original for evidence. "
        "Return a JSON object with schema_version=openkb-evidence-answer/v1, project_id, kb, generation, "
        "answer, evidence_ids, rationale, conditions, conflicts, missing. Only select evidence_ids returned "
        "by read_registered_original. Preserve conditions, uncertainty and missing material. "
        "This is a proposal, not engineer acceptance. No tool can accept a judgment.")
    return base.clone(tools=[*base.tools, read_registered_original], instructions=instructions), bridge

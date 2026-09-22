"""Local algorithm capture in the same project, without replacing its source map."""
from __future__ import annotations

import time
from collections import Counter

from pydantic import BaseModel, ConfigDict, Field

from reqpilotx_server.adapters.persistence.sql_source_capture import SqlSourceCaptureRepository
from reqpilotx_server.application.source_capture import capture_current_document_source
from reqpilotx_server.application.fast_source_capture import _capture_gaps, _page_facts
from reqpilotx_server.application.extraction.resolve_structure import build_structure_tree
from reqpilotx_server.application.agent_workflow.source_requirement_baseline import canonical_json_sha256
from reqpilotx_server.application.transport import ResponseSink


class FastSourceCaptureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def capture_project_source_fast(project_id: str, document_id: str,
                                payload: FastSourceCaptureRequest, response: ResponseSink) -> dict:
    """Capture and inspect; no Agent, classification, or formal source mutation."""
    from reqpilotx_server.application.agent_workflow.native_structure_bridge import _no_store_headers
    response.headers.update(_no_store_headers())
    started = time.perf_counter()
    repository = SqlSourceCaptureRepository()
    existing = repository.get_capture(project_id=project_id, sor_document_id=document_id,
                                      source_sha256=payload.expected_source_sha256)
    capture = capture_current_document_source(project_id=project_id, sor_document_id=document_id,
        expected_source_sha256=payload.expected_source_sha256, repository=repository)
    source_ir = capture.source_ir
    pages = [n.native_locator.page_number for n in source_ir.nodes if n.native_locator.page_number]
    from reqpilotx_server.application.source_capture import require_current_source_document
    from reqpilotx_server.application.source_runtime import resolve_source_record_path
    document = require_current_source_document(project_id=project_id, sor_document_id=document_id,
                                               expected_source_sha256=payload.expected_source_sha256)
    annotations = None
    page_count = max(pages) if pages else None
    if str(document.get("file_type", "")).upper() == "PDF":
        from reqpilotx_server.application.pdf_annotations import capture_pdf_annotations
        annotations = capture_pdf_annotations(resolve_source_record_path(document), expected_sha256=source_ir.source_sha256)
        page_count = annotations["page_count"]
    proposal = build_structure_tree(source_ir) if source_ir.nodes else None
    value = {"schema_version": "project-fast-source-capture/v1", "project_id": project_id,
        "document_id": document_id, "source_sha256": source_ir.source_sha256,
        "source_ir_id": source_ir.source_ir_id, "capture_id": capture.capture_id,
        "cache_reused": existing is not None, "capture_status": source_ir.status.value,
        "semantic_status": "NOT_CLASSIFIED", "mode": "LOCAL_ALGORITHMS_NO_AI",
        "page_facts": _page_facts(source_ir, page_count), "pdf_annotations": annotations,
        "gaps": _capture_gaps(source_ir), "capture_counts": dict(Counter(n.kind.value for n in source_ir.nodes)),
        "source_ir": source_ir.model_dump(mode="json"),
        "structure_proposal": proposal.model_dump(mode="json") if proposal else None,
        "structure_authority": "UNACCEPTED_ALGORITHM_PROPOSAL",
        "source_map_changed": False, "source_ledger_changed": False,
        "elapsed_seconds": round(time.perf_counter() - started, 3)}
    value["capture_packet_sha256"] = canonical_json_sha256(value)
    return value

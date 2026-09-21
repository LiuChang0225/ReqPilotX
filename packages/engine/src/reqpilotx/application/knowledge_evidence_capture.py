"""Pure source-bound knowledge capture for application and portable hosts.

The caller supplies a registered document record and its actual original bytes.
Capture verifies project and source identity, preserves exact locations and
applies the shared evidence lifecycle. It does not create document authority or
product applicability, and it has no database or project-store dependency.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any

from reqpilotx.application.build_source_ir import build_source_ir
from reqpilotx.application.extraction.ingest_document import inspect_document_for_ingestion
from reqpilotx.domain.extraction.preflight import DocumentFormat
from reqpilotx.domain.extraction.text import decode_plain_text_strict
from reqpilotx.domain.knowledge.lifecycle import evaluate_knowledge_evidence_eligibility


class KnowledgeFactsError(ValueError):
    pass


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def capture_knowledge_facts(record: dict, source_path: str | Path, *,
                            project_id: str, as_of: date | None = None) -> dict:
    """Capture one registered original; this does not assign product applicability."""
    if record.get("project_id") != project_id or not record.get("id"):
        raise KnowledgeFactsError("Knowledge document does not belong to this project")
    source_sha = str(record.get("source_sha256") or "")
    report = inspect_document_for_ingestion(source_path)
    if not source_sha or report.source_sha256 != source_sha:
        raise KnowledgeFactsError("Knowledge source bytes do not match the registered SHA256")
    eligibility = evaluate_knowledge_evidence_eligibility(record, as_of=as_of)
    metadata = {key: record.get(key) or "" for key in (
        "id", "file_name", "doc_version", "doc_type", "status", "authority",
        "claim_role", "applicability", "effective_from", "effective_to",
    )}
    metadata.update(project_id=project_id, source_sha256=source_sha,
                    eligible=eligibility.allowed, eligibility_reason=eligibility.reason)
    result: dict[str, Any] = {"document": metadata, "facts": {}, "gaps": []}
    # Inactive/expired material stays visible in the inventory, but cannot be
    # silently used as current evidence by either a model or a retrieval backend.
    if not eligibility.allowed:
        return result

    def add(text: str, source_ref: dict, *, table: dict | None = None) -> None:
        if not text.strip():
            return
        text_sha = hashlib.sha256(text.encode()).hexdigest()
        identity = {"document_id": record["id"], "source_sha256": source_sha,
                    "source_ref": source_ref, "text_sha256": text_sha}
        fact_id = "knowledge-fact-" + _digest(identity)[:32]
        role = ("PRODUCT_EVIDENCE" if record.get("claim_role") in {
            "DESIGN_DECLARATION", "VALIDATION_RESULT", "TEST_RESULT",
            "CERTIFICATION", "COMPLIANCE_RECORD", "RELEASED_DESIGN_EVIDENCE",
        } else "INTERNAL_KNOWLEDGE")
        result["facts"][fact_id] = {
            **identity, "fact_id": fact_id, "role": role, "text": text,
            "document_name": metadata["file_name"], "document_version": metadata["doc_version"],
            "authority": metadata["authority"], "claim_role": metadata["claim_role"],
            "applicability": metadata["applicability"], "eligible": True,
            "authority_verified": metadata["authority"] not in {"", "UNKNOWN"},
            "capture_mode": "NATIVE_SOURCE", **({"table": table} if table else {}),
        }

    if report.identity.detected_format is DocumentFormat.TXT:
        text = decode_plain_text_strict(Path(source_path).read_bytes())
        # A whole plain-text source avoids manufacturing sentence boundaries.
        add(text, {"format": "txt", "start_offset": 0, "end_offset": len(text)})
    else:
        snapshot = build_source_ir(source_path, preflight_report=report)
        result["source_ir_id"] = snapshot.source_ir_id
        result["capture_status"] = snapshot.status.value
        for node in snapshot.nodes:
            if node.parent_source_node_id is not None:
                continue
            ref = {"source_node_id": node.source_node_id,
                   "location": node.native_locator.model_dump(mode="json", exclude_none=True)}
            if node.table_payload is not None:
                table = node.table_payload.model_dump(mode="json")
                # Exact cell values remain separately addressable, and the whole
                # table accompanies every citation; physical cells aren't claims.
                text = "\n".join(str(cell.raw_text or cell.display_value or "")
                                 for cell in sorted(node.table_payload.cells,
                                                    key=lambda c: (c.row, c.column)))
                add(text, ref, table=table)
            elif node.raw_text.strip():
                add(node.raw_text, ref)
            elif node.asset_ref:
                result["gaps"].append({"code": "VISUAL_EVIDENCE_REQUIRES_READING",
                                       "source_ref": ref, "asset_ref": node.asset_ref})
        result["gaps"].extend({"code": d.code, "message": d.message}
                              for d in snapshot.diagnostics)
    if not result["facts"]:
        result["gaps"].append({"code": "NO_NATIVE_TEXT_FACTS"})
    return result

from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path

from app.adapters.agents.codex_app_server.native_semantic_provider import (
    _AssemblyPayload,
    _ClassificationPayload,
)
from app.adapters.agents.codex_app_server.semantic_mcp import _JointPayload
from app.adapters.agents.codex_app_server.semantic_app_server import (
    LocalCodexAppServerSemanticDriver,
)
from app.domain.extraction.requirement_assembly import RequirementAssemblyStatus
from app.domain.extraction.requirement_view import RequirementProjectionRole
from app.domain.extraction.semantics import RequirementSubtype, SemanticClass
from app.domain.extraction.structure import (
    StructureNodeKind,
    StructureSourceRef,
    StructureSourceRefKind,
)
from app.ports.semantic_provider import (
    RequirementAssemblyNodeInput,
    RequirementAssemblyRequest,
    SemanticClassificationRequest,
    SemanticEvidenceInput,
    SemanticImageInput,
    SemanticNodeInput,
)


TARGET = "structure-node-" + "1" * 32
SOURCE_REF = StructureSourceRef(
    kind=StructureSourceRefKind.SOURCE_NODE,
    source_id="node-" + "2" * 32,
)
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _classification_request() -> SemanticClassificationRequest:
    text = "The component shall withstand 12 V continuously."
    evidence = SemanticEvidenceInput(
        source_ref=SOURCE_REF,
        text=text,
        text_sha256=hashlib.sha256(text.encode()).hexdigest(),
    )
    return SemanticClassificationRequest(
        source_ir_id="source-ir-semantic-canary",
        source_sha256="3" * 64,
        structure_snapshot_id="structure-semantic-canary",
        scope_structure_node_ids=(TARGET,),
        nodes=(
            SemanticNodeInput(
                target_structure_node_id=TARGET,
                source_order=0,
                kind=StructureNodeKind.CLAUSE,
                image_page_numbers=(1,),
                target_source_refs=(SOURCE_REF,),
                evidence=(evidence,),
            ),
        ),
        images=(
            SemanticImageInput(
                source_sha256="3" * 64,
                page_number=1,
                width_px=1,
                height_px=1,
                image_bytes=PNG_1X1,
                image_sha256=hashlib.sha256(PNG_1X1).hexdigest(),
            ),
        ),
    )


def _assembly_request() -> RequirementAssemblyRequest:
    text = "The component shall withstand 12 V continuously."
    evidence = SemanticEvidenceInput(
        source_ref=SOURCE_REF,
        text=text,
        text_sha256=hashlib.sha256(text.encode()).hexdigest(),
    )
    return RequirementAssemblyRequest(
        source_ir_id="source-ir-semantic-canary",
        structure_snapshot_id="structure-semantic-canary",
        semantic_snapshot_id="semantic-semantic-canary",
        scope_structure_node_ids=(TARGET,),
        nodes=(
            RequirementAssemblyNodeInput(
                target_structure_node_id=TARGET,
                source_order=0,
                kind=StructureNodeKind.CLAUSE,
                semantic_annotation_ids=("semantic-annotation-canary",),
                semantic_class=SemanticClass.REQUIREMENT,
                requirement_subtype=RequirementSubtype.PRODUCT_REQUIREMENT,
                evidence=(evidence,),
            ),
        ),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--codex-bin",
        type=Path,
        default=Path("/Applications/ChatGPT.app/Contents/Resources/codex"),
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=Path.home() / ".codex-reqpilotx",
    )
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument("--timeout-seconds", type=float, default=360.0)
    parser.add_argument(
        "--operation",
        choices=("classification", "assembly", "joint"),
        default="classification",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    driver = LocalCodexAppServerSemanticDriver(
        codex_bin=args.codex_bin.resolve(strict=True),
        codex_home=args.codex_home.resolve(strict=True),
        repository_root=Path(__file__).resolve().parents[2],
        model_id=args.model,
        timeout_seconds=args.timeout_seconds,
    )
    if args.operation == "classification":
        result = driver.classify_once(_classification_request())
        payload = _ClassificationPayload.model_validate(result.candidate_payload)
        decision = payload.decisions[0]
        summary = {
            "schema_version": "codex-semantic-app-server-canary/v1",
            "status": "PASS",
            "operation": result.operation,
            "candidate_sha256": result.candidate_sha256,
            "request_sha256": result.request_sha256,
            "tool_receipt_sha256": result.tool_receipt_sha256,
            "semantic_class": decision.semantic_class.value,
            "requirement_subtype": (
                decision.requirement_subtype.value
                if decision.requirement_subtype is not None
                else None
            ),
            "evidence_ref_count": len(decision.evidence_source_refs),
            "page_image_count": 1,
        }
    elif args.operation == "assembly":
        result = driver.assemble_once(_assembly_request())
        payload = _AssemblyPayload.model_validate(result.candidate_payload)
        decision = payload.decisions[0]
        summary = {
            "schema_version": "codex-semantic-app-server-canary/v1",
            "status": "PASS",
            "operation": result.operation,
            "candidate_sha256": result.candidate_sha256,
            "request_sha256": result.request_sha256,
            "tool_receipt_sha256": result.tool_receipt_sha256,
            "include_in_view": decision.include_in_view,
            "role": decision.role.value if decision.role is not None else None,
            "assembly_status": decision.status.value,
            "candidate_status_expected": (
                decision.status is RequirementAssemblyStatus.CANDIDATE
            ),
            "primary_role_expected": (
                decision.role is RequirementProjectionRole.PRIMARY
            ),
        }
    else:
        result = driver.classify_and_assemble_once(_classification_request())
        payload = _JointPayload.model_validate(result.candidate_payload)
        classification = payload.classifications[0]
        assembly = payload.assemblies[0]
        summary = {
            "schema_version": "codex-semantic-app-server-canary/v1",
            "status": "PASS",
            "operation": result.operation,
            "candidate_sha256": result.candidate_sha256,
            "request_sha256": result.request_sha256,
            "tool_receipt_sha256": result.tool_receipt_sha256,
            "semantic_class": classification.semantic_class.value,
            "requirement_subtype": (
                classification.requirement_subtype.value
                if classification.requirement_subtype is not None
                else None
            ),
            "include_in_view": assembly.include_in_view,
            "role": assembly.role.value if assembly.role is not None else None,
            "assembly_status": assembly.status.value,
        }
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

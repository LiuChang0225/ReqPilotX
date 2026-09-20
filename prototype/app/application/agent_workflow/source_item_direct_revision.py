"""Durable import of actual Codex-authored source-preserving working revisions.

This handler runs domain validation/publication, not a second model. It keeps the
direct host record distinct from App Server execution and all human acceptance.
"""
from copy import deepcopy

from app.application.agent_workflow.document_map_jobs import _container, _projection
from app.application.agent_workflow.source_item_agent_jobs import _source_context, _write_private, _read_private
from app.application.agent_workflow.source_requirement_baseline import (
    SourceRequirementClassificationCoordinationDecisionV1,
    compile_source_requirement_classification_coordination, canonical_json_sha256,
)
from app.application.agent_workflow.source_item_decomposition import validate_source_item_decomposition
from app.application.agent_workflow.source_item_visual import compile_source_item_visual_readings, rebind_source_item_image_annotations
from app.application.agent_workflow.source_requirement_jobs import publish_agent_source_item_revision, SourceItemDirectRunReceiptV1

DIRECT_REVISION_JOB_KIND = "agent.source-item.direct-revision"


def load_direct_decomposition(container, project_id, document_id, baseline):
    job = container.jobs.get_latest_succeeded(kind=DIRECT_REVISION_JOB_KIND,
        aggregate_type="sor-document", aggregate_id=document_id)
    if (not job or job.payload.get("project_id") != project_id or not job.result
            or (job.result.get("baseline_revision_artifact") or {}).get("baseline_artifact_sha256") != baseline.artifact_sha256
            or not job.result.get("decomposition_artifact")):
        return None
    value = _read_private(container, job.result["decomposition_artifact"], project_id=project_id,
        document_id=document_id, namespace="working")
    if value.get("baseline_sha256") != baseline.artifact_sha256 or value.get("source_sha256") != baseline.source_sha256:
        raise ValueError("Direct decomposition source baseline changed")
    return value


def compile_direct_revision(bundle, value, previews):
    previous, document_map = bundle["baseline"], bundle["document_map"]
    if (value.get("expected_source_sha256") != previous.source_sha256
            or value.get("expected_baseline_sha256") != previous.artifact_sha256
            or value.get("expected_document_map_sha256") != document_map.artifact_sha256):
        raise ValueError("Direct revision source, predecessor or Map changed")
    SourceItemDirectRunReceiptV1.model_validate({**value["provenance"],
        "source_sha256": previous.source_sha256, "document_map_artifact_id": document_map.artifact_id,
        "document_map_artifact_sha256": document_map.artifact_sha256, "predecessor_sha256": previous.artifact_sha256,
        "operation": "CLASSIFICATION_REVISION", "request_sha256": canonical_json_sha256(value),
        "candidate_sha256": value["expected_candidate_sha256"]})
    decisions = tuple(SourceRequirementClassificationCoordinationDecisionV1.model_validate(d) for d in value["decisions"])
    result = compile_source_requirement_classification_coordination(previous_artifact=previous,
        previous_validation_receipt=bundle["receipt"], decisions=decisions, model_id=value["provenance"]["model_id"])
    baseline, validation, coordination, proof = result
    if value.get("expected_candidate_sha256") != baseline.artifact_sha256:
        raise ValueError("Direct candidate differs from its reviewed decisions")
    decomposition = value.get("decomposition")
    if decomposition:
        validate_source_item_decomposition(baseline, decomposition, table_previews=previews)
    annotations = value.get("base_annotations")
    if value.get("visual_readings"):
        annotations = compile_source_item_visual_readings(document_map, baseline, value["visual_readings"], base_annotations=annotations)
    elif bundle.get("image_annotations"):
        annotations = rebind_source_item_image_annotations(document_map, baseline, bundle["image_annotations"])
    return baseline, validation, annotations, decomposition, {
        "coordination": coordination.model_dump(mode="json"), "validation": proof.model_dump(mode="json"),
        "source_retained": len(baseline.records), "visual_reading_count": len(value.get("visual_readings", [])),
        "decomposition_sha256": (decomposition or {}).get("artifact_sha256", ""),
    }


def start_direct_revision(project_id, document_id, value):
    if value.get("schema_version") != "source-item-direct-revision/v1":
        raise ValueError("Unsupported direct revision input")
    container = _container()
    digest = canonical_json_sha256({"project_id": project_id, "document_id": document_id, "input": value})
    existing = container.jobs.get_by_idempotency(kind=DIRECT_REVISION_JOB_KIND, idempotency_key=digest)
    if existing:
        return _projection(existing)
    _, bundle, _, _ = _source_context(project_id, document_id, ())
    _, bundle, _, previews = _source_context(project_id, document_id, tuple(r.source_record_id for r in bundle["baseline"].records))
    compile_direct_revision(bundle, value, previews)
    pointer = _write_private(container, project_id=project_id, document_id=document_id,
        namespace="direct-revision", stage="input", value=value)
    return _projection(container.jobs.enqueue(kind=DIRECT_REVISION_JOB_KIND, aggregate_type="sor-document",
        aggregate_id=document_id, idempotency_key=digest, priority=24, max_attempts=1,
        payload={"application_entrypoint": "source-item-workbench-v1", "project_id": project_id,
            "document_id": document_id, "source_sha256": value["expected_source_sha256"],
            "input_artifact": pointer, "request_sha256": digest}))


def run_direct_revision(job, context):
    container = _container()
    project_id, document_id = job.payload["project_id"], job.aggregate_id
    value = _read_private(container, job.payload["input_artifact"], project_id=project_id,
        document_id=document_id, namespace="direct-revision")
    _, bundle, _, _ = _source_context(project_id, document_id, ())
    _, bundle, _, previews = _source_context(project_id, document_id, tuple(r.source_record_id for r in bundle["baseline"].records))
    baseline, validation, annotations, decomposition, proof = compile_direct_revision(bundle, value, previews)
    decomposition_pointer = None
    if decomposition:
        decomposition_pointer = _write_private(container, project_id=project_id, document_id=document_id,
            namespace="working", stage="direct-decomposition", value=decomposition)
    context.heartbeat(progress={"message": "来源与图像释读已核验，保存同源后继工作版本。"})
    result = publish_agent_source_item_revision(project_id=project_id, document_id=document_id,
        document_map=bundle["document_map"], previous_bundle=bundle, baseline=baseline,
        validation_receipt=validation, operation="CLASSIFICATION_REVISION",
        affected_scope_ids=tuple(sorted(baseline.covered_scope_ids)), runtime_receipts=(),
        request_sha256=job.payload["request_sha256"], candidate_sha256=canonical_json_sha256(value),
        domain_proof=proof, context=context, container=container, image_annotations=annotations,
        direct_provenance=deepcopy(value["provenance"]), manual_decomposition=decomposition)
    if decomposition_pointer:
        result["decomposition_artifact"] = decomposition_pointer
    return result

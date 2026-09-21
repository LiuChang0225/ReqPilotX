"""Explicit, evidence-bound SourceItem reasoning jobs on the shared domain core."""

from __future__ import annotations

from io import BytesIO
import hashlib
import json
from pathlib import Path
import tempfile
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from reqpilotx_server.application.agent_workflow.document_map_jobs import (
    _container,
    _digest,
    _projection,
)
from reqpilotx_server.application.agent_workflow.native_structure_baseline_jobs import (
    _current_reference_context,
)
from reqpilotx_server.application.agent_workflow.native_structure_bridge import (
    _no_store_headers,
    _require_project_document,
    materialize_native_structure_table_preview,
    _verified_projection_operations,
)
from reqpilotx_server.application.agent_workflow.source_requirement_reference_bridge import (
    _load_effective_reference,
)
from reqpilotx_server.application.agent_workflow.source_requirement_baseline import (
    SourceRequirementBaselineArtifactV1,
    RequirementKind,
)
from reqpilotx_server.application.agent_workflow.source_item_decomposition import (
    build_source_item_decomposition_packet,
    compile_source_item_decomposition,
    validate_source_item_decomposition,
    SourceItemDecompositionError,
)
from reqpilotx_server.application.errors import ApplicationError
from reqpilotx_server.application.source_runtime import resolve_source_record_path
from reqpilotx_server.application.transport import ResponseSink
from reqpilotx_server.ports.job_queue import DurableJob, JobExecutionContext, NonRetryableJobError
from reqpilotx_server.adapters.agents.codex_app_server.json_stage_driver import (
    LocalCodexAppServerJsonStageDriver,
    SourceBoundImage,
)

DECOMPOSITION_JOB_KIND = "agent.source-item.decompose"
ANALYSIS_JOB_KIND = "agent.source-item.analyze"
ROOT = Path(__file__).resolve().parents[4]


def get_source_item_agent_job(
    project_id: str, document_id: str, job_id: str, response: ResponseSink
):
    response.headers.update(_no_store_headers())
    _require_project_document(project_id, document_id)
    job = _container().jobs.get(job_id)
    if (
        job is None
        or job.kind not in {DECOMPOSITION_JOB_KIND, ANALYSIS_JOB_KIND}
        or job.aggregate_id != document_id
        or job.payload.get("project_id") != project_id
    ):
        raise ApplicationError(
            status_code=404,
            detail={
                "code": "source_item_agent_job_not_found",
                "message": "The requested Agent job is not part of this document.",
            },
        )
    return _projection(job)


def get_source_item_agent_artifact(
    project_id: str, document_id: str, job_id: str, response: ResponseSink
):
    value = get_source_item_agent_job(project_id, document_id, job_id, response)
    job = _container().jobs.get(job_id)
    if (
        value["status"] != "SUCCEEDED"
        or not job.result
        or not job.result.get("artifact")
    ):
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "source_item_agent_artifact_not_ready",
                "message": "The Agent artifact has not passed Host validation.",
            },
        )
    return _read_private(
        _container(),
        job.result["artifact"],
        project_id=project_id,
        document_id=document_id,
        namespace=job.payload["operation_namespace"],
    )


class SourceItemDecompositionStartRequestV1(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
    expected_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_baseline_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_item_ids: tuple[str, ...] = Field(min_length=1, max_length=32)
    explicit_trigger: Literal[True]
    operation_namespace: str = Field(
        default="working", pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$"
    )
    publication_mode: Literal["WORKING_PROPOSAL", "ISOLATED_TEST"] = "WORKING_PROPOSAL"
    visual_evidence: bool = True
    timeout_seconds: int = Field(default=1200, ge=60, le=7200)


class SourceItemAnalysisStartRequestV1(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
    expected_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_baseline_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    selected_unit_ids: tuple[str, ...] | None = None
    knowledge_document_ids: tuple[str, ...] | None = None
    decomposition_job_id: str | None = Field(
        default=None, pattern=r"^job-[0-9a-f]{32}$"
    )
    operation_namespace: str = Field(
        default="working", pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$"
    )
    publication_mode: Literal["WORKING_PROPOSAL", "ISOLATED_TEST"] = "WORKING_PROPOSAL"
    visual_evidence: bool = True
    timeout_seconds: int = Field(default=1200, ge=60, le=7200)


def start_source_item_analysis(
    project_id: str,
    document_id: str,
    payload: SourceItemAnalysisStartRequestV1,
    response: ResponseSink,
):
    from reqpilotx_server.application.knowledge_facts import build_project_knowledge_catalog
    from reqpilotx_server.application.agent_workflow.source_item_analysis import (
        build_source_item_analysis_packet,
    )

    response.headers.update(_no_store_headers())
    container = _container()
    current = _current_reference_context(project_id, document_id)
    bundle = _load_effective_reference(
        project_id=project_id,
        document_id=document_id,
        source_sha256=current["source_sha256"],
    )
    baseline = bundle["baseline"]
    if (
        baseline.source_sha256 != payload.expected_source_sha256
        or baseline.artifact_sha256 != payload.expected_baseline_sha256
    ):
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "analysis_source_version_changed",
                "message": "The SourceItem version changed before analysis.",
            },
        )
    if container.settings.codex_bin is None or container.settings.codex_home is None:
        raise ApplicationError(
            status_code=503,
            detail={
                "code": "analysis_codex_not_configured",
                "message": "The Codex App Server runtime is unavailable.",
            },
        )
    _current, _bundle, _source, previews = _source_context(
        project_id, document_id, tuple(r.source_record_id for r in baseline.records)
    )
    decomposition = None
    decomp = (
        container.jobs.get(payload.decomposition_job_id)
        if payload.decomposition_job_id
        else container.jobs.get_latest_succeeded(
            kind=DECOMPOSITION_JOB_KIND,
            aggregate_type="sor-document",
            aggregate_id=document_id,
            payload_filters={
                "operation_namespace": "working",
                "publication_mode": "WORKING_PROPOSAL",
                "baseline_sha256": baseline.artifact_sha256,
            },
        )
    )
    if decomp is not None or payload.decomposition_job_id:
        if (
            not decomp
            or decomp.kind != DECOMPOSITION_JOB_KIND
            or decomp.aggregate_id != document_id
            or decomp.payload.get("project_id") != project_id
            or decomp.status.value != "SUCCEEDED"
        ):
            raise ApplicationError(
                status_code=409,
                detail={
                    "code": "analysis_decomposition_not_ready",
                    "message": "The selected decomposition has not been validated for this document.",
                },
            )
        if (
            decomp.payload["publication_mode"] == "ISOLATED_TEST"
            and payload.publication_mode != "ISOLATED_TEST"
        ):
            raise ApplicationError(
                status_code=409,
                detail={
                    "code": "analysis_decomposition_isolated",
                    "message": "An isolated decomposition can only feed an isolated analysis.",
                },
            )
        decomposition = _read_private(
            container,
            decomp.result["artifact"],
            project_id=project_id,
            document_id=document_id,
            namespace=decomp.payload["operation_namespace"],
        )
    if decomposition is None and not payload.decomposition_job_id:
        from reqpilotx_server.application.agent_workflow.source_item_loop_jobs import load_native_loop_artifact
        decomposition, _ = load_native_loop_artifact(container, project_id, document_id, baseline, "decomposition_artifact")
    if decomposition is None and not payload.decomposition_job_id:
        from reqpilotx_server.application.agent_workflow.source_item_direct_revision import load_direct_decomposition
        decomposition = load_direct_decomposition(container, project_id, document_id, baseline)
    catalog = build_project_knowledge_catalog(
        project_id, document_ids=payload.knowledge_document_ids
    )
    packet = build_source_item_analysis_packet(
        bundle["document_map"],
        baseline,
        previews,
        catalog,
        decomposition=decomposition,
        selected_unit_ids=payload.selected_unit_ids,
        image_annotations=bundle.get("image_annotations"),
    )
    idempotency = _digest(
        {
            "project_id": project_id,
            "document_id": document_id,
            **payload.model_dump(mode="json"),
            "catalog_sha256": catalog["catalog_sha256"],
            "packet_sha256": packet["packet_sha256"],
        }
    )
    existing = container.jobs.get_by_idempotency(
        kind=ANALYSIS_JOB_KIND, idempotency_key=idempotency
    )
    if existing:
        return _projection(existing)
    args = {
        "project_id": project_id,
        "document_id": document_id,
        "namespace": payload.operation_namespace,
    }
    pointer = _write_private(
        container,
        **args,
        stage="analysis-input",
        value={"packet": packet, "knowledge_catalog": catalog},
    )
    job = container.jobs.enqueue(
        kind=ANALYSIS_JOB_KIND,
        aggregate_type="sor-document",
        aggregate_id=document_id,
        idempotency_key=idempotency,
        payload={
            "schema_version": "source-item-analysis-job/v1",
            "application_entrypoint": "source-item-workbench-v1",
            "project_id": project_id,
            "document_id": document_id,
            "source_sha256": baseline.source_sha256,
            "baseline_sha256": baseline.artifact_sha256,
            "decomposition_sha256": packet["decomposition_sha256"],
            "knowledge_base_version": catalog["knowledge_base_version"],
            "catalog_sha256": catalog["catalog_sha256"],
            "knowledge_document_ids": list(payload.knowledge_document_ids)
            if payload.knowledge_document_ids is not None
            else None,
            "operation_namespace": payload.operation_namespace,
            "publication_mode": payload.publication_mode,
            "visual_evidence": payload.visual_evidence,
            "timeout_seconds": payload.timeout_seconds,
            "input_artifact": pointer,
        },
        priority=21,
        max_attempts=3,
    )
    return _projection(job)


def run_durable_source_item_analysis(job: DurableJob, context: JobExecutionContext):
    from collections import Counter
    from reqpilotx_server import db
    from reqpilotx_server.application.knowledge_facts import build_project_knowledge_catalog
    from reqpilotx_server.application.agent_workflow.source_item_analysis import (
        ANALYSIS_SKILL,
        source_item_analysis_output_schema,
        compile_source_item_assessments,
        validate_source_item_assessments,
    )

    container = _container()
    value = job.payload
    args = {
        "project_id": value["project_id"],
        "document_id": job.aggregate_id,
        "namespace": value["operation_namespace"],
    }
    inputs = _read_private(container, value["input_artifact"], **args)
    packet = inputs["packet"]
    if (
        packet["baseline_sha256"] != value["baseline_sha256"]
        or packet["source_sha256"] != value["source_sha256"]
        or packet["catalog_sha256"] != value["catalog_sha256"]
    ):
        raise NonRetryableJobError(
            error_code="analysis_input_identity_mismatch",
            message="The frozen analysis input changed.",
        )
    attempts = []
    artifact = None
    predecessor_job = container.jobs.get_latest_succeeded(
        kind=ANALYSIS_JOB_KIND,
        aggregate_type="sor-document",
        aggregate_id=job.aggregate_id,
        payload_filters={
            key: value[key]
            for key in (
                "operation_namespace",
                "publication_mode",
                "baseline_sha256",
                "decomposition_sha256",
                "knowledge_base_version",
                "catalog_sha256",
            )
        },
    )
    previous = (
        _read_private(container, predecessor_job.result["artifact"], **args)
        if predecessor_job
        and predecessor_job.result
        and predecessor_job.result.get("artifact")
        else None
    )
    checkpoint = job.progress.get("candidate_checkpoint")
    if checkpoint:
        recovered = _read_private(container, checkpoint, **args)
        artifact = _recover_analysis_checkpoint(
            packet,
            recovered["compiled"],
            previous=previous,
            input_packet_ref=value["input_artifact"],
        )
        attempts = recovered["attempts"]
    if artifact is None:
        driver = LocalCodexAppServerJsonStageDriver(
            codex_bin=container.settings.codex_bin,
            codex_home=container.settings.codex_home,
            repository_root=ROOT,
            model_id=container.settings.agent_model_id,
        )
        feedback = None
        with tempfile.TemporaryDirectory(
            prefix="reqpilotx-analysis-evidence-", dir=container.settings.scratch_root
        ) as raw:
            image_root = Path(raw)
            images = []
            if value["visual_evidence"]:
                current = _current_reference_context(
                    value["project_id"], job.aggregate_id
                )
                source_path = resolve_source_record_path(current["document"]).resolve(
                    strict=True
                )
                pages = sorted(
                    {page for unit in packet["units"] for page in unit["pages"]}
                )
                if len(pages) > 8:
                    raise NonRetryableJobError(
                        error_code="analysis_visual_scope_too_large",
                        message="Select a smaller source scope for simultaneous original-image analysis; no pages were silently dropped.",
                    )
                images.extend(
                    _render_source_pages(
                        source_path, pages, image_root, packet["source_sha256"]
                    )
                )
                documents = {
                    document["id"]: document
                    for document in db.list_knowledge_documents(value["project_id"])
                }
                rendered = set()
                for fact_id, fact in packet["evidence_catalog"].items():
                    ref = fact.get("source_ref", {}).get("location", {})
                    page = ref.get("page_number")
                    key = (fact["document_id"], page)
                    if not page or key in rendered:
                        continue
                    record = documents.get(fact["document_id"])
                    if not record:
                        raise ValueError(
                            "Registered knowledge source disappeared before visual analysis"
                        )
                    source = resolve_source_record_path(record).resolve(strict=True)
                    if source.suffix.lower() != ".pdf":
                        continue
                    target_root = image_root / fact["document_id"]
                    target_root.mkdir(exist_ok=True)
                    rendered_images = _render_source_pages(
                        source, [page], target_root, fact["source_sha256"]
                    )
                    for item in rendered_images:
                        images.append(
                            SourceBoundImage(
                                path=item.path,
                                expected_sha256=item.expected_sha256,
                                source_sha256=item.source_sha256,
                                fact_id=fact_id,
                            )
                        )
                    rendered.add(key)
                if len(images) > 16:
                    raise NonRetryableJobError(
                        error_code="analysis_visual_scope_too_large",
                        message="Select fewer knowledge/source pages; no image evidence was silently omitted.",
                    )
            for attempt in range(3):

                def observe(receipt):
                    context.heartbeat(
                        progress={
                            "stage": "ANALYSIS_AGENT",
                            "attempt": attempt + 1,
                            "runtime_receipt": dict(receipt),
                            "catalog_sha256": packet["catalog_sha256"],
                            "message": "Agent 正在逐项比较原文要求、适用工况和已登记内部证据。",
                        }
                    )

                result = driver.complete(
                    packet=packet
                    if feedback is None
                    else {**packet, "host_validation_feedback": feedback},
                    output_schema=source_item_analysis_output_schema(
                        [unit["unit_id"] for unit in packet["units"]]
                    ),
                    skill_name="reqpilotx-source-item-evidence-analysis",
                    skill_markdown=ANALYSIS_SKILL,
                    task_directive="Assess exactly the requested units using the frozen registered knowledge catalog and original images. The catalog is supplied by the Host; do not create or replace it. Compare product, conditions, method, units and thresholds. Do not use file existence, similar wording, unavailable facts or UNKNOWN authority as proof. Return only decisions with exact fact_id/quote citations; preserve missing evidence and clarification questions.",
                    timeout_seconds=value["timeout_seconds"],
                    cancellation_requested=context.cancellation_requested,
                    receipt_sink=observe,
                    image_inputs=images,
                    authorized_image_roots=(image_root,),
                )
                attempts.append(
                    {"candidate": result.candidate, "runtime_receipt": result.receipt}
                )
                try:
                    artifact = compile_source_item_assessments(
                        packet,
                        result.candidate,
                        previous=previous,
                        input_packet_ref=value["input_artifact"],
                    )
                    validate_source_item_assessments(packet, artifact)
                    break
                except ValueError as exc:
                    feedback = {
                        "status": "REJECTED",
                        "reason": str(exc)[:1600],
                        "previous_candidate": result.candidate,
                    }
                    attempts[-1]["validation_failure"] = str(exc)[:1600]
                    pointer = _write_private(
                        container,
                        **args,
                        stage="analysis-attempts",
                        value={"attempts": attempts},
                    )
                    context.heartbeat(
                        progress={
                            "stage": "ANALYSIS_REPAIR_REQUIRED",
                            "attempt_receipts": pointer,
                            "attempt": attempt + 1,
                        }
                    )
            if artifact is None:
                raise NonRetryableJobError(
                    error_code="analysis_candidate_unresolved",
                    message="Analysis still fails source/evidence checks; raw attempts remain available.",
                )
        checkpoint = _write_private(
            container,
            **args,
            stage="analysis-candidate",
            value={"compiled": artifact, "attempts": attempts},
        )
        context.heartbeat(
            progress={
                "stage": "ANALYSIS_VALIDATED",
                "candidate_checkpoint": checkpoint,
                "model_call_count": len(attempts),
            }
        )
    if value["publication_mode"] == "WORKING_PROPOSAL":
        current = _load_effective_reference(
            project_id=value["project_id"],
            document_id=job.aggregate_id,
            source_sha256=packet["source_sha256"],
        )
        fresh_catalog = build_project_knowledge_catalog(
            value["project_id"], document_ids=value["knowledge_document_ids"]
        )
        if (
            current["baseline"].artifact_sha256 != packet["baseline_sha256"]
            or fresh_catalog["catalog_sha256"] != packet["catalog_sha256"]
        ):
            raise NonRetryableJobError(
                error_code="analysis_input_version_changed",
                message="The source or knowledge version changed; results remain bound to their original snapshot.",
            )
    context.begin_publication()
    pointer = _write_private(container, **args, stage="analysis", value=artifact)
    return {
        "schema_version": "source-item-analysis-job-result/v1",
        "status": artifact["status"],
        "source_sha256": packet["source_sha256"],
        "baseline_sha256": packet["baseline_sha256"],
        "catalog_sha256": packet["catalog_sha256"],
        "knowledge_base_version": packet["knowledge_base_version"],
        "decomposition_sha256": packet["decomposition_sha256"],
        "operation_namespace": value["operation_namespace"],
        "publication_mode": value["publication_mode"],
        "artifact": pointer,
        "artifact_id": artifact["artifact_id"],
        "artifact_sha256": artifact["artifact_sha256"],
        "unit_count": len(artifact["records"]),
        "affected_unit_ids": artifact["affected_unit_ids"],
        "unassessed_unit_count": artifact["unassessed_unit_count"],
        "decision_counts": dict(
            Counter(item["decision"] for item in artifact["records"].values())
        ),
        "pending_unit_count": artifact["pending_unit_count"],
        "model_call_count": len(attempts),
        "is_human_accepted": False,
    }


def _recover_analysis_checkpoint(packet, compiled, *, previous, input_packet_ref):
    """Reuse a validated model decision while retaining newer unselected results."""
    from reqpilotx_server.application.agent_workflow.source_item_analysis import (
        compile_source_item_assessments,
        validate_source_item_assessments,
    )

    artifact = validate_source_item_assessments(packet, compiled)
    expected_predecessor = previous["artifact_sha256"] if previous else ""
    if artifact["predecessor_sha256"] == expected_predecessor:
        return artifact
    candidate = {
        "decisions": [
            {"unit_id": unit_id, **artifact["records"][unit_id]}
            for unit_id in artifact["affected_unit_ids"]
        ]
    }
    rebased = compile_source_item_assessments(
        packet, candidate, previous=previous, input_packet_ref=input_packet_ref
    )
    return validate_source_item_assessments(packet, rebased)


def _write_private(container, *, project_id, document_id, namespace, stage, value):
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    digest = hashlib.sha256(raw).hexdigest()
    key = f"artifacts/source-item-agents/{project_id}/{document_id}/{namespace}/{stage}/{digest}.json"
    obj = container.object_store.put(
        key=key,
        source=BytesIO(raw),
        expected_sha256=digest,
        media_type="application/json",
    )
    return {
        "object_key": obj.key,
        "object_sha256": obj.sha256,
        "size": obj.size,
        "media_type": obj.media_type,
    }


def _read_private(container, pointer, *, project_id, document_id, namespace):
    prefix = f"artifacts/source-item-agents/{project_id}/{document_id}/{namespace}/"
    if not str(pointer["object_key"]).startswith(prefix):
        raise ValueError("Reasoning artifact belongs to another source scope")
    with container.object_store.open(pointer["object_key"]) as stream:
        raw = stream.read(32 * 1024 * 1024 + 1)
    if (
        len(raw) != pointer["size"]
        or hashlib.sha256(raw).hexdigest() != pointer["object_sha256"]
    ):
        raise ValueError("Reasoning artifact failed integrity validation")
    return json.loads(raw)


def _source_context(project_id, document_id, source_ids):
    current = _current_reference_context(project_id, document_id)
    bundle = _load_effective_reference(
        project_id=project_id,
        document_id=document_id,
        source_sha256=current["source_sha256"],
    )
    baseline = bundle["baseline"]
    records = {r.source_record_id: r for r in baseline.records}
    if len(source_ids) != len(set(source_ids)) or set(source_ids) - set(records):
        raise ValueError("Selected SourceItem identity is missing or duplicated")
    source_path = resolve_source_record_path(current["document"]).resolve(strict=True)
    previews = {}
    scopes = {
        records[key].logical_table_scope_id
        for key in source_ids
        if records[key].logical_table_scope_id
    }
    if set(source_ids) == set(records):
        # Whole-document delivery retains original tables even when a table has
        # only headers/placeholders and deliberately creates no SourceItem.
        scopes.update(table.scope_id for table in bundle["document_map"].logical_tables)
    for scope in scopes:
        previews[scope] = materialize_native_structure_table_preview(
            bundle=current["bundle"],
            source_sha256=current["source_sha256"],
            scope_id=scope,
            projection_operations=_verified_projection_operations(
                current["handoff_attempts"]
            ),
            source_path=source_path,
        )
    return current, bundle, source_path, previews


def start_source_item_decomposition(
    project_id: str,
    document_id: str,
    payload: SourceItemDecompositionStartRequestV1,
    response: ResponseSink,
):
    response.headers.update(_no_store_headers())
    container = _container()
    current, bundle, _source_path, previews = _source_context(
        project_id, document_id, payload.source_item_ids
    )
    baseline = bundle["baseline"]
    if (
        current["source_sha256"] != payload.expected_source_sha256
        or baseline.artifact_sha256 != payload.expected_baseline_sha256
    ):
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "decomposition_source_version_changed",
                "message": "The selected source baseline changed before decomposition.",
            },
        )
    settings = container.settings
    if settings.codex_bin is None or settings.codex_home is None:
        raise ApplicationError(
            status_code=503,
            detail={
                "code": "decomposition_codex_not_configured",
                "message": "The Codex App Server runtime is unavailable.",
            },
        )
    packets = [
        build_source_item_decomposition_packet(baseline, key, table_previews=previews)
        for key in payload.source_item_ids
    ]
    nodes = {n.node_id: n for n in bundle["document_map"].nodes}
    records = {r.source_record_id: r for r in baseline.records}
    for packet in packets:
        context = []
        node_id = records[packet["source_item_id"]].node_id
        while node_id:
            node = nodes[node_id]
            context.append(
                {
                    "node_id": node.node_id,
                    "label": node.label,
                    "title": node.title,
                    "source_content": [
                        content.model_dump(mode="json")
                        for content in node.source_content
                    ],
                }
            )
            node_id = node.parent_id
        packet["document_context"] = list(reversed(context))
    previous_job = container.jobs.get_latest_succeeded(
        kind=DECOMPOSITION_JOB_KIND,
        aggregate_type="sor-document",
        aggregate_id=document_id,
        payload_filters={
            "operation_namespace": payload.operation_namespace,
            "publication_mode": payload.publication_mode,
            "baseline_sha256": baseline.artifact_sha256,
        },
    )
    previous = None
    if previous_job and previous_job.result and previous_job.result.get("artifact"):
        previous = _read_private(
            container,
            previous_job.result["artifact"],
            project_id=project_id,
            document_id=document_id,
            namespace=payload.operation_namespace,
        )
    if previous is None and payload.operation_namespace == "working" and payload.publication_mode == "WORKING_PROPOSAL":
        from reqpilotx_server.application.agent_workflow.source_item_loop_jobs import load_native_loop_artifact
        previous, _ = load_native_loop_artifact(container, project_id, document_id, baseline, "decomposition_artifact")
    if previous is None and payload.operation_namespace == "working" and payload.publication_mode == "WORKING_PROPOSAL":
        from reqpilotx_server.application.agent_workflow.source_item_direct_revision import load_direct_decomposition
        previous = load_direct_decomposition(container, project_id, document_id, baseline)
    identity = {
        "project_id": project_id,
        "document_id": document_id,
        **payload.model_dump(mode="json"),
    }
    idempotency = _digest(identity)
    existing = container.jobs.get_by_idempotency(
        kind=DECOMPOSITION_JOB_KIND, idempotency_key=idempotency
    )
    if existing is not None:
        return _projection(existing)
    packet = {
        "schema_version": "source-item-decomposition-agent-input/v1",
        "explicit_trigger_required": True,
        "source_sha256": baseline.source_sha256,
        "baseline_sha256": baseline.artifact_sha256,
        "selected_source_item_ids": list(payload.source_item_ids),
        "parents": packets,
    }
    inputs = {
        "baseline": baseline.model_dump(mode="json"),
        "table_previews": previews,
        "packet": packet,
        "previous": previous,
        "source_pages": sorted(
            {page for key in payload.source_item_ids for page in records[key].pages}
        ),
    }
    pointer = _write_private(
        container,
        project_id=project_id,
        document_id=document_id,
        namespace=payload.operation_namespace,
        stage="decomposition-input",
        value=inputs,
    )
    job = container.jobs.enqueue(
        kind=DECOMPOSITION_JOB_KIND,
        aggregate_type="sor-document",
        aggregate_id=document_id,
        idempotency_key=idempotency,
        payload={
            "schema_version": "source-item-decomposition-job/v1",
            "application_entrypoint": "source-item-workbench-v1",
            "project_id": project_id,
            "document_id": document_id,
            "source_sha256": baseline.source_sha256,
            "baseline_sha256": baseline.artifact_sha256,
            "source_item_ids": list(payload.source_item_ids),
            "operation_namespace": payload.operation_namespace,
            "publication_mode": payload.publication_mode,
            "visual_evidence": payload.visual_evidence,
            "timeout_seconds": payload.timeout_seconds,
            "input_artifact": pointer,
        },
        priority=22,
        max_attempts=3,
    )
    return _projection(job)


def decomposition_output_schema(source_ids):
    def obj(properties):
        return {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        }

    quote = obj(
        {
            "segment_index": {"type": "integer", "minimum": 0},
            "quote": {"type": "string", "minLength": 1},
        }
    )
    child = obj(
        {
            "text": {"type": "string", "minLength": 1},
            "derivation_type": {
                "type": "string",
                "enum": ["VERBATIM_SPAN", "NORMALIZED_DERIVATION"],
            },
            "requirement_kinds": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": [item.value for item in RequirementKind],
                },
                "minItems": 1,
            },
            "source_quotes": {"type": "array", "items": quote, "minItems": 1},
        }
    )
    context = obj(
        {
            **quote["properties"],
            "disposition": {
                "type": "string",
                "enum": [
                    "CONTEXT",
                    "HEADING",
                    "METHOD",
                    "PRESENTATION",
                    "REFERENCE",
                    "UNRESOLVED",
                ],
            },
        }
    )
    decision = obj(
        {
            "source_item_id": {"type": "string", "enum": list(source_ids)},
            "action": {"type": "string", "enum": ["SPLIT", "KEEP"]},
            "reason": {"type": "string", "minLength": 1},
            "logic": {"type": "string", "enum": ["ALL_OF", "ANY_OF"]},
            "children": {"type": "array", "items": child},
            "context_quotes": {"type": "array", "items": context},
        }
    )
    return obj(
        {
            "decisions": {
                "type": "array",
                "items": decision,
                "minItems": len(source_ids),
                "maxItems": len(source_ids),
            }
        }
    )


def bind_decomposition_quotes(packet, candidate):
    parents = {p["source_item_id"]: p for p in packet["parents"]}
    decisions = []
    for decision in candidate["decisions"]:
        parent = parents.get(decision["source_item_id"])
        if parent is None:
            raise SourceItemDecompositionError("Unknown selected SourceItem")
        if decision["action"] == "KEEP" and (
            decision["children"] or decision["context_quotes"]
        ):
            raise SourceItemDecompositionError(
                "KEEP must not emit derived children or source ranges"
            )

        def bind(quote):
            index = quote["segment_index"]
            if type(index) is not int or not 0 <= index < len(parent["segments"]):
                raise SourceItemDecompositionError("Unknown source segment")
            text = parent["segments"][index]["text"]
            match = quote["quote"]
            if not match or text.count(match) != 1:
                raise SourceItemDecompositionError(
                    "Source quote must match exactly once in its declared segment"
                )
            start = text.index(match)
            return {
                "segment_index": index,
                "start_offset": start,
                "end_offset": start + len(match),
            }

        value = {
            key: decision[key]
            for key in ("source_item_id", "action", "reason", "logic")
        }
        value["children"] = [
            {
                **{
                    key: child[key]
                    for key in ("text", "derivation_type", "requirement_kinds")
                },
                "source_ranges": [bind(quote) for quote in child["source_quotes"]],
            }
            for child in decision["children"]
        ]
        value["context_ranges"] = [
            {"source_range": bind(quote), "disposition": quote["disposition"]}
            for quote in decision["context_quotes"]
        ]
        decisions.append(value)
    if {d["source_item_id"] for d in decisions} != set(parents) or len(
        decisions
    ) != len(parents):
        raise SourceItemDecompositionError(
            "Decisions must cover exactly the explicit SourceItem scope"
        )
    return decisions


def _render_source_pages(source_path, pages, root, source_sha):
    import fitz

    images = []
    if hashlib.sha256(source_path.read_bytes()).hexdigest() != source_sha:
        raise ValueError("Source PDF changed before visual evidence rendering")
    if source_path.suffix.lower() != ".pdf":
        return images
    with fitz.open(source_path) as pdf:
        for page in pages:
            target = root / f"source-page-{page}.png"
            pdf[page - 1].get_pixmap(matrix=fitz.Matrix(1.25, 1.25), alpha=False).save(
                target
            )
            images.append(
                SourceBoundImage(
                    path=target,
                    expected_sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
                    source_sha256=source_sha,
                    fact_id=f"customer-source-page-{page}",
                )
            )
    return images


def run_durable_source_item_decomposition(
    job: DurableJob, context: JobExecutionContext
):
    container = _container()
    value = job.payload
    args = {
        "project_id": value["project_id"],
        "document_id": job.aggregate_id,
        "namespace": value["operation_namespace"],
    }
    inputs = _read_private(container, value["input_artifact"], **args)
    baseline = SourceRequirementBaselineArtifactV1.model_validate(inputs["baseline"])
    if (
        baseline.artifact_sha256 != value["baseline_sha256"]
        or baseline.source_sha256 != value["source_sha256"]
    ):
        raise NonRetryableJobError(
            error_code="decomposition_input_identity_mismatch",
            message="The frozen decomposition input changed.",
        )
    packet = inputs["packet"]
    # A command may have waited behind an earlier decomposition. Read its actual
    # namespace predecessor now so Snn never restarts from a stale enqueue view.
    predecessor_job = container.jobs.get_latest_succeeded(
        kind=DECOMPOSITION_JOB_KIND,
        aggregate_type="sor-document",
        aggregate_id=job.aggregate_id,
        payload_filters={
            "operation_namespace": value["operation_namespace"],
            "publication_mode": value["publication_mode"],
            "baseline_sha256": baseline.artifact_sha256,
        },
    )
    if (
        predecessor_job
        and predecessor_job.result
        and predecessor_job.result.get("artifact")
    ):
        inputs["previous"] = _read_private(
            container, predecessor_job.result["artifact"], **args
        )
    if inputs["previous"]:
        needed = tuple(
            set(value["source_item_ids"]) | set(inputs["previous"]["records"])
        )
        _current, previous_bundle, _path, previous_previews = _source_context(
            value["project_id"], job.aggregate_id, needed
        )
        if previous_bundle["baseline"].artifact_sha256 != baseline.artifact_sha256:
            raise NonRetryableJobError(
                error_code="decomposition_predecessor_source_changed",
                message="The prior decomposition is no longer on the selected source baseline.",
            )
        inputs["table_previews"].update(previous_previews)
    attempts = []
    checkpoint = job.progress.get("candidate_checkpoint")
    compiled = None
    if checkpoint:
        recovered = _read_private(container, checkpoint, **args)
        compiled = validate_source_item_decomposition(
            baseline, recovered["compiled"], table_previews=inputs["table_previews"]
        )
        attempts = recovered["attempts"]
        expected_predecessor = (
            inputs["previous"]["artifact_sha256"] if inputs["previous"] else ""
        )
        if compiled["predecessor_sha256"] != expected_predecessor:
            compiled = compile_source_item_decomposition(
                baseline,
                bind_decomposition_quotes(packet, attempts[-1]["candidate"]),
                table_previews=inputs["table_previews"],
                previous=inputs["previous"],
                authorized_source_item_ids=packet["selected_source_item_ids"],
            )
    if compiled is None:
        current = _current_reference_context(value["project_id"], job.aggregate_id)
        source_path = resolve_source_record_path(current["document"]).resolve(
            strict=True
        )
        driver = LocalCodexAppServerJsonStageDriver(
            codex_bin=container.settings.codex_bin,
            codex_home=container.settings.codex_home,
            repository_root=ROOT,
            model_id=container.settings.agent_model_id,
        )
        skill = (
            ROOT
            / "agent-kit/.agents/skills/reqpilotx-requirement-decomposition/SKILL.md"
        ).read_text()
        from reqpilotx_server.adapters.agents.portable_kit import load_portable_agent_kit
        from reqpilotx_server.application.agent_workflow.extraction_runtime import (
            AgentExtractionStage,
        )

        kit = load_portable_agent_kit(ROOT / "agent-kit")
        method = next(
            item
            for item in kit.manifest.stages
            if item.stage is AgentExtractionStage.REQUIREMENT_DECOMPOSITION
        )
        # The legacy D4 artifact endpoint is still planned; its locked references
        # are capability methods, not a gate on the implemented SourceItem core.
        for reference in (kit.manifest.policy_path, *method.reference_paths):
            body = (kit.root / reference).read_text()
            skill += f"\n\nBEGIN LOCKED REFERENCE {reference} {hashlib.sha256(body.encode()).hexdigest()}\n{body}\nEND LOCKED REFERENCE\n"
        skill += (
            "\n\nCURRENT CLASSIFICATION V1 (authoritative current product rules):\n"
            + (ROOT / "docs/需求提取/分类与验证关联规则V1.md").read_text()
        )
        directive = (
            "This is an explicitly selected SourceItem decomposition. Use the supplied source-item packet and output schema, not the older D4 artifact shape. "
            "The Host will bind exact quoted substrings to Unicode offsets and run the existing exact-cover/compiler validators. "
            "Choose SPLIT only for at least two useful independently handled obligations; otherwise choose KEEP with empty children/context_quotes. "
            "Do not force splitting for a demo or create a pseudo S01. Keep shared applicability lists/conditions together and preserve checked/unchecked distinctions. "
            "For a split, every non-whitespace character of every selected segment must occur exactly once across child source_quotes and context_quotes. "
            "Quote exact source text including punctuation and newlines; source_quotes must be uniquely locatable within their segment. "
            "Preserve all obligations and applicable conditions, do not invent thresholds, laws, standards text or countries. "
            "Classify derived obligations with the current finite tree (FUNCTIONAL/QUALITY/CONSTRAINT, PROJECT, PROCESS). "
            "The parent remains unchanged. Do not claim Host validation or human acceptance."
        )
        feedback = None
        with tempfile.TemporaryDirectory(
            prefix="reqpilotx-decomposition-evidence-",
            dir=container.settings.scratch_root,
        ) as raw:
            image_root = Path(raw)
            images = (
                _render_source_pages(
                    source_path,
                    inputs["source_pages"],
                    image_root,
                    baseline.source_sha256,
                )
                if value["visual_evidence"]
                else []
            )
            for attempt in range(3):
                if context.cancellation_requested():
                    raise NonRetryableJobError(
                        error_code="decomposition_cancelled",
                        message="Explicit decomposition was cancelled.",
                    )

                def observe(receipt):
                    context.heartbeat(
                        progress={
                            "stage": "DECOMPOSITION_AGENT",
                            "attempt": attempt + 1,
                            "runtime_receipt": dict(receipt),
                            "message": "Agent 正在读取明确选中的原文父项，判断是否需要派生单一需求。",
                        }
                    )

                turn_packet = (
                    packet
                    if feedback is None
                    else {**packet, "host_validation_feedback": feedback}
                )
                result = driver.complete(
                    packet=turn_packet,
                    output_schema=decomposition_output_schema(value["source_item_ids"]),
                    skill_name="reqpilotx-source-item-decomposition",
                    skill_markdown=skill,
                    task_directive=directive,
                    timeout_seconds=value["timeout_seconds"],
                    cancellation_requested=context.cancellation_requested,
                    receipt_sink=observe,
                    image_inputs=images,
                    authorized_image_roots=(image_root,),
                )
                attempts.append(
                    {"candidate": result.candidate, "runtime_receipt": result.receipt}
                )
                try:
                    decisions = bind_decomposition_quotes(packet, result.candidate)
                    compiled = compile_source_item_decomposition(
                        baseline,
                        decisions,
                        table_previews=inputs["table_previews"],
                        previous=inputs["previous"],
                        authorized_source_item_ids=packet["selected_source_item_ids"],
                    )
                    validate_source_item_decomposition(
                        baseline, compiled, table_previews=inputs["table_previews"]
                    )
                    break
                except ValueError as exc:
                    feedback = {
                        "status": "REJECTED",
                        "reason": str(exc)[:1600],
                        "previous_candidate": result.candidate,
                    }
                    attempts[-1]["validation_failure"] = str(exc)[:1600]
                    pointer = _write_private(
                        container,
                        **args,
                        stage="decomposition-attempts",
                        value={"attempts": attempts},
                    )
                    context.heartbeat(
                        progress={
                            "stage": "DECOMPOSITION_REPAIR_REQUIRED",
                            "attempt_receipts": pointer,
                            "attempt": attempt + 1,
                        }
                    )
            if compiled is None:
                raise NonRetryableJobError(
                    error_code="decomposition_candidate_unresolved",
                    message="The decomposition candidate still fails source preservation checks; failed attempts are retained.",
                )
        checkpoint = _write_private(
            container,
            **args,
            stage="decomposition-candidate",
            value={"compiled": compiled, "attempts": attempts},
        )
        context.heartbeat(
            progress={
                "stage": "DECOMPOSITION_VALIDATED",
                "candidate_checkpoint": checkpoint,
                "model_call_count": len(attempts),
            }
        )
    if value["publication_mode"] == "WORKING_PROPOSAL":
        current = _load_effective_reference(
            project_id=value["project_id"],
            document_id=job.aggregate_id,
            source_sha256=baseline.source_sha256,
        )
        if current["baseline"].artifact_sha256 != baseline.artifact_sha256:
            raise NonRetryableJobError(
                error_code="decomposition_baseline_changed",
                message="A newer source baseline exists; the candidate remains tied to its original version.",
            )
    context.begin_publication()
    pointer = _write_private(container, **args, stage="decomposition", value=compiled)
    result = {
        "schema_version": "source-item-decomposition-job-result/v1",
        "status": compiled["status"],
        "source_sha256": baseline.source_sha256,
        "baseline_sha256": baseline.artifact_sha256,
        "operation_namespace": value["operation_namespace"],
        "publication_mode": value["publication_mode"],
        "artifact": pointer,
        "artifact_id": compiled["artifact_id"],
        "artifact_sha256": compiled["artifact_sha256"],
        "revision": compiled["revision"],
        "parent_count": len(compiled["records"]),
        "child_count": sum(len(items) for items in compiled["records"].values()),
        "kept_source_ids": compiled["kept_source_ids"],
        "model_call_count": len(attempts),
        "is_human_accepted": False,
    }
    if value["publication_mode"] == "WORKING_PROPOSAL":
        from reqpilotx_server.application.agent_workflow.source_item_manual_publication import manual_publication_enabled, current_manual_publication_context, attach_manual_publication_contexts
        if manual_publication_enabled(container):
            before = current_manual_publication_context(container, value["project_id"], job.aggregate_id)
            _, complete, _, tables = _source_context(value["project_id"], job.aggregate_id, tuple(record.source_record_id for record in baseline.records))
            if complete["baseline"].artifact_sha256 != baseline.artifact_sha256:
                raise ValueError("Source changed before decomposition/manual-context publication")
            attach_manual_publication_contexts(result, container=container, project_id=value["project_id"], document_id=job.aggregate_id,
                previous_context=before, document_map=complete["document_map"], baseline=baseline, table_previews=tables,
                decomposition=compiled, annotations=complete.get("image_annotations"))
    return result

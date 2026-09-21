"""Source-preserving Agent reclassification with the whole verification context."""

from __future__ import annotations

from pathlib import Path
import tempfile

from pydantic import BaseModel, ConfigDict, Field

from reqpilotx_server.application.agent_workflow.document_map_jobs import (
    _container,
    _digest,
    _projection,
)
from reqpilotx_server.application.agent_workflow.source_item_agent_jobs import (
    _read_private,
    _write_private,
    _source_context,
    _render_source_pages,
)
from reqpilotx_server.application.agent_workflow.classification_context import (
    build_classification_context,
)
from reqpilotx_server.application.agent_workflow.source_requirement_baseline import (
    SourceRequirementBaselineArtifactV1,
    SourceRequirementBaselineValidationReceiptV1,
    SourceRequirementClassificationCoordinationDecisionV1,
    compile_source_requirement_classification_coordination,
)
from reqpilotx_server.application.agent_workflow.source_requirement_jobs import (
    publish_agent_source_item_revision,
)
from reqpilotx_server.application.agent_workflow.native_structure_bridge import _no_store_headers
from reqpilotx_server.application.agent_workflow.runner_output_schema import (
    compile_strict_runner_output_schema,
)
from reqpilotx_server.application.errors import ApplicationError
from reqpilotx_server.application.transport import ResponseSink
from reqpilotx_server.ports.job_queue import DurableJob, JobExecutionContext, NonRetryableJobError
from reqpilotx_server.adapters.agents.codex_app_server.json_stage_driver import (
    LocalCodexAppServerJsonStageDriver,
)

ROOT = Path(__file__).resolve().parents[4]
CLASSIFICATION_JOB_KIND = "agent.source-item.classify"


class SourceItemClassificationReviewStartRequestV1(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command_id: str = Field(min_length=1, max_length=160)
    expected_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_baseline_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_item_ids: tuple[str, ...] = Field(min_length=1, max_length=128)
    feedback: str = Field(min_length=1, max_length=4000)
    feedback_ids: tuple[str, ...] = Field(default=(), max_length=64, exclude_if=lambda value: not value)


def _bound_review_feedback(project_id, document_id, baseline, source_item_ids, feedback_ids):
    from reqpilotx_server.application.agent_workflow.source_requirement_reference_bridge import _read_feedbacks
    if len(feedback_ids) != len(set(feedback_ids)):
        raise ApplicationError(status_code=422, detail="反馈ID不能重复。")
    if not feedback_ids:
        return []
    by_id = {item.get("feedback_id"): item for item in _read_feedbacks(
        source_sha256=baseline.source_sha256, baseline_artifact_id=baseline.artifact_id)}
    values = []
    for fid in feedback_ids:
        item = by_id.get(fid)
        if (not item or item.get("project_id") != project_id or item.get("document_id") != document_id
                or item.get("source_sha256") != baseline.source_sha256
                or item.get("baseline_artifact_id") != baseline.artifact_id
                or item.get("document_map_artifact_sha256") != baseline.document_map_artifact_sha256
                or item.get("source_record_id") not in source_item_ids):
            raise ApplicationError(status_code=409, detail="反馈与当前项目、版本或所选条目不一致。")
        values.append(item)
    return values


def _carry_review_feedback(project_id, document_id, previous, successor, consumed_ids):
    from reqpilotx_server.application.agent_workflow.source_requirement_reference_bridge import _read_feedbacks, _carry_feedbacks_to_baseline
    current = [f for f in _read_feedbacks(source_sha256=previous.source_sha256, baseline_artifact_id=previous.artifact_id)
               if f.get("project_id") == project_id and f.get("document_id") == document_id
               and f.get("source_sha256") == previous.source_sha256 and f.get("baseline_artifact_id") == previous.artifact_id
               and f.get("document_map_artifact_sha256") == previous.document_map_artifact_sha256]
    consumed = {f["feedback_id"] for f in current if f["feedback_id"] in consumed_ids or f.get("status") == "CONSUMED"}
    pending = {f["feedback_id"] for f in current if f["feedback_id"] not in consumed and f.get("status") in {"PENDING_AGENT", "ACKNOWLEDGED"}}
    for status, ids in (("CONSUMED", consumed), ("ACKNOWLEDGED", pending)):
        if ids:
            _carry_feedbacks_to_baseline(source_sha256=previous.source_sha256, from_baseline_artifact_id=previous.artifact_id,
                to_baseline_artifact_id=successor.artifact_id, feedback_ids=tuple(sorted(ids)), status=status)


class ClassificationCandidateV1(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decisions: tuple[SourceRequirementClassificationCoordinationDecisionV1, ...] = (
        Field(min_length=1, max_length=128)
    )


def start_source_item_classification_review(
    project_id: str,
    document_id: str,
    payload: SourceItemClassificationReviewStartRequestV1,
    response: ResponseSink,
):
    response.headers.update(_no_store_headers())
    container = _container()
    if not container.settings.codex_bin or not container.settings.codex_home:
        raise ApplicationError(status_code=503, detail="当前分类Agent执行器未配置，反馈可先保存。")
    current, bundle, _source_path, previews = _source_context(
        project_id, document_id, payload.source_item_ids
    )
    baseline = bundle["baseline"]
    if (
        baseline.source_sha256 != payload.expected_source_sha256
        or baseline.artifact_sha256 != payload.expected_baseline_sha256
    ):
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "classification_review_source_changed",
                "message": "The SourceItem baseline changed before review.",
            },
        )
    records = [
        record
        for record in baseline.records
        if record.source_record_id in set(payload.source_item_ids)
    ]
    node_ids = tuple(dict.fromkeys(record.node_id for record in records))
    feedback_records = _bound_review_feedback(project_id, document_id, baseline,
        set(payload.source_item_ids), payload.feedback_ids)
    packet = {
        "schema_version": "source-item-classification-review-packet/v1",
        "source_sha256": baseline.source_sha256,
        "baseline_sha256": baseline.artifact_sha256,
        "selected_source_item_ids": list(payload.source_item_ids),
        "feedback": payload.feedback,
        "feedback_records": feedback_records,
        "working_records": [record.model_dump(mode="json") for record in records],
        "semantic_context": build_classification_context(
            bundle["document_map"], node_ids=node_ids, table_previews=previews
        ),
    }
    identity = _digest(
        {
            "project_id": project_id,
            "document_id": document_id,
            **payload.model_dump(mode="json"),
        }
    )
    existing = container.jobs.get_by_idempotency(
        kind=CLASSIFICATION_JOB_KIND, idempotency_key=identity
    )
    if existing:
        return _projection(existing)
    inputs = {
        "baseline": baseline.model_dump(mode="json"),
        "validation_receipt": bundle["receipt"].model_dump(mode="json"),
        "packet": packet,
        "pages": sorted({page for record in records for page in record.pages}),
    }
    pointer = _write_private(
        container,
        project_id=project_id,
        document_id=document_id,
        namespace="classification-review",
        stage="input",
        value=inputs,
    )
    return _projection(
        container.jobs.enqueue(
            kind=CLASSIFICATION_JOB_KIND,
            aggregate_type="sor-document",
            aggregate_id=document_id,
            idempotency_key=identity,
            payload={
                "application_entrypoint": "source-item-workbench-v1",
                "schema_version": "source-item-classification-review-job/v1",
                "project_id": project_id,
                "document_id": document_id,
                "source_sha256": baseline.source_sha256,
                "baseline_sha256": baseline.artifact_sha256,
                "source_item_ids": list(payload.source_item_ids),
                "feedback_ids": list(payload.feedback_ids),
                "input_artifact": pointer,
            },
            priority=24,
            max_attempts=3,
        )
    )


def run_durable_source_item_classification_review(
    job: DurableJob, context: JobExecutionContext
):
    from reqpilotx_server.adapters.agents.portable_kit import load_portable_agent_kit
    from reqpilotx_server.adapters.agents.stage_context import compile_agent_stage_context
    from reqpilotx_server.application.agent_workflow.extraction_runtime import AgentExtractionStage

    container = _container()
    args = {
        "project_id": job.payload["project_id"],
        "document_id": job.aggregate_id,
        "namespace": "classification-review",
    }
    inputs = _read_private(container, job.payload["input_artifact"], **args)
    baseline = SourceRequirementBaselineArtifactV1.model_validate(inputs["baseline"])
    _bound_review_feedback(job.payload["project_id"], job.aggregate_id, baseline,
        set(job.payload["source_item_ids"]), tuple(job.payload.get("feedback_ids", [])))
    validation = SourceRequirementBaselineValidationReceiptV1.model_validate(
        inputs["validation_receipt"]
    )
    current, bundle, source_path, _previews = _source_context(
        job.payload["project_id"],
        job.aggregate_id,
        tuple(job.payload["source_item_ids"]),
    )
    if bundle["baseline"].artifact_sha256 != baseline.artifact_sha256:
        raise NonRetryableJobError(
            error_code="classification_review_baseline_changed",
            message="The classification review is tied to a different current baseline.",
        )
    kit = load_portable_agent_kit(ROOT / "agent-kit")
    closure = compile_agent_stage_context(
        kit, AgentExtractionStage.PARENT_PROVISION_RECOGNITION
    )
    skill = (
        closure.skill_path.read_text()
        + "\n"
        + closure.context_text
        + "\n"
        + (ROOT / "docs/需求提取/分类与验证关联规则V1.md").read_text()
    )
    driver = LocalCodexAppServerJsonStageDriver(
        codex_bin=container.settings.codex_bin,
        codex_home=container.settings.codex_home,
        repository_root=ROOT,
        model_id=container.settings.agent_model_id,
    )
    packet = inputs["packet"]
    attempts = []
    revised = None
    feedback = None
    with tempfile.TemporaryDirectory(
        prefix="reqpilotx-classification-review-", dir=container.settings.scratch_root
    ) as raw:
        root = Path(raw)
        images = _render_source_pages(
            source_path, inputs["pages"], root, baseline.source_sha256
        )
        for attempt in range(3):
            result = driver.complete(
                packet=packet
                if feedback is None
                else {**packet, "validation_feedback": feedback},
                output_schema=compile_strict_runner_output_schema(
                    ClassificationCandidateV1
                ),
                skill_name="reqpilotx-contextual-classification-review",
                skill_markdown=skill,
                task_directive="Re-evaluate only the selected working SourceItems using the full immutable source section, original logical table, document context and current classification policy. Do not trust previous classifications. Do not change any source text, row grouping, title, SRC identity or source binding. Decide whether each item is an independent requirement or retained context/gap information, then its legitimate class/role. Use the verification target and the complete provision; transport windows are not semantic boundaries. Return exactly one decision per selected source_record_id; do not add records or invent missing criteria. Write concise Chinese explanations and sorted reason codes.",
                image_inputs=images,
                authorized_image_roots=(root,),
                cancellation_requested=context.cancellation_requested,
                receipt_sink=lambda receipt: context.heartbeat(
                    progress={
                        "stage": "CLASSIFICATION_CONTEXT_REVIEW",
                        "attempt": attempt + 1,
                        "runtime_receipt": dict(receipt),
                    }
                ),
            )
            attempts.append(
                {"candidate": result.candidate, "runtime_receipt": result.receipt}
            )
            try:
                candidate = ClassificationCandidateV1.model_validate(result.candidate)
                if {
                    decision.source_record_id for decision in candidate.decisions
                } != set(job.payload["source_item_ids"]) or len(
                    candidate.decisions
                ) != len(job.payload["source_item_ids"]):
                    raise ValueError(
                        "Review must decide each requested SourceItem exactly once"
                    )
                revised, revised_validation, coordination, proof = (
                    compile_source_requirement_classification_coordination(
                        previous_artifact=baseline,
                        previous_validation_receipt=validation,
                        decisions=candidate.decisions,
                        model_id=container.settings.agent_model_id,
                        coordinator_role="APP_SERVER_WORKING_AGENT",
                    )
                )
                break
            except ValueError as exc:
                revised = None
                feedback = {
                    "reason": str(exc)[:1200],
                    "previous_candidate": result.candidate,
                }
                attempts[-1]["validation_failure"] = str(exc)[:1200]
        pointer = _write_private(
            container, **args, stage="attempts", value={"attempts": attempts}
        )
        context.heartbeat(
            progress={
                "stage": "CLASSIFICATION_REVIEW_VALIDATED"
                if revised is not None
                else "CLASSIFICATION_REVIEW_UNRESOLVED",
                "attempts_artifact": pointer,
            }
        )
        if revised is None:
            raise NonRetryableJobError(
                error_code="classification_review_unresolved",
                message="The source-preserving classification review failed validation; attempts are retained.",
            )
    result = publish_agent_source_item_revision(
        project_id=job.payload["project_id"],
        document_id=job.aggregate_id,
        document_map=bundle["document_map"],
        previous_bundle=bundle,
        baseline=revised,
        validation_receipt=revised_validation,
        operation="CLASSIFICATION_REVISION",
        affected_scope_ids=tuple(job.payload["source_item_ids"]),
        runtime_receipts=tuple(attempt["runtime_receipt"] for attempt in attempts),
        request_sha256=_digest(packet),
        candidate_sha256=_digest(attempts[-1]["candidate"]),
        domain_proof={
            "coordination": coordination.model_dump(mode="json"),
            "coordination_receipt": proof.model_dump(mode="json"),
        },
        context=context,
        container=container,
    )
    result.update(
        model_call_count=len(attempts),
        changed_source_item_ids=list(job.payload["source_item_ids"]),
    )
    _carry_review_feedback(job.payload["project_id"], job.aggregate_id, baseline, revised,
        set(job.payload.get("feedback_ids", [])))
    result["consumed_feedback_ids"] = list(job.payload.get("feedback_ids", []))
    return result

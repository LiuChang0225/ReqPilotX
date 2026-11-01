"""Atomic, resumable planning of sibling tables competing for one text scope.

Only immutable, source-bound private candidates are checkpointed here. The caller
publishes the entire table/text group once the shared domain compiler accepts it.
"""
from __future__ import annotations

from io import BytesIO
import hashlib
import json
from typing import Any, Mapping

from reqpilotx_server.ports.job_queue import NonRetryableJobError
from reqpilotx_server.ports.source_requirement_window_executor import SourceRequirementWindowTaskV1


_COMPLETED = "source_requirement_completed_scopes"
_WINDOWS = "source_requirement_joint_window_checkpoints"


def _executor_identity(executor, current):
    from . import source_requirement_jobs as jobs
    identity = {key: getattr(executor, key, None) for key in (
        "adapter_id", "adapter_version", "transport_kind", "model_id", "reasoning_effort", "provider",
    )}
    # Cached completed candidates must obey the same installed-kit boundary as
    # window checkpoints, even though no executor/model call is needed on retry.
    if callable(getattr(executor, "_load_stage_kit", None)):
        kit = executor._load_stage_kit()
        identity["kit_version"] = kit.identity.kit_version
        identity["kit_bundle_sha256"] = kit.identity.bundle_sha256
    identity["semantic_context_sha256"] = jobs.canonical_json_sha256(current.get("semantic_context"))
    return identity


def load_completed_table(*, current, progress, container, project_id, document_id, executor):
    from . import source_requirement_jobs as jobs
    request = current["request"]
    pointer = (progress.get(_COMPLETED) or {}).get(request.logical_table_scope_id)
    if pointer is None:
        return None
    try:
        if pointer.get("executor_identity") != _executor_identity(executor, current):
            raise ValueError("completed joint table executor/context identity changed")
        encoded = jobs._read_private_artifact(
            container.object_store, pointer,
            required_prefix=("runtime/agent-source-requirement-scope-results/"
                f"{jobs._safe_token(project_id)}/{jobs._safe_token(document_id)}/"
                f"{request.source_sha256}/{request.request_sha256}/"),
            expected_media_type=jobs.SOURCE_REQUIREMENT_SCOPE_ARTIFACT_MEDIA_TYPE,
        )
        artifact = jobs.SourceRequirementScopeResultArtifactV1.model_validate_json(encoded)
        if (artifact.request != request
                or artifact.document_map_artifact_sha256 != current["document_map"].artifact_sha256
                or artifact.run_receipt.model_id != executor.model_id
                or artifact.run_receipt.reasoning_effort != executor.reasoning_effort):
            raise ValueError("completed joint table checkpoint identity changed")
        jobs.validate_source_requirement_slice_candidate(request, artifact.candidate)
        return artifact
    except (OSError, TypeError, ValueError) as exc:
        raise NonRetryableJobError(
            error_code="source_requirement_joint_result_checkpoint_invalid",
            message="A completed joint table candidate failed identity or integrity validation.",
        ) from exc


def save_completed_table(*, current, candidate, run_receipt, progress, context,
                         container, project_id, document_id):
    from . import source_requirement_jobs as jobs
    artifact = jobs._build_result_artifact(document_map=current["document_map"],
        request=current["request"], candidate=candidate, run_receipt=run_receipt)
    encoded = json.dumps(artifact.model_dump(mode="json"), ensure_ascii=False,
        sort_keys=True, separators=(",", ":")).encode("utf-8")
    stored = container.object_store.put(
        key=jobs._scope_result_object_key(project_id=project_id, document_id=document_id, artifact=artifact),
        source=BytesIO(encoded), expected_sha256=hashlib.sha256(encoded).hexdigest(),
        media_type=jobs.SOURCE_REQUIREMENT_SCOPE_ARTIFACT_MEDIA_TYPE,
    )
    if stored.sha256 != hashlib.sha256(encoded).hexdigest() or stored.size != len(encoded):
        raise ValueError("joint candidate object-store receipt drifted")
    # Copy nested state rather than mutating a DurableJob's historical progress.
    completed = dict(progress.get(_COMPLETED) or {})
    completed[current["request"].logical_table_scope_id] = {
        "object_key": stored.key, "object_sha256": stored.sha256,
        "size": stored.size, "media_type": stored.media_type,
        "executor_identity": _executor_identity(container.source_requirement_window_executor, current),
    }
    progress[_COMPLETED] = completed
    context.heartbeat(progress={**progress, "stage": "JOINT_TABLE_CANDIDATE_CHECKPOINTED",
        "message": "已保存联合计划中的表格候选；尚未发布产品版本。"})
    return artifact


class _JointCheckpointContext:
    """Keep sibling window receipts separate from the primary table receipt."""
    def __init__(self, parent, progress, scope_id):
        self.parent, self.progress, self.scope_id = parent, progress, scope_id

    def heartbeat(self, progress=None):
        update = dict(progress or {})
        receipt = update.pop("source_requirement_window_checkpoint", None)
        if receipt is not None:
            checkpoints = dict(self.progress.get(_WINDOWS) or {})
            checkpoints[self.scope_id] = receipt
            self.progress[_WINDOWS] = checkpoints
        return self.parent.heartbeat(progress={**self.progress, **update,
            "joint_table_scope_id": self.scope_id})


def run_joint_tables(*, current, candidate, container, context, project_id,
                     document_id, progress, timeout_seconds, max_concurrency):
    from . import source_requirement_jobs as jobs
    request = current["request"]
    document_map = current["document_map"]
    node = next(item for item in document_map.nodes if item.node_id == request.node_id)
    sibling_ids = tuple(dict.fromkeys(block.logical_table_scope_id for block in node.content_blocks
        if block.logical_table_scope_id and block.logical_table_scope_id != request.logical_table_scope_id))
    if not sibling_ids:
        return ()
    covered = set()
    base_bundle = current.get("source_requirement_base_bundle")
    if isinstance(base_bundle, Mapping):
        effective = jobs.resolve_effective_source_requirement_bundle(
            project_id=project_id, document_id=document_id, base_bundle=base_bundle, container=container)
        covered = set(effective["baseline"].covered_scope_ids)
        if f"text:{node.node_id}" in covered:
            raise NonRetryableJobError(error_code="source_requirement_mixed_text_already_covered",
                message="The text scope was already published and requires an explicit joint revision.")
    executor = container.source_requirement_window_executor
    artifacts = []
    for scope_id in sibling_ids:
        if scope_id in covered:
            continue
        if context.cancellation_requested():
            raise NonRetryableJobError(error_code="source_requirement_joint_cancelled",
                message="Joint table processing was cancelled before publication.")
        sibling = jobs._current_scope_context(project_id=project_id, document_id=document_id,
            logical_table_scope_id=scope_id)
        if (sibling["source_sha256"] != current["source_sha256"]
                or sibling["native_artifact_id"] != current["native_artifact_id"]
                or sibling["document_map"].artifact_sha256 != document_map.artifact_sha256
                or sibling["request"].logical_table_scope_id != scope_id
                or sibling["request"].node_id != node.node_id):
            raise NonRetryableJobError(error_code="source_requirement_joint_identity_changed",
                message="A sibling table no longer belongs to the pinned source, map and node.")
        artifact = load_completed_table(current=sibling, progress=progress, container=container,
            project_id=project_id, document_id=document_id, executor=executor)
        if artifact is None:
            checkpoint_progress = jobs.NativeSourceRequirementCheckpointProgress(
                context=_JointCheckpointContext(context, progress, scope_id),
                store=jobs.NativeSourceRequirementDurableCheckpointStore(
                    object_store=container.object_store, project_id=project_id, document_id=document_id),
                initial={"source_requirement_window_checkpoint": (progress.get(_WINDOWS) or {}).get(scope_id)},
            )
            try:
                checkpoint = checkpoint_progress.load()
            except ValueError as exc:
                raise NonRetryableJobError(error_code="source_requirement_joint_window_checkpoint_invalid",
                    message="A sibling table window checkpoint failed integrity validation.") from exc
            def save_window(value):
                checkpoint_progress.save(jobs._validated_checkpoint(value))
            commitments = [{"logical_table_scope_id": request.logical_table_scope_id,
                "candidate": candidate.model_dump(mode="json")}] + [
                    {"logical_table_scope_id": item.request.logical_table_scope_id,
                     "candidate": item.candidate.model_dump(mode="json")} for item in artifacts]
            semantic_context = {**(sibling.get("semantic_context") or {}), "joint_table_plan": {
                "node_id": node.node_id,
                "table_scope_ids": [request.logical_table_scope_id, *sibling_ids],
                "completed_table_candidates": commitments,
                "instruction": "Decide this table in the full node context. Preserve prior exact text ownership; shared context is not ownership. Conflicting ownership requires revision, never silent truncation.",
            }}
            execution = executor.execute(SourceRequirementWindowTaskV1(
                project_id=project_id, document_id=document_id, source_path=sibling["source_path"],
                document_map=document_map.model_dump(mode="json"),
                scope_request=sibling["request"].model_dump(mode="json"),
                timeout_seconds=timeout_seconds, max_concurrency=max_concurrency,
                semantic_context=semantic_context,
            ), resume_checkpoint=None if checkpoint is None else checkpoint.model_dump(mode="json"),
                checkpoint_sink=save_window, cancellation_requested=context.cancellation_requested)
            sibling_candidate, receipt = jobs._validate_execution(execution, request=sibling["request"])
            artifact = save_completed_table(current=sibling, candidate=sibling_candidate,
                run_receipt=receipt, progress=progress, context=context, container=container,
                project_id=project_id, document_id=document_id)
        artifacts.append(artifact)
    return tuple(artifacts)

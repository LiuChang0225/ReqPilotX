#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from typing import Any, Mapping
from uuid import uuid4


PROTOTYPE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PROTOTYPE_ROOT.parent
if str(PROTOTYPE_ROOT) not in sys.path:
    sys.path.insert(0, str(PROTOTYPE_ROOT))

from app.adapters.agents.codex_app_server.canonical_document_session import (  # noqa: E402
    CanonicalProductEvidenceRequest,
    prepare_canonical_app_server_document_session,
)
from app.adapters.agents.codex_app_server.canonical_stage_driver import (  # noqa: E402
    canonical_sha256,
)
from app.adapters.agents.codex_app_server.canonical_stage_transport import (  # noqa: E402
    LocalCodexAppServerStructuredStageTransport,
)
from app.adapters.agents.codex_app_server.document_map_heading_scope_driver import (  # noqa: E402
    CanonicalDocumentMapHeadingScopeStageDriver,
    DocumentMapHeadingScopeCandidateValidationError,
    DocumentMapHeadingScopeHostCompilationError,
)
from app.adapters.agents.codex_app_server.document_map_replacement_driver import (  # noqa: E402
    CanonicalDocumentMapScopeReplacementStageDriver,
    DocumentMapScopeReplacementCandidateValidationError,
    DocumentMapScopeReplacementHostCompilationError,
)
from app.adapters.agents.codex_app_server.lifecycle import (  # noqa: E402
    LifecycleProbeError,
    _canary_runtime_state,
)
from app.adapters.agents.portable_kit import load_portable_agent_kit  # noqa: E402
from app.adapters.agents.reference_workspace import (  # noqa: E402
    materialize_product_evidence_workspace,
)
from app.adapters.persistence.native_document_map_scope_checkpoints import (  # noqa: E402
    NativeDocumentMapScopeCheckpointStore,
)
from app.adapters.storage.local_object_store import LocalObjectStore  # noqa: E402
from app.application.agent_workflow.document_map_exploration import (  # noqa: E402
    DocumentMapSurveyCompilationV1,
    DocumentMapVisualCoverageReceiptV1,
)
from app.application.agent_workflow.document_map_scope import (  # noqa: E402
    DocumentMapScopeRequestV1,
    build_document_map_scope_request_from_evidence_index,
)
from app.application.agent_workflow.document_map_replacement import (  # noqa: E402
    DocumentMapScopeReplacementCompilationV1,
    build_document_map_scope_replacement_request,
    load_document_map_scope_evidence,
)
from app.application.agent_workflow.document_map_builder import (  # noqa: E402
    assemble_and_publish_document_map_working_revision,
    project_document_map_scope_replacement,
    restore_document_map_scope_projections,
)
from app.application.agent_workflow.native_structure_working_revision_bridge import (  # noqa: E402
    load_native_structure_working_revision,
)
from app.application.agent_workflow.extraction_runtime import (  # noqa: E402
    AgentExtractionStage,
)
from app.application.agent_workflow.native_heading_skeleton import (  # noqa: E402
    NativeHeadingSkeletonArtifactV1,
)


CANARY_SCHEMA_VERSION = "reqpilotx-document-map-heading-scope-canary/v1"


def run_document_map_heading_scope_canary(
    *,
    source_path: Path,
    corpus_root: Path,
    survey_result_path: Path,
    heading_skeleton_path: Path,
    chapter_numbers: tuple[str, ...],
    document_role: str,
    codex_bin: Path,
    codex_home: Path,
    repository_root: Path,
    timeout_seconds: float,
    private_output: Path | None = None,
    checkpoint_root: Path | None = None,
    complete_replacement: bool = False,
    publication_project_id: str | None = None,
    publication_document_id: str | None = None,
    publication_repository: Any = None,
    publication_object_store: Any = None,
    resume_scope_checkpoint_paths: tuple[Path, ...] = (),
    replay_scope_checkpoint_paths: tuple[Path, ...] = (),
) -> dict[str, object]:
    source = source_path.expanduser().resolve(strict=True)
    corpus = corpus_root.expanduser().resolve(strict=True)
    repository = repository_root.expanduser().resolve(strict=True)
    survey_path = survey_result_path.expanduser().resolve(strict=True)
    skeleton_path = heading_skeleton_path.expanduser().resolve(strict=True)
    if source_path.is_symlink() or not source.is_file():
        raise LifecycleProbeError("HEADING_SCOPE_SOURCE_INVALID")
    try:
        source.relative_to(corpus)
    except ValueError as exc:
        raise LifecycleProbeError("HEADING_SCOPE_SOURCE_UNAUTHORIZED") from exc
    if timeout_seconds <= 0 or not chapter_numbers:
        raise LifecycleProbeError("HEADING_SCOPE_ARGUMENT_INVALID")
    publication_values = (
        publication_project_id,
        publication_document_id,
        publication_repository,
        publication_object_store,
    )
    if any(value is not None for value in publication_values) and (
        not complete_replacement
        or not all(value is not None for value in publication_values)
    ):
        raise LifecycleProbeError("DOCUMENT_MAP_PUBLICATION_ARGUMENT_INVALID")
    if (
        resume_scope_checkpoint_paths or replay_scope_checkpoint_paths
    ) and publication_repository is None:
        raise LifecycleProbeError("DOCUMENT_MAP_RESUME_REQUIRES_PRODUCT_REVISION")
    replay_compilations = tuple(
        DocumentMapScopeReplacementCompilationV1.model_validate(
            _read_json_mapping(path.resolve(strict=True))
        )
        for path in replay_scope_checkpoint_paths
    )
    replay_by_scope = {item.scope_id: item for item in replay_compilations}
    source_sha256 = _sha256(source)
    survey_payload = _read_json_mapping(survey_path)
    try:
        survey = DocumentMapSurveyCompilationV1.model_validate(
            survey_payload["compilation"]
        )
        visual = DocumentMapVisualCoverageReceiptV1.model_validate(
            survey_payload["visual_coverage"]
        )
        skeleton = NativeHeadingSkeletonArtifactV1.model_validate_json(
            skeleton_path.read_bytes()
        )
    except (KeyError, OSError, ValueError) as exc:
        raise LifecycleProbeError("HEADING_SCOPE_PREDECESSOR_INVALID") from exc
    if (
        survey.source_sha256 != source_sha256
        or visual.source_sha256 != source_sha256
        or skeleton.source_sha256 != source_sha256
    ):
        raise LifecycleProbeError("HEADING_SCOPE_PREDECESSOR_SOURCE_MISMATCH")
    chapter_roots = {
        item.number: item
        for item in skeleton.candidates
        if item.level == 1 and item.chapter_candidate_id == item.candidate_id
    }
    ordered_roots = tuple(
        sorted(chapter_roots.values(), key=lambda item: item.source_order)
    )
    next_root_by_number = {
        current.number: following
        for current, following in zip(ordered_roots, ordered_roots[1:])
    }
    if any(
        number not in chapter_roots or number not in next_root_by_number
        for number in chapter_numbers
    ):
        raise LifecycleProbeError("HEADING_SCOPE_CHAPTER_BOUNDARY_MISSING")

    output = private_output.expanduser().resolve() if private_output else None
    checkpoint = (
        checkpoint_root.expanduser().resolve() if checkpoint_root is not None else None
    )
    for target in (output, checkpoint):
        if target is None:
            continue
        try:
            target.relative_to(corpus)
        except ValueError:
            pass
        else:
            raise LifecycleProbeError("HEADING_SCOPE_OUTPUT_INSIDE_SOURCE_CORPUS")

    started = time.monotonic()
    private_results: list[dict[str, object]] = []
    public_scopes: list[dict[str, object]] = []
    scope_projections = []
    working_revision = None
    inherited_compilations = ()
    with _canary_runtime_state(
        codex_bin=codex_bin,
        codex_home=codex_home,
        repository_root=repository,
    ) as base_runtime:
        kit = load_portable_agent_kit(repository / "agent-kit")
        if resume_scope_checkpoint_paths:
            inherited_compilations = tuple(
                DocumentMapScopeReplacementCompilationV1.model_validate(
                    _read_json_mapping(path.resolve(strict=True))
                )
                for path in resume_scope_checkpoint_paths
            )
            head = publication_repository.get_working_revision_head(
                project_id=publication_project_id,
                document_id=publication_document_id,
                source_sha256=source_sha256,
            )
            if head is None or head.run_id != survey.run_id:
                raise LifecycleProbeError("DOCUMENT_MAP_RESUME_HEAD_MISMATCH")
            record = publication_repository.get_working_revision_object(
                project_id=publication_project_id,
                document_id=publication_document_id,
                run_id=head.run_id,
                revision=head.revision,
            )
            working_revision = load_native_structure_working_revision(
                record=record, object_store=publication_object_store
            )
            scope_projections.extend(
                restore_document_map_scope_projections(
                    revision=working_revision,
                    compilations=inherited_compilations,
                    survey=survey,
                    kit_bundle_sha256=kit.identity.bundle_sha256,
                    object_store=publication_object_store,
                )
            )
        source_handle = "source-handle-" + uuid4().hex
        with tempfile.TemporaryDirectory(
            prefix="document-map-scope-evidence-",
            dir=base_runtime.state_path,
        ) as raw_template:
            template = Path(raw_template) / "workspace"
            manifest = materialize_product_evidence_workspace(
                source,
                expected_source_sha256=source_sha256,
                document_role=document_role,
                output_directory=template,
                corpus_root=corpus,
                repository_root=repository,
                run_id=survey.run_id,
                opaque_source_handle=source_handle,
                agent_kit_root=repository / "agent-kit",
            )
            ledger = _read_jsonl(template / "pages" / "source-ordinal-ledger.jsonl")
            source_locations = {
                item["source_handle"]: item.get("bbox")
                for item in _read_jsonl(template / "pages" / "source-map.jsonl")
            }
            scope_ledger = tuple(
                {**item, "bbox": source_locations.get(item["source_node_id"])}
                for item in ledger
            )
            capture = _require_mapping(manifest.get("capture"))
            if any(
                item.source_ir_canonical_sha256
                != capture.get("source_ir_canonical_sha256")
                for item in inherited_compilations
            ):
                raise LifecycleProbeError("DOCUMENT_MAP_RESUME_SOURCE_IR_DRIFTED")
            bundle_sha256 = str(manifest.get("workspace_bundle_sha256") or "")
            requests = tuple(
                build_document_map_scope_request_from_evidence_index(
                    run_id=survey.run_id,
                    source_sha256=source_sha256,
                    source_ir_id=str(capture.get("source_ir_id") or ""),
                    source_ir_canonical_sha256=str(
                        capture.get("source_ir_canonical_sha256") or ""
                    ),
                    evidence_workspace_bundle_sha256=bundle_sha256,
                    source_ordinal_records=scope_ledger,
                    survey=survey,
                    visual_coverage=visual,
                    heading_skeleton=skeleton,
                    start_candidate_id=chapter_roots[number].candidate_id,
                    next_candidate_id=next_root_by_number[number].candidate_id,
                )
                for number in chapter_numbers
            )
            object_store_root = (
                checkpoint
                if checkpoint is not None
                else base_runtime.state_path / "document-map-scope-checkpoints"
            )
            checkpoint_store = NativeDocumentMapScopeCheckpointStore(
                object_store=LocalObjectStore(object_store_root),
                project_id="f06-reference",
                document_id="f06-bz-0007",
            )
            for request in requests:
                if any(
                    item.scope_id == request.scope_id for item in inherited_compilations
                ):
                    raise LifecycleProbeError(
                        "DOCUMENT_MAP_RESUME_SCOPE_ALREADY_ACCEPTED"
                    )
                scope_started = time.monotonic()
                if request.scope_id in replay_by_scope:
                    compiled = replay_by_scope[request.scope_id]
                    if compiled.survey_compilation_sha256 != survey.compilation_sha256:
                        raise LifecycleProbeError("DOCUMENT_MAP_REPLAY_SURVEY_DRIFTED")
                    index = load_document_map_scope_evidence(
                        root=template, request=request
                    )
                    scope_projections.append(
                        project_document_map_scope_replacement(
                            compilation=compiled, evidence=index
                        )
                    )
                    working_revision = (
                        assemble_and_publish_document_map_working_revision(
                            project_id=publication_project_id or "",
                            document_id=publication_document_id or "",
                            run_id=survey.run_id,
                            source_sha256=source_sha256,
                            source_ir_canonical_sha256=str(
                                capture["source_ir_canonical_sha256"]
                            ),
                            evidence_workspace_bundle_sha256=bundle_sha256,
                            survey=survey,
                            scope_replacements=tuple(scope_projections),
                            repository=publication_repository,
                            object_store=publication_object_store,
                            kit_bundle_sha256=kit.identity.bundle_sha256,
                            revision=1
                            if working_revision is None
                            else working_revision.revision + 1,
                            previous=working_revision,
                        )
                    )
                    public_scopes.append(
                        {
                            "chapter_number": request.advisory_number,
                            "coordination_required": False,
                            "execution_mode": "HOST_CHECKPOINT_REPLAY",
                            "agent_call_count": 0,
                            "accepted_heading_count": len(compiled.heading_nodes),
                            "source_node_count": len(request.source_node_ids),
                            "replacement": {
                                "status": compiled.status,
                                "coordination_required": False,
                                "logical_table_count": len(compiled.logical_tables),
                                "physical_fragment_count": sum(
                                    len(item.fragments)
                                    for item in compiled.logical_tables
                                ),
                                "compilation_sha256": compiled.compilation_sha256,
                            },
                        }
                    )
                    private_results.append(
                        {
                            "request": request.model_dump(mode="json"),
                            "execution_mode": "HOST_CHECKPOINT_REPLAY",
                            "compilation": compiled.model_dump(mode="json"),
                        }
                    )
                    continue
                session = prepare_canonical_app_server_document_session(
                    base_runtime=base_runtime,
                    canonical_kit_root=repository / "agent-kit",
                    stages=(AgentExtractionStage.DOCUMENT_UNDERSTANDING,),
                    evidence_request=CanonicalProductEvidenceRequest(
                        source_path=source,
                        expected_source_sha256=source_sha256,
                        document_role=document_role,
                        authorized_corpus_root=corpus,
                        run_id=survey.run_id,
                        opaque_source_handle=source_handle,
                    ),
                    document_map_scope_stage_input_request=request,
                    evidence_workspace_template=template,
                    repository_root=repository,
                )
                try:
                    result = CanonicalDocumentMapHeadingScopeStageDriver(
                        transport=LocalCodexAppServerStructuredStageTransport(
                            timeout_seconds=timeout_seconds,
                        )
                    ).run(
                        session,
                        request=request,
                        task_binding_sha256=canonical_sha256(
                            {
                                "run_id": survey.run_id,
                                "source_sha256": source_sha256,
                                "kit_bundle_sha256": kit.identity.bundle_sha256,
                                "execution_mode": "DOCUMENT_MAP_HEADING_SCOPE",
                                "request_sha256": request.request_sha256,
                            }
                        ),
                    )
                finally:
                    session.close()
                receipt = None
                if result.compilation is not None:
                    receipt = checkpoint_store.save(
                        request=request,
                        compilation=result.compilation,
                        kit_bundle_sha256=kit.identity.bundle_sha256,
                    )
                    if checkpoint_store.load(receipt) != result.compilation:
                        raise LifecycleProbeError(
                            "HEADING_SCOPE_CHECKPOINT_ROUNDTRIP_FAILED"
                        )
                replacement_result = None
                replacement_request = None
                replacement_receipt = None
                replacement_elapsed_ms = None
                if complete_replacement and result.compilation is not None:
                    replacement_started = time.monotonic()
                    replacement_request = build_document_map_scope_replacement_request(
                        scope_request=request,
                        heading_compilation=result.compilation,
                    )
                    replacement_session = prepare_canonical_app_server_document_session(
                        base_runtime=base_runtime,
                        canonical_kit_root=repository / "agent-kit",
                        stages=(AgentExtractionStage.DOCUMENT_UNDERSTANDING,),
                        evidence_request=CanonicalProductEvidenceRequest(
                            source_path=source,
                            expected_source_sha256=source_sha256,
                            document_role=document_role,
                            authorized_corpus_root=corpus,
                            run_id=survey.run_id,
                            opaque_source_handle=source_handle,
                        ),
                        document_map_replacement_stage_input_request=replacement_request,
                        evidence_workspace_template=template,
                        repository_root=repository,
                    )
                    try:
                        replacement_result = CanonicalDocumentMapScopeReplacementStageDriver(
                            transport=LocalCodexAppServerStructuredStageTransport(
                                timeout_seconds=timeout_seconds,
                            )
                        ).run(
                            replacement_session,
                            request=replacement_request,
                            task_binding_sha256=canonical_sha256(
                                {
                                    "run_id": survey.run_id,
                                    "source_sha256": source_sha256,
                                    "kit_bundle_sha256": kit.identity.bundle_sha256,
                                    "execution_mode": "DOCUMENT_MAP_SCOPE_REPLACEMENT",
                                    "request_sha256": replacement_request.request_sha256,
                                }
                            ),
                        )
                    finally:
                        replacement_session.close()
                    if replacement_result.compilation is not None:
                        replacement_receipt = checkpoint_store.save(
                            request=replacement_request,
                            compilation=replacement_result.compilation,
                            kit_bundle_sha256=kit.identity.bundle_sha256,
                        )
                        if (
                            checkpoint_store.load(replacement_receipt)
                            != replacement_result.compilation
                        ):
                            raise LifecycleProbeError(
                                "REPLACEMENT_CHECKPOINT_ROUNDTRIP_FAILED"
                            )
                        scope_evidence = load_document_map_scope_evidence(
                            root=template,
                            request=replacement_request,
                        )
                        scope_projections.append(
                            project_document_map_scope_replacement(
                                compilation=replacement_result.compilation,
                                evidence=scope_evidence,
                            )
                        )
                        if publication_repository is not None:
                            working_revision = (
                                assemble_and_publish_document_map_working_revision(
                                    project_id=publication_project_id or "",
                                    document_id=publication_document_id or "",
                                    run_id=survey.run_id,
                                    source_sha256=source_sha256,
                                    source_ir_canonical_sha256=str(
                                        capture["source_ir_canonical_sha256"]
                                    ),
                                    evidence_workspace_bundle_sha256=bundle_sha256,
                                    survey=survey,
                                    scope_replacements=tuple(scope_projections),
                                    repository=publication_repository,
                                    object_store=publication_object_store,
                                    kit_bundle_sha256=kit.identity.bundle_sha256,
                                    revision=1
                                    if working_revision is None
                                    else working_revision.revision + 1,
                                    previous=working_revision,
                                )
                            )
                    replacement_elapsed_ms = round(
                        (time.monotonic() - replacement_started) * 1000
                    )
                elapsed_ms = round((time.monotonic() - scope_started) * 1000)
                public_scope = _public_scope_result(
                    request=request,
                    result=result,
                    checkpoint_receipt=receipt,
                    elapsed_milliseconds=elapsed_ms,
                )
                if replacement_result is not None:
                    replacement_compilation = replacement_result.compilation
                    public_scope["replacement"] = {
                        "action": replacement_result.candidate.action.value,
                        "coordination_required": replacement_result.coordination_required,
                        "status": replacement_compilation.status
                        if replacement_compilation
                        else None,
                        "logical_table_count": len(
                            replacement_compilation.logical_tables
                        )
                        if replacement_compilation
                        else 0,
                        "physical_fragment_count": sum(
                            len(item.fragments)
                            for item in replacement_compilation.logical_tables
                        )
                        if replacement_compilation
                        else 0,
                        "text_ownership_count": len(
                            replacement_compilation.text_ownership
                        )
                        if replacement_compilation
                        else 0,
                        "table_text_alias_count": sum(
                            item.table_evidence_scope_id is not None
                            for item in replacement_compilation.text_ownership
                        )
                        if replacement_compilation
                        else 0,
                        "asset_placement_count": len(
                            replacement_compilation.asset_ownership
                        )
                        if replacement_compilation
                        else 0,
                        "compilation_sha256": replacement_compilation.compilation_sha256
                        if replacement_compilation
                        else None,
                        "checkpoint_receipt_sha256": replacement_receipt.receipt_sha256
                        if replacement_receipt
                        else None,
                        "elapsed_milliseconds": replacement_elapsed_ms,
                        "skill_inputs": replacement_result.tool_activity_summary.get(
                            "skill_inputs"
                        ),
                        "pdf_primary_runtime": replacement_result.tool_activity_summary.get(
                            "pdf_primary_runtime"
                        ),
                    }
                public_scopes.append(public_scope)
                private_results.append(
                    {
                        "request": request.model_dump(mode="json"),
                        "candidate": result.candidate.model_dump(mode="json"),
                        "compilation": (
                            result.compilation.model_dump(mode="json")
                            if result.compilation is not None
                            else None
                        ),
                        "checkpoint_receipt": (
                            receipt.model_dump(mode="json")
                            if receipt is not None
                            else None
                        ),
                        "tool_activity_summary": result.tool_activity_summary,
                        "replacement": (
                            {
                                "request": replacement_request.model_dump(mode="json"),
                                "candidate": replacement_result.candidate.model_dump(
                                    mode="json"
                                ),
                                "compilation": replacement_result.compilation.model_dump(
                                    mode="json"
                                )
                                if replacement_result.compilation
                                else None,
                                "checkpoint_receipt": replacement_receipt.model_dump(
                                    mode="json"
                                )
                                if replacement_receipt
                                else None,
                                "tool_activity_summary": replacement_result.tool_activity_summary,
                            }
                            if replacement_request is not None
                            and replacement_result is not None
                            else None
                        ),
                    }
                )

    if output is not None:
        _write_private_result(
            output,
            {
                "schema_version": CANARY_SCHEMA_VERSION,
                "source_sha256": source_sha256,
                "survey_compilation_sha256": survey.compilation_sha256,
                "kit_version": kit.identity.kit_version,
                "kit_bundle_sha256": kit.identity.bundle_sha256,
                "adapter_version": kit.runner_profile(
                    "app-server-product"
                ).adapter_version,
                "scopes": private_results,
            },
        )
    source_node_sets = [
        set(item["request"]["source_node_ids"])  # type: ignore[index]
        for item in private_results
    ]
    source_overlap_count = sum(
        len(left.intersection(right))
        for index, left in enumerate(source_node_sets)
        for right in source_node_sets[index + 1 :]
    )
    return {
        "schema_version": CANARY_SCHEMA_VERSION,
        "status": (
            "PASS"
            if all(
                not bool(item["coordination_required"])
                and (
                    not complete_replacement
                    or not bool(
                        (item.get("replacement") or {}).get(
                            "coordination_required", True
                        )
                    )
                )
                for item in public_scopes
            )
            else "COORDINATION_REQUIRED"
        ),
        "source_sha256": source_sha256,
        "survey_compilation_sha256": survey.compilation_sha256,
        "kit_version": kit.identity.kit_version,
        "kit_bundle_sha256": kit.identity.bundle_sha256,
        "adapter_version": kit.runner_profile("app-server-product").adapter_version,
        "scope_count": len(public_scopes),
        "inherited_scope_count": len(inherited_compilations),
        "published_working_revision": working_revision.head().model_dump(mode="json")
        if working_revision is not None
        else None,
        "source_overlap_count": source_overlap_count,
        "scopes": public_scopes,
        "elapsed_milliseconds": round((time.monotonic() - started) * 1000),
        "private_output_written": output is not None,
        "durable_checkpoint_root_used": checkpoint is not None,
        "complete_scope_replacement_enabled": complete_replacement,
        "working_revision_head": working_revision.head().model_dump(mode="json")
        if working_revision
        else None,
    }


def _public_scope_result(
    *,
    request: DocumentMapScopeRequestV1,
    result: Any,
    checkpoint_receipt: Any,
    elapsed_milliseconds: int,
) -> dict[str, object]:
    compilation = result.compilation
    return {
        "chapter_number": request.advisory_number,
        "page_numbers": list(request.page_numbers),
        "source_node_count": len(request.source_node_ids),
        "advisory_heading_candidate_count": len(request.heading_candidates),
        "action": result.candidate.action.value,
        "coordination_required": result.coordination_required,
        "opened_high_resolution_page_numbers": list(
            result.opened_high_resolution_page_numbers
        ),
        "accepted_heading_count": (
            len(compilation.heading_nodes) if compilation is not None else 0
        ),
        "rejected_heading_candidate_count": (
            len(compilation.non_heading_candidate_ids) if compilation is not None else 0
        ),
        "finding_codes": [item.finding_code for item in result.candidate.findings],
        "skill_inputs": result.tool_activity_summary.get("skill_inputs"),
        "request_sha256": request.request_sha256,
        "candidate_sha256": (
            compilation.candidate_sha256 if compilation is not None else None
        ),
        "compilation_sha256": (
            compilation.compilation_sha256 if compilation is not None else None
        ),
        "checkpoint_receipt_sha256": (
            checkpoint_receipt.receipt_sha256
            if checkpoint_receipt is not None
            else None
        ),
        "elapsed_milliseconds": elapsed_milliseconds,
    }


def _read_json_mapping(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LifecycleProbeError("HEADING_SCOPE_PRIVATE_INPUT_INVALID") from exc
    return _require_mapping(value)


def _read_jsonl(path: Path) -> tuple[Mapping[str, Any], ...]:
    try:
        values = tuple(
            _require_mapping(json.loads(line))
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise LifecycleProbeError("HEADING_SCOPE_EVIDENCE_INDEX_INVALID") from exc
    if not values:
        raise LifecycleProbeError("HEADING_SCOPE_EVIDENCE_INDEX_INVALID")
    return values


def _require_mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise LifecycleProbeError("HEADING_SCOPE_PRIVATE_INPUT_INVALID")
    return value


def _write_private_result(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(encoded)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--corpus-root", type=Path, required=True)
    parser.add_argument("--survey-result", type=Path, required=True)
    parser.add_argument("--heading-skeleton", type=Path, required=True)
    parser.add_argument("--chapter", action="append", required=True)
    parser.add_argument("--document-role", default="CUSTOMER_SOR")
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
    parser.add_argument("--repository-root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--timeout-seconds", type=float, default=900.0)
    parser.add_argument("--private-output", type=Path)
    parser.add_argument("--checkpoint-root", type=Path)
    parser.add_argument("--complete-replacement", action="store_true")
    parser.add_argument("--publish-project-id")
    parser.add_argument("--publish-document-id")
    parser.add_argument(
        "--resume-scope-checkpoint", type=Path, action="append", default=[]
    )
    parser.add_argument(
        "--replay-scope-checkpoint", type=Path, action="append", default=[]
    )
    args = parser.parse_args()
    container = None
    try:
        if bool(args.publish_project_id) != bool(args.publish_document_id):
            raise LifecycleProbeError("DOCUMENT_MAP_PUBLICATION_ARGUMENT_INVALID")
        if args.publish_project_id:
            if not args.complete_replacement:
                raise LifecycleProbeError(
                    "DOCUMENT_MAP_PUBLICATION_REQUIRES_REPLACEMENT"
                )
            from app.application import runtime
            from app.application.agent_workflow.native_structure_bridge import (
                _document_source_sha256,
                _require_project_document,
            )
            from app.infrastructure.runtime_container import build_runtime_container
            from app.infrastructure.runtime_settings import RuntimeSettings

            settings = RuntimeSettings.from_environment(prototype_dir=PROTOTYPE_ROOT)
            container = build_runtime_container(settings, apply_migrations=True)
            runtime.configure_worker_runtime(container)
            document = _require_project_document(
                args.publish_project_id, args.publish_document_id
            )
            if _document_source_sha256(document) != _sha256(
                args.source.expanduser().resolve(strict=True)
            ):
                raise LifecycleProbeError("DOCUMENT_MAP_PUBLICATION_SOURCE_MISMATCH")
        result = run_document_map_heading_scope_canary(
            source_path=args.source,
            corpus_root=args.corpus_root,
            survey_result_path=args.survey_result,
            heading_skeleton_path=args.heading_skeleton,
            chapter_numbers=tuple(args.chapter),
            document_role=args.document_role,
            codex_bin=args.codex_bin,
            codex_home=args.codex_home,
            repository_root=args.repository_root,
            timeout_seconds=args.timeout_seconds,
            private_output=args.private_output,
            checkpoint_root=args.checkpoint_root,
            complete_replacement=args.complete_replacement,
            publication_project_id=args.publish_project_id,
            publication_document_id=args.publish_document_id,
            publication_repository=(
                container.agent_structure_loop_repository if container else None
            ),
            publication_object_store=(container.object_store if container else None),
            resume_scope_checkpoint_paths=tuple(args.resume_scope_checkpoint),
            replay_scope_checkpoint_paths=tuple(args.replay_scope_checkpoint),
        )
    except DocumentMapScopeReplacementCandidateValidationError as exc:
        if args.private_output is not None:
            _write_private_result(
                args.private_output.expanduser().resolve(),
                {
                    "status": "FAILED_REPLACEMENT_CANDIDATE_VALIDATION",
                    "candidate": exc.private_candidate_payload,
                    "safe_issue_codes": list(exc.safe_issue_codes),
                    "tool_activity_summary": exc.tool_activity_summary,
                },
            )
        print(
            json.dumps(
                {
                    "schema_version": CANARY_SCHEMA_VERSION,
                    "status": "FAIL",
                    "reason_code": exc.reason_code,
                    "safe_issue_codes": list(exc.safe_issue_codes),
                },
                sort_keys=True,
            )
        )
        return 2
    except DocumentMapScopeReplacementHostCompilationError as exc:
        if args.private_output is not None:
            _write_private_result(
                args.private_output.expanduser().resolve(),
                {
                    "status": "FAILED_REPLACEMENT_HOST_COMPILATION",
                    "candidate": exc.private_candidate_payload,
                    "safe_issue_code": exc.safe_issue_code,
                    "tool_activity_summary": exc.tool_activity_summary,
                },
            )
        print(
            json.dumps(
                {
                    "schema_version": CANARY_SCHEMA_VERSION,
                    "status": "FAIL",
                    "reason_code": exc.reason_code,
                    "safe_issue_code": exc.safe_issue_code,
                },
                sort_keys=True,
            )
        )
        return 2
    except DocumentMapHeadingScopeCandidateValidationError as exc:
        if args.private_output is not None:
            _write_private_result(
                args.private_output.expanduser().resolve(),
                {
                    "status": "FAILED_CANDIDATE_VALIDATION",
                    "candidate": exc.private_candidate_payload,
                    "safe_issue_codes": list(exc.safe_issue_codes),
                    "tool_activity_summary": exc.tool_activity_summary,
                },
            )
        print(
            json.dumps(
                {
                    "schema_version": CANARY_SCHEMA_VERSION,
                    "status": "FAIL",
                    "reason_code": exc.reason_code,
                    "safe_issue_codes": list(exc.safe_issue_codes),
                    "private_failure_output_written": args.private_output is not None,
                },
                sort_keys=True,
            )
        )
        return 2
    except DocumentMapHeadingScopeHostCompilationError as exc:
        if args.private_output is not None:
            _write_private_result(
                args.private_output.expanduser().resolve(),
                {
                    "status": "FAILED_HOST_COMPILATION",
                    "candidate": exc.private_candidate_payload,
                    "safe_issue_code": exc.safe_issue_code,
                    "tool_activity_summary": exc.tool_activity_summary,
                },
            )
        print(
            json.dumps(
                {
                    "schema_version": CANARY_SCHEMA_VERSION,
                    "status": "FAIL",
                    "reason_code": exc.reason_code,
                    "safe_issue_code": exc.safe_issue_code,
                    "private_failure_output_written": args.private_output is not None,
                },
                sort_keys=True,
            )
        )
        return 2
    except LifecycleProbeError as exc:
        print(
            json.dumps(
                {
                    "schema_version": CANARY_SCHEMA_VERSION,
                    "status": "FAIL",
                    "reason_code": exc.reason_code,
                },
                sort_keys=True,
            )
        )
        return 2
    finally:
        if container is not None:
            container.close()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "PASS" else 3


if __name__ == "__main__":
    raise SystemExit(main())

"""Produce a source-bound working Document Map through existing Agent Kit drivers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Mapping

from app.adapters.agents.codex_app_server.canonical_document_session import (
    CanonicalProductEvidenceRequest,
    prepare_canonical_app_server_document_session,
)
from app.adapters.agents.codex_app_server.canonical_stage_transport import (
    LocalCodexAppServerStructuredStageTransport,
)
from app.adapters.agents.codex_app_server.document_map_survey_driver import (
    CanonicalDocumentMapSurveyStageDriver,
)
from app.adapters.agents.codex_app_server.document_map_heading_scope_driver import (
    CanonicalDocumentMapHeadingScopeStageDriver,
)
from app.adapters.agents.codex_app_server.document_map_replacement_driver import (
    CanonicalDocumentMapScopeReplacementStageDriver,
)
from app.adapters.agents.codex_app_server.lifecycle import (
    LifecycleProbeError,
    _canary_runtime_state,
)
from app.adapters.agents.portable_kit import load_portable_agent_kit
from app.adapters.agents.reference_workspace import (
    materialize_product_evidence_workspace,
)
from app.application.build_source_ir import build_source_ir
from app.application.agent_workflow.document_map_builder import (
    assemble_and_publish_document_map_working_revision,
    project_document_map_scope_replacement,
)
from app.application.agent_workflow.document_map_exploration import (
    DocumentMapSurveyCompilationV1,
    DocumentMapVisualCoverageReceiptV1,
    DocumentMapVisualCoverageMode,
)
from app.application.agent_workflow.document_map_scope import (
    DocumentMapScopeRequestV1,
    DocumentMapHeadingCandidateRefV1,
    DocumentMapHeadingScopeCompilationV1,
    DocumentMapSourceEndpointV1,
    DocumentMapSourceSpanV1,
    DocumentMapDetailScopeKind,
    expand_document_map_heading_candidates,
)
from app.application.agent_workflow.document_map_replacement import (
    DocumentMapScopeReplacementCompilationV1,
    build_document_map_scope_replacement_request,
    load_document_map_scope_evidence,
)
from app.application.agent_workflow.extraction_runtime import AgentExtractionStage
from app.application.agent_workflow.native_heading_skeleton import (
    build_native_heading_skeleton,
)
from app.application.agent_workflow.native_structure_working_revision import (
    canonical_json_sha256,
)
from app.application.agent_workflow.native_structure_working_revision_bridge import (
    load_native_structure_working_revision,
)


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def build_unit_scope_requests(
    *,
    source_ir: Any,
    manifest: Mapping[str, Any],
    root: Path,
    survey: DocumentMapSurveyCompilationV1,
    visual: DocumentMapVisualCoverageReceiptV1,
):
    """One source-owned scope per Agent-surveyed unit; headings remain proposals.

    The final unit needs no invented following chapter. All prefix text stays
    attached to its actual document unit and never becomes a fabricated chapter.
    """
    ledger = _jsonl(root / "pages/source-ordinal-ledger.jsonl")
    try:
        skeleton = build_native_heading_skeleton(source_ir)
    except ValueError:
        return (
            (),
            {},
            (
                {
                    "code": "NATIVE_HEADING_SCOPE_UNRESOLVED",
                    "pages": list(range(1, survey.page_count + 1)),
                    "message": "The source needs a heading-recovery capability before a source-bound Map can be published.",
                },
            ),
            ledger,
        )
    source_by_id = {node.source_node_id: node for node in source_ir.nodes}
    requests = []
    context = {}
    unresolved = []
    for unit in survey.scopes:
        owned = tuple(
            item for item in ledger if item["page_number"] in unit.page_numbers
        )
        refs = tuple(
            item
            for item in skeleton.candidates
            if item.page_number in unit.page_numbers
        )
        roots = [item for item in refs if item.level == 1]
        if not owned or not roots:
            unresolved.append(
                {
                    "scope_id": unit.scope_id,
                    "code": "NATIVE_HEADING_SCOPE_UNRESOLVED",
                    "pages": list(unit.page_numbers),
                    "message": "The native heading proposals do not yet cover this source unit; no structure was invented.",
                }
            )
            continue
        first = roots[0]
        # Parser table/asset nodes can be appended after all text; own objects
        # by surveyed pages and all text from the first source heading onward.
        body = tuple(
            item
            for item in owned
            if item["source_order"] >= first.source_order
            or item.get("table_id")
            or item.get("asset_ref")
        )
        prefix = tuple(item for item in owned if item not in body)
        refs = tuple(item for item in refs if item.source_order >= first.source_order)
        last = body[-1]
        span = DocumentMapSourceSpanV1(
            start=DocumentMapSourceEndpointV1(
                source_node_id=body[0]["source_node_id"], offset=0
            ),
            end=DocumentMapSourceEndpointV1(
                source_node_id=last["source_node_id"], offset=last["source_text_length"]
            ),
        )
        values = {
            "run_id": survey.run_id,
            "source_sha256": survey.source_sha256,
            "source_ir_id": manifest["capture"]["source_ir_id"],
            "source_ir_canonical_sha256": manifest["capture"][
                "source_ir_canonical_sha256"
            ],
            "evidence_workspace_bundle_sha256": manifest["workspace_bundle_sha256"],
            "survey_compilation_sha256": survey.compilation_sha256,
            "visual_coverage_receipt_sha256": visual.receipt_sha256,
            "heading_skeleton_sha256": skeleton.artifact_sha256,
            "scope_id": "document-map-detail-scope-"
            + canonical_json_sha256(
                {"unit": unit.scope_id, "source": survey.source_sha256}
            )[:32],
            "kind": DocumentMapDetailScopeKind.SECTION_GROUP,
            "advisory_title": unit.label,
            "suggested_source_span": span,
            "source_node_ids": tuple(item["source_node_id"] for item in body),
            "page_numbers": tuple(unit.page_numbers),
            "physical_table_ids": tuple(
                dict.fromkeys(item["table_id"] for item in body if item.get("table_id"))
            ),
            "asset_ids": tuple(
                dict.fromkeys(
                    item["asset_ref"] for item in body if item.get("asset_ref")
                )
            ),
            "heading_candidates": tuple(
                DocumentMapHeadingCandidateRefV1(
                    candidate_id=item.candidate_id,
                    source_node_id=item.source_node_id,
                    source_order=item.source_order,
                    page_number=item.page_number,
                    number=item.number,
                    title=item.title,
                    level=item.level,
                    advisory_parent_candidate_id=item.parent_candidate_id,
                    advisory_state=item.state,
                )
                for item in refs
            ),
        }
        draft = DocumentMapScopeRequestV1.model_construct(
            **values, request_sha256="0" * 64
        )
        request = DocumentMapScopeRequestV1(
            **values,
            request_sha256=canonical_json_sha256(
                draft.model_dump(mode="json", exclude={"request_sha256"})
            ),
        )
        requests.append(request)
        if prefix:
            # Read prefix through the same evidence verifier, then retain exact
            # source text under the surveyed document root (not the first chapter).
            prefix_values = request.model_dump(mode="json", exclude={"request_sha256"})
            prefix_values["source_node_ids"] = [
                item["source_node_id"] for item in owned
            ]
            prefix_values["suggested_source_span"]["start"] = {
                "source_node_id": owned[0]["source_node_id"],
                "offset": 0,
            }
            prefix_request = DocumentMapScopeRequestV1(
                **prefix_values, request_sha256=canonical_json_sha256(prefix_values)
            )
            evidence = load_document_map_scope_evidence(
                root=root, request=prefix_request
            )
            prefix_ids = {item["source_node_id"] for item in prefix}
            context[unit.scope_id] = tuple(
                item for item in evidence.records if item.source_node_id in prefix_ids
            )
        if any(node_id not in source_by_id for node_id in request.source_node_ids):
            raise ValueError("document unit leaves the current SourceIR")
    return tuple(requests), context, tuple(unresolved), ledger


def execute_document_map(
    *,
    source_path: Path,
    source_sha256: str,
    run_id: str,
    project_id: str,
    document_id: str,
    expected_kit_sha256: str,
    codex_bin: Path,
    codex_home: Path,
    repository_root: Path,
    timeout_seconds: float,
    object_store: Any,
    repository: Any,
    checkpoint: Mapping[str, Any] | None,
    checkpoint_sink: Callable,
    cancellation_requested: Callable[[], bool],
    before_publication: Callable,
):
    kit = load_portable_agent_kit(repository_root / "agent-kit")
    if kit.identity.bundle_sha256 != expected_kit_sha256:
        raise LifecycleProbeError("DOCUMENT_MAP_KIT_CHANGED")
    identity = {
        "schema_version": "document-map-checkpoint/v1",
        "source_sha256": source_sha256,
        "run_id": run_id,
        "kit_bundle_sha256": expected_kit_sha256,
    }
    saved = dict(checkpoint or {**identity, "scopes": {}, "calls": []})
    if any(saved.get(key) != value for key, value in identity.items()):
        raise LifecycleProbeError("DOCUMENT_MAP_CHECKPOINT_IDENTITY_MISMATCH")
    head = repository.get_working_revision_head(
        project_id=project_id, document_id=document_id, source_sha256=source_sha256
    )
    previous = None
    if head is not None and head.run_id == run_id:
        previous = load_native_structure_working_revision(
            record=repository.get_working_revision_object(
                project_id=project_id,
                document_id=document_id,
                run_id=run_id,
                revision=head.revision,
            ),
            object_store=object_store,
        )
        if previous.state == "ACCEPTED":
            return _result(
                previous, model_call_count=_model_call_count(saved), replayed=True
            )
    source_handle = "source-handle-" + hashlib.sha256(run_id.encode()).hexdigest()[:32]
    evidence_request = CanonicalProductEvidenceRequest(
        source_path=source_path,
        expected_source_sha256=source_sha256,
        document_role="CUSTOMER_SOR",
        authorized_corpus_root=source_path.parent,
        run_id=run_id,
        opaque_source_handle=source_handle,
    )
    checkpoint_sink(saved, "SOURCE_PREFLIGHT")
    source_ir = build_source_ir(source_path)

    def transport():
        return LocalCodexAppServerStructuredStageTransport(
            timeout_seconds=timeout_seconds,
            cancellation_requested=cancellation_requested,
        )

    with _canary_runtime_state(
        codex_bin=codex_bin, codex_home=codex_home, repository_root=repository_root
    ) as runtime:
        template = runtime.state_path / "document-evidence"
        manifest = materialize_product_evidence_workspace(
            source_path,
            expected_source_sha256=source_sha256,
            document_role="CUSTOMER_SOR",
            output_directory=template,
            corpus_root=source_path.parent,
            repository_root=repository_root,
            run_id=run_id,
            opaque_source_handle=source_handle,
            agent_kit_root=repository_root / "agent-kit",
        )

        def session(**extra):
            return prepare_canonical_app_server_document_session(
                base_runtime=runtime,
                canonical_kit_root=repository_root / "agent-kit",
                stages=(AgentExtractionStage.DOCUMENT_UNDERSTANDING,),
                evidence_request=evidence_request,
                evidence_workspace_template=template,
                repository_root=repository_root,
                **extra,
            )

        if "survey" not in saved:
            active = session()
            try:
                result = CanonicalDocumentMapSurveyStageDriver(
                    transport=transport(),
                    visual_coverage_mode=DocumentMapVisualCoverageMode.REFERENCE_FULL_PAGE,
                ).run(active, task_binding_sha256=canonical_json_sha256(identity))
            finally:
                active.close()
            saved["survey"] = result.compilation.model_dump(mode="json")
            saved["visual"] = result.visual_coverage.model_dump(mode="json")
            saved["calls"].append(
                {
                    "stage": "SURVEY",
                    "thread_id": result.runtime_thread_id,
                    "turn_id": result.runtime_turn_id,
                    "activity": result.tool_activity_summary,
                }
            )
            checkpoint_sink(saved, "DOCUMENT_SURVEY_COMPLETE")
        survey = DocumentMapSurveyCompilationV1.model_validate(saved["survey"])
        visual = DocumentMapVisualCoverageReceiptV1.model_validate(saved["visual"])
        requests, document_context, unresolved, ledger = build_unit_scope_requests(
            source_ir=source_ir,
            manifest=manifest,
            root=template,
            survey=survey,
            visual=visual,
        )
        projections = []
        unresolved = list(unresolved)
        for request in requests:
            if cancellation_requested():
                raise LifecycleProbeError("DOCUMENT_MAP_CANCELLED")
            state = saved["scopes"].setdefault(request.scope_id, {})
            # Expansion requests are explicit Agent-selected exact source nodes.
            if "request" in state:
                restored = DocumentMapScopeRequestV1.model_validate(state["request"])
                if (
                    restored.source_sha256 != source_sha256
                    or restored.evidence_workspace_bundle_sha256
                    != request.evidence_workspace_bundle_sha256
                ):
                    raise LifecycleProbeError("DOCUMENT_MAP_SCOPE_CHECKPOINT_DRIFTED")
                request = restored
            if "headings" not in state:
                for _attempt in range(3):
                    checkpoint_sink(saved, "HEADINGS_RUNNING")
                    active = session(document_map_scope_stage_input_request=request)
                    try:
                        heading_result = CanonicalDocumentMapHeadingScopeStageDriver(
                            transport=transport()
                        ).run(
                            active,
                            request=request,
                            task_binding_sha256=request.request_sha256,
                        )
                    finally:
                        active.close()
                    saved["calls"].append(
                        {
                            "stage": "HEADINGS",
                            "scope_id": request.scope_id,
                            "thread_id": heading_result.runtime_thread_id,
                            "turn_id": heading_result.runtime_turn_id,
                            "activity": heading_result.tool_activity_summary,
                        }
                    )
                    if heading_result.compilation is not None:
                        state["headings"] = heading_result.compilation.model_dump(
                            mode="json"
                        )
                        state["request"] = request.model_dump(mode="json")
                        checkpoint_sink(saved, "HEADINGS_VERIFIED")
                        break
                    if (
                        heading_result.candidate.action.value
                        == "EXPAND_HEADING_CANDIDATES"
                    ):
                        request = expand_document_map_heading_candidates(
                            request=request,
                            candidate=heading_result.candidate,
                            source_ir=source_ir,
                        )
                        state["request"] = request.model_dump(mode="json")
                        checkpoint_sink(saved, "HEADING_SCOPE_EXPANDED")
                    else:
                        state["coordination"] = heading_result.candidate.model_dump(
                            mode="json"
                        )
                        checkpoint_sink(saved, "HEADING_COORDINATION_REQUIRED")
                        break
            if "headings" not in state:
                unresolved.append(
                    {
                        "scope_id": request.scope_id,
                        "code": "HEADING_COORDINATION_REQUIRED",
                        "pages": list(request.page_numbers),
                    }
                )
                continue
            heading = DocumentMapHeadingScopeCompilationV1.model_validate(
                state["headings"]
            )
            replacement_request = build_document_map_scope_replacement_request(
                scope_request=request, heading_compilation=heading
            )
            if "replacement" not in state:
                checkpoint_sink(saved, "SOURCE_OWNERSHIP_RUNNING")
                active = session(
                    document_map_replacement_stage_input_request=replacement_request
                )
                try:
                    result = CanonicalDocumentMapScopeReplacementStageDriver(
                        transport=transport()
                    ).run(
                        active,
                        request=replacement_request,
                        task_binding_sha256=replacement_request.request_sha256,
                    )
                finally:
                    active.close()
                saved["calls"].append(
                    {
                        "stage": "SOURCE_OWNERSHIP",
                        "scope_id": request.scope_id,
                        "thread_id": result.runtime_thread_id,
                        "turn_id": result.runtime_turn_id,
                        "activity": result.tool_activity_summary,
                    }
                )
                if result.compilation is None:
                    state["coordination"] = result.candidate.model_dump(mode="json")
                    checkpoint_sink(saved, "SOURCE_COORDINATION_REQUIRED")
                    unresolved.append(
                        {
                            "scope_id": request.scope_id,
                            "code": "SOURCE_COORDINATION_REQUIRED",
                            "pages": list(request.page_numbers),
                        }
                    )
                    continue
                state["replacement"] = result.compilation.model_dump(mode="json")
                checkpoint_sink(saved, "SOURCE_SCOPE_VERIFIED")
            compiled = DocumentMapScopeReplacementCompilationV1.model_validate(
                state["replacement"]
            )
            evidence = load_document_map_scope_evidence(
                root=template, request=replacement_request
            )
            projections.append(
                project_document_map_scope_replacement(
                    compilation=compiled, evidence=evidence
                )
            )
        if not projections:
            saved["unresolved"] = unresolved
            checkpoint_sink(saved, "PARTIAL_NO_PUBLISHABLE_SCOPE")
            return {
                "schema_version": "document-map-job-result/v1",
                "status": "PARTIAL",
                "run_id": run_id,
                "source_sha256": source_sha256,
                "unresolved": unresolved,
                "model_call_count": _model_call_count(saved),
            }
        if source_ir.status.value != "COMPLETE" or survey.status.value != "COMPLETE":
            unresolved.append(
                {
                    "code": "SOURCE_OR_SURVEY_PARTIAL",
                    "source_ir_status": source_ir.status.value,
                    "survey_status": survey.status.value,
                }
            )
        if any(projection.unresolved for projection in projections):
            unresolved.append({"code": "SCOPE_CONTENT_UNRESOLVED"})
        before_publication()
        # Physical tables are separately exact-covered by the existing compiled
        # logical-table fragment validator; this ledger checks text/placements.
        expected_ids = tuple(
            item["source_node_id"] for item in ledger if not item.get("table_id")
        )
        working = assemble_and_publish_document_map_working_revision(
            project_id=project_id,
            document_id=document_id,
            run_id=run_id,
            source_sha256=source_sha256,
            source_ir_canonical_sha256=manifest["capture"][
                "source_ir_canonical_sha256"
            ],
            evidence_workspace_bundle_sha256=manifest["workspace_bundle_sha256"],
            survey=survey,
            scope_replacements=projections,
            repository=repository,
            object_store=object_store,
            kit_bundle_sha256=expected_kit_sha256,
            document_context=document_context,
            unresolved_scopes=unresolved,
            complete_source_node_ids=expected_ids if not unresolved else None,
            complete_physical_table_ids=tuple(
                item["table_id"] for item in ledger if item.get("table_id")
            )
            if not unresolved
            else None,
            previous=previous,
            revision=1 if previous is None else previous.revision + 1,
        )
        saved["published_revision"] = working.revision
        saved["published_revision_sha256"] = working.revision_sha256
        checkpoint_sink(saved, "DOCUMENT_MAP_PUBLISHED")
        return _result(working, model_call_count=_model_call_count(saved))


def _model_call_count(checkpoint):
    return sum(
        max(1, int(call.get("activity", {}).get("scope_attempt_count", 1)))
        for call in checkpoint["calls"]
    )


def _result(working, *, model_call_count: int, replayed: bool = False):
    return {
        "schema_version": "document-map-job-result/v1",
        "status": "COMPLETE" if working.state == "ACCEPTED" else "PARTIAL",
        "run_id": working.run_id,
        "source_sha256": working.source_sha256,
        "working_revision": working.revision,
        "revision_sha256": working.revision_sha256,
        "artifact_id": working.base_artifact_id,
        "node_count": len(working.document_tree["nodes"]),
        "logical_table_count": len(working.logical_tables),
        "model_call_count": model_call_count,
        "unresolved": list(working.unresolved),
        "publication_replayed": replayed,
        "is_human_accepted": False,
        "next_action": "EXTRACT_SOURCE_ITEMS",
    }

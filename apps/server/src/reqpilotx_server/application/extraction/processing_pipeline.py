from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Callable

from reqpilotx_server.adapters.documents.pdf_source_ir_adapter import PdfSourceIRAdapter
from reqpilotx_server.adapters.documents.preflight import LocalDocumentPreflight
from reqpilotx_server.application.errors import ApplicationError
from reqpilotx_server.application.extraction.build_shadow_preview import (
    _serialize_finding,
    build_document_view,
)
from reqpilotx_server.application.extraction.build_llm_shadow_preview import (
    _serialize_structure_agent,
)
from reqpilotx_server.application.extraction.build_source_ir import build_source_ir
from reqpilotx_server.application.extraction.inspect_structure import (
    StructureInspectionCandidateCategory,
    StructureInspectionDisposition,
    StructureInspectionSnapshotV1,
    inspect_structure,
)
from reqpilotx_server.application.extraction.orchestrate_structure_agent import (
    initialize_structure_repair_without_provider,
)
from reqpilotx_server.application.extraction.resolve_structure import build_structure_tree
from reqpilotx_server.application.extraction.requirement_tree import (
    REQUIREMENT_TREE_PROJECTION_RUNTIME_VERSION,
)
from reqpilotx_server.application.extraction.revise_structure import (
    validate_structure_revision_chain,
)
from reqpilotx_server.application.extraction.table_evidence_projection import (
    build_table_evidence_bindings,
)
from reqpilotx_server.application.extraction.table_record_projection import (
    build_table_record_projections,
)
from reqpilotx_server.application.extraction.table_row_group_review_index import (
    TABLE_ROW_GROUP_REVIEW_INDEX_KEY,
    build_table_row_group_review_index,
)
from reqpilotx_server.domain.extraction.processing_profile import ResolvedProcessingPlanV1
from reqpilotx_server.domain.extraction.source_ir import SourceIRSnapshot
from reqpilotx_server.domain.extraction.source_ir import (
    NativeDocumentFormat,
    NativeLocator,
    SourceDiagnostic,
    SourceDiagnosticSeverity,
    SourceIRStatus,
)
from reqpilotx_server.domain.extraction.structure_revision import (
    EffectiveStructureSnapshotV1,
    StructureRevisionV1,
    canonical_model_sha256,
)
from reqpilotx_server.services.extraction_visual_routes import (
    extraction_visual_strategy_identity,
)
from reqpilotx_server.services.visual_text_snapshot_cache import (
    VisualTextProviderTelemetryCallback,
    VisualTextSnapshotCacheBinding,
)


LOCAL_STRUCTURE_PROJECTION_MODE = "LOCAL_STRUCTURE_PROJECTION"
LOCAL_CONTENT_ROLE_RUNTIME_VERSION = "local-content-role/v1"


def build_local_structure_document_result(
    source_path: Path,
    *,
    plan: ResolvedProcessingPlanV1,
    result_runtime_sha256: str,
    structure_revisions: tuple[StructureRevisionV1, ...] = (),
    include_source_read_model: bool = False,
    page_progress_callback: Callable[[int, int, int], None] | None = None,
    page_staging_callback: Callable[[dict[str, Any]], None] | None = None,
    visual_text_snapshot_cache: VisualTextSnapshotCacheBinding | None = None,
    provider_telemetry_callback: VisualTextProviderTelemetryCallback | None = None,
) -> dict[str, Any]:
    """Build source facts and a semantic-neutral content tree locally.

    Local rules recover source structure, directly owned prose and proven
    table reading units.  They deliberately do *not* decide whether any of
    that content is a requirement, what type of requirement it may be, or
    whether it should become zero, one or many atomic requirements.  Those are
    model/human decisions performed by the optional semantic workflow.
    """

    if (
        not isinstance(result_runtime_sha256, str)
        or len(result_runtime_sha256) != 64
        or any(
            character not in "0123456789abcdef"
            for character in result_runtime_sha256
        )
    ):
        raise ValueError("result_runtime_sha256 must be a lowercase SHA-256")

    source_ir, page_receipts = build_source_ir_and_receipts_for_plan(
        source_path,
        plan=plan,
        page_progress_callback=page_progress_callback,
        page_staging_callback=page_staging_callback,
        visual_text_snapshot_cache=visual_text_snapshot_cache,
        provider_telemetry_callback=provider_telemetry_callback,
    )
    base_tree = build_structure_tree(source_ir)
    structure_inspection = inspect_structure(source_ir, base_tree)
    repair = initialize_structure_repair_without_provider(
        source_ir,
        base_tree,
        error_code="structure_audit_not_requested",
        revisions=structure_revisions,
    )
    validate_structure_revision_chain(
        source_ir,
        base_tree,
        structure_revisions,
    )
    effective_structure = repair.effective_structure
    structure_inspection_payload = _serialize_structure_inspection(
        structure_inspection,
        effective_revision=effective_structure.revision_number,
    )
    inspection_counts = structure_inspection_payload["counts"]
    inspection_requires_review = bool(inspection_counts["actionable_candidate_count"])
    rules_projection = _build_rules_requirement_projection(
        source_ir,
        effective_structure,
    )
    table_record_projections = build_table_record_projections(
        source_ir,
        effective_structure,
    )
    table_evidence_bindings = build_table_evidence_bindings(
        table_record_projections=table_record_projections,
        tables=rules_projection["tables"],
    )
    table_row_group_review_index = build_table_row_group_review_index(
        source_ir,
        effective_structure,
        automatic_projections=table_record_projections,
    )
    structure_agent = _serialize_structure_agent(
        repair,
        editable=True,
        final=True,
    )
    for stage in structure_agent.get("stages") or []:
        stage_name = stage.get("stage")
        if stage_name in {
            "STRUCTURE_REPAIR",
            "OPTIONAL_ATOMIZATION",
            "OPTIONAL_REFINEMENT",
        }:
            stage["status"] = "SKIPPED"
        elif stage_name == "REQUIREMENT_CLASSIFICATION":
            stage["status"] = "SKIPPED"
        elif stage_name == "VALIDATION":
            stage["status"] = (
                "PARTIAL" if inspection_requires_review else "COMPLETE"
            )
    structure_agent["execution_mode"] = "LOCAL_STRUCTURE"
    structure_agent["processing_plan"] = plan.model_dump(mode="json")
    structure_agent["ai_capabilities"] = {
        "structure_optimization": False,
        "requirement_classification": False,
        "atomization": False,
        "refinement": False,
    }
    structure_agent["local_capabilities"] = {
        "content_role_projection": True,
        "requirement_classification_evidence": False,
        "deterministic_structure_inspection": True,
        "structure_proxy_projection": True,
        "table_record_projection": True,
        "manual_structure_revision": True,
    }
    structure_agent["deterministic_inspection"] = structure_inspection_payload
    result = {
        "schema_version": "extraction-llm-shadow-preview/v5",
        # This read-model runtime is part of the durable result identity.  A
        # Structure-compatible result built by an older RequirementTree
        # projector must never be reused after projection semantics change.
        "requirement_tree_projection_runtime_version": (
            REQUIREMENT_TREE_PROJECTION_RUNTIME_VERSION
        ),
        "result_runtime_sha256": result_runtime_sha256,
        "mode": LOCAL_STRUCTURE_PROJECTION_MODE,
        "collection": "local_structure_document",
        "status": (
            "REVIEW_REQUIRED"
            if inspection_requires_review
            else rules_projection["status"]
        ),
        "serving_impact": "NONE",
        "source_sha256": source_ir.source_sha256,
        "processing_plan": plan.model_dump(mode="json"),
        "generation": {
            "mode": "DETERMINISTIC_STRUCTURE",
            "remote_provider_calls": 0,
            "ai_review_generated": False,
            "semantic_authority": "NONE",
            "atomization_status": "NOT_RUN",
            "refinement_status": "NOT_RUN",
        },
        "page_processing_receipts": [receipt.as_dict() for receipt in page_receipts],
        "degraded_page_numbers": [
            receipt.page_number
            for receipt in page_receipts
            if receipt.state == "DEGRADED"
        ],
        "pipeline": {
            "source_ir": {
                "id": source_ir.source_ir_id,
                "adapter_id": source_ir.adapter_id,
                "version": source_ir.adapter_version,
                "status": source_ir.status.value,
                "canonical_sha256": _stable_source_ir_sha256(source_ir),
            },
            "structure": {
                "id": effective_structure.effective_snapshot_id,
                "resolver_id": effective_structure.resolver_id,
                "version": effective_structure.resolver_version,
                "revision": effective_structure.revision_number,
                "head_revision_id": effective_structure.head_revision_id,
                "canonical_sha256": canonical_model_sha256(effective_structure),
                "base_snapshot_id": base_tree.structure_snapshot_id,
                "base_canonical_sha256": canonical_model_sha256(base_tree),
                "inspection": {
                    "id": structure_inspection_payload["inspection_id"],
                    "status": structure_inspection_payload["status"],
                    "actionable_candidate_count": (
                        structure_inspection_payload["counts"][
                            "actionable_candidate_count"
                        ]
                    ),
                },
            },
            "semantic": rules_projection["semantic"],
            "requirement_view": rules_projection["requirement_view"],
            "table_records": {
                "schema_version": "table-record-projection/v1",
                "projection_count": len(table_record_projections),
                "record_count": sum(
                    len(projection.records) for projection in table_record_projections
                ),
                "whole_table_count": sum(
                    projection.mode.value == "WHOLE_TABLE"
                    for projection in table_record_projections
                ),
                "evidence_binding_count": len(table_evidence_bindings),
            },
        },
        "presentation": {
            "default_collection": "review_groups",
            "recognized_requirement_collection": "requirement_groups",
            "diagnostic_collection": "items",
            "response_collection": "local_structure_document",
            "omitted_collections": [],
        },
        "structure_agent": structure_agent,
        "structure_inspection": structure_inspection_payload,
        "items": rules_projection["items"],
        "requirement_groups": rules_projection["requirement_groups"],
        "review_groups": rules_projection["review_groups"],
        "review_counts": rules_projection["review_counts"],
        "findings": rules_projection["findings"],
        "tables": rules_projection["tables"],
        "table_record_projections": [
            projection.model_dump(mode="json")
            for projection in table_record_projections
        ],
        TABLE_ROW_GROUP_REVIEW_INDEX_KEY: table_row_group_review_index,
        "table_evidence_bindings": [
            binding.model_dump(mode="json") for binding in table_evidence_bindings
        ],
        "counts": {
            "source_nodes": len(source_ir.nodes),
            "structure_nodes": len(effective_structure.nodes),
            "structure_relations": len(effective_structure.relations),
            "structure_inspection_actionable": (
                inspection_counts["actionable_candidate_count"]
            ),
            "table_record_projections": len(table_record_projections),
            "table_records": sum(
                len(projection.records) for projection in table_record_projections
            ),
            "table_evidence_bindings": len(table_evidence_bindings),
            **rules_projection["counts"],
            "successful_provisions": 0,
            "unresolved_provisions": 0,
            "blocked_provisions": 0,
            "coverage_status": (
                "SOURCE_RESTORATION_REQUIRED"
                if inspection_counts["capture_gap_count"]
                else "STRUCTURE_REVIEW_REQUIRED"
                if inspection_counts["structural_actionable_candidate_count"]
                else "COMPLETE_LOCAL_STRUCTURE"
            ),
        },
        "llm_review": None,
        "requirement_set_id": None,
    }
    if include_source_read_model:
        from reqpilotx_server.application.extraction.structure_presentation import (
            build_structure_presentation_index,
        )
        from reqpilotx_server.application.extraction.source_page_facts import (
            SOURCE_PAGE_PREVIEW_KEY,
            build_source_page_preview,
        )

        result["structure_presentation_index"] = build_structure_presentation_index(
            source_ir,
            structure=effective_structure,
        )
        page_preview = build_source_page_preview(
            source_path,
            source_ir=source_ir,
            structure=effective_structure,
        )
        result["structure_presentation_index"] = build_structure_presentation_index(
            source_ir,
            structure=effective_structure,
        )
        if page_preview is not None:
            result[SOURCE_PAGE_PREVIEW_KEY] = page_preview
    return result


def _serialize_structure_inspection(
    inspection: StructureInspectionSnapshotV1,
    *,
    effective_revision: int,
) -> dict[str, Any]:
    """Publish a bounded detect-only receipt without turning it into a gate."""

    actionable = tuple(
        candidate
        for candidate in inspection.candidates
        if candidate.disposition is StructureInspectionDisposition.ACTIONABLE
    )
    document_capture_gaps = tuple(
        candidate
        for candidate in actionable
        if candidate.category is StructureInspectionCandidateCategory.SOURCE_CAPTURE_GAP
        and candidate.source_ref is None
    )
    ungrounded_source_facts = tuple(
        candidate
        for candidate in actionable
        if candidate.category is StructureInspectionCandidateCategory.SOURCE_CAPTURE_GAP
        and candidate.source_ref is not None
    )
    structural_actionable = tuple(
        candidate for candidate in actionable if candidate not in document_capture_gaps
    )
    candidate_counts = Counter(
        f"{candidate.category.value}:{candidate.disposition.value}"
        for candidate in inspection.candidates
    )
    stale = effective_revision > 0
    active_structural_actionable = () if stale else structural_actionable
    status = (
        "REQUIRES_REEXTRACTION"
        if document_capture_gaps
        else "STALE_AFTER_STRUCTURE_REVISION"
        if stale
        else "REVIEW_REQUIRED"
        if structural_actionable
        else "COMPLETE"
    )
    return {
        "schema_version": inspection.schema_version,
        "inspection_id": inspection.inspection_id,
        "canonical_sha256": inspection.canonical_sha256,
        "source_ir_id": inspection.source_ir_id,
        "source_sha256": inspection.source_sha256,
        "structure_snapshot_id": inspection.structure_snapshot_id,
        "resolver_id": inspection.resolver_id,
        "resolver_version": inspection.resolver_version,
        "inspection_runtime_version": inspection.inspection_runtime_version,
        "boundary_ruleset_version": inspection.boundary_ruleset_version,
        "basis": "BASE_STRUCTURE",
        "basis_revision": 0,
        "effective_revision": effective_revision,
        "stale": stale,
        "status": status,
        "counts": {
            "inspected_source_node_count": inspection.inspected_source_node_count,
            "inspected_visual_observation_count": (
                inspection.inspected_visual_observation_count
            ),
            "visual_text_snapshot_count": len(
                inspection.visual_text_snapshot_ids
            ),
            "grounded_source_node_count": inspection.grounded_source_node_count,
            "candidate_count": len(inspection.candidates),
            "observed_actionable_candidate_count": len(actionable),
            "observed_structural_actionable_candidate_count": len(
                structural_actionable
            ),
            "observed_document_capture_gap_count": len(document_capture_gaps),
            "observed_ungrounded_source_fact_count": len(ungrounded_source_facts),
            "actionable_candidate_count": (
                len(document_capture_gaps) + len(active_structural_actionable)
            ),
            "structural_actionable_candidate_count": len(active_structural_actionable),
            "capture_gap_count": len(document_capture_gaps),
            "ungrounded_source_fact_count": (
                0 if stale else len(ungrounded_source_facts)
            ),
            "suppressed_candidate_count": sum(
                candidate.disposition is StructureInspectionDisposition.SUPPRESSED
                for candidate in inspection.candidates
            ),
            "by_category_and_disposition": dict(sorted(candidate_counts.items())),
        },
        "actionable_candidates": (
            []
            if stale
            else [
                candidate.model_dump(mode="json") for candidate in structural_actionable
            ]
        ),
    }


def _build_rules_requirement_projection(
    source_ir: SourceIRSnapshot,
    effective_structure: EffectiveStructureSnapshotV1,
) -> dict[str, Any]:
    """Return local presentation facts without manufacturing semantics.

    The historical implementation ran ``classify_content`` and
    ``project_requirements`` here, then labelled their output as suggestion
    only.  That still let vocabulary rules change which tree nodes appeared,
    conflating a source-content boundary with a semantic requirement decision.
    The local path now exposes only structure/table facts.  The deterministic
    classifiers remain available to explicit evaluators, but they are not part
    of the no-LLM serving contract.
    """

    document_view = build_document_view(source_ir, effective_structure)
    source_catalog = document_view["source_catalog"]
    tables = document_view["tables"]
    finding_payloads = [
        _serialize_finding(finding, source_catalog)
        for finding in effective_structure.findings
    ]
    review_counts = {
        "total": 0,
        "content_candidates": 0,
        "recognized_requirement_groups": 0,
        "requirement": 0,
        "needs_classification": 0,
        "information": 0,
    }
    return {
        "status": "STRUCTURE_READY",
        "semantic": {
            "id": None,
            "structure_snapshot_id": effective_structure.effective_snapshot_id,
            "runtime_version": LOCAL_CONTENT_ROLE_RUNTIME_VERSION,
            "status": "NOT_RUN",
            "origin": "NONE",
            "authority": "NONE",
            "counts": {
                "annotations": 0,
                "unresolved": 0,
                "classes": {},
                "statuses": {},
            },
        },
        "requirement_view": {
            "id": None,
            "structure_snapshot_id": effective_structure.effective_snapshot_id,
            "semantic_snapshot_id": None,
            "runtime_version": LOCAL_CONTENT_ROLE_RUNTIME_VERSION,
            "status": "NOT_RUN",
            "origin": "NONE",
            "authority": "NONE",
            "counts": {
                "nodes": 0,
                "requirement_groups": 0,
                "review_groups": 0,
            },
        },
        "items": [],
        "requirement_groups": [],
        "review_groups": [],
        "review_counts": review_counts,
        "findings": finding_payloads,
        "tables": tables,
        "counts": {
            "content_candidates": 0,
            "recognized_requirement_groups": 0,
            "requirement_groups": 0,
            "review_groups": 0,
            "items": 0,
            "tables": len(tables),
            "semantic_annotations": 0,
            "semantic_chunks": 0,
            "semantic_unresolved": 0,
            "semantic_classes": {},
            "semantic_statuses": {},
        },
    }


def build_source_ir_for_plan(
    source_path: Path,
    *,
    plan: ResolvedProcessingPlanV1,
) -> SourceIRSnapshot:
    source_ir, _ = build_source_ir_and_receipts_for_plan(
        source_path,
        plan=plan,
    )
    return source_ir


def build_source_ir_and_receipts_for_plan(
    source_path: Path,
    *,
    plan: ResolvedProcessingPlanV1,
    page_progress_callback: Callable[[int, int, int], None] | None = None,
    page_staging_callback: Callable[[dict[str, Any]], None] | None = None,
    visual_text_snapshot_cache: VisualTextSnapshotCacheBinding | None = None,
    provider_telemetry_callback: VisualTextProviderTelemetryCallback | None = None,
) -> tuple[SourceIRSnapshot, tuple[Any, ...]]:
    from reqpilotx_server.application.extraction.source_asset_objects import (
        runtime_source_asset_publisher,
    )

    source_asset_publisher = runtime_source_asset_publisher()
    report = LocalDocumentPreflight().inspect(source_path)
    if report.source_sha256 != plan.source_sha256:
        raise ApplicationError(
            status_code=409,
            code="processing_plan_source_mismatch",
            detail={
                "reason_code": "processing_plan_source_mismatch",
                "message": "Source bytes changed after the processing plan was frozen.",
            },
        )
    if report.identity.detected_format.value != "pdf":
        if plan.document.primary_parser != "pymupdf":
            raise ApplicationError(
                status_code=422,
                code="primary_parser_document_format_unsupported",
                detail={
                    "reason_code": "primary_parser_document_format_unsupported",
                    "message": (
                        "Azure Document Intelligence primary parsing is currently "
                        "limited to PDF documents."
                    ),
                },
            )
        return (
            build_source_ir(
                source_path,
                preflight_report=report,
            ),
            (),
        )
    if plan.document.primary_parser != "pymupdf":
        from reqpilotx_server.adapters.documents.azure_document_intelligence_source_ir_adapter import (
            build_azure_document_intelligence_source_ir_adapter,
        )

        adapter = build_azure_document_intelligence_source_ir_adapter(
            plan_identity_sha256=plan.canonical_sha256,
        )
        source_ir = build_source_ir(
            source_path,
            preflight_report=report,
            adapters=(adapter,),
        )
        if plan.document.enhancement_chain or plan.document.complex_vision:
            from reqpilotx_server.application.extraction.processing_visual_chain import (
                build_composable_pdf_source_ir_adapter,
                page_processing_receipts_from_adapter,
            )

            visual_adapter_options: dict[str, Any] = {
                "source_asset_publisher": source_asset_publisher,
            }
            if page_progress_callback is not None:
                visual_adapter_options["page_progress_callback"] = (
                    page_progress_callback
                )
            if page_staging_callback is not None:
                visual_adapter_options["page_staging_callback"] = (
                    page_staging_callback
                )
            if visual_text_snapshot_cache is not None:
                visual_adapter_options["visual_text_snapshot_cache"] = (
                    visual_text_snapshot_cache
                )
            if provider_telemetry_callback is not None:
                visual_adapter_options["provider_telemetry_callback"] = (
                    provider_telemetry_callback
                )
            visual_adapter = build_composable_pdf_source_ir_adapter(
                plan,
                **visual_adapter_options,
            )
            visual_source_ir = build_source_ir(
                source_path,
                preflight_report=report,
                adapters=(visual_adapter,),
            )
            receipts = page_processing_receipts_from_adapter(visual_adapter)
            degraded = tuple(
                receipt for receipt in receipts if receipt.state == "DEGRADED"
            )
            diagnostics = list(source_ir.diagnostics)
            diagnostics.extend(
                SourceDiagnostic(
                    code="processing_page_degraded",
                    severity=SourceDiagnosticSeverity.WARNING,
                    message=(
                        f"Page {receipt.page_number} retained the previous "
                        "trusted parsing stage after a provider failure."
                    ),
                    retryable=True,
                    native_locator=NativeLocator(
                        format=NativeDocumentFormat.PDF,
                        page_number=receipt.page_number,
                        bbox=(0.0, 0.0, 0.0, 0.0),
                    ),
                )
                for receipt in degraded
            )
            source_ir = SourceIRSnapshot(
                **{
                    **source_ir.model_dump(mode="python"),
                    "status": (
                        SourceIRStatus.PARTIAL if degraded else source_ir.status
                    ),
                    "visual_text_snapshots": (visual_source_ir.visual_text_snapshots),
                    "diagnostics": tuple(diagnostics),
                }
            )
            return source_ir, receipts
        return source_ir, ()
    else:
        if plan.document.enhancement_chain or plan.document.complex_vision:
            from reqpilotx_server.application.extraction.processing_visual_chain import (
                build_composable_pdf_source_ir_adapter,
                page_processing_receipts_from_adapter,
            )

            adapter_options: dict[str, Any] = {
                "source_asset_publisher": source_asset_publisher,
            }
            if page_progress_callback is not None:
                adapter_options["page_progress_callback"] = page_progress_callback
            if page_staging_callback is not None:
                adapter_options["page_staging_callback"] = page_staging_callback
            if visual_text_snapshot_cache is not None:
                adapter_options["visual_text_snapshot_cache"] = (
                    visual_text_snapshot_cache
                )
            if provider_telemetry_callback is not None:
                adapter_options["provider_telemetry_callback"] = (
                    provider_telemetry_callback
                )
            adapter = build_composable_pdf_source_ir_adapter(
                plan,
                **adapter_options,
            )
        else:
            adapter = PdfSourceIRAdapter(
                source_asset_publisher=source_asset_publisher,
                page_progress_callback=page_progress_callback,
                page_staging_callback=page_staging_callback,
            )
    source_ir = build_source_ir(
        source_path,
        preflight_report=report,
        adapters=(adapter,),
    )
    receipts = (
        page_processing_receipts_from_adapter(adapter)
        if plan.document.enhancement_chain or plan.document.complex_vision
        else ()
    )
    return source_ir, receipts


def _stable_source_ir_sha256(source_ir: SourceIRSnapshot) -> str:
    import hashlib
    import json

    payload = source_ir.model_dump(mode="json", exclude={"created_at"})
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def processing_plan_visual_strategy(
    plan: ResolvedProcessingPlanV1,
) -> dict[str, Any]:
    if plan.document.primary_parser == "azure_document_intelligence":
        from reqpilotx_server.adapters.documents.azure_document_intelligence_source_ir_adapter import (
            AZURE_DI_SOURCE_IR_ADAPTER_VERSION,
        )

        adapter_version = (
            f"{AZURE_DI_SOURCE_IR_ADAPTER_VERSION}+plan.{plan.canonical_sha256[:12]}"
        )
    elif plan.document.enhancement_chain or plan.document.complex_vision:
        from reqpilotx_server.application.extraction.processing_visual_chain import (
            build_composable_pdf_source_ir_adapter,
        )

        adapter_version = build_composable_pdf_source_ir_adapter(plan).adapter_version
    else:
        adapter_version = PdfSourceIRAdapter.adapter_version
    return extraction_visual_strategy_identity(
        None,
        source_ir_adapter_version=adapter_version,
    )


__all__ = [
    "LOCAL_STRUCTURE_PROJECTION_MODE",
    "build_local_structure_document_result",
    "build_source_ir_and_receipts_for_plan",
    "build_source_ir_for_plan",
    "processing_plan_visual_strategy",
]

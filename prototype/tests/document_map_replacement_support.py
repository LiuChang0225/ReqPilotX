from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path

from app.application.agent_workflow.document_map_exploration import (
    DocumentMapSurveyCandidateV1,
    DocumentMapSurveyCompilationV1,
    DocumentMapSurveyRangeCandidateV1,
    DocumentMapSurveyRangeKind,
    DocumentMapSurveyStatus,
    DocumentMapVisualCoverageMode,
    build_document_map_visual_coverage_receipt,
    compile_document_map_survey,
)
from app.application.agent_workflow.document_map_replacement import (
    DocumentMapAssetOwnershipDecisionV1,
    DocumentMapAssetRole,
    DocumentMapContentDisposition,
    DocumentMapLogicalTableDecisionV1,
    DocumentMapScopeEvidenceIndex,
    DocumentMapScopeEvidenceRecord,
    DocumentMapScopeReplacementAction,
    DocumentMapScopeReplacementCandidateV1,
    DocumentMapScopeReplacementRequestV1,
    DocumentMapTableFragmentDecisionV1,
    DocumentMapTableTitleAuthority,
    DocumentMapTextOwnershipOverrideV1,
    DocumentMapTextOwnershipPolicy,
    build_document_map_scope_replacement_request,
)
from app.application.agent_workflow.document_map_scope import (
    DocumentMapDetailScopeKind,
    DocumentMapHeadingCandidateRefV1,
    DocumentMapHeadingDecisionV1,
    DocumentMapHeadingDisposition,
    DocumentMapHeadingScopeActionV2,
    DocumentMapHeadingScopeCandidateV2,
    DocumentMapScopeRequestV1,
    DocumentMapSourceEndpointV1,
    DocumentMapSourceSpanV1,
    compile_document_map_heading_scope,
)
from app.application.agent_workflow.extraction_runtime import AgentKitIdentityV1
from app.application.agent_workflow.native_heading_skeleton import (
    NativeHeadingCandidateState,
)
from app.application.agent_workflow.native_structure_working_revision import (
    canonical_json_sha256,
)


@dataclass(frozen=True)
class ReplacementFixture:
    request: DocumentMapScopeReplacementRequestV1
    evidence: DocumentMapScopeEvidenceIndex
    candidate: DocumentMapScopeReplacementCandidateV1
    survey: DocumentMapSurveyCompilationV1


def build_replacement_fixture(
    root: Path,
    *,
    source_sha: str = "1" * 64,
    workspace_bundle_sha256: str = "5" * 64,
    run_id: str = "agent-run-" + "a" * 32,
    split_survey_units: bool = False,
) -> ReplacementFixture:
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve()
    node_ids = tuple("node-" + f"{index:032x}" for index in range(1, 9))
    table_ids = ("table-" + "b" * 32, "table-" + "c" * 32)
    asset_id = "asset-" + "d" * 32
    kit = AgentKitIdentityV1(
        kit_id="reqpilotx-document-extraction",
        kit_version="0.15.0",
        manifest_sha256="2" * 64,
        bundle_sha256="3" * 64,
        policy_id="reqpilotx-extraction-policy",
        policy_version="1.0.0",
        tool_registry_id="reqpilotx-document-evidence-tools",
        tool_registry_version="1.3.0",
    )
    survey_candidate = DocumentMapSurveyCandidateV1(
        run_id=run_id,
        source_sha256=source_sha,
        kit_identity=kit,
        page_count=2,
        status=DocumentMapSurveyStatus.COMPLETE,
        document_summary="Two-page synthetic technical document.",
        ranges=(
            DocumentMapSurveyRangeCandidateV1(
                proposal_key="main",
                kind=DocumentMapSurveyRangeKind.MAIN_DOCUMENT,
                label="Technical document",
                page_numbers=(1,) if split_survey_units else (1, 2),
                evidence_refs=("page:1",)
                if split_survey_units
                else ("page:1", "page:2"),
                public_summary="One continuous source document.",
            ),
            *(
                (
                    DocumentMapSurveyRangeCandidateV1(
                        proposal_key="supplement",
                        kind=DocumentMapSurveyRangeKind.ATTACHMENT,
                        label="Provisional supplementary range",
                        page_numbers=(2,),
                        evidence_refs=("page:2",),
                        public_summary="The page also contains continuation of the main chapter.",
                    ),
                )
                if split_survey_units
                else ()
            ),
        ),
        findings=(),
    )
    visual = build_document_map_visual_coverage_receipt(
        source_sha256=source_sha,
        page_count=2,
        mode=DocumentMapVisualCoverageMode.REFERENCE_FULL_PAGE,
        event_summary={
            "schema_version": "reqpilotx-canonical-stage-event-ledger/v1",
            "visual_inspection": {
                "contact_sheet_opened": True,
                "high_resolution_page_numbers": [1, 2],
                "other_image_view_count": 0,
            },
        },
    )
    survey = compile_document_map_survey(
        survey_candidate,
        visual_coverage=visual,
        expected_run_id=run_id,
        expected_source_sha256=source_sha,
        expected_kit_identity=kit,
    )
    headings = (
        DocumentMapHeadingCandidateRefV1(
            candidate_id="heading-candidate-" + "1" * 32,
            source_node_id=node_ids[0],
            source_order=0,
            page_number=1,
            number="1",
            title="Scope",
            level=1,
            advisory_state=NativeHeadingCandidateState.PROPOSED,
        ),
        DocumentMapHeadingCandidateRefV1(
            candidate_id="heading-candidate-" + "2" * 32,
            source_node_id=node_ids[2],
            source_order=2,
            page_number=1,
            number="1.1",
            title="Responsibilities",
            level=2,
            advisory_parent_candidate_id="heading-candidate-" + "1" * 32,
            advisory_state=NativeHeadingCandidateState.PROPOSED,
        ),
    )
    scope_values = {
        "run_id": run_id,
        "source_sha256": source_sha,
        "source_ir_id": "sir-synthetic-replacement",
        "source_ir_canonical_sha256": "4" * 64,
        "evidence_workspace_bundle_sha256": workspace_bundle_sha256,
        "survey_compilation_sha256": survey.compilation_sha256,
        "visual_coverage_receipt_sha256": visual.receipt_sha256,
        "heading_skeleton_sha256": "6" * 64,
        "scope_id": "document-map-detail-scope-" + "7" * 32,
        "kind": DocumentMapDetailScopeKind.CHAPTER,
        "advisory_number": "1",
        "advisory_title": "Scope",
        "suggested_source_span": DocumentMapSourceSpanV1(
            start=DocumentMapSourceEndpointV1(source_node_id=node_ids[0], offset=0),
            end=DocumentMapSourceEndpointV1(source_node_id=node_ids[7], offset=0),
        ),
        "source_node_ids": node_ids[:7],
        "page_numbers": (1, 2),
        "physical_table_ids": table_ids,
        "asset_ids": (asset_id,),
        "heading_candidates": headings,
        "preceding_context_source_node_ids": (),
        "following_context_source_node_ids": (node_ids[7],),
    }
    scope_draft = DocumentMapScopeRequestV1.model_construct(
        **scope_values,
        request_sha256="0" * 64,
    )
    scope = DocumentMapScopeRequestV1(
        **scope_values,
        request_sha256=canonical_json_sha256(
            scope_draft.model_dump(mode="json", exclude={"request_sha256"})
        ),
    )
    heading_candidate = DocumentMapHeadingScopeCandidateV2(
        request_id=scope.scope_id,
        request_sha256=scope.request_sha256,
        action=DocumentMapHeadingScopeActionV2.ACCEPT_SCOPE,
        accepted_source_span=scope.suggested_source_span,
        heading_decisions=(
            DocumentMapHeadingDecisionV1(
                candidate_id=headings[0].candidate_id,
                disposition=DocumentMapHeadingDisposition.HEADING,
                parent_candidate_id=None,
                confidence=0.99,
                public_summary="Chapter heading.",
            ),
            DocumentMapHeadingDecisionV1(
                candidate_id=headings[1].candidate_id,
                disposition=DocumentMapHeadingDisposition.HEADING,
                parent_candidate_id=headings[0].candidate_id,
                confidence=0.99,
                public_summary="Section heading.",
            ),
        ),
        heading_expansion_proposals=(),
        findings=(),
        public_summary="Both source headings confirmed.",
    )
    heading_compilation = compile_document_map_heading_scope(
        request=scope,
        candidate=heading_candidate,
    )
    request = build_document_map_scope_replacement_request(
        scope_request=scope,
        heading_compilation=heading_compilation,
    )
    first_heading, child_heading = heading_compilation.heading_nodes
    records = tuple(
        _record(
            node_id=node_ids[index],
            order=index,
            page=page,
            text=text,
            table_id=table_id,
            asset_id=record_asset,
        )
        for index, page, text, table_id, record_asset in (
            (0, 1, "1 Scope", None, None),
            (1, 1, "Lead text with exact trailing space. ", None, None),
            (2, 1, "1.1 Responsibilities", None, None),
            (3, 1, "", table_ids[0], None),
            (4, 2, "", table_ids[1], None),
            (5, 2, "Closing statement for the chapter.", None, None),
            (6, 2, "", None, asset_id),
        )
    )
    tables = {
        table_ids[0]: _table(table_ids[0], "T0001", page=1, columns=3),
        table_ids[1]: _table(table_ids[1], "T0002", page=2, columns=4),
    }
    table_index = {
        table_id: {
            "table_id": table_id,
            "table_token": table["table_token"],
            "table_sha256": canonical_json_sha256(table),
        }
        for table_id, table in tables.items()
    }
    evidence = DocumentMapScopeEvidenceIndex(
        root=root,
        source_sha256=source_sha,
        source_ir_canonical_sha256="4" * 64,
        scope_id=scope.scope_id,
        records=records,
        tables_by_id=tables,
        table_index_by_id=table_index,
        assets_by_id={asset_id: {"asset_id": asset_id}},
    )
    candidate = DocumentMapScopeReplacementCandidateV1(
        request_id=scope.scope_id,
        request_sha256=request.request_sha256,
        heading_compilation_sha256=heading_compilation.compilation_sha256,
        action=DocumentMapScopeReplacementAction.ACCEPT_SCOPE,
        text_ownership_policy=(
            DocumentMapTextOwnershipPolicy.NEAREST_PRECEDING_ACCEPTED_HEADING
        ),
        text_ownership_overrides=(
            DocumentMapTextOwnershipOverrideV1(
                source_node_ids=(node_ids[5],),
                disposition=DocumentMapContentDisposition.OWNED_BY_HEADING,
                display_heading_node_id=first_heading.stable_node_id,
                public_summary="The closing paragraph explicitly applies to the chapter.",
            ),
        ),
        logical_tables=(
            DocumentMapLogicalTableDecisionV1(
                logical_table_key="responsibility-matrix",
                owner_heading_node_id=child_heading.stable_node_id,
                title="Responsibilities matrix",
                title_authority=DocumentMapTableTitleAuthority.AGENT_LABEL,
                fragments=tuple(
                    DocumentMapTableFragmentDecisionV1(
                        physical_table_id=table_id,
                        start_row=0,
                        end_row=2,
                    )
                    for table_id in table_ids
                ),
                public_summary="The second page continues the same matrix.",
            ),
        ),
        table_remainders=(),
        asset_ownership=(
            DocumentMapAssetOwnershipDecisionV1(
                placement_source_node_id=node_ids[6],
                display_heading_node_id=child_heading.stable_node_id,
                disposition=DocumentMapContentDisposition.OWNED_BY_HEADING,
                role=DocumentMapAssetRole.FIGURE,
                public_summary="The figure illustrates this responsibility section.",
            ),
        ),
        findings=(),
        public_summary="Complete source scope with one continued table and one figure.",
    )
    return ReplacementFixture(request, evidence, candidate, survey)


def _record(
    *,
    node_id: str,
    order: int,
    page: int,
    text: str,
    table_id: str | None,
    asset_id: str | None,
) -> DocumentMapScopeEvidenceRecord:
    return DocumentMapScopeEvidenceRecord(
        source_node_id=node_id,
        source_order=order,
        source_kind="table" if table_id else "image" if asset_id else "paragraph",
        page_number=page,
        bbox=(50.0, 50.0 + order * 10.0, 500.0, 80.0 + order * 10.0),
        page_bounds=(0.0, 0.0, 612.0, 792.0),
        raw_text=text,
        source_text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        table_id=table_id,
        asset_id=asset_id,
    )


def _table(table_id: str, token: str, *, page: int, columns: int) -> dict:
    cells = [
        {
            "cell_id": f"cell-{table_id[-8:]}-{row}-{column}",
            "row": row,
            "column": column,
            "row_span": 1,
            "column_span": 1,
            "raw_text": f"row-{row}-column-{column}",
            "native_locator": {
                "bbox": [
                    50.0 + column * 80,
                    150.0 + row * 20,
                    130.0 + column * 80,
                    170.0 + row * 20,
                ],
            },
        }
        for row in range(2)
        for column in range(columns)
    ]
    return {
        "schema_version": "reqpilotx-reference-physical-table/v2",
        "table_id": table_id,
        "table_token": token,
        "page_number": page,
        "bbox": [50.0, 150.0, 500.0, 190.0],
        "row_count": 2,
        "column_count": columns,
        "header_regions": [],
        "merged_regions": [],
        "notes": [],
        "cells": cells,
    }

from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import unittest
from datetime import datetime, timezone

from pydantic import ValidationError

from app.application.agent_workflow.d2_logical_structure import (
    D2LogicalObjectKind,
    D2LogicalObjectV1,
    D2LogicalStructureProposalV1,
    D2LogicalStructureStatus,
    D2PhysicalTableDispositionKind,
    D2PhysicalTableDispositionV1,
    D2SourceEvidenceDispositionKind,
    D2SourceEvidenceDispositionV1,
    D2SourceHandleKind,
    D2SourceSegmentV1,
    D2ValidatedD1ArtifactIdentityV1,
    stable_d2_logical_object_id,
    stable_d2_logical_object_sha256,
    stable_d2_logical_structure_proposal_sha256,
    stable_d2_source_disposition_id,
    stable_d2_source_disposition_sha256,
    stable_d2_source_segment_id,
    stable_d2_source_segment_sha256,
    stable_d2_table_disposition_sha256,
    validate_d2_logical_structure_proposal,
)
from app.application.agent_workflow.d2_structure_quality_oracle import (
    LogicalStructureBoundaryExpectationV1,
    LogicalStructureExpectedNestedRegionV1,
    LogicalStructureExpectedPageOwnerV1,
    LogicalStructureExpectedTableGroupV1,
    LogicalStructureQualityAxisStatus,
    LogicalStructureQualityOracleV1,
    LogicalStructureQualityStatus,
    LogicalStructureRegionPairV1,
    LogicalStructureTableRegionSelectorKind,
    LogicalStructureTableRegionV1,
    evaluate_logical_structure_quality,
    stable_logical_structure_quality_oracle_sha256,
)
from app.application.agent_workflow.extraction_runtime import (
    AgentKitIdentityV1,
    AgentStageArtifactStatus,
)
from app.application.extraction.agent_logical_table_scope import (
    AgentD1TableMapArtifactIdentityV1,
    AgentLogicalTableScopeV1,
    build_agent_logical_table_scope,
    source_ir_canonical_sha256,
)
from app.domain.extraction.source_ir import (
    NativeDocumentFormat,
    NativeLocator,
    SourceIRSnapshot,
    SourceIRStatus,
    SourceNode,
    SourceNodeKind,
    TableCellIR,
    TableIR,
    stable_cell_id,
    stable_document_id,
    stable_node_id,
    stable_source_ir_id,
    stable_table_id,
)
from app.domain.extraction.structure_revision import canonical_model_sha256


SOURCE_SHA = "a" * 64
RUN_ID = "agent-run-" + "1" * 32


class LogicalStructureQualityOracleTests(unittest.TestCase):
    def test_exact_cover_does_not_hide_wrong_seven_or_nine_table_groups(self) -> None:
        expected_group_indices = tuple(
            tuple(range(start, start + 2)) for start in range(0, 30, 2)
        )
        observed_groupings = (
            (
                (0, 1, 2, 3, 4),
                (5, 6, 7, 8, 9),
                (10, 11, 12, 13),
                (14, 15, 16, 17),
                (18, 19, 20, 21),
                (22, 23, 24, 25),
                (26, 27, 28, 29),
            ),
            (
                (0, 1, 2, 3),
                (4, 5, 6, 7),
                (8, 9, 10, 11),
                (12, 13, 14),
                (15, 16, 17),
                (18, 19, 20),
                (21, 22, 23),
                (24, 25, 26),
                (27, 28, 29),
            ),
        )

        for observed_groups in observed_groupings:
            with self.subTest(observed_group_count=len(observed_groups)):
                fixture = _build_fixture(
                    table_count=30,
                    business_groups=observed_groups,
                )
                expectation = _expectation(
                    "primary",
                    page_count=31,
                    table_ids=fixture.table_ids,
                    group_indices=expected_group_indices,
                    must_separate=((0, 2),),
                )
                oracle = _oracle(fixture, expectation)

                report = evaluate_logical_structure_quality(
                    fixture.source_ir,
                    fixture.proposal,
                    fixture.receipt,
                    oracle=oracle,
                    expected_oracle_sha256=oracle.oracle_sha256,
                )

                self.assertEqual(len(fixture.receipt.physical_table_ids), 30)
                self.assertEqual(
                    report.coverage_status,
                    LogicalStructureQualityAxisStatus.PASS,
                )
                self.assertEqual(
                    report.boundary_status,
                    LogicalStructureQualityAxisStatus.FAIL,
                )
                self.assertEqual(
                    report.overall_status, LogicalStructureQualityStatus.FAIL
                )
                self.assertIn(
                    "logical_table_grouping_mismatch",
                    {item.code for item in report.findings},
                )
                self.assertIn(
                    "must_separate_boundary_merged",
                    {item.code for item in report.findings},
                )

    def test_same_physical_table_can_require_distinct_region_owners(self) -> None:
        fixture = _build_fixture(table_count=1, business_groups=((0,),))
        table_id = fixture.table_ids[0]
        first = _row_region("top-region", table_id, (0,))
        second = _row_region("bottom-region", table_id, (1,))
        expectation = LogicalStructureBoundaryExpectationV1(
            expectation_id="split-one-physical-table",
            page_owners=(_page_owner(2),),
            logical_table_groups=(
                _group("top-logical-region", (first,)),
                _group("bottom-logical-region", (second,)),
            ),
            must_separate_pairs=(
                LogicalStructureRegionPairV1(
                    first_region_id=first.region_id,
                    second_region_id=second.region_id,
                ),
            ),
        )
        oracle = _oracle(fixture, expectation)

        report = evaluate_logical_structure_quality(
            fixture.source_ir,
            fixture.proposal,
            fixture.receipt,
            oracle=oracle,
            expected_oracle_sha256=oracle.oracle_sha256,
        )

        self.assertEqual(report.coverage_status, LogicalStructureQualityAxisStatus.PASS)
        self.assertEqual(report.boundary_status, LogicalStructureQualityAxisStatus.FAIL)
        codes = {item.code for item in report.findings}
        self.assertIn("region_granularity_mismatch", codes)
        self.assertIn("must_separate_boundary_merged", codes)

    def test_allowed_alternative_can_match_without_weakening_primary(self) -> None:
        fixture = _build_fixture(
            table_count=4,
            business_groups=((0, 1), (2, 3)),
        )
        primary = _expectation(
            "primary",
            page_count=5,
            table_ids=fixture.table_ids,
            group_indices=((0,), (1,), (2,), (3,)),
        )
        accepted_alternative = _expectation(
            "accepted-alternative",
            page_count=5,
            table_ids=fixture.table_ids,
            group_indices=((0, 1), (2, 3)),
        )
        oracle = _oracle(
            fixture,
            primary,
            alternatives=(accepted_alternative,),
        )

        report = evaluate_logical_structure_quality(
            fixture.source_ir,
            fixture.proposal,
            fixture.receipt,
            oracle=oracle,
            expected_oracle_sha256=oracle.oracle_sha256,
        )

        self.assertEqual(report.overall_status, LogicalStructureQualityStatus.PASS)
        self.assertEqual(report.matched_expectation_id, "accepted-alternative")
        self.assertEqual(
            tuple(item.status for item in report.alternative_evaluations),
            (LogicalStructureQualityStatus.FAIL, LogicalStructureQualityStatus.PASS),
        )

    def test_nested_and_false_table_regions_are_evaluated_explicitly(self) -> None:
        fixture = _build_fixture(
            table_count=4,
            business_groups=((0, 1),),
            nested={2: 0},
            false_tables={3},
        )
        business_regions = tuple(
            _whole_region(f"business-{index}", fixture.table_ids[index])
            for index in (0, 1)
        )
        nested_region = _whole_region("nested-2", fixture.table_ids[2])
        false_region = _whole_region("false-3", fixture.table_ids[3])
        expectation = LogicalStructureBoundaryExpectationV1(
            expectation_id="nested-and-false",
            page_owners=(_page_owner(5),),
            logical_table_groups=(_group("business-group", business_regions),),
            nested_regions=(
                LogicalStructureExpectedNestedRegionV1(
                    region=nested_region,
                    owner_region_id=business_regions[0].region_id,
                ),
            ),
            false_regions=(false_region,),
        )
        oracle = _oracle(fixture, expectation)

        report = evaluate_logical_structure_quality(
            fixture.source_ir,
            fixture.proposal,
            fixture.receipt,
            oracle=oracle,
            expected_oracle_sha256=oracle.oracle_sha256,
        )

        self.assertEqual(report.coverage_status, LogicalStructureQualityAxisStatus.PASS)
        self.assertEqual(report.boundary_status, LogicalStructureQualityAxisStatus.PASS)
        self.assertEqual(report.overall_status, LogicalStructureQualityStatus.PASS)

    def test_document_unit_and_attachment_page_ownership_is_not_a_count_gate(
        self,
    ) -> None:
        fixture = _build_fixture(
            table_count=4,
            business_groups=((0, 1), (2, 3)),
        )
        table_groups = tuple(
            _group(
                f"group-{group_index}",
                tuple(
                    _whole_region(f"region-{index}", fixture.table_ids[index])
                    for index in indices
                ),
            )
            for group_index, indices in enumerate(((0, 1), (2, 3)))
        )
        expectation = LogicalStructureBoundaryExpectationV1(
            expectation_id="attachment-boundaries",
            page_owners=(
                LogicalStructureExpectedPageOwnerV1(
                    owner_ref="main-document",
                    object_kind=D2LogicalObjectKind.DOCUMENT_UNIT,
                    page_numbers=(1, 2),
                ),
                LogicalStructureExpectedPageOwnerV1(
                    owner_ref="attachment",
                    object_kind=D2LogicalObjectKind.ATTACHMENT,
                    page_numbers=(3, 4, 5),
                ),
            ),
            logical_table_groups=table_groups,
        )
        oracle = _oracle(fixture, expectation)

        report = evaluate_logical_structure_quality(
            fixture.source_ir,
            fixture.proposal,
            fixture.receipt,
            oracle=oracle,
            expected_oracle_sha256=oracle.oracle_sha256,
        )

        self.assertEqual(report.coverage_status, LogicalStructureQualityAxisStatus.PASS)
        self.assertEqual(report.boundary_status, LogicalStructureQualityAxisStatus.FAIL)
        self.assertIn(
            "page_ownership_mismatch",
            {item.code for item in report.findings},
        )

    def test_source_kit_and_oracle_drift_fail_closed(self) -> None:
        fixture = _build_fixture(table_count=2, business_groups=((0, 1),))
        expectation = _expectation(
            "matching",
            page_count=3,
            table_ids=fixture.table_ids,
            group_indices=((0, 1),),
        )
        oracle = _oracle(fixture, expectation)

        cases = (
            (
                "source",
                _copy_oracle(oracle, source_sha256="f" * 64),
                None,
                "source_identity_drift",
            ),
            (
                "kit",
                _copy_oracle(oracle, kit_bundle_sha256="e" * 64),
                None,
                "kit_identity_drift",
            ),
            (
                "oracle",
                oracle,
                "d" * 64,
                "oracle_sha256_drift",
            ),
        )
        for name, selected_oracle, expected_sha, expected_code in cases:
            with self.subTest(name=name):
                report = evaluate_logical_structure_quality(
                    fixture.source_ir,
                    fixture.proposal,
                    fixture.receipt,
                    oracle=selected_oracle,
                    expected_oracle_sha256=(
                        expected_sha or selected_oracle.oracle_sha256
                    ),
                )
                self.assertEqual(
                    report.identity_status,
                    LogicalStructureQualityAxisStatus.FAIL,
                )
                self.assertEqual(
                    report.coverage_status,
                    LogicalStructureQualityAxisStatus.NOT_EVALUATED,
                )
                self.assertEqual(
                    report.boundary_status,
                    LogicalStructureQualityAxisStatus.NOT_EVALUATED,
                )
                self.assertIn(expected_code, {item.code for item in report.findings})

    def test_report_hash_is_stable_and_contracts_reject_extras(self) -> None:
        fixture = _build_fixture(table_count=2, business_groups=((0, 1),))
        expectation = _expectation(
            "matching",
            page_count=3,
            table_ids=fixture.table_ids,
            group_indices=((0, 1),),
        )
        oracle = _oracle(fixture, expectation)

        first = evaluate_logical_structure_quality(
            fixture.source_ir,
            fixture.proposal,
            fixture.receipt,
            oracle=oracle,
            expected_oracle_sha256=oracle.oracle_sha256,
        )
        second = evaluate_logical_structure_quality(
            fixture.source_ir,
            fixture.proposal,
            fixture.receipt,
            oracle=oracle,
            expected_oracle_sha256=oracle.oracle_sha256,
        )

        self.assertEqual(first, second)
        self.assertEqual(first.report_sha256, second.report_sha256)
        invalid = oracle.model_dump(mode="json") | {"unexpected": True}
        with self.assertRaises(ValidationError):
            LogicalStructureQualityOracleV1.model_validate(invalid)


class _Fixture:
    def __init__(
        self,
        *,
        source_ir: SourceIRSnapshot,
        proposal: D2LogicalStructureProposalV1,
        receipt,
        kit: AgentKitIdentityV1,
        table_ids: tuple[str, ...],
    ) -> None:
        self.source_ir = source_ir
        self.proposal = proposal
        self.receipt = receipt
        self.kit = kit
        self.table_ids = table_ids


def _build_fixture(
    *,
    table_count: int,
    business_groups: tuple[tuple[int, ...], ...],
    nested: dict[int, int] | None = None,
    false_tables: set[int] | None = None,
) -> _Fixture:
    nested = nested or {}
    false_tables = false_tables or set()
    source = _source_fixture(table_count)
    kit = _kit_identity()
    d1 = _d1_identity(kit, page_count=table_count + 1)
    table_map = AgentD1TableMapArtifactIdentityV1(
        source_sha256=SOURCE_SHA,
        artifact_id="d1-table-map-" + "4" * 32,
        artifact_sha256="5" * 64,
    )
    table_nodes = tuple(
        node for node in source.nodes if node.kind is SourceNodeKind.TABLE
    )
    table_ids = tuple(
        node.table_payload.table_id
        for node in table_nodes
        if node.table_payload is not None
    )

    scopes: list[AgentLogicalTableScopeV1] = []
    for group in business_groups:
        scopes.append(
            build_agent_logical_table_scope(
                source,
                d1_table_map_artifact=table_map,
                physical_table_ids=tuple(table_ids[index] for index in group),
                reason_codes=("same_logical_object",),
            )
        )

    all_segments = tuple(_segment(node) for node in source.nodes)
    document = _logical_object(
        object_kind=D2LogicalObjectKind.DOCUMENT_UNIT,
        source_segments=all_segments,
    )
    table_objects = tuple(_table_object(scope) for scope in scopes)
    logical_objects = tuple(
        sorted(
            (document, *table_objects),
            key=lambda item: (item.source_order, item.logical_object_id),
        )
    )

    scope_by_table = {
        table_id: scope.logical_object_id
        for scope in scopes
        for table_id in scope.physical_table_ids
    }
    dispositions = []
    for index, table_id in enumerate(table_ids):
        if table_id in scope_by_table:
            dispositions.append(
                _disposition(
                    table_id,
                    D2PhysicalTableDispositionKind.LOGICAL_SCOPE_PART,
                    scope=scope_by_table[table_id],
                )
            )
        elif index in nested:
            dispositions.append(
                _disposition(
                    table_id,
                    D2PhysicalTableDispositionKind.NESTED_VALUE_TABLE,
                    owner=table_ids[nested[index]],
                )
            )
        elif index in false_tables:
            dispositions.append(
                _disposition(
                    table_id,
                    D2PhysicalTableDispositionKind.NON_BUSINESS_FALSE_TABLE,
                )
            )
        else:  # pragma: no cover - fixture misuse is intentionally loud.
            raise AssertionError(f"physical table {index} lacks a disposition")

    text_segment = all_segments[0]
    source_disposition = _source_disposition(
        text_segment,
        document.logical_object_id,
    )
    proposal_values = dict(
        run_id=RUN_ID,
        source_sha256=SOURCE_SHA,
        source_ir_id=source.source_ir_id,
        source_ir_canonical_sha256=source_ir_canonical_sha256(source),
        kit_identity=kit,
        predecessor_d1_artifact=d1,
        d1_table_map_artifact=table_map,
        status=D2LogicalStructureStatus.COMPLETE,
        logical_objects=logical_objects,
        logical_table_scopes=tuple(scopes),
        source_evidence_dispositions=(source_disposition,),
        physical_table_dispositions=tuple(dispositions),
        unresolved_codes=(),
    )
    draft = D2LogicalStructureProposalV1.model_construct(
        **proposal_values,
        proposal_sha256="0" * 64,
    )
    proposal = D2LogicalStructureProposalV1(
        **proposal_values,
        proposal_sha256=stable_d2_logical_structure_proposal_sha256(draft),
    )
    receipt = validate_d2_logical_structure_proposal(
        source,
        proposal,
        expected_d1_artifact=d1,
        expected_d1_table_map_artifact=table_map,
        expected_kit_identity=kit,
    )
    return _Fixture(
        source_ir=source,
        proposal=proposal,
        receipt=receipt,
        kit=kit,
        table_ids=table_ids,
    )


def _kit_identity() -> AgentKitIdentityV1:
    return AgentKitIdentityV1(
        kit_id="reqpilotx-extraction",
        kit_version="0.7.0",
        manifest_sha256="2" * 64,
        bundle_sha256="3" * 64,
        policy_id="reqpilotx-extraction-policy",
        policy_version="1.0.0",
        tool_registry_id="reqpilotx-document-evidence-tools",
        tool_registry_version="1.0.0",
    )


def _d1_identity(
    kit: AgentKitIdentityV1,
    *,
    page_count: int,
) -> D2ValidatedD1ArtifactIdentityV1:
    return D2ValidatedD1ArtifactIdentityV1(
        artifact_id="agent-artifact-" + "6" * 32,
        artifact_sha256="7" * 64,
        run_id=RUN_ID,
        source_sha256=SOURCE_SHA,
        status=AgentStageArtifactStatus.VALIDATED,
        proposal_sha256="8" * 64,
        evidence_sha256="9" * 64,
        validator_receipt_sha256="b" * 64,
        page_count=page_count,
        kit_bundle_sha256=kit.bundle_sha256,
    )


def _source_fixture(table_count: int) -> SourceIRSnapshot:
    nodes: list[SourceNode] = []
    paragraph_locator = NativeLocator(
        format=NativeDocumentFormat.PDF,
        page_number=1,
        block_index=0,
    )
    nodes.append(
        SourceNode(
            source_node_id=stable_node_id(
                source_sha256=SOURCE_SHA,
                adapter_id="quality-oracle-test",
                adapter_version="1.0.0",
                kind=SourceNodeKind.PARAGRAPH,
                native_locator=paragraph_locator,
            ),
            kind=SourceNodeKind.PARAGRAPH,
            source_order=0,
            raw_text="A source-faithful provision.",
            native_locator=paragraph_locator,
        )
    )
    for source_order in range(1, table_count + 1):
        page_number = source_order + 1
        locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=page_number,
            table_index=0,
        )
        table_id = stable_table_id(
            source_sha256=SOURCE_SHA,
            adapter_id="quality-oracle-test",
            adapter_version="1.0.0",
            native_locator=locator,
        )
        cells = tuple(
            TableCellIR(
                cell_id=stable_cell_id(table_id, row, column),
                row=row,
                column=column,
                raw_text=value,
                native_locator=NativeLocator(
                    format=NativeDocumentFormat.PDF,
                    page_number=page_number,
                    table_index=0,
                    row_index=row,
                    column_index=column,
                ),
            )
            for row, values in enumerate((("ID", "Value"), ("1", "Requirement")))
            for column, value in enumerate(values)
        )
        table = TableIR(
            table_id=table_id,
            row_count=2,
            column_count=2,
            cells=cells,
        )
        nodes.append(
            SourceNode(
                source_node_id=stable_node_id(
                    source_sha256=SOURCE_SHA,
                    adapter_id="quality-oracle-test",
                    adapter_version="1.0.0",
                    kind=SourceNodeKind.TABLE,
                    native_locator=locator,
                ),
                kind=SourceNodeKind.TABLE,
                source_order=source_order,
                native_locator=locator,
                table_payload=table,
            )
        )
    document_id = stable_document_id(SOURCE_SHA)
    return SourceIRSnapshot(
        source_ir_id=stable_source_ir_id(
            document_id=document_id,
            source_sha256=SOURCE_SHA,
            adapter_id="quality-oracle-test",
            adapter_version="1.0.0",
        ),
        document_id=document_id,
        source_sha256=SOURCE_SHA,
        adapter_id="quality-oracle-test",
        adapter_version="1.0.0",
        status=SourceIRStatus.COMPLETE,
        nodes=tuple(nodes),
        created_at=datetime(2026, 8, 25, tzinfo=timezone.utc),
    )


def _segment(node: SourceNode) -> D2SourceSegmentV1:
    page_number = node.native_locator.page_number
    assert page_number is not None
    if node.raw_text:
        content_sha = hashlib.sha256(node.raw_text.encode("utf-8")).hexdigest()
        start, end = 0, len(node.raw_text)
    else:
        assert node.table_payload is not None
        content_sha = canonical_model_sha256(node.table_payload)
        start, end = None, None
    segment_id = stable_d2_source_segment_id(
        source_sha256=SOURCE_SHA,
        handle_kind=D2SourceHandleKind.SOURCE_NODE,
        source_handle=node.source_node_id,
        start_offset=start,
        end_offset=end,
    )
    values = dict(
        segment_id=segment_id,
        source_sha256=SOURCE_SHA,
        handle_kind=D2SourceHandleKind.SOURCE_NODE,
        source_handle=node.source_node_id,
        owner_source_node_id=node.source_node_id,
        source_order=node.source_order,
        page_numbers=(page_number,),
        start_offset=start,
        end_offset=end,
        source_content_sha256=content_sha,
    )
    return D2SourceSegmentV1(
        **values,
        segment_sha256=stable_d2_source_segment_sha256(**values),
    )


def _logical_object(
    *,
    object_kind: D2LogicalObjectKind,
    source_segments: tuple[D2SourceSegmentV1, ...],
) -> D2LogicalObjectV1:
    logical_object_id = stable_d2_logical_object_id(
        source_sha256=SOURCE_SHA,
        object_kind=object_kind,
        source_segment_ids=tuple(item.segment_id for item in source_segments),
    )
    values = dict(
        logical_object_id=logical_object_id,
        object_kind=object_kind,
        parent_logical_object_id=None,
        source_order=min(item.source_order for item in source_segments),
        page_numbers=tuple(
            sorted({page for item in source_segments for page in item.page_numbers})
        ),
        source_segments=source_segments,
        logical_table_scope_ids=(),
        reason_codes=("source_boundary",),
    )
    return D2LogicalObjectV1(
        **values,
        object_sha256=stable_d2_logical_object_sha256(**values),
    )


def _table_object(scope: AgentLogicalTableScopeV1) -> D2LogicalObjectV1:
    values = dict(
        logical_object_id=scope.logical_object_id,
        object_kind=D2LogicalObjectKind.TABLE,
        parent_logical_object_id=None,
        source_order=scope.physical_parts[0].source_order,
        page_numbers=tuple(
            sorted(
                {page for part in scope.physical_parts for page in part.page_numbers}
            )
        ),
        source_segments=(),
        logical_table_scope_ids=(scope.logical_object_id,),
        reason_codes=("logical_table_scope",),
    )
    return D2LogicalObjectV1(
        **values,
        object_sha256=stable_d2_logical_object_sha256(**values),
    )


def _disposition(
    table_id: str,
    kind: D2PhysicalTableDispositionKind,
    *,
    scope: str | None = None,
    owner: str | None = None,
) -> D2PhysicalTableDispositionV1:
    values = dict(
        table_id=table_id,
        disposition=kind,
        logical_table_scope_id=scope,
        nested_owner_table_id=owner,
        reason_codes=("table_disposition",),
        evidence_refs=(f"document-table-map:{table_id}",),
    )
    return D2PhysicalTableDispositionV1(
        **values,
        disposition_sha256=stable_d2_table_disposition_sha256(**values),
    )


def _source_disposition(
    segment: D2SourceSegmentV1,
    logical_object_id: str,
) -> D2SourceEvidenceDispositionV1:
    disposition_id = stable_d2_source_disposition_id(
        source_sha256=segment.source_sha256,
        source_node_id=segment.owner_source_node_id,
        start_offset=segment.start_offset,
        end_offset=segment.end_offset,
    )
    values = dict(
        disposition_id=disposition_id,
        source_sha256=segment.source_sha256,
        source_node_id=segment.owner_source_node_id,
        source_order=segment.source_order,
        start_offset=segment.start_offset,
        end_offset=segment.end_offset,
        disposition=D2SourceEvidenceDispositionKind.LOGICAL_OBJECT_PART,
        logical_object_id=logical_object_id,
        source_segment_id=segment.segment_id,
        reason_codes=("source_disposition",),
        evidence_refs=(segment.owner_source_node_id,),
    )
    return D2SourceEvidenceDispositionV1(
        **values,
        disposition_sha256=stable_d2_source_disposition_sha256(**values),
    )


def _page_owner(page_count: int) -> LogicalStructureExpectedPageOwnerV1:
    return LogicalStructureExpectedPageOwnerV1(
        owner_ref="document",
        object_kind=D2LogicalObjectKind.DOCUMENT_UNIT,
        page_numbers=tuple(range(1, page_count + 1)),
    )


def _whole_region(region_id: str, table_id: str) -> LogicalStructureTableRegionV1:
    return LogicalStructureTableRegionV1(
        region_id=region_id,
        table_id=table_id,
        selector_kind=LogicalStructureTableRegionSelectorKind.WHOLE_TABLE,
    )


def _row_region(
    region_id: str,
    table_id: str,
    rows: tuple[int, ...],
) -> LogicalStructureTableRegionV1:
    return LogicalStructureTableRegionV1(
        region_id=region_id,
        table_id=table_id,
        selector_kind=LogicalStructureTableRegionSelectorKind.ROW_INDICES,
        row_indices=rows,
    )


def _group(
    group_ref: str,
    regions: tuple[LogicalStructureTableRegionV1, ...],
) -> LogicalStructureExpectedTableGroupV1:
    return LogicalStructureExpectedTableGroupV1(
        logical_group_ref=group_ref,
        regions=regions,
        continuation_pairs=tuple(
            LogicalStructureRegionPairV1(
                first_region_id=first.region_id,
                second_region_id=second.region_id,
            )
            for first, second in zip(regions, regions[1:], strict=False)
        ),
    )


def _expectation(
    expectation_id: str,
    *,
    page_count: int,
    table_ids: tuple[str, ...],
    group_indices: tuple[tuple[int, ...], ...],
    must_separate: tuple[tuple[int, int], ...] = (),
) -> LogicalStructureBoundaryExpectationV1:
    regions = tuple(
        _whole_region(f"region-{index}", table_id)
        for index, table_id in enumerate(table_ids)
    )
    return LogicalStructureBoundaryExpectationV1(
        expectation_id=expectation_id,
        page_owners=(_page_owner(page_count),),
        logical_table_groups=tuple(
            _group(
                f"group-{group_index}",
                tuple(regions[index] for index in indices),
            )
            for group_index, indices in enumerate(group_indices)
        ),
        must_separate_pairs=tuple(
            LogicalStructureRegionPairV1(
                first_region_id=regions[first].region_id,
                second_region_id=regions[second].region_id,
            )
            for first, second in must_separate
        ),
    )


def _oracle(
    fixture: _Fixture,
    primary: LogicalStructureBoundaryExpectationV1,
    *,
    alternatives: tuple[LogicalStructureBoundaryExpectationV1, ...] = (),
) -> LogicalStructureQualityOracleV1:
    values = dict(
        oracle_id="quality-oracle-fixture",
        source_sha256=fixture.source_ir.source_sha256,
        source_ir_canonical_sha256=source_ir_canonical_sha256(fixture.source_ir),
        kit_bundle_sha256=fixture.kit.bundle_sha256,
        expected_page_count=fixture.proposal.predecessor_d1_artifact.page_count,
        expected_physical_table_ids=fixture.table_ids,
        primary_expectation=primary,
        allowed_alternatives=alternatives,
    )
    draft = LogicalStructureQualityOracleV1.model_construct(
        **values,
        oracle_sha256="0" * 64,
    )
    return LogicalStructureQualityOracleV1(
        **values,
        oracle_sha256=stable_logical_structure_quality_oracle_sha256(draft),
    )


def _copy_oracle(
    oracle: LogicalStructureQualityOracleV1,
    **changes,
) -> LogicalStructureQualityOracleV1:
    draft = oracle.model_copy(
        update=changes | {"oracle_sha256": "0" * 64},
    )
    return LogicalStructureQualityOracleV1.model_validate(
        draft.model_dump(mode="json", exclude={"oracle_sha256"})
        | {"oracle_sha256": stable_logical_structure_quality_oracle_sha256(draft)}
    )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

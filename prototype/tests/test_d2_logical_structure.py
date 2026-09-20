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
    compile_validated_d1_artifact_identity,
    stable_d2_logical_object_id,
    stable_d2_logical_object_sha256,
    stable_d2_logical_structure_proposal_sha256,
    stable_d2_source_segment_id,
    stable_d2_source_segment_sha256,
    stable_d2_source_disposition_id,
    stable_d2_source_disposition_sha256,
    stable_d2_table_disposition_sha256,
    validate_d2_logical_structure_proposal,
)
from app.application.agent_workflow.extraction_runtime import (
    AgentKitIdentityV1,
    AgentStageArtifactStatus,
    AgentStageArtifactV1,
)
from app.application.agent_workflow.document_understanding import (
    DocumentEvidenceAuthority,
    DocumentUnderstandingPageV2,
    DocumentUnderstandingProposalV2,
    DocumentUnderstandingStatus,
    DocumentUnderstandingValidationReceiptV2,
    canonical_json_sha256,
)
from app.application.extraction.agent_logical_table_scope import (
    AgentD1TableMapArtifactIdentityV1,
    AgentLogicalTableScopeEvidenceV1,
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


class D2LogicalStructureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = _source_fixture()
        self.kit = _kit_identity()
        self.d1 = _d1_identity(self.kit)
        self.table_map = AgentD1TableMapArtifactIdentityV1(
            source_sha256=SOURCE_SHA,
            artifact_id="d1-table-map-" + "4" * 32,
            artifact_sha256="5" * 64,
        )
        self.table_nodes = tuple(
            node for node in self.source.nodes if node.kind is SourceNodeKind.TABLE
        )
        self.table_ids = tuple(
            node.table_payload.table_id
            for node in self.table_nodes
            if node.table_payload is not None
        )
        self.scope = _scope(
            self.source,
            self.table_map,
            self.table_ids[:2],
        )

    def test_validates_complete_contract_and_returns_stable_identity(self) -> None:
        proposal = self._complete_proposal()

        receipt = self._validate(proposal)

        self.assertEqual(receipt.status, D2LogicalStructureStatus.COMPLETE)
        self.assertEqual(receipt.artifact_sha256, proposal.proposal_sha256)
        self.assertEqual(
            receipt.logical_table_scope_ids, (self.scope.logical_object_id,)
        )
        self.assertEqual(receipt.physical_table_ids, self.table_ids)
        self.assertEqual(receipt.unresolved_table_ids, ())
        self.assertEqual(receipt.unresolved_source_evidence_disposition_ids, ())
        self.assertEqual(len(receipt.source_evidence_disposition_ids), 1)
        self.assertEqual(len(receipt.source_segment_ids), 3)
        self.assertRegex(receipt.artifact_id, r"^agent-artifact-[0-9a-f]{32}$")

    def test_compiles_only_an_exact_validated_d1_predecessor(self) -> None:
        pages = tuple(
            DocumentUnderstandingPageV2(
                page_number=page_number,
                authority=DocumentEvidenceAuthority.NATIVE,
                page_facts_sha256=f"{page_number:x}" * 64,
                page_text_sha256=f"{page_number + 5:x}" * 64,
            )
            for page_number in range(1, 6)
        )
        proposal = DocumentUnderstandingProposalV2(
            run_id=RUN_ID,
            source_sha256=SOURCE_SHA,
            page_count=5,
            ledger_sha256="c" * 64,
            status=DocumentUnderstandingStatus.COMPLETE,
            pages=pages,
        )
        proposal_sha256 = canonical_json_sha256(proposal.model_dump(mode="json"))
        receipt_payload = {
            "run_id": RUN_ID,
            "source_sha256": SOURCE_SHA,
            "kit_bundle_sha256": self.kit.bundle_sha256,
            "status": DocumentUnderstandingStatus.COMPLETE,
            "page_count": 5,
            "unresolved_pages": (),
            "proposal_sha256": proposal_sha256,
            "evidence_sha256": "d" * 64,
        }
        validation_receipt = DocumentUnderstandingValidationReceiptV2(
            **receipt_payload,
            receipt_sha256=canonical_json_sha256(
                DocumentUnderstandingValidationReceiptV2.model_construct(
                    **receipt_payload,
                    receipt_sha256="0" * 64,
                ).model_dump(mode="json", exclude={"receipt_sha256"})
            ),
        )
        artifact = AgentStageArtifactV1(
            artifact_id="agent-artifact-" + "e" * 32,
            run_id=RUN_ID,
            source_sha256=SOURCE_SHA,
            stage="D1_DOCUMENT_UNDERSTANDING",
            status=AgentStageArtifactStatus.VALIDATED,
            proposal_schema_id="document-understanding-proposal/v2",
            payload_sha256=proposal_sha256,
            evidence_sha256=validation_receipt.evidence_sha256,
            validator_receipt_sha256=validation_receipt.receipt_sha256,
        )

        compiled = compile_validated_d1_artifact_identity(
            stage_artifact=artifact,
            proposal=proposal,
            validation_receipt=validation_receipt,
            kit_identity=self.kit,
        )

        self.assertEqual(compiled.artifact_id, artifact.artifact_id)
        self.assertEqual(compiled.artifact_sha256, proposal_sha256)
        with self.assertRaisesRegex(ValueError, "stale D1"):
            compile_validated_d1_artifact_identity(
                stage_artifact=artifact.model_copy(update={"payload_sha256": "f" * 64}),
                proposal=proposal,
                validation_receipt=validation_receipt,
                kit_identity=self.kit,
            )

    def test_non_table_source_requires_exact_disposition_and_unresolved_is_partial(
        self,
    ) -> None:
        complete = self._complete_proposal()
        missing = _proposal(
            source=self.source,
            kit=self.kit,
            d1=self.d1,
            table_map=self.table_map,
            logical_objects=complete.logical_objects,
            scopes=complete.logical_table_scopes,
            dispositions=complete.physical_table_dispositions,
            source_dispositions=(),
        )
        with self.assertRaisesRegex(ValueError, "every non-table SourceIR node"):
            self._validate(missing)

        unresolved = _unowned_source_disposition(
            self.source.nodes[0],
            D2SourceEvidenceDispositionKind.UNRESOLVED,
        )
        partial = _proposal(
            source=self.source,
            kit=self.kit,
            d1=self.d1,
            table_map=self.table_map,
            logical_objects=(complete.logical_objects[1],),
            scopes=complete.logical_table_scopes,
            dispositions=complete.physical_table_dispositions,
            source_dispositions=(unresolved,),
            status=D2LogicalStructureStatus.PARTIAL,
        )
        receipt = self._validate(partial)
        self.assertEqual(
            receipt.unresolved_source_evidence_disposition_ids,
            (unresolved.disposition_id,),
        )

    def test_rejects_missing_table_and_duplicate_scope_membership(self) -> None:
        complete = self._complete_proposal()
        missing = _proposal(
            source=self.source,
            kit=self.kit,
            d1=self.d1,
            table_map=self.table_map,
            logical_objects=complete.logical_objects,
            scopes=complete.logical_table_scopes,
            dispositions=complete.physical_table_dispositions[:-1],
        )
        with self.assertRaisesRegex(ValueError, "every physical TableIR"):
            self._validate(missing)

        duplicate_scope = _scope(
            self.source,
            self.table_map,
            (self.table_ids[1],),
        )
        duplicate_object = _table_object(duplicate_scope, source_segments=())
        duplicate_membership = _proposal(
            source=self.source,
            kit=self.kit,
            d1=self.d1,
            table_map=self.table_map,
            logical_objects=tuple(
                sorted(
                    (*complete.logical_objects, duplicate_object),
                    key=lambda item: (item.source_order, item.logical_object_id),
                )
            ),
            scopes=(self.scope, duplicate_scope),
            dispositions=complete.physical_table_dispositions,
        )
        with self.assertRaisesRegex(ValueError, "multiple logical scopes"):
            self._validate(duplicate_membership)

    def test_rejects_nested_owner_missing_self_or_outside_validated_scope(self) -> None:
        complete = self._complete_proposal()
        missing_owner = _replace_disposition(
            complete,
            2,
            _disposition(
                self.table_ids[2],
                D2PhysicalTableDispositionKind.NESTED_VALUE_TABLE,
                owner="unknown-table",
            ),
        )
        with self.assertRaisesRegex(ValueError, "owner does not exist"):
            self._validate(missing_owner)

        with self.assertRaisesRegex(ValidationError, "cannot own itself"):
            _disposition(
                self.table_ids[2],
                D2PhysicalTableDispositionKind.NESTED_VALUE_TABLE,
                owner=self.table_ids[2],
            )

        false_owner = _replace_disposition(
            complete,
            2,
            _disposition(
                self.table_ids[2],
                D2PhysicalTableDispositionKind.NESTED_VALUE_TABLE,
                owner=self.table_ids[3],
            ),
        )
        with self.assertRaisesRegex(ValueError, "validated logical-scope part"):
            self._validate(false_owner)

    def test_rejects_arbitrary_disposition_evidence_and_invalid_reason_code(
        self,
    ) -> None:
        complete = self._complete_proposal()
        arbitrary = _replace_disposition(
            complete,
            3,
            _disposition(
                self.table_ids[3],
                D2PhysicalTableDispositionKind.NON_BUSINESS_FALSE_TABLE,
                evidence_refs=("page-render:5",),
            ),
        )
        with self.assertRaisesRegex(ValueError, "table-bound evidence"):
            self._validate(arbitrary)

        with self.assertRaisesRegex(ValidationError, "lowercase snake_case"):
            _disposition(
                self.table_ids[3],
                D2PhysicalTableDispositionKind.NON_BUSINESS_FALSE_TABLE,
                reason_codes=("False Table",),
            )

    def test_enforces_complete_partial_consistency(self) -> None:
        complete = self._complete_proposal()
        unresolved_disposition = _disposition(
            self.table_ids[3],
            D2PhysicalTableDispositionKind.UNRESOLVED,
        )
        inconsistent_complete = _replace_disposition(
            complete,
            3,
            unresolved_disposition,
        )
        with self.assertRaisesRegex(ValueError, "COMPLETE/PARTIAL"):
            self._validate(inconsistent_complete)

        partial = _proposal(
            source=self.source,
            kit=self.kit,
            d1=self.d1,
            table_map=self.table_map,
            logical_objects=complete.logical_objects,
            scopes=complete.logical_table_scopes,
            dispositions=(
                *complete.physical_table_dispositions[:3],
                unresolved_disposition,
            ),
            status=D2LogicalStructureStatus.PARTIAL,
        )
        receipt = self._validate(partial)
        self.assertEqual(receipt.status, D2LogicalStructureStatus.PARTIAL)
        self.assertEqual(receipt.unresolved_table_ids, (self.table_ids[3],))

        unsupported_partial = _proposal(
            source=self.source,
            kit=self.kit,
            d1=self.d1,
            table_map=self.table_map,
            logical_objects=complete.logical_objects,
            scopes=complete.logical_table_scopes,
            dispositions=complete.physical_table_dispositions,
            status=D2LogicalStructureStatus.PARTIAL,
        )
        with self.assertRaisesRegex(ValueError, "COMPLETE/PARTIAL"):
            self._validate(unsupported_partial)

    def test_rejects_stale_source_d1_kit_and_proposal_hashes(self) -> None:
        complete = self._complete_proposal()
        drifted_d1 = self.d1.model_copy(update={"artifact_sha256": "f" * 64})
        with self.assertRaisesRegex(ValueError, "D1 artifact identity drifted"):
            validate_d2_logical_structure_proposal(
                self.source,
                complete,
                expected_d1_artifact=drifted_d1,
                expected_d1_table_map_artifact=self.table_map,
                expected_kit_identity=self.kit,
            )

        drifted_kit = self.kit.model_copy(update={"bundle_sha256": "f" * 64})
        with self.assertRaisesRegex(ValueError, "Kit identity drifted"):
            validate_d2_logical_structure_proposal(
                self.source,
                complete,
                expected_d1_artifact=self.d1,
                expected_d1_table_map_artifact=self.table_map,
                expected_kit_identity=drifted_kit,
            )

        payload = complete.model_dump(mode="json")
        payload["unresolved_codes"] = ["changed_after_hash"]
        with self.assertRaisesRegex(ValidationError, "proposal SHA"):
            D2LogicalStructureProposalV1.model_validate(payload)

    def test_rejects_stale_segment_binding_and_cross_object_overlap(self) -> None:
        complete = self._complete_proposal()
        text_object = complete.logical_objects[0]
        original = text_object.source_segments[0]
        stale_segment = _segment(
            self.source.nodes[0],
            start=0,
            end=len(self.source.nodes[0].raw_text),
            source_content_sha256="f" * 64,
        )
        stale_text_object = _logical_object(
            source_sha256=SOURCE_SHA,
            object_kind=D2LogicalObjectKind.SECTION,
            source_segments=(stale_segment,),
        )
        stale = _proposal(
            source=self.source,
            kit=self.kit,
            d1=self.d1,
            table_map=self.table_map,
            logical_objects=(stale_text_object, complete.logical_objects[1]),
            scopes=complete.logical_table_scopes,
            dispositions=complete.physical_table_dispositions,
        )
        with self.assertRaisesRegex(ValueError, "stale or tampered"):
            self._validate(stale)

        overlap = _segment(self.source.nodes[0], start=1, end=original.end_offset)
        overlap_object = _logical_object(
            source_sha256=SOURCE_SHA,
            object_kind=D2LogicalObjectKind.CONTEXT,
            source_segments=(overlap,),
        )
        overlapping = _proposal(
            source=self.source,
            kit=self.kit,
            d1=self.d1,
            table_map=self.table_map,
            logical_objects=tuple(
                sorted(
                    (*complete.logical_objects, overlap_object),
                    key=lambda item: (item.source_order, item.logical_object_id),
                )
            ),
            scopes=complete.logical_table_scopes,
            dispositions=complete.physical_table_dispositions,
        )
        with self.assertRaisesRegex(ValueError, "cannot overlap"):
            self._validate(overlapping)

    def _complete_proposal(self) -> D2LogicalStructureProposalV1:
        text_node = self.source.nodes[0]
        text_segment = _segment(
            text_node,
            start=0,
            end=len(text_node.raw_text),
        )
        text_object = _logical_object(
            source_sha256=SOURCE_SHA,
            object_kind=D2LogicalObjectKind.SECTION,
            source_segments=(text_segment,),
        )
        table_segments = tuple(_segment(node) for node in self.table_nodes[:2])
        table_object = _table_object(self.scope, source_segments=table_segments)
        dispositions = (
            _disposition(
                self.table_ids[0],
                D2PhysicalTableDispositionKind.LOGICAL_SCOPE_PART,
                scope=self.scope.logical_object_id,
            ),
            _disposition(
                self.table_ids[1],
                D2PhysicalTableDispositionKind.LOGICAL_SCOPE_PART,
                scope=self.scope.logical_object_id,
            ),
            _disposition(
                self.table_ids[2],
                D2PhysicalTableDispositionKind.NESTED_VALUE_TABLE,
                owner=self.table_ids[0],
            ),
            _disposition(
                self.table_ids[3],
                D2PhysicalTableDispositionKind.NON_BUSINESS_FALSE_TABLE,
            ),
        )
        return _proposal(
            source=self.source,
            kit=self.kit,
            d1=self.d1,
            table_map=self.table_map,
            logical_objects=(text_object, table_object),
            scopes=(self.scope,),
            dispositions=dispositions,
        )

    def _validate(self, proposal: D2LogicalStructureProposalV1):
        return validate_d2_logical_structure_proposal(
            self.source,
            proposal,
            expected_d1_artifact=self.d1,
            expected_d1_table_map_artifact=self.table_map,
            expected_kit_identity=self.kit,
        )


def _kit_identity() -> AgentKitIdentityV1:
    return AgentKitIdentityV1(
        kit_id="reqpilotx-extraction",
        kit_version="0.4.0",
        manifest_sha256="2" * 64,
        bundle_sha256="3" * 64,
        policy_id="reqpilotx-extraction-policy",
        policy_version="1.0.0",
        tool_registry_id="reqpilotx-document-evidence-tools",
        tool_registry_version="1.0.0",
    )


def _d1_identity(kit: AgentKitIdentityV1) -> D2ValidatedD1ArtifactIdentityV1:
    return D2ValidatedD1ArtifactIdentityV1(
        artifact_id="agent-artifact-" + "6" * 32,
        artifact_sha256="7" * 64,
        run_id=RUN_ID,
        source_sha256=SOURCE_SHA,
        status=AgentStageArtifactStatus.VALIDATED,
        proposal_sha256="8" * 64,
        evidence_sha256="9" * 64,
        validator_receipt_sha256="b" * 64,
        page_count=5,
        kit_bundle_sha256=kit.bundle_sha256,
    )


def _source_fixture() -> SourceIRSnapshot:
    nodes: list[SourceNode] = []
    text_locator = NativeLocator(
        format=NativeDocumentFormat.PDF,
        page_number=1,
        block_index=0,
    )
    nodes.append(
        SourceNode(
            source_node_id=stable_node_id(
                source_sha256=SOURCE_SHA,
                adapter_id="d2-test",
                adapter_version="1.0.0",
                kind=SourceNodeKind.PARAGRAPH,
                native_locator=text_locator,
            ),
            kind=SourceNodeKind.PARAGRAPH,
            source_order=0,
            raw_text="A source-faithful provision.",
            native_locator=text_locator,
        )
    )
    for source_order, page_number in enumerate(range(2, 6), start=1):
        locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=page_number,
            table_index=0,
        )
        table_id = stable_table_id(
            source_sha256=SOURCE_SHA,
            adapter_id="d2-test",
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
                    adapter_id="d2-test",
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
            adapter_id="d2-test",
            adapter_version="1.0.0",
        ),
        document_id=document_id,
        source_sha256=SOURCE_SHA,
        adapter_id="d2-test",
        adapter_version="1.0.0",
        status=SourceIRStatus.COMPLETE,
        nodes=tuple(nodes),
        created_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
    )


def _scope(
    source: SourceIRSnapshot,
    table_map: AgentD1TableMapArtifactIdentityV1,
    table_ids: tuple[str, ...],
) -> AgentLogicalTableScopeV1:
    pages = tuple(
        node.native_locator.page_number
        for node in source.nodes
        if node.table_payload is not None
        and node.table_payload.table_id in table_ids
        and node.native_locator.page_number is not None
    )
    return build_agent_logical_table_scope(
        source,
        d1_table_map_artifact=table_map,
        physical_table_ids=table_ids,
        reason_codes=("same_logical_object",),
        supplemental_evidence=(
            AgentLogicalTableScopeEvidenceV1(
                evidence_ref="page-render:" + "-".join(map(str, pages)),
                evidence_sha256="c" * 64,
                page_numbers=pages,
            ),
        ),
    )


def _segment(
    node: SourceNode,
    *,
    start: int | None = None,
    end: int | None = None,
    source_content_sha256: str | None = None,
) -> D2SourceSegmentV1:
    page_number = node.native_locator.page_number
    assert page_number is not None
    if node.raw_text:
        content_sha = hashlib.sha256(node.raw_text.encode("utf-8")).hexdigest()
    else:
        assert node.table_payload is not None
        content_sha = canonical_model_sha256(node.table_payload)
    content_sha = source_content_sha256 or content_sha
    segment_id = stable_d2_source_segment_id(
        source_sha256=SOURCE_SHA,
        handle_kind=D2SourceHandleKind.SOURCE_NODE,
        source_handle=node.source_node_id,
        start_offset=start,
        end_offset=end,
    )
    return D2SourceSegmentV1(
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
        segment_sha256=stable_d2_source_segment_sha256(
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
        ),
    )


def _logical_object(
    *,
    source_sha256: str,
    object_kind: D2LogicalObjectKind,
    source_segments: tuple[D2SourceSegmentV1, ...],
) -> D2LogicalObjectV1:
    logical_object_id = stable_d2_logical_object_id(
        source_sha256=source_sha256,
        object_kind=object_kind,
        source_segment_ids=tuple(item.segment_id for item in source_segments),
    )
    pages = tuple(
        sorted({page for item in source_segments for page in item.page_numbers})
    )
    source_order = min(item.source_order for item in source_segments)
    values = dict(
        logical_object_id=logical_object_id,
        object_kind=object_kind,
        parent_logical_object_id=None,
        source_order=source_order,
        page_numbers=pages,
        source_segments=source_segments,
        logical_table_scope_ids=(),
        reason_codes=("source_boundary",),
    )
    return D2LogicalObjectV1(
        **values,
        object_sha256=stable_d2_logical_object_sha256(**values),
    )


def _table_object(
    scope: AgentLogicalTableScopeV1,
    *,
    source_segments: tuple[D2SourceSegmentV1, ...],
) -> D2LogicalObjectV1:
    pages = tuple(
        sorted({page for part in scope.physical_parts for page in part.page_numbers})
    )
    values = dict(
        logical_object_id=scope.logical_object_id,
        object_kind=D2LogicalObjectKind.TABLE,
        parent_logical_object_id=None,
        source_order=scope.physical_parts[0].source_order,
        page_numbers=pages,
        source_segments=source_segments,
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
    reason_codes: tuple[str, ...] = ("table_disposition",),
    evidence_refs: tuple[str, ...] | None = None,
) -> D2PhysicalTableDispositionV1:
    evidence_refs = evidence_refs or (f"document-table-map:{table_id}",)
    values = dict(
        table_id=table_id,
        disposition=kind,
        logical_table_scope_id=scope,
        nested_owner_table_id=owner,
        reason_codes=reason_codes,
        evidence_refs=evidence_refs,
    )
    return D2PhysicalTableDispositionV1(
        **values,
        disposition_sha256=stable_d2_table_disposition_sha256(**values),
    )


def _source_disposition(
    segment: D2SourceSegmentV1,
    logical_object_id: str,
    *,
    kind: D2SourceEvidenceDispositionKind = (
        D2SourceEvidenceDispositionKind.LOGICAL_OBJECT_PART
    ),
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
        disposition=kind,
        logical_object_id=logical_object_id,
        source_segment_id=segment.segment_id,
        reason_codes=("source_disposition",),
        evidence_refs=(segment.owner_source_node_id,),
    )
    return D2SourceEvidenceDispositionV1(
        **values,
        disposition_sha256=stable_d2_source_disposition_sha256(**values),
    )


def _unowned_source_disposition(
    node: SourceNode,
    kind: D2SourceEvidenceDispositionKind,
) -> D2SourceEvidenceDispositionV1:
    start = 0 if node.raw_text else None
    end = len(node.raw_text) if node.raw_text else None
    disposition_id = stable_d2_source_disposition_id(
        source_sha256=SOURCE_SHA,
        source_node_id=node.source_node_id,
        start_offset=start,
        end_offset=end,
    )
    values = dict(
        disposition_id=disposition_id,
        source_sha256=SOURCE_SHA,
        source_node_id=node.source_node_id,
        source_order=node.source_order,
        start_offset=start,
        end_offset=end,
        disposition=kind,
        logical_object_id=None,
        source_segment_id=None,
        reason_codes=("source_disposition",),
        evidence_refs=(node.source_node_id,),
    )
    return D2SourceEvidenceDispositionV1(
        **values,
        disposition_sha256=stable_d2_source_disposition_sha256(**values),
    )


def _proposal(
    *,
    source: SourceIRSnapshot,
    kit: AgentKitIdentityV1,
    d1: D2ValidatedD1ArtifactIdentityV1,
    table_map: AgentD1TableMapArtifactIdentityV1,
    logical_objects: tuple[D2LogicalObjectV1, ...],
    scopes: tuple[AgentLogicalTableScopeV1, ...],
    dispositions: tuple[D2PhysicalTableDispositionV1, ...],
    source_dispositions: tuple[D2SourceEvidenceDispositionV1, ...] | None = None,
    status: D2LogicalStructureStatus = D2LogicalStructureStatus.COMPLETE,
    unresolved_codes: tuple[str, ...] = (),
) -> D2LogicalStructureProposalV1:
    table_node_ids = {
        node.source_node_id
        for node in source.nodes
        if node.kind is SourceNodeKind.TABLE
    }
    if source_dispositions is None:
        source_dispositions = tuple(
            sorted(
                (
                    _source_disposition(segment, logical_object.logical_object_id)
                    for logical_object in logical_objects
                    for segment in logical_object.source_segments
                    if segment.owner_source_node_id not in table_node_ids
                    and segment.handle_kind is D2SourceHandleKind.SOURCE_NODE
                ),
                key=lambda item: (
                    item.source_order,
                    item.source_node_id,
                    -1 if item.start_offset is None else item.start_offset,
                    item.disposition_id,
                ),
            )
        )
    values = dict(
        run_id=RUN_ID,
        source_sha256=source.source_sha256,
        source_ir_id=source.source_ir_id,
        source_ir_canonical_sha256=source_ir_canonical_sha256(source),
        kit_identity=kit,
        predecessor_d1_artifact=d1,
        d1_table_map_artifact=table_map,
        status=status,
        logical_objects=logical_objects,
        logical_table_scopes=scopes,
        source_evidence_dispositions=source_dispositions,
        physical_table_dispositions=dispositions,
        unresolved_codes=unresolved_codes,
    )
    draft = D2LogicalStructureProposalV1.model_construct(
        **values,
        proposal_sha256="0" * 64,
    )
    return D2LogicalStructureProposalV1(
        **values,
        proposal_sha256=stable_d2_logical_structure_proposal_sha256(draft),
    )


def _replace_disposition(
    proposal: D2LogicalStructureProposalV1,
    index: int,
    replacement: D2PhysicalTableDispositionV1,
) -> D2LogicalStructureProposalV1:
    dispositions = list(proposal.physical_table_dispositions)
    dispositions[index] = replacement
    return _proposal(
        source=_source_fixture(),
        kit=proposal.kit_identity,
        d1=proposal.predecessor_d1_artifact,
        table_map=proposal.d1_table_map_artifact,
        logical_objects=proposal.logical_objects,
        scopes=proposal.logical_table_scopes,
        dispositions=tuple(dispositions),
        source_dispositions=proposal.source_evidence_dispositions,
        status=proposal.status,
        unresolved_codes=proposal.unresolved_codes,
    )


if __name__ == "__main__":
    unittest.main()

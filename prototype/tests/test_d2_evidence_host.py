from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import json
import unittest
from datetime import datetime, timezone

from pydantic import ValidationError

from app.adapters.agents.d2_evidence_host import (
    D2EvidenceToolError,
    D2EvidenceToolHost,
)
from app.application.agent_workflow.d2_evidence_tools import (
    D2LogicalTableCandidatesInputV1,
    D2LogicalTableEvidenceInputV1,
    D2ResolveSourceHandlesInputV1,
    D2ResolverSuggestionKind,
    D2ResolverSuggestionProviderStatus,
    D2ResolverSuggestionV1,
    D2ResolverSuggestionsInputV1,
    D2SourceHandleSelectionV1,
    canonical_d2_tool_sha256,
)
from app.application.agent_workflow.d2_logical_structure import (
    D2SourceHandleKind,
    D2ValidatedD1ArtifactIdentityV1,
)
from app.application.agent_workflow.extraction_runtime import (
    AgentKitIdentityV1,
    AgentStageArtifactStatus,
)
from app.application.extraction.agent_logical_table_scope import (
    AgentD1TableMapArtifactIdentityV1,
    build_agent_logical_table_scope,
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


SOURCE_SHA = "a" * 64
SOURCE_HANDLE = "source-handle-" + "b" * 32
RUN_ID = "agent-run-" + "c" * 32


class D2EvidenceToolHostTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = _source_fixture()
        self.kit = _kit_identity()
        self.d1 = _d1_identity(self.kit)
        self.table_map = AgentD1TableMapArtifactIdentityV1(
            source_sha256=SOURCE_SHA,
            artifact_id="d1-table-map-" + "d" * 32,
            artifact_sha256="e" * 64,
        )
        self.table_nodes = tuple(
            node for node in self.source.nodes if node.kind is SourceNodeKind.TABLE
        )
        self.table_ids = tuple(
            node.table_payload.table_id
            for node in self.table_nodes
            if node.table_payload is not None
        )
        self.host = self._host()

    def test_candidate_inventory_is_one_aggregate_source_bound_read(self) -> None:
        inventory = self.host.get_logical_table_candidates(
            D2LogicalTableCandidatesInputV1(
                context=self.host.expected_context(),
                context_nodes_per_side=2,
            )
        )

        self.assertEqual(
            tuple(item.physical_part.table_id for item in inventory.physical_tables),
            self.table_ids,
        )
        self.assertEqual(
            tuple(len(item.cells) for item in inventory.physical_tables), (4, 4, 4)
        )
        self.assertEqual(len(inventory.adjacency_candidates), 2)
        self.assertEqual(
            inventory.authority,
            "SOURCE_EVIDENCE_WITH_ADVISORY_ADJACENCY",
        )
        self.assertEqual(inventory.content_trust, "UNTRUSTED_SOURCE_DATA")
        self.assertTrue(inventory.adjacency_candidates[0].same_header_contract)
        self.assertFalse(inventory.adjacency_candidates[1].same_header_contract)
        self.assertTrue(inventory.physical_tables[0].leading_context)
        self.assertTrue(inventory.physical_tables[0].trailing_context)
        self.assertEqual(
            self.host.call_counts["document.get_logical_table_candidates"],
            1,
        )
        serialized = json.dumps(inventory.model_dump(mode="json"), sort_keys=True)
        self.assertNotIn("/Users/", serialized)
        self.assertNotIn(".pdf", serialized)

    def test_logical_table_read_requires_a_current_validated_agent_scope(self) -> None:
        scope = build_agent_logical_table_scope(
            self.source,
            d1_table_map_artifact=self.table_map,
            physical_table_ids=self.table_ids[:2],
            reason_codes=("same_logical_object",),
        )
        evidence = self.host.get_logical_table(
            D2LogicalTableEvidenceInputV1(
                context=self.host.expected_context(),
                logical_table_scope=scope,
            )
        )

        self.assertIs(evidence.logical_table_scope, scope)
        self.assertEqual(
            tuple(item.physical_part.table_id for item in evidence.physical_tables),
            self.table_ids[:2],
        )

        other_table_map = self.table_map.model_copy(
            update={"artifact_sha256": "f" * 64}
        )
        stale_scope = build_agent_logical_table_scope(
            self.source,
            d1_table_map_artifact=other_table_map,
            physical_table_ids=self.table_ids[:2],
            reason_codes=("same_logical_object",),
        )
        with self.assertRaisesRegex(ValueError, "artifact identity drifted"):
            self.host.get_logical_table(
                D2LogicalTableEvidenceInputV1(
                    context=self.host.expected_context(),
                    logical_table_scope=stale_scope,
                )
            )

    def test_source_handle_resolution_batches_nodes_cells_and_exact_ranges(
        self,
    ) -> None:
        paragraph = self.source.nodes[0]
        first_table = self.table_nodes[0].table_payload
        assert first_table is not None
        cell = first_table.cells[0]
        batch = self.host.resolve_source_handles(
            D2ResolveSourceHandlesInputV1(
                context=self.host.expected_context(),
                selections=(
                    D2SourceHandleSelectionV1(
                        handle_kind=D2SourceHandleKind.SOURCE_NODE,
                        source_handle=paragraph.source_node_id,
                        start_offset=0,
                        end_offset=7,
                    ),
                    D2SourceHandleSelectionV1(
                        handle_kind=D2SourceHandleKind.TABLE_CELL,
                        source_handle=cell.cell_id,
                    ),
                    D2SourceHandleSelectionV1(
                        handle_kind=D2SourceHandleKind.SOURCE_NODE,
                        source_handle=self.table_nodes[0].source_node_id,
                    ),
                ),
            )
        )

        self.assertEqual(
            tuple(item.exact_text for item in batch.segments[:2]), ("Section", "ID")
        )
        self.assertEqual(batch.segments[2].exact_text, "")
        self.assertIsNone(batch.segments[2].segment.start_offset)
        self.assertEqual(
            tuple(item.segment.owner_source_node_id for item in batch.segments),
            (
                paragraph.source_node_id,
                self.table_nodes[0].source_node_id,
                self.table_nodes[0].source_node_id,
            ),
        )

        with self.assertRaisesRegex(D2EvidenceToolError, "source_range_invalid"):
            self.host.resolve_source_handles(
                D2ResolveSourceHandlesInputV1(
                    context=self.host.expected_context(),
                    selections=(
                        D2SourceHandleSelectionV1(
                            handle_kind=D2SourceHandleKind.SOURCE_NODE,
                            source_handle=paragraph.source_node_id,
                            start_offset=0,
                            end_offset=999,
                        ),
                    ),
                )
            )

    def test_resolver_suggestions_are_optional_advisory_and_source_bound(self) -> None:
        absent = self.host.query_resolver_suggestions(
            D2ResolverSuggestionsInputV1(context=self.host.expected_context())
        )
        self.assertEqual(
            absent.provider_status,
            D2ResolverSuggestionProviderStatus.NOT_CONFIGURED,
        )
        self.assertEqual(absent.suggestions, ())

        suggestion = _suggestion(self.table_ids[0], self.table_ids[1])
        configured = self._host(resolver_suggestions=(suggestion,))
        result = configured.query_resolver_suggestions(
            D2ResolverSuggestionsInputV1(
                context=configured.expected_context(),
                physical_table_ids=self.table_ids[:2],
            )
        )
        self.assertEqual(
            result.provider_status,
            D2ResolverSuggestionProviderStatus.AVAILABLE,
        )
        self.assertEqual(result.authority, "ADVISORY_EVIDENCE")
        self.assertEqual(result.suggestions, (suggestion,))

        invalid = _suggestion(
            self.table_ids[0],
            self.table_ids[1],
            evidence_refs=("page-render:1", "page-render:2"),
        )
        with self.assertRaisesRegex(
            D2EvidenceToolError,
            "resolver_suggestion_evidence_invalid",
        ):
            self._host(resolver_suggestions=(invalid,))

    def test_context_identity_and_call_budgets_fail_closed(self) -> None:
        drifted_context = self.host.expected_context().model_copy(
            update={"expected_d1_artifact_sha256": "f" * 64}
        )
        with self.assertRaisesRegex(
            D2EvidenceToolError,
            "context_identity_mismatch",
        ):
            self.host.get_logical_table_candidates(
                D2LogicalTableCandidatesInputV1(context=drifted_context)
            )

        request = D2LogicalTableCandidatesInputV1(context=self.host.expected_context())
        self.host.get_logical_table_candidates(request)
        with self.assertRaisesRegex(D2EvidenceToolError, "tool_budget_exceeded"):
            self.host.get_logical_table_candidates(request)

    def test_constructor_rejects_cross_source_or_kit_lineage(self) -> None:
        drifted_d1 = self.d1.model_copy(update={"source_sha256": "f" * 64})
        with self.assertRaisesRegex(D2EvidenceToolError, "source_lineage_mismatch"):
            D2EvidenceToolHost(
                source_ir=self.source,
                source_handle=SOURCE_HANDLE,
                kit_identity=self.kit,
                validated_d1_artifact=drifted_d1,
                d1_table_map_artifact=self.table_map,
            )

        drifted_kit = self.kit.model_copy(update={"bundle_sha256": "f" * 64})
        with self.assertRaisesRegex(D2EvidenceToolError, "kit_lineage_mismatch"):
            D2EvidenceToolHost(
                source_ir=self.source,
                source_handle=SOURCE_HANDLE,
                kit_identity=drifted_kit,
                validated_d1_artifact=self.d1,
                d1_table_map_artifact=self.table_map,
            )

    def test_input_and_output_schemas_reject_duplicates_and_hash_tampering(
        self,
    ) -> None:
        selection = D2SourceHandleSelectionV1(
            handle_kind=D2SourceHandleKind.SOURCE_NODE,
            source_handle=self.source.nodes[0].source_node_id,
        )
        with self.assertRaisesRegex(ValidationError, "must be unique"):
            D2ResolveSourceHandlesInputV1(
                context=self.host.expected_context(),
                selections=(selection, selection),
            )

        inventory = self.host.get_logical_table_candidates(
            D2LogicalTableCandidatesInputV1(context=self.host.expected_context())
        )
        payload = inventory.model_dump(mode="json")
        payload["inventory_sha256"] = "f" * 64
        with self.assertRaisesRegex(ValidationError, "inventory SHA"):
            type(inventory).model_validate(payload)

    def _host(
        self,
        *,
        resolver_suggestions: tuple[D2ResolverSuggestionV1, ...] | None = None,
    ) -> D2EvidenceToolHost:
        return D2EvidenceToolHost(
            source_ir=self.source,
            source_handle=SOURCE_HANDLE,
            kit_identity=self.kit,
            validated_d1_artifact=self.d1,
            d1_table_map_artifact=self.table_map,
            resolver_suggestions=resolver_suggestions,
        )


def _kit_identity() -> AgentKitIdentityV1:
    return AgentKitIdentityV1(
        kit_id="reqpilotx-extraction",
        kit_version="0.4.0",
        manifest_sha256="1" * 64,
        bundle_sha256="2" * 64,
        policy_id="reqpilotx-extraction-policy",
        policy_version="1.0.0",
        tool_registry_id="reqpilotx-document-evidence-tools",
        tool_registry_version="1.2.0",
    )


def _d1_identity(kit: AgentKitIdentityV1) -> D2ValidatedD1ArtifactIdentityV1:
    return D2ValidatedD1ArtifactIdentityV1(
        artifact_id="agent-artifact-" + "3" * 32,
        artifact_sha256="4" * 64,
        run_id=RUN_ID,
        source_sha256=SOURCE_SHA,
        status=AgentStageArtifactStatus.VALIDATED,
        proposal_sha256="5" * 64,
        evidence_sha256="6" * 64,
        validator_receipt_sha256="7" * 64,
        page_count=3,
        kit_bundle_sha256=kit.bundle_sha256,
    )


def _source_fixture() -> SourceIRSnapshot:
    nodes: list[SourceNode] = []
    text_specs = (
        (1, "Section Alpha"),
        (2, "Continuation context"),
        (3, "Independent object"),
    )
    source_order = 0
    for table_index, (page_number, text) in enumerate(text_specs):
        text_locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=page_number,
            block_index=0,
        )
        nodes.append(
            SourceNode(
                source_node_id=stable_node_id(
                    source_sha256=SOURCE_SHA,
                    adapter_id="d2-tool-test",
                    adapter_version="1.0.0",
                    kind=SourceNodeKind.PARAGRAPH,
                    native_locator=text_locator,
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=source_order,
                raw_text=text,
                native_locator=text_locator,
            )
        )
        source_order += 1
        table_locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=page_number,
            table_index=table_index,
        )
        table_id = stable_table_id(
            source_sha256=SOURCE_SHA,
            adapter_id="d2-tool-test",
            adapter_version="1.0.0",
            native_locator=table_locator,
        )
        header = "ID" if table_index < 2 else "Parameter"
        values = ((header, "Value"), (str(table_index + 1), "Synthetic fact"))
        cells = tuple(
            TableCellIR(
                cell_id=stable_cell_id(table_id, row, column),
                row=row,
                column=column,
                raw_text=value,
                native_locator=NativeLocator(
                    format=NativeDocumentFormat.PDF,
                    page_number=page_number,
                    table_index=table_index,
                    row_index=row,
                    column_index=column,
                ),
            )
            for row, row_values in enumerate(values)
            for column, value in enumerate(row_values)
        )
        table = TableIR(
            table_id=table_id,
            row_count=2,
            column_count=2,
            cells=cells,
            notes=("Synthetic note",),
        )
        nodes.append(
            SourceNode(
                source_node_id=stable_node_id(
                    source_sha256=SOURCE_SHA,
                    adapter_id="d2-tool-test",
                    adapter_version="1.0.0",
                    kind=SourceNodeKind.TABLE,
                    native_locator=table_locator,
                ),
                kind=SourceNodeKind.TABLE,
                source_order=source_order,
                native_locator=table_locator,
                table_payload=table,
            )
        )
        source_order += 1
    document_id = stable_document_id(SOURCE_SHA)
    return SourceIRSnapshot(
        source_ir_id=stable_source_ir_id(
            document_id=document_id,
            source_sha256=SOURCE_SHA,
            adapter_id="d2-tool-test",
            adapter_version="1.0.0",
        ),
        document_id=document_id,
        source_sha256=SOURCE_SHA,
        adapter_id="d2-tool-test",
        adapter_version="1.0.0",
        status=SourceIRStatus.COMPLETE,
        nodes=tuple(nodes),
        created_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
    )


def _suggestion(
    left_table_id: str,
    right_table_id: str,
    *,
    evidence_refs: tuple[str, ...] | None = None,
) -> D2ResolverSuggestionV1:
    evidence_refs = evidence_refs or tuple(
        sorted(
            (
                f"document-table-map:{left_table_id}",
                f"document-table-map:{right_table_id}",
            )
        )
    )
    identity_values = {
        "kind": D2ResolverSuggestionKind.CONTINUES.value,
        "left_table_id": left_table_id,
        "right_table_id": right_table_id,
        "reason_codes": ["resolver_continuation_candidate"],
        "evidence_refs": list(evidence_refs),
    }
    suggestion_id = (
        "d2-resolver-suggestion-" + canonical_d2_tool_sha256(identity_values)[:32]
    )
    values = {
        "suggestion_id": suggestion_id,
        "kind": D2ResolverSuggestionKind.CONTINUES,
        "left_table_id": left_table_id,
        "right_table_id": right_table_id,
        "reason_codes": ("resolver_continuation_candidate",),
        "evidence_refs": evidence_refs,
    }
    draft = D2ResolverSuggestionV1.model_construct(
        **values,
        suggestion_sha256="0" * 64,
    )
    return D2ResolverSuggestionV1(
        **values,
        suggestion_sha256=canonical_d2_tool_sha256(
            draft.model_dump(mode="json", exclude={"suggestion_sha256"})
        ),
    )


if __name__ == "__main__":
    unittest.main()

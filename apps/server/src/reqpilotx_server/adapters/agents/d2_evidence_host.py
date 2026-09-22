from __future__ import annotations

from collections.abc import Iterable
import hashlib

from reqpilotx_server.application.agent_workflow.d2_evidence_tools import (
    D2EvidenceToolContextV1,
    D2LogicalTableCandidateInventoryV1,
    D2LogicalTableCandidatesInputV1,
    D2LogicalTableEvidenceInputV1,
    D2LogicalTableEvidenceV1,
    D2PhysicalTableEvidenceV1,
    D2ResolveSourceHandlesInputV1,
    D2ResolvedSourceSegmentBatchV1,
    D2ResolvedSourceSegmentV1,
    D2ResolverSuggestionBatchV1,
    D2ResolverSuggestionProviderStatus,
    D2ResolverSuggestionV1,
    D2ResolverSuggestionsInputV1,
    D2SourceContextEvidenceV1,
    D2TableAdjacencyCandidateV1,
    canonical_d2_tool_sha256,
)
from reqpilotx_server.application.agent_workflow.d2_logical_structure import (
    D2SourceHandleKind,
    D2SourceSegmentV1,
    D2ValidatedD1ArtifactIdentityV1,
    stable_d2_source_segment_id,
    stable_d2_source_segment_sha256,
)
from reqpilotx_server.application.agent_workflow.document_understanding import (
    DocumentTableCellFactV1,
)
from reqpilotx_server.application.agent_workflow.extraction_runtime import AgentKitIdentityV1
from reqpilotx_server.application.extraction.agent_logical_table_scope import (
    AgentD1TableMapArtifactIdentityV1,
    build_agent_logical_table_scope,
    source_ir_canonical_sha256,
    validate_agent_logical_table_scope,
)
from reqpilotx_server.domain.extraction.source_ir import (
    SourceIRSnapshot,
    SourceIRStatus,
    SourceNode,
    SourceNodeKind,
    TableCellIR,
)
from reqpilotx_server.domain.extraction.structure_revision import canonical_model_sha256


TOOL_GET_LOGICAL_TABLE_CANDIDATES = "document.get_logical_table_candidates"
TOOL_GET_LOGICAL_TABLE = "document.get_logical_table"
TOOL_RESOLVE_SOURCE_HANDLES = "source.resolve_handles"
TOOL_QUERY_RESOLVER_SUGGESTIONS = "resolver.query_suggestions"

D2_HIGH_LEVEL_TOOL_NAMES = (
    TOOL_GET_LOGICAL_TABLE,
    TOOL_GET_LOGICAL_TABLE_CANDIDATES,
    TOOL_QUERY_RESOLVER_SUGGESTIONS,
    TOOL_RESOLVE_SOURCE_HANDLES,
)

_MAX_PHYSICAL_TABLES = 512
_MAX_TABLE_CELLS = 100_000
_MAX_AGGREGATED_TEXT_CHARACTERS = 4_000_000
_MAX_RESOLVED_TEXT_CHARACTERS = 2_000_000


class D2EvidenceToolError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class D2EvidenceToolHost:
    """Read-only, aggregate D2 evidence tools over one Host-pinned SourceIR."""

    def __init__(
        self,
        *,
        source_ir: SourceIRSnapshot,
        source_handle: str,
        kit_identity: AgentKitIdentityV1,
        validated_d1_artifact: D2ValidatedD1ArtifactIdentityV1,
        d1_table_map_artifact: AgentD1TableMapArtifactIdentityV1,
        resolver_suggestions: tuple[D2ResolverSuggestionV1, ...] | None = None,
    ) -> None:
        if source_ir.status is SourceIRStatus.FAILED:
            raise D2EvidenceToolError("d2_source_ir_failed")
        if (
            validated_d1_artifact.source_sha256 != source_ir.source_sha256
            or d1_table_map_artifact.source_sha256 != source_ir.source_sha256
        ):
            raise D2EvidenceToolError("d2_source_lineage_mismatch")
        if validated_d1_artifact.kit_bundle_sha256 != kit_identity.bundle_sha256:
            raise D2EvidenceToolError("d2_kit_lineage_mismatch")
        if not (
            source_handle.startswith("source-handle-")
            and len(source_handle) == len("source-handle-") + 32
            and all(character in "0123456789abcdef" for character in source_handle[14:])
        ):
            raise D2EvidenceToolError("d2_source_handle_invalid")

        self.source_ir = source_ir
        self.source_handle = source_handle
        self.kit_identity = kit_identity
        self.validated_d1_artifact = validated_d1_artifact
        self.d1_table_map_artifact = d1_table_map_artifact
        self.source_ir_sha256 = source_ir_canonical_sha256(source_ir)
        self.nodes_by_id = {node.source_node_id: node for node in source_ir.nodes}
        self.tables_by_id = _physical_table_catalog(source_ir)
        self.cells_by_id = _table_cell_catalog(self.tables_by_id.values())
        self.call_counts: dict[str, int] = {}

        native_pages = tuple(
            page
            for node in source_ir.nodes
            if (page := node.native_locator.page_number) is not None
        )
        if native_pages and max(native_pages) > validated_d1_artifact.page_count:
            raise D2EvidenceToolError("d2_d1_page_scope_mismatch")
        _require_table_budget(self.tables_by_id.values())

        self.resolver_configured = resolver_suggestions is not None
        self.resolver_suggestions = resolver_suggestions or ()
        self._validate_resolver_suggestions()

    def expected_context(self) -> D2EvidenceToolContextV1:
        return D2EvidenceToolContextV1(
            source_handle=self.source_handle,
            expected_source_sha256=self.source_ir.source_sha256,
            expected_source_ir_canonical_sha256=self.source_ir_sha256,
            expected_d1_artifact_id=self.validated_d1_artifact.artifact_id,
            expected_d1_artifact_sha256=self.validated_d1_artifact.artifact_sha256,
            expected_d1_table_map_artifact_id=self.d1_table_map_artifact.artifact_id,
            expected_d1_table_map_artifact_sha256=(
                self.d1_table_map_artifact.artifact_sha256
            ),
            expected_kit_bundle_sha256=self.kit_identity.bundle_sha256,
        )

    def get_logical_table_candidates(
        self,
        request: D2LogicalTableCandidatesInputV1,
    ) -> D2LogicalTableCandidateInventoryV1:
        self._claim(TOOL_GET_LOGICAL_TABLE_CANDIDATES, ceiling=2)
        self._require_context(request.context)
        physical_tables = tuple(
            self._physical_table_evidence(
                node,
                context_nodes_per_side=request.context_nodes_per_side,
            )
            for node in self.tables_by_id.values()
        )
        candidates = tuple(
            self._adjacency_candidate(left, right)
            for left, right in zip(physical_tables, physical_tables[1:], strict=False)
        )
        values = {
            "source_sha256": self.source_ir.source_sha256,
            "source_ir_id": self.source_ir.source_ir_id,
            "source_ir_canonical_sha256": self.source_ir_sha256,
            "d1_artifact_id": self.validated_d1_artifact.artifact_id,
            "d1_artifact_sha256": self.validated_d1_artifact.artifact_sha256,
            "d1_table_map_artifact_id": self.d1_table_map_artifact.artifact_id,
            "d1_table_map_artifact_sha256": (
                self.d1_table_map_artifact.artifact_sha256
            ),
            "tool_context_sha256": _tool_context_sha256(request.context),
            "physical_tables": physical_tables,
            "adjacency_candidates": candidates,
        }
        draft = D2LogicalTableCandidateInventoryV1.model_construct(
            **values,
            inventory_sha256="0" * 64,
        )
        result = D2LogicalTableCandidateInventoryV1(
            **values,
            inventory_sha256=canonical_d2_tool_sha256(
                draft.model_dump(mode="json", exclude={"inventory_sha256"})
            ),
        )
        _require_aggregated_text_budget(result.physical_tables)
        return result

    def get_logical_table(
        self,
        request: D2LogicalTableEvidenceInputV1,
    ) -> D2LogicalTableEvidenceV1:
        self._claim(TOOL_GET_LOGICAL_TABLE, ceiling=_MAX_PHYSICAL_TABLES)
        self._require_context(request.context)
        scope = validate_agent_logical_table_scope(
            self.source_ir,
            request.logical_table_scope,
            expected_d1_table_map_artifact=self.d1_table_map_artifact,
        )
        physical_tables = tuple(
            self._physical_table_evidence(
                self.tables_by_id[table_id],
                context_nodes_per_side=request.context_nodes_per_side,
            )
            for table_id in scope.physical_table_ids
        )
        values = {
            "source_sha256": self.source_ir.source_sha256,
            "tool_context_sha256": _tool_context_sha256(request.context),
            "logical_table_scope": scope,
            "physical_tables": physical_tables,
        }
        draft = D2LogicalTableEvidenceV1.model_construct(
            **values,
            evidence_sha256="0" * 64,
        )
        result = D2LogicalTableEvidenceV1(
            **values,
            evidence_sha256=canonical_d2_tool_sha256(
                draft.model_dump(mode="json", exclude={"evidence_sha256"})
            ),
        )
        _require_aggregated_text_budget(result.physical_tables)
        return result

    def resolve_source_handles(
        self,
        request: D2ResolveSourceHandlesInputV1,
    ) -> D2ResolvedSourceSegmentBatchV1:
        self._claim(TOOL_RESOLVE_SOURCE_HANDLES, ceiling=16)
        self._require_context(request.context)
        resolved = tuple(self._resolve_selection(item) for item in request.selections)
        if (
            sum(len(item.exact_text) for item in resolved)
            > _MAX_RESOLVED_TEXT_CHARACTERS
        ):
            raise D2EvidenceToolError("d2_resolved_text_budget_exceeded")
        values = {
            "source_sha256": self.source_ir.source_sha256,
            "source_ir_canonical_sha256": self.source_ir_sha256,
            "tool_context_sha256": _tool_context_sha256(request.context),
            "segments": resolved,
        }
        draft = D2ResolvedSourceSegmentBatchV1.model_construct(
            **values,
            batch_sha256="0" * 64,
        )
        return D2ResolvedSourceSegmentBatchV1(
            **values,
            batch_sha256=canonical_d2_tool_sha256(
                draft.model_dump(mode="json", exclude={"batch_sha256"})
            ),
        )

    def query_resolver_suggestions(
        self,
        request: D2ResolverSuggestionsInputV1,
    ) -> D2ResolverSuggestionBatchV1:
        self._claim(TOOL_QUERY_RESOLVER_SUGGESTIONS, ceiling=2)
        self._require_context(request.context)
        requested = set(request.physical_table_ids)
        unknown = requested.difference(self.tables_by_id)
        if unknown:
            raise D2EvidenceToolError("d2_resolver_table_unknown")
        suggestions = tuple(
            item
            for item in self.resolver_suggestions
            if not requested
            or {item.left_table_id, item.right_table_id}.issubset(requested)
        )
        values = {
            "source_sha256": self.source_ir.source_sha256,
            "tool_context_sha256": _tool_context_sha256(request.context),
            "provider_status": (
                D2ResolverSuggestionProviderStatus.AVAILABLE
                if self.resolver_configured
                else D2ResolverSuggestionProviderStatus.NOT_CONFIGURED
            ),
            "suggestions": suggestions,
        }
        draft = D2ResolverSuggestionBatchV1.model_construct(
            **values,
            batch_sha256="0" * 64,
        )
        return D2ResolverSuggestionBatchV1(
            **values,
            batch_sha256=canonical_d2_tool_sha256(
                draft.model_dump(mode="json", exclude={"batch_sha256"})
            ),
        )

    def _require_context(self, context: D2EvidenceToolContextV1) -> None:
        if context != self.expected_context():
            raise D2EvidenceToolError("d2_evidence_context_identity_mismatch")
        if source_ir_canonical_sha256(self.source_ir) != self.source_ir_sha256:
            raise D2EvidenceToolError("d2_source_ir_identity_drift")

    def _physical_table_evidence(
        self,
        node: SourceNode,
        *,
        context_nodes_per_side: int,
    ) -> D2PhysicalTableEvidenceV1:
        table = node.table_payload
        if table is None:
            raise D2EvidenceToolError("d2_physical_table_payload_missing")
        scope = build_agent_logical_table_scope(
            self.source_ir,
            d1_table_map_artifact=self.d1_table_map_artifact,
            physical_table_ids=(table.table_id,),
            reason_codes=("physical_table_inventory",),
        )
        if not scope.physical_parts[0].page_numbers:
            raise D2EvidenceToolError("d2_physical_table_page_unavailable")
        cells = tuple(
            DocumentTableCellFactV1(
                cell_id=cell.cell_id,
                row=cell.row,
                column=cell.column,
                row_span=cell.row_span,
                column_span=cell.column_span,
                text=cell.raw_text,
                text_sha256=hashlib.sha256(cell.raw_text.encode("utf-8")).hexdigest(),
                bbox=cell.native_locator.bbox,
            )
            for cell in sorted(
                table.cells,
                key=lambda value: (value.row, value.column, value.cell_id),
            )
        )
        leading, trailing = self._context_for_node(
            node,
            nodes_per_side=context_nodes_per_side,
        )
        values = {
            "physical_part": scope.physical_parts[0],
            "parent_source_node_id": node.parent_source_node_id,
            "bbox": node.native_locator.bbox,
            "header_regions": tuple(
                (item.row, item.column, item.row_span, item.column_span)
                for item in table.header_regions
            ),
            "merged_regions": tuple(
                (item.row, item.column, item.row_span, item.column_span)
                for item in table.merged_regions
            ),
            "notes": table.notes,
            "cells": cells,
            "leading_context": leading,
            "trailing_context": trailing,
        }
        draft = D2PhysicalTableEvidenceV1.model_construct(
            **values,
            evidence_sha256="0" * 64,
        )
        return D2PhysicalTableEvidenceV1(
            **values,
            evidence_sha256=canonical_d2_tool_sha256(
                draft.model_dump(mode="json", exclude={"evidence_sha256"})
            ),
        )

    def _context_for_node(
        self,
        node: SourceNode,
        *,
        nodes_per_side: int,
    ) -> tuple[
        tuple[D2SourceContextEvidenceV1, ...],
        tuple[D2SourceContextEvidenceV1, ...],
    ]:
        if nodes_per_side == 0:
            return (), ()
        index = self.source_ir.nodes.index(node)
        leading_nodes = _nearest_text_nodes(
            reversed(self.source_ir.nodes[:index]),
            limit=nodes_per_side,
        )
        leading_nodes = tuple(reversed(leading_nodes))
        trailing_nodes = _nearest_text_nodes(
            self.source_ir.nodes[index + 1 :],
            limit=nodes_per_side,
        )
        return (
            tuple(_source_context(item) for item in leading_nodes),
            tuple(_source_context(item) for item in trailing_nodes),
        )

    def _adjacency_candidate(
        self,
        left: D2PhysicalTableEvidenceV1,
        right: D2PhysicalTableEvidenceV1,
    ) -> D2TableAdjacencyCandidateV1:
        left_part = left.physical_part
        right_part = right.physical_part
        if not left_part.page_numbers or not right_part.page_numbers:
            raise D2EvidenceToolError("d2_physical_table_page_unavailable")
        left_last_page = max(left_part.page_numbers)
        right_first_page = min(right_part.page_numbers)
        values = {
            "left_table_id": left_part.table_id,
            "right_table_id": right_part.table_id,
            "left_page_numbers": left_part.page_numbers,
            "right_page_numbers": right_part.page_numbers,
            "source_order_gap": right_part.source_order - left_part.source_order,
            "page_gap": max(0, right_first_page - left_last_page),
            "same_column_count": (left_part.column_count == right_part.column_count),
            "same_header_contract": (
                _header_contract_sha256(left) == _header_contract_sha256(right)
            ),
            "evidence_refs": tuple(
                sorted(
                    (
                        f"document-table-map:{left_part.table_id}",
                        f"document-table-map:{right_part.table_id}",
                    )
                )
            ),
        }
        draft = D2TableAdjacencyCandidateV1.model_construct(
            **values,
            candidate_sha256="0" * 64,
        )
        return D2TableAdjacencyCandidateV1(
            **values,
            candidate_sha256=canonical_d2_tool_sha256(
                draft.model_dump(mode="json", exclude={"candidate_sha256"})
            ),
        )

    def _resolve_selection(self, selection) -> D2ResolvedSourceSegmentV1:  # type: ignore[no-untyped-def]
        if selection.handle_kind is D2SourceHandleKind.SOURCE_NODE:
            owner = self.nodes_by_id.get(selection.source_handle)
            if owner is None:
                raise D2EvidenceToolError("d2_source_node_handle_unknown")
            source_text = owner.raw_text
            content_sha256 = _source_node_content_sha256(owner)
            page_number = owner.native_locator.page_number
        else:
            cell_binding = self.cells_by_id.get(selection.source_handle)
            if cell_binding is None:
                raise D2EvidenceToolError("d2_table_cell_handle_unknown")
            owner, cell = cell_binding
            source_text = cell.raw_text
            content_sha256 = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
            page_number = cell.native_locator.page_number
        if page_number is None:
            raise D2EvidenceToolError("d2_source_page_unavailable")

        start = selection.start_offset
        end = selection.end_offset
        if source_text:
            if start is None:
                start, end = 0, len(source_text)
            assert end is not None
            if end > len(source_text):
                raise D2EvidenceToolError("d2_source_range_invalid")
            exact_text = source_text[start:end]
        else:
            if start is not None or end is not None:
                raise D2EvidenceToolError("d2_textless_source_range_invalid")
            exact_text = ""
        if len(exact_text) > 1_000_000:
            raise D2EvidenceToolError("d2_resolved_item_text_budget_exceeded")

        segment_id = stable_d2_source_segment_id(
            source_sha256=self.source_ir.source_sha256,
            handle_kind=selection.handle_kind,
            source_handle=selection.source_handle,
            start_offset=start,
            end_offset=end,
        )
        segment_values = {
            "segment_id": segment_id,
            "source_sha256": self.source_ir.source_sha256,
            "handle_kind": selection.handle_kind,
            "source_handle": selection.source_handle,
            "owner_source_node_id": owner.source_node_id,
            "source_order": owner.source_order,
            "page_numbers": (page_number,),
            "start_offset": start,
            "end_offset": end,
            "source_content_sha256": content_sha256,
        }
        segment = D2SourceSegmentV1(
            **segment_values,
            segment_sha256=stable_d2_source_segment_sha256(**segment_values),
        )
        return D2ResolvedSourceSegmentV1(
            segment=segment,
            exact_text=exact_text,
            exact_text_sha256=hashlib.sha256(exact_text.encode("utf-8")).hexdigest(),
        )

    def _validate_resolver_suggestions(self) -> None:
        identifiers: set[str] = set()
        for suggestion in self.resolver_suggestions:
            if suggestion.suggestion_id in identifiers:
                raise D2EvidenceToolError("d2_resolver_suggestion_duplicate")
            identifiers.add(suggestion.suggestion_id)
            if (
                suggestion.left_table_id not in self.tables_by_id
                or suggestion.right_table_id not in self.tables_by_id
            ):
                raise D2EvidenceToolError("d2_resolver_suggestion_table_unknown")
            if not _refs_bind_table(
                suggestion.evidence_refs,
                self.tables_by_id[suggestion.left_table_id],
            ) or not _refs_bind_table(
                suggestion.evidence_refs,
                self.tables_by_id[suggestion.right_table_id],
            ):
                raise D2EvidenceToolError("d2_resolver_suggestion_evidence_invalid")

    def _claim(self, tool_name: str, *, ceiling: int) -> None:
        count = self.call_counts.get(tool_name, 0) + 1
        self.call_counts[tool_name] = count
        if count > ceiling:
            raise D2EvidenceToolError("d2_tool_budget_exceeded")


def _physical_table_catalog(source_ir: SourceIRSnapshot) -> dict[str, SourceNode]:
    catalog: dict[str, SourceNode] = {}
    for node in source_ir.nodes:
        if node.kind is not SourceNodeKind.TABLE or node.table_payload is None:
            continue
        if node.table_payload.table_id in catalog:
            raise D2EvidenceToolError("d2_physical_table_id_ambiguous")
        catalog[node.table_payload.table_id] = node
    return catalog


def _table_cell_catalog(
    table_nodes: Iterable[SourceNode],
) -> dict[str, tuple[SourceNode, TableCellIR]]:
    catalog: dict[str, tuple[SourceNode, TableCellIR]] = {}
    for node in table_nodes:
        table = node.table_payload
        if table is None:
            continue
        for cell in table.cells:
            if cell.cell_id in catalog:
                raise D2EvidenceToolError("d2_table_cell_id_ambiguous")
            catalog[cell.cell_id] = (node, cell)
    return catalog


def _nearest_text_nodes(
    nodes: Iterable[SourceNode],
    *,
    limit: int,
) -> tuple[SourceNode, ...]:
    selected: list[SourceNode] = []
    for node in nodes:
        if node.raw_text:
            selected.append(node)
            if len(selected) == limit:
                break
    return tuple(selected)


def _source_context(node: SourceNode) -> D2SourceContextEvidenceV1:
    if len(node.raw_text) > 100_000:
        raise D2EvidenceToolError("d2_context_text_budget_exceeded")
    page_number = node.native_locator.page_number
    return D2SourceContextEvidenceV1(
        source_handle=node.source_node_id,
        source_order=node.source_order,
        page_numbers=() if page_number is None else (page_number,),
        kind=node.kind.value,
        text=node.raw_text,
        text_sha256=hashlib.sha256(node.raw_text.encode("utf-8")).hexdigest(),
        bbox=node.native_locator.bbox,
    )


def _source_node_content_sha256(node: SourceNode) -> str:
    if node.raw_text:
        return hashlib.sha256(node.raw_text.encode("utf-8")).hexdigest()
    if node.table_payload is not None:
        return canonical_model_sha256(node.table_payload)
    if node.asset_ref is not None:
        return canonical_model_sha256(node)
    raise D2EvidenceToolError("d2_source_node_content_unavailable")


def _header_contract_sha256(table: D2PhysicalTableEvidenceV1) -> str:
    header_positions = {
        (row, column)
        for row, column, row_span, column_span in table.header_regions
        for row in range(row, row + row_span)
        for column in range(column, column + column_span)
    }
    if not header_positions:
        header_positions = {
            (item.row, item.column) for item in table.cells if item.row == 0
        }
    return canonical_d2_tool_sha256(
        {
            "column_count": table.physical_part.column_count,
            "header_regions": [list(item) for item in table.header_regions],
            "header_cells": [
                {
                    "row": item.row,
                    "column": item.column,
                    "row_span": item.row_span,
                    "column_span": item.column_span,
                    "text_sha256": item.text_sha256,
                }
                for item in table.cells
                if (item.row, item.column) in header_positions
            ],
        }
    )


def _refs_bind_table(evidence_refs: tuple[str, ...], node: SourceNode) -> bool:
    table = node.table_payload
    if table is None:
        return False
    valid = {
        f"document-table-map:{table.table_id}",
        node.source_node_id,
        *(cell.cell_id for cell in table.cells),
    }
    return bool(valid.intersection(evidence_refs))


def _require_table_budget(table_nodes: Iterable[SourceNode]) -> None:
    nodes = tuple(table_nodes)
    if len(nodes) > _MAX_PHYSICAL_TABLES:
        raise D2EvidenceToolError("d2_physical_table_budget_exceeded")
    cell_count = sum(
        len(node.table_payload.cells)
        for node in nodes
        if node.table_payload is not None
    )
    if cell_count > _MAX_TABLE_CELLS:
        raise D2EvidenceToolError("d2_table_cell_budget_exceeded")


def _require_aggregated_text_budget(
    tables: tuple[D2PhysicalTableEvidenceV1, ...],
) -> None:
    character_count = sum(
        sum(len(cell.text) for cell in table.cells)
        + sum(len(note) for note in table.notes)
        + sum(
            len(item.text) for item in (*table.leading_context, *table.trailing_context)
        )
        for table in tables
    )
    if character_count > _MAX_AGGREGATED_TEXT_CHARACTERS:
        raise D2EvidenceToolError("d2_aggregated_text_budget_exceeded")


def _tool_context_sha256(context: D2EvidenceToolContextV1) -> str:
    return canonical_d2_tool_sha256(context.model_dump(mode="json"))


__all__ = [
    "D2EvidenceToolError",
    "D2EvidenceToolHost",
    "D2_HIGH_LEVEL_TOOL_NAMES",
    "TOOL_GET_LOGICAL_TABLE",
    "TOOL_GET_LOGICAL_TABLE_CANDIDATES",
    "TOOL_QUERY_RESOLVER_SUGGESTIONS",
    "TOOL_RESOLVE_SOURCE_HANDLES",
]

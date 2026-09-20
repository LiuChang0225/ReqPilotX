from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import unittest
from datetime import datetime, timezone

from pydantic import ValidationError

from app.application.extraction.agent_logical_table_scope import (
    AgentD1TableMapArtifactIdentityV1,
    AgentLogicalTablePhysicalPartV1,
    AgentLogicalTableScopeEvidenceV1,
    AgentLogicalTableScopeV1,
    agent_logical_table_scope_to_parent_materializer_input,
    build_agent_logical_table_scope,
    source_ir_canonical_sha256,
    stable_agent_logical_table_scope_sha256,
    validate_agent_logical_table_scope,
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
from app.domain.extraction.table_records import (
    TableFactPartV1,
    TableRecordProjectionV1,
    TableRecordProjectionMode,
    build_table_fact_binding,
    build_table_record_projection,
    normalize_table_record_source_refs,
)


SOURCE_SHA = "a" * 64
ADAPTER_ID = "agent-logical-table-test"
ADAPTER_VERSION = "1.0.0"
D1_SHA = "b" * 64


class AgentLogicalTableScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = _source_with_tables(
            SOURCE_SHA,
            (
                (1, (("序号", "要求"), ("1", "第一条"))),
                (2, (("序号", "要求"), ("2", "第二条"))),
                (3, (("独立对象", "说明"), ("A", "不得误接"))),
            ),
        )
        self.table_ids = tuple(
            node.table_payload.table_id
            for node in self.source.nodes
            if node.table_payload is not None
        )
        self.d1 = _d1_identity(SOURCE_SHA)

    def test_builds_agent_owned_scope_from_exact_current_tableir_facts(self) -> None:
        scope = self._scope(self.table_ids[:2])

        validated = validate_agent_logical_table_scope(
            self.source,
            scope,
            expected_d1_table_map_artifact=self.d1,
        )

        self.assertIs(validated, scope)
        self.assertEqual(scope.physical_table_ids, self.table_ids[:2])
        self.assertEqual(
            tuple(part.page_numbers for part in scope.physical_parts),
            ((1,), (2,)),
        )
        self.assertEqual(
            tuple(part.source_order for part in scope.physical_parts),
            (0, 1),
        )
        self.assertTrue(
            all(len(part.table_ir_sha256) == 64 for part in scope.physical_parts)
        )
        evidence_by_ref = {item.evidence_ref: item for item in scope.evidence}
        self.assertTrue(
            all(
                evidence_by_ref[f"document-table-map:{part.table_id}"].evidence_sha256
                == part.d1_table_map_sha256
                for part in scope.physical_parts
            )
        )
        self.assertRegex(
            scope.logical_object_id,
            r"^agent-logical-table-[0-9a-f]{32}$",
        )
        # The scope contract takes no StructureTree and therefore cannot adopt
        # parser/resolver CONTINUES relations as logical-object authority.
        self.assertNotIn("structure", scope.model_dump(mode="json"))

    def test_rejects_duplicate_unknown_and_reordered_agent_parts(self) -> None:
        with self.assertRaisesRegex(ValueError, "repeat a physical table"):
            self._scope((self.table_ids[0], self.table_ids[0]))
        with self.assertRaisesRegex(ValueError, "unknown physical table"):
            self._scope(("table-unknown",))
        with self.assertRaisesRegex(ValueError, "follow SourceIR order"):
            self._scope((self.table_ids[1], self.table_ids[0]))

    def test_rejects_hash_tampering_and_rehashed_stale_tableir_binding(self) -> None:
        scope = self._scope(self.table_ids[:2])
        hash_tamper = scope.model_dump(mode="json")
        hash_tamper["reason_codes"] = ["another_reason"]
        with self.assertRaisesRegex(ValidationError, "hash does not bind"):
            AgentLogicalTableScopeV1.model_validate(hash_tamper)

        stale_first = AgentLogicalTablePhysicalPartV1(
            **{
                **scope.physical_parts[0].model_dump(mode="python"),
                "table_ir_sha256": "f" * 64,
            }
        )
        stale_parts = (stale_first, *scope.physical_parts[1:])
        stale_scope = AgentLogicalTableScopeV1(
            source_ir_id=scope.source_ir_id,
            source_sha256=scope.source_sha256,
            source_ir_canonical_sha256=scope.source_ir_canonical_sha256,
            d1_table_map_artifact=scope.d1_table_map_artifact,
            logical_object_id=scope.logical_object_id,
            physical_parts=stale_parts,
            reason_codes=scope.reason_codes,
            evidence=scope.evidence,
            scope_sha256=stable_agent_logical_table_scope_sha256(
                source_ir_id=scope.source_ir_id,
                source_sha256=scope.source_sha256,
                source_ir_canonical_sha256=scope.source_ir_canonical_sha256,
                d1_table_map_artifact=scope.d1_table_map_artifact,
                logical_object_id=scope.logical_object_id,
                physical_parts=stale_parts,
                reason_codes=scope.reason_codes,
                evidence=scope.evidence,
            ),
        )
        with self.assertRaisesRegex(ValueError, "stale or tampered"):
            validate_agent_logical_table_scope(
                self.source,
                stale_scope,
                expected_d1_table_map_artifact=self.d1,
            )

    def test_rejects_cross_source_and_d1_artifact_drift(self) -> None:
        scope = self._scope(self.table_ids[:2])
        other_source = _source_with_tables(
            "d" * 64,
            (
                (1, (("序号", "要求"), ("1", "第一条"))),
                (2, (("序号", "要求"), ("2", "第二条"))),
            ),
        )
        with self.assertRaisesRegex(ValueError, "another source snapshot"):
            validate_agent_logical_table_scope(
                other_source,
                scope,
                expected_d1_table_map_artifact=_d1_identity("d" * 64),
            )
        drifted_d1 = AgentD1TableMapArtifactIdentityV1(
            source_sha256=SOURCE_SHA,
            artifact_id=self.d1.artifact_id,
            artifact_sha256="e" * 64,
        )
        with self.assertRaisesRegex(ValueError, "artifact identity drifted"):
            validate_agent_logical_table_scope(
                self.source,
                scope,
                expected_d1_table_map_artifact=drifted_d1,
            )

    def test_materializer_adapter_accepts_exact_facts_and_rejects_old_shell_scope(self) -> None:
        scope = self._scope(self.table_ids[:2])
        exact_projection = _base_projection(self.source, scope.physical_parts)

        materializer_input = (
            agent_logical_table_scope_to_parent_materializer_input(
                self.source,
                scope,
                expected_d1_table_map_artifact=self.d1,
                base_projection=exact_projection,
            )
        )

        self.assertIs(materializer_input.base_projection, exact_projection)
        self.assertEqual(
            materializer_input.expected_agent_logical_table_id,
            scope.logical_object_id,
        )
        self.assertEqual(
            materializer_input.agent_logical_table_scope_sha256,
            scope.scope_sha256,
        )
        self.assertEqual(
            materializer_input.d1_table_map_artifact_id,
            self.d1.artifact_id,
        )
        self.assertEqual(
            materializer_input.d1_table_map_artifact_sha256,
            self.d1.artifact_sha256,
        )

        # A legacy/resolver materializer handle that selected only one part is
        # not allowed to shrink the Agent-owned logical object.
        stale_shell_projection = _base_projection(
            self.source,
            scope.physical_parts[:1],
        )
        with self.assertRaisesRegex(ValueError, "parts differ"):
            agent_logical_table_scope_to_parent_materializer_input(
                self.source,
                scope,
                expected_d1_table_map_artifact=self.d1,
                base_projection=stale_shell_projection,
            )

    def _scope(self, physical_table_ids: tuple[str, ...]) -> AgentLogicalTableScopeV1:
        return build_agent_logical_table_scope(
            self.source,
            d1_table_map_artifact=self.d1,
            physical_table_ids=physical_table_ids,
            reason_codes=("same_logical_object",),
            supplemental_evidence=(
                AgentLogicalTableScopeEvidenceV1(
                    evidence_ref="page-render:1-2",
                    evidence_sha256="c" * 64,
                    page_numbers=(1, 2),
                ),
            ),
        )


def _d1_identity(source_sha256: str) -> AgentD1TableMapArtifactIdentityV1:
    return AgentD1TableMapArtifactIdentityV1(
        source_sha256=source_sha256,
        artifact_id="agent-d1-table-map-" + source_sha256[:32],
        artifact_sha256=D1_SHA,
    )


def _source_with_tables(
    source_sha256: str,
    specs: tuple[tuple[int, tuple[tuple[str, ...], ...]], ...],
) -> SourceIRSnapshot:
    nodes: list[SourceNode] = []
    for source_order, (page_number, rows) in enumerate(specs):
        locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=page_number,
            table_index=0,
        )
        table_id = stable_table_id(
            source_sha256=source_sha256,
            adapter_id=ADAPTER_ID,
            adapter_version=ADAPTER_VERSION,
            native_locator=locator,
        )
        cells = tuple(
            TableCellIR(
                cell_id=stable_cell_id(table_id, row_index, column_index),
                row=row_index,
                column=column_index,
                raw_text=value,
                native_locator=NativeLocator(
                    format=NativeDocumentFormat.PDF,
                    page_number=page_number,
                    table_index=0,
                    row_index=row_index,
                    column_index=column_index,
                ),
            )
            for row_index, row in enumerate(rows)
            for column_index, value in enumerate(row)
        )
        table = TableIR(
            table_id=table_id,
            row_count=len(rows),
            column_count=max(len(row) for row in rows),
            cells=cells,
        )
        nodes.append(
            SourceNode(
                source_node_id=stable_node_id(
                    source_sha256=source_sha256,
                    adapter_id=ADAPTER_ID,
                    adapter_version=ADAPTER_VERSION,
                    kind=SourceNodeKind.TABLE,
                    native_locator=locator,
                ),
                kind=SourceNodeKind.TABLE,
                source_order=source_order,
                native_locator=locator,
                table_payload=table,
            )
        )
    document_id = stable_document_id(source_sha256)
    return SourceIRSnapshot(
        source_ir_id=stable_source_ir_id(
            document_id=document_id,
            source_sha256=source_sha256,
            adapter_id=ADAPTER_ID,
            adapter_version=ADAPTER_VERSION,
        ),
        document_id=document_id,
        source_sha256=source_sha256,
        adapter_id=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
        status=SourceIRStatus.COMPLETE,
        nodes=tuple(nodes),
        created_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
    )


def _base_projection(
    source: SourceIRSnapshot,
    parts: tuple[AgentLogicalTablePhysicalPartV1, ...],
) -> TableRecordProjectionV1:
    fact_parts = tuple(
        TableFactPartV1(
            table_id=part.table_id,
            source_order=part.source_order,
            row_count=part.row_count,
            column_count=part.column_count,
            cell_ids=part.cell_ids,
            table_content_sha256=part.table_ir_sha256,
            source_refs=part.source_refs,
        )
        for part in parts
    )
    whole_refs = normalize_table_record_source_refs(
        tuple(ref for part in parts for ref in part.source_refs)
    )
    fact = build_table_fact_binding(
        source_ir_id=source.source_ir_id,
        source_sha256=source.source_sha256,
        source_ir_canonical_sha256=source_ir_canonical_sha256(source),
        structure_snapshot_id="structure-materializer-fixture",
        logical_table_id="legacy-structure-logical-table",
        owner_structure_node_id="legacy-structure-table-owner",
        table_parts=fact_parts,
        whole_table_source_refs=whole_refs,
    )
    return build_table_record_projection(
        table_fact=fact,
        mode=TableRecordProjectionMode.WHOLE_TABLE,
        projection_revision=0,
    )


if __name__ == "__main__":
    unittest.main()

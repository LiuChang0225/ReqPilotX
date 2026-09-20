from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import copy
import unittest

from pydantic import ValidationError

from app.application.agent_workflow.d2_decision_candidate import (
    D2CandidateSourceRangeV1,
    D2CandidateSourceSpanV2,
    D2LogicalObjectDecisionCandidateV2,
    D2LogicalScopePartTableDispositionDecisionCandidateV1,
    D2LogicalStructureDecisionCandidateV2,
    D2LogicalTableScopeDecisionCandidateV1,
    D2NestedValueTableDispositionDecisionCandidateV1,
    D2StandaloneTableDispositionDecisionCandidateV1,
    compile_and_validate_d2_logical_structure_candidate,
)
from app.application.agent_workflow.d2_logical_structure import (
    D2LogicalObjectKind,
    D2LogicalStructureStatus,
    D2PhysicalTableDispositionKind,
    D2SourceEvidenceDispositionKind,
    D2SourceHandleKind,
)
from app.application.extraction.agent_logical_table_scope import (
    AgentD1TableMapArtifactIdentityV1,
)
from app.domain.extraction.source_ir import SourceNodeKind

if __package__:
    from .test_d2_logical_structure import (
        SOURCE_SHA,
        _d1_identity,
        _kit_identity,
        _source_fixture,
    )
else:
    from test_d2_logical_structure import (
        SOURCE_SHA,
        _d1_identity,
        _kit_identity,
        _source_fixture,
    )


class D2DecisionCandidateCompilerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = _source_fixture()
        self.kit = _kit_identity()
        self.d1 = _d1_identity(self.kit)
        self.table_map = AgentD1TableMapArtifactIdentityV1(
            source_sha256=SOURCE_SHA,
            artifact_id="d1-table-map-" + "4" * 32,
            artifact_sha256="5" * 64,
        )
        self.text_node = self.source.nodes[0]
        self.table_nodes = tuple(
            node for node in self.source.nodes if node.kind is SourceNodeKind.TABLE
        )
        self.table_ids = tuple(
            node.table_payload.table_id
            for node in self.table_nodes
            if node.table_payload is not None
        )

    def test_host_compiles_only_decisions_into_a_complete_validated_d2_artifact(
        self,
    ) -> None:
        candidate = self._candidate()

        first = self._compile(candidate)
        second = self._compile(candidate)

        self.assertEqual(first, second)
        proposal = first.proposal
        receipt = first.validation_receipt
        self.assertEqual(proposal.run_id, self.d1.run_id)
        self.assertEqual(proposal.source_sha256, self.source.source_sha256)
        self.assertEqual(proposal.kit_identity, self.kit)
        self.assertEqual(proposal.predecessor_d1_artifact, self.d1)
        self.assertEqual(proposal.d1_table_map_artifact, self.table_map)
        self.assertEqual(proposal.status, D2LogicalStructureStatus.COMPLETE)
        self.assertEqual(receipt.artifact_sha256, proposal.proposal_sha256)
        self.assertEqual(receipt.status, D2LogicalStructureStatus.COMPLETE)
        self.assertEqual(receipt.physical_table_ids, self.table_ids)
        self.assertEqual(len(proposal.logical_table_scopes), 1)
        self.assertRegex(
            proposal.logical_table_scopes[0].logical_object_id,
            r"^agent-logical-table-[0-9a-f]{32}$",
        )
        self.assertTrue(
            all(
                item.evidence_refs
                == tuple(
                    sorted(
                        (
                            self.table_nodes[index].source_node_id,
                            f"document-table-map:{item.table_id}",
                        )
                    )
                )
                for index, item in enumerate(proposal.physical_table_dispositions)
            )
        )

    def test_compact_source_span_expands_to_exact_host_source_segments(self) -> None:
        payload = self._candidate().model_dump(mode="json")
        payload["logical_objects"][0]["source_ranges"] = []
        payload["logical_objects"][0]["source_spans"] = [
            D2CandidateSourceSpanV2(
                start_source_handle=self.text_node.source_node_id,
                start_offset=None,
                end_source_handle=self.text_node.source_node_id,
                end_offset=None,
            ).model_dump(mode="json")
        ]

        compilation = self._compile(
            D2LogicalStructureDecisionCandidateV2.model_validate(payload)
        )
        section = next(
            item
            for item in compilation.proposal.logical_objects
            if item.object_kind is D2LogicalObjectKind.SECTION
        )

        self.assertEqual(len(section.source_segments), 1)
        self.assertEqual(
            section.source_segments[0].source_handle,
            self.text_node.source_node_id,
        )

        payload["logical_objects"][0]["source_spans"][0]["end_source_handle"] = (
            "node-" + "f" * 32
        )
        with self.assertRaisesRegex(ValueError, "unknown source handle"):
            self._compile(D2LogicalStructureDecisionCandidateV2.model_validate(payload))

    def test_wire_schema_rejects_all_host_derived_and_table_record_fields(self) -> None:
        base = self._candidate().model_dump(mode="json")
        mutations = (
            (("run_id",), self.d1.run_id),
            (("source_sha256",), SOURCE_SHA),
            (("kit_identity",), self.kit.model_dump(mode="json")),
            (("proposal_sha256",), "f" * 64),
            (
                ("logical_objects", 0, "logical_object_id"),
                "d2-logical-object-" + "1" * 32,
            ),
            (("logical_objects", 1, "source_order"), 1),
            (("logical_objects", 1, "page_numbers"), [1]),
            (("logical_objects", 1, "row_boundaries"), ((0, 1),)),
            (("logical_table_scopes", 0, "scope_sha256"), "e" * 64),
            (("physical_table_dispositions", 0, "disposition_sha256"), "d" * 64),
        )
        for path, value in mutations:
            with self.subTest(path=path):
                payload = copy.deepcopy(base)
                target = payload
                for part in path[:-1]:
                    target = target[part]
                target[path[-1]] = value
                with self.assertRaisesRegex(
                    ValidationError,
                    "Extra inputs are not permitted",
                ):
                    D2LogicalStructureDecisionCandidateV2.model_validate(payload)

        properties = set(
            D2LogicalStructureDecisionCandidateV2.model_json_schema()["properties"]
        )
        self.assertEqual(
            properties,
            {
                "schema_version",
                "logical_objects",
                "logical_table_scopes",
                "source_evidence_dispositions",
                "physical_table_dispositions",
                "unresolved_codes",
            },
        )

    def test_rejects_unknown_handles_and_cross_source_host_inputs(self) -> None:
        payload = self._candidate().model_dump(mode="json")
        payload["logical_objects"][0]["source_ranges"][0]["source_handle"] = (
            "node-" + "f" * 32
        )
        unknown = D2LogicalStructureDecisionCandidateV2.model_validate(payload)
        with self.assertRaisesRegex(ValueError, "unknown source handle"):
            self._compile(unknown)

        another_source = self.source.model_copy(update={"source_sha256": "f" * 64})
        with self.assertRaisesRegex(ValueError, "another source"):
            compile_and_validate_d2_logical_structure_candidate(
                self._candidate(),
                source_ir=another_source,
                expected_d1_artifact=self.d1,
                expected_d1_table_map_artifact=self.table_map,
                expected_kit_identity=self.kit,
            )

    def test_rejects_unknown_duplicate_or_cyclic_object_parentage(self) -> None:
        payload = self._candidate().model_dump(mode="json")
        payload["logical_objects"][0]["parent_object_key"] = "object:missing"
        unknown_parent = D2LogicalStructureDecisionCandidateV2.model_validate(payload)
        with self.assertRaisesRegex(ValueError, "unknown parent key"):
            self._compile(unknown_parent)

        payload = self._candidate().model_dump(mode="json")
        payload["logical_objects"][0]["parent_object_key"] = "object:table"
        payload["logical_objects"][1]["parent_object_key"] = "object:section"
        cycle = D2LogicalStructureDecisionCandidateV2.model_validate(payload)
        with self.assertRaisesRegex(ValueError, "contains a cycle"):
            self._compile(cycle)

        payload = self._candidate().model_dump(mode="json")
        duplicate = copy.deepcopy(payload["logical_objects"][0])
        duplicate["object_key"] = "object:duplicate_section"
        payload["logical_objects"].append(duplicate)
        collapsed = D2LogicalStructureDecisionCandidateV2.model_validate(payload)
        with self.assertRaisesRegex(ValueError, "duplicate stable identities"):
            self._compile(collapsed)

    def test_rejects_incomplete_table_and_marks_source_gap_unresolved(self) -> None:
        payload = self._candidate().model_dump(mode="json")
        payload["physical_table_dispositions"].pop()
        missing_table = D2LogicalStructureDecisionCandidateV2.model_validate(payload)
        with self.assertRaisesRegex(ValueError, "every current physical TableIR"):
            self._compile(missing_table)

        payload = self._candidate().model_dump(mode="json")
        payload["logical_objects"][0]["source_ranges"][0]["end_offset"] -= 1
        missing_source = D2LogicalStructureDecisionCandidateV2.model_validate(payload)
        compilation = self._compile(missing_source)

        self.assertEqual(compilation.proposal.status, D2LogicalStructureStatus.PARTIAL)
        unresolved = tuple(
            item
            for item in compilation.proposal.source_evidence_dispositions
            if item.disposition is D2SourceEvidenceDispositionKind.UNRESOLVED
        )
        self.assertEqual(len(unresolved), 1)
        self.assertEqual(
            (unresolved[0].start_offset, unresolved[0].end_offset),
            (len(self.text_node.raw_text) - 1, len(self.text_node.raw_text)),
        )
        self.assertEqual(
            unresolved[0].reason_codes,
            ("host_completed_uncovered_source",),
        )

    def test_host_marks_an_entire_omitted_non_table_node_unresolved(self) -> None:
        payload = self._candidate().model_dump(mode="json")
        payload["logical_objects"].pop(0)

        compilation = self._compile(
            D2LogicalStructureDecisionCandidateV2.model_validate(payload)
        )
        unresolved = tuple(
            item
            for item in compilation.proposal.source_evidence_dispositions
            if item.disposition is D2SourceEvidenceDispositionKind.UNRESOLVED
        )

        self.assertEqual(compilation.proposal.status, D2LogicalStructureStatus.PARTIAL)
        self.assertEqual(len(unresolved), 1)
        self.assertEqual(unresolved[0].source_node_id, self.text_node.source_node_id)
        self.assertEqual(
            (unresolved[0].start_offset, unresolved[0].end_offset),
            (0, len(self.text_node.raw_text)),
        )

    def test_host_derives_partial_status_from_unresolved_decisions(self) -> None:
        payload = self._candidate().model_dump(mode="json")
        payload["physical_table_dispositions"][-1]["disposition"] = "UNRESOLVED"
        partial = D2LogicalStructureDecisionCandidateV2.model_validate(payload)

        compilation = self._compile(partial)

        self.assertEqual(compilation.proposal.status, D2LogicalStructureStatus.PARTIAL)
        self.assertEqual(
            compilation.validation_receipt.unresolved_table_ids,
            (self.table_ids[-1],),
        )

    def test_empty_document_container_derives_identity_and_pages_from_children(
        self,
    ) -> None:
        payload = self._candidate().model_dump(mode="json")
        payload["logical_objects"].insert(
            0,
            {
                "object_key": "document-root",
                "object_kind": "DOCUMENT_UNIT",
                "parent_object_key": None,
                "source_ranges": [],
                "logical_table_scope_key": None,
                "reason_codes": ["document container"],
            },
        )
        payload["logical_objects"][1]["parent_object_key"] = "document-root"
        payload["logical_objects"][2]["parent_object_key"] = "document-root"

        compilation = self._compile(
            D2LogicalStructureDecisionCandidateV2.model_validate(payload)
        )
        document = next(
            item
            for item in compilation.proposal.logical_objects
            if item.object_kind is D2LogicalObjectKind.DOCUMENT_UNIT
        )

        self.assertEqual(document.source_segments, ())
        self.assertEqual(document.page_numbers, (1, 2, 3))

    def test_empty_container_derives_pages_and_identity_from_table_scope(
        self,
    ) -> None:
        payload = self._candidate().model_dump(mode="json")
        payload["logical_objects"].insert(
            0,
            {
                "object_key": "table-only-container",
                "object_kind": "SECTION",
                "parent_object_key": None,
                "source_ranges": [],
                "logical_table_scope_key": None,
                "reason_codes": ["table container"],
            },
        )
        payload["logical_objects"][2]["parent_object_key"] = "table-only-container"
        payload["logical_objects"][2]["source_ranges"] = []

        first = self._compile(
            D2LogicalStructureDecisionCandidateV2.model_validate(payload)
        ).proposal
        second = self._compile(
            D2LogicalStructureDecisionCandidateV2.model_validate(payload)
        ).proposal
        container = next(
            item
            for item in first.logical_objects
            if item.object_kind is D2LogicalObjectKind.SECTION
            and not item.source_segments
        )

        self.assertEqual(container.page_numbers, (2, 3))
        table = next(
            item
            for item in first.logical_objects
            if item.object_kind is D2LogicalObjectKind.TABLE
        )
        self.assertEqual(
            tuple(segment.source_handle for segment in table.source_segments),
            tuple(node.source_node_id for node in self.table_nodes[:2]),
        )
        self.assertEqual(first, second)

    def test_empty_container_identity_preserves_multiple_scope_source_order(
        self,
    ) -> None:
        payload = self._candidate().model_dump(mode="json")
        payload["logical_objects"].insert(
            0,
            {
                "object_key": "document-root",
                "object_kind": "DOCUMENT_UNIT",
                "parent_object_key": None,
                "source_ranges": [],
                "logical_table_scope_key": None,
                "reason_codes": ["document container"],
            },
        )
        payload["logical_objects"][1]["parent_object_key"] = "document-root"
        payload["logical_objects"][2]["parent_object_key"] = "document-root"
        payload["logical_objects"][2]["source_ranges"] = []
        payload["logical_objects"].append(
            {
                "object_key": "object:second_table",
                "object_kind": "TABLE",
                "parent_object_key": "document-root",
                "source_ranges": [],
                "logical_table_scope_key": "scope:second_table",
                "reason_codes": ["logical table scope"],
            }
        )
        payload["logical_table_scopes"][0]["physical_table_ids"] = [self.table_ids[0]]
        payload["logical_table_scopes"].append(
            {
                "scope_key": "scope:second_table",
                "physical_table_ids": [self.table_ids[1]],
                "reason_codes": ["separate logical object"],
            }
        )
        payload["physical_table_dispositions"][1]["logical_table_scope_key"] = (
            "scope:second_table"
        )

        proposal = self._compile(
            D2LogicalStructureDecisionCandidateV2.model_validate(payload)
        ).proposal
        document = next(
            item
            for item in proposal.logical_objects
            if item.object_kind is D2LogicalObjectKind.DOCUMENT_UNIT
        )

        self.assertEqual(document.page_numbers, (1, 2, 3))
        self.assertEqual(len(proposal.logical_table_scopes), 2)

    def test_nested_empty_same_kind_containers_bind_parent_identity(self) -> None:
        payload = self._candidate().model_dump(mode="json")
        payload["logical_objects"][:0] = [
            {
                "object_key": "section-outer",
                "object_kind": "SECTION",
                "parent_object_key": None,
                "source_ranges": [],
                "logical_table_scope_key": None,
                "reason_codes": ["outer container"],
            },
            {
                "object_key": "section-inner",
                "object_kind": "SECTION",
                "parent_object_key": "section-outer",
                "source_ranges": [],
                "logical_table_scope_key": None,
                "reason_codes": ["inner container"],
            },
        ]
        payload["logical_objects"][2]["parent_object_key"] = "section-inner"
        payload["logical_objects"][3]["parent_object_key"] = "section-inner"

        proposal = self._compile(
            D2LogicalStructureDecisionCandidateV2.model_validate(payload)
        ).proposal
        empty_sections = tuple(
            item
            for item in proposal.logical_objects
            if item.object_kind is D2LogicalObjectKind.SECTION
            and not item.source_segments
        )

        self.assertEqual(len(empty_sections), 2)
        self.assertEqual(len({item.logical_object_id for item in empty_sections}), 2)

    def test_empty_leaf_container_is_pruned_as_evidence_free(self) -> None:
        payload = self._candidate().model_dump(mode="json")
        payload["logical_objects"].append(
            {
                "object_key": "empty-note-container",
                "object_kind": "CONTEXT",
                "parent_object_key": None,
                "source_ranges": [],
                "logical_table_scope_key": None,
                "reason_codes": ["empty container"],
            }
        )

        proposal = self._compile(
            D2LogicalStructureDecisionCandidateV2.model_validate(payload)
        ).proposal

        self.assertFalse(
            any(
                item.object_kind is D2LogicalObjectKind.CONTEXT
                and not item.source_segments
                for item in proposal.logical_objects
            )
        )

    def _compile(self, candidate: D2LogicalStructureDecisionCandidateV2):
        return compile_and_validate_d2_logical_structure_candidate(
            candidate,
            source_ir=self.source,
            expected_d1_artifact=self.d1,
            expected_d1_table_map_artifact=self.table_map,
            expected_kit_identity=self.kit,
        )

    def _candidate(self) -> D2LogicalStructureDecisionCandidateV2:
        text_range = D2CandidateSourceRangeV1(
            handle_kind=D2SourceHandleKind.SOURCE_NODE,
            source_handle=self.text_node.source_node_id,
            start_offset=0,
            end_offset=len(self.text_node.raw_text),
        )
        table_ranges = tuple(
            D2CandidateSourceRangeV1(
                handle_kind=D2SourceHandleKind.SOURCE_NODE,
                source_handle=node.source_node_id,
            )
            for node in self.table_nodes[:2]
        )
        return D2LogicalStructureDecisionCandidateV2(
            logical_objects=(
                D2LogicalObjectDecisionCandidateV2(
                    object_key="object:section",
                    object_kind=D2LogicalObjectKind.SECTION,
                    source_ranges=(text_range,),
                    reason_codes=("source_boundary",),
                ),
                D2LogicalObjectDecisionCandidateV2(
                    object_key="object:table",
                    object_kind=D2LogicalObjectKind.TABLE,
                    source_ranges=table_ranges,
                    logical_table_scope_key="scope:main_table",
                    reason_codes=("logical_table_scope",),
                ),
            ),
            logical_table_scopes=(
                D2LogicalTableScopeDecisionCandidateV1(
                    scope_key="scope:main_table",
                    physical_table_ids=self.table_ids[:2],
                    reason_codes=("same_logical_object",),
                ),
            ),
            source_evidence_dispositions=(),
            physical_table_dispositions=(
                D2LogicalScopePartTableDispositionDecisionCandidateV1(
                    table_id=self.table_ids[0],
                    disposition=D2PhysicalTableDispositionKind.LOGICAL_SCOPE_PART,
                    logical_table_scope_key="scope:main_table",
                    reason_codes=("logical_table_part",),
                ),
                D2LogicalScopePartTableDispositionDecisionCandidateV1(
                    table_id=self.table_ids[1],
                    disposition=D2PhysicalTableDispositionKind.LOGICAL_SCOPE_PART,
                    logical_table_scope_key="scope:main_table",
                    reason_codes=("logical_table_part",),
                ),
                D2NestedValueTableDispositionDecisionCandidateV1(
                    table_id=self.table_ids[2],
                    disposition=D2PhysicalTableDispositionKind.NESTED_VALUE_TABLE,
                    nested_owner_table_id=self.table_ids[0],
                    reason_codes=("nested_value_table",),
                ),
                D2StandaloneTableDispositionDecisionCandidateV1(
                    table_id=self.table_ids[3],
                    disposition=(
                        D2PhysicalTableDispositionKind.NON_BUSINESS_FALSE_TABLE
                    ),
                    reason_codes=("non_business_false_table",),
                ),
            ),
        )


if __name__ == "__main__":
    unittest.main()

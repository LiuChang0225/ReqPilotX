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

from pydantic import ValidationError

from app.application.agent_workflow.d2_logical_structure import (
    D2LogicalObjectKind,
    D2LogicalStructureProposalV1,
    D2LogicalStructureStatus,
    D2PhysicalTableDispositionKind,
    D2ValidatedD1ArtifactIdentityV1,
    stable_d2_logical_structure_proposal_sha256,
    validate_d2_logical_structure_proposal,
)
from app.application.agent_workflow.extraction_runtime import AgentKitIdentityV1
from app.application.agent_workflow.logical_structure_repeatability import (
    LogicalStructureRepeatabilityDriftKind,
    LogicalStructureRepeatabilityVerdict,
    LogicalStructureTablePartitionMode,
    LogicalStructureTableRegionIdentityMode,
    build_logical_structure_repeatability_fingerprint,
    evaluate_logical_structure_repeatability,
    logical_structure_repeatability_safe_summary,
)
from app.application.extraction.agent_logical_table_scope import (
    AgentD1TableMapArtifactIdentityV1,
)
from app.domain.extraction.source_ir import SourceNodeKind

if __package__:
    from .test_d2_logical_structure import (
        SOURCE_SHA,
        _d1_identity,
        _disposition,
        _kit_identity,
        _logical_object,
        _proposal,
        _scope,
        _segment,
        _source_fixture,
        _table_object,
    )
else:
    from test_d2_logical_structure import (
        SOURCE_SHA,
        _d1_identity,
        _disposition,
        _kit_identity,
        _logical_object,
        _proposal,
        _scope,
        _segment,
        _source_fixture,
        _table_object,
    )


class LogicalStructureRepeatabilityTests(unittest.TestCase):
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
            item for item in self.source.nodes if item.kind is SourceNodeKind.TABLE
        )
        self.table_ids = tuple(
            item.table_payload.table_id
            for item in self.table_nodes
            if item.table_payload is not None
        )

    def test_same_profile_with_distinct_run_receipts_passes_in_any_input_order(
        self,
    ) -> None:
        proposal, receipt = self._split_run(12)
        rerun_proposal, rerun_receipt = self._rerun(
            proposal,
            run_id="agent-run-" + "e" * 32,
        )
        first = build_logical_structure_repeatability_fingerprint(
            proposal,
            receipt,
        )
        second = build_logical_structure_repeatability_fingerprint(
            rerun_proposal,
            rerun_receipt,
        )

        forward = evaluate_logical_structure_repeatability((first, second))
        reversed_input = evaluate_logical_structure_repeatability((second, first))

        self.assertEqual(forward, reversed_input)
        self.assertEqual(forward.verdict, LogicalStructureRepeatabilityVerdict.PASS)
        self.assertEqual(forward.drift_kinds, ())
        self.assertNotEqual(first.fingerprint_sha256, second.fingerprint_sha256)
        self.assertEqual(
            first.comparison_identity_sha256,
            second.comparison_identity_sha256,
        )

    def test_exact_same_coverage_with_twelve_and_twenty_three_objects_fails(
        self,
    ) -> None:
        twelve = self._fingerprint(*self._split_run(12))
        twenty_three = self._fingerprint(*self._split_run(23))

        evaluation = evaluate_logical_structure_repeatability((twenty_three, twelve))

        self.assertEqual(evaluation.verdict, LogicalStructureRepeatabilityVerdict.FAIL)
        self.assertEqual(
            evaluation.drift_kinds,
            (LogicalStructureRepeatabilityDriftKind.OBJECT_BOUNDARY_DRIFT,),
        )
        self.assertEqual(
            twelve.coverage_profile_sha256, twenty_three.coverage_profile_sha256
        )
        self.assertNotEqual(
            twelve.object_profile_sha256, twenty_three.object_profile_sha256
        )
        self.assertEqual(len(twelve.object_profile), 12)
        self.assertEqual(len(twenty_three.object_profile), 23)

    def test_exact_same_coverage_with_seven_and_nine_objects_fails(self) -> None:
        seven = self._fingerprint(*self._split_run(7))
        nine = self._fingerprint(*self._split_run(9))

        evaluation = evaluate_logical_structure_repeatability((seven, nine))

        self.assertEqual(evaluation.verdict, LogicalStructureRepeatabilityVerdict.FAIL)
        self.assertIn(
            LogicalStructureRepeatabilityDriftKind.OBJECT_BOUNDARY_DRIFT,
            evaluation.drift_kinds,
        )
        self.assertNotIn(
            LogicalStructureRepeatabilityDriftKind.COVERAGE_DRIFT,
            evaluation.drift_kinds,
        )

    def test_drift_dimensions_are_reported_separately(self) -> None:
        base_proposal, base_receipt = self._split_run(12)
        base = self._fingerprint(base_proposal, base_receipt)

        changed_kit = AgentKitIdentityV1.model_validate(
            self.kit.model_dump(mode="json")
            | {
                "kit_version": "9.9.9",
                "manifest_sha256": "d" * 64,
                "bundle_sha256": "e" * 64,
            }
        )
        identity_changed = self._fingerprint(*self._split_run(12, kit=changed_kit))
        identity_evaluation = evaluate_logical_structure_repeatability(
            (base, identity_changed)
        )
        self.assertEqual(
            identity_evaluation.drift_kinds,
            (LogicalStructureRepeatabilityDriftKind.IDENTITY_DRIFT,),
        )

        unresolved_dispositions = tuple(
            base_proposal.physical_table_dispositions[:-1]
        ) + (
            _disposition(
                self.table_ids[-1],
                D2PhysicalTableDispositionKind.UNRESOLVED,
            ),
        )
        status_proposal = _proposal(
            source=self.source,
            kit=self.kit,
            d1=self.d1,
            table_map=self.table_map,
            logical_objects=base_proposal.logical_objects,
            scopes=(),
            dispositions=unresolved_dispositions,
            source_dispositions=base_proposal.source_evidence_dispositions,
            status=D2LogicalStructureStatus.PARTIAL,
        )
        status_receipt = self._validate(status_proposal, d1=self.d1)
        status_changed = self._fingerprint(status_proposal, status_receipt)
        status_evaluation = evaluate_logical_structure_repeatability(
            (base, status_changed)
        )
        self.assertEqual(
            status_evaluation.drift_kinds,
            (LogicalStructureRepeatabilityDriftKind.STATUS_DRIFT,),
        )

    def test_coverage_and_table_grouping_drift_are_not_hidden_by_exact_cover(
        self,
    ) -> None:
        grouped_proposal, grouped_receipt = self._table_grouped_run(split=False)
        split_proposal, split_receipt = self._table_grouped_run(split=True)
        grouped = self._fingerprint(grouped_proposal, grouped_receipt)
        split = self._fingerprint(split_proposal, split_receipt)

        grouping_evaluation = evaluate_logical_structure_repeatability((grouped, split))

        self.assertIn(
            LogicalStructureRepeatabilityDriftKind.TABLE_GROUPING_DRIFT,
            grouping_evaluation.drift_kinds,
        )
        self.assertIn(
            LogicalStructureRepeatabilityDriftKind.OBJECT_BOUNDARY_DRIFT,
            grouping_evaluation.drift_kinds,
        )
        self.assertNotIn(
            LogicalStructureRepeatabilityDriftKind.COVERAGE_DRIFT,
            grouping_evaluation.drift_kinds,
        )

        coverage_proposal = _proposal(
            source=self.source,
            kit=self.kit,
            d1=self.d1,
            table_map=self.table_map,
            logical_objects=grouped_proposal.logical_objects,
            scopes=grouped_proposal.logical_table_scopes,
            dispositions=(
                *grouped_proposal.physical_table_dispositions[:2],
                _disposition(
                    self.table_ids[2],
                    D2PhysicalTableDispositionKind.NON_BUSINESS_FALSE_TABLE,
                ),
                grouped_proposal.physical_table_dispositions[3],
            ),
            source_dispositions=grouped_proposal.source_evidence_dispositions,
        )
        coverage_receipt = self._validate(coverage_proposal, d1=self.d1)
        coverage_changed = self._fingerprint(coverage_proposal, coverage_receipt)
        coverage_evaluation = evaluate_logical_structure_repeatability(
            (grouped, coverage_changed)
        )
        self.assertIn(
            LogicalStructureRepeatabilityDriftKind.COVERAGE_DRIFT,
            coverage_evaluation.drift_kinds,
        )
        self.assertIn(
            LogicalStructureRepeatabilityDriftKind.STATUS_DRIFT,
            coverage_evaluation.drift_kinds,
        )

    def test_table_profile_declares_whole_table_only_expressiveness_gap(self) -> None:
        proposal, receipt = self._table_grouped_run(split=False)
        fingerprint = self._fingerprint(proposal, receipt)

        self.assertEqual(
            fingerprint.table_partition_capability,
            LogicalStructureTablePartitionMode.WHOLE_TABLE_ONLY,
        )
        self.assertEqual(len(fingerprint.logical_table_scopes), 1)
        scope = fingerprint.logical_table_scopes[0]
        self.assertEqual(scope.physical_table_ordinals, (0, 1))
        self.assertEqual(
            tuple(item.physical_table_ordinal for item in scope.regions),
            (0, 1),
        )
        self.assertTrue(
            all(
                item.identity_mode
                is LogicalStructureTableRegionIdentityMode.WHOLE_PHYSICAL_TABLE
                and item.cell_partition_sha256 is None
                for item in scope.regions
            )
        )
        self.assertIn(
            "PHYSICAL_TABLE_MULTI_REGION_UNREPRESENTABLE_IN_LOGICAL_STRUCTURE_PROPOSAL_V1",
            fingerprint.schema_expressiveness_codes,
        )

    def test_fingerprint_and_default_summary_contain_no_source_body(self) -> None:
        proposal, receipt = self._table_grouped_run(split=False)
        fingerprint = self._fingerprint(proposal, receipt)
        serialized = json.dumps(
            {
                "fingerprint": fingerprint.model_dump(mode="json"),
                "summary": logical_structure_repeatability_safe_summary(fingerprint),
            },
            ensure_ascii=False,
            sort_keys=True,
        )

        self.assertNotIn(self.source.nodes[0].raw_text, serialized)
        self.assertNotIn(self.source.nodes[0].source_node_id, serialized)
        self.assertNotIn(self.table_ids[0], serialized)
        summary = logical_structure_repeatability_safe_summary(fingerprint)
        self.assertEqual(summary["logical_object_count"], 2)
        self.assertEqual(summary["physical_table_count"], 4)
        self.assertEqual(summary["table_partition_capability"], "WHOLE_TABLE_ONLY")

    def test_mismatched_proposal_and_receipt_are_rejected(self) -> None:
        proposal, _ = self._split_run(12)
        _, other_receipt = self._split_run(23)

        with self.assertRaisesRegex(ValueError, "exact validated proposal/receipt"):
            build_logical_structure_repeatability_fingerprint(
                proposal,
                other_receipt,
            )

    def test_stable_fingerprint_and_evaluation_hashes_reject_tampering(self) -> None:
        first = self._fingerprint(*self._split_run(7))
        second = self._fingerprint(*self._split_run(9))
        evaluation = evaluate_logical_structure_repeatability((first, second))

        with self.assertRaisesRegex(ValidationError, "fingerprint SHA drifted"):
            type(first).model_validate(
                first.model_dump(mode="json") | {"fingerprint_sha256": "f" * 64}
            )
        with self.assertRaisesRegex(ValidationError, "evaluation SHA drifted"):
            type(evaluation).model_validate(
                evaluation.model_dump(mode="json") | {"evaluation_sha256": "f" * 64}
            )

    def _split_run(
        self,
        object_count: int,
        *,
        kit: AgentKitIdentityV1 | None = None,
    ):
        kit = kit or self.kit
        d1 = _d1_identity(kit)
        text_node = self.source.nodes[0]
        text_length = len(text_node.raw_text)
        self.assertLessEqual(object_count, text_length)
        base_width, extra = divmod(text_length, object_count)
        offsets: list[tuple[int, int]] = []
        start = 0
        for ordinal in range(object_count):
            end = start + base_width + (1 if ordinal < extra else 0)
            offsets.append((start, end))
            start = end
        objects = tuple(
            sorted(
                (
                    _logical_object(
                        source_sha256=SOURCE_SHA,
                        object_kind=D2LogicalObjectKind.PROVISION_SCOPE,
                        source_segments=(_segment(text_node, start=start, end=end),),
                    )
                    for start, end in offsets
                ),
                key=lambda item: (item.source_order, item.logical_object_id),
            )
        )
        proposal = _proposal(
            source=self.source,
            kit=kit,
            d1=d1,
            table_map=self.table_map,
            logical_objects=objects,
            scopes=(),
            dispositions=tuple(
                _disposition(
                    table_id,
                    D2PhysicalTableDispositionKind.NON_BUSINESS_FALSE_TABLE,
                )
                for table_id in self.table_ids
            ),
        )
        return proposal, self._validate(proposal, d1=d1, kit=kit)

    def _table_grouped_run(self, *, split: bool):
        text_node = self.source.nodes[0]
        text_segment = _segment(text_node, start=0, end=len(text_node.raw_text))
        text_object = _logical_object(
            source_sha256=SOURCE_SHA,
            object_kind=D2LogicalObjectKind.SECTION,
            source_segments=(text_segment,),
        )
        if split:
            scopes = (
                _scope(self.source, self.table_map, (self.table_ids[0],)),
                _scope(self.source, self.table_map, (self.table_ids[1],)),
            )
        else:
            scopes = (_scope(self.source, self.table_map, self.table_ids[:2]),)
        table_objects = tuple(
            _table_object(
                scope,
                source_segments=tuple(
                    _segment(node)
                    for node in self.table_nodes
                    if node.table_payload is not None
                    and node.table_payload.table_id in scope.physical_table_ids
                ),
            )
            for scope in scopes
        )
        logical_objects = tuple(
            sorted(
                (text_object, *table_objects),
                key=lambda item: (item.source_order, item.logical_object_id),
            )
        )
        scope_by_table = {
            table_id: scope.logical_object_id
            for scope in scopes
            for table_id in scope.physical_table_ids
        }
        proposal = _proposal(
            source=self.source,
            kit=self.kit,
            d1=self.d1,
            table_map=self.table_map,
            logical_objects=logical_objects,
            scopes=scopes,
            dispositions=(
                _disposition(
                    self.table_ids[0],
                    D2PhysicalTableDispositionKind.LOGICAL_SCOPE_PART,
                    scope=scope_by_table[self.table_ids[0]],
                ),
                _disposition(
                    self.table_ids[1],
                    D2PhysicalTableDispositionKind.LOGICAL_SCOPE_PART,
                    scope=scope_by_table[self.table_ids[1]],
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
            ),
        )
        return proposal, self._validate(proposal, d1=self.d1)

    def _rerun(
        self,
        proposal: D2LogicalStructureProposalV1,
        *,
        run_id: str,
    ):
        predecessor = D2ValidatedD1ArtifactIdentityV1(
            **proposal.predecessor_d1_artifact.model_dump(
                mode="python",
                exclude={"artifact_id", "run_id"},
            ),
            artifact_id="agent-artifact-" + "e" * 32,
            run_id=run_id,
        )
        values = {
            "run_id": run_id,
            "source_sha256": proposal.source_sha256,
            "source_ir_id": proposal.source_ir_id,
            "source_ir_canonical_sha256": proposal.source_ir_canonical_sha256,
            "kit_identity": proposal.kit_identity,
            "predecessor_d1_artifact": predecessor,
            "d1_table_map_artifact": proposal.d1_table_map_artifact,
            "status": proposal.status,
            "logical_objects": proposal.logical_objects,
            "logical_table_scopes": proposal.logical_table_scopes,
            "source_evidence_dispositions": proposal.source_evidence_dispositions,
            "physical_table_dispositions": proposal.physical_table_dispositions,
            "unresolved_codes": proposal.unresolved_codes,
        }
        draft = D2LogicalStructureProposalV1.model_construct(
            **values,
            proposal_sha256="0" * 64,
        )
        rerun = D2LogicalStructureProposalV1(
            **values,
            proposal_sha256=stable_d2_logical_structure_proposal_sha256(draft),
        )
        return rerun, self._validate(rerun, d1=predecessor)

    def _validate(
        self,
        proposal: D2LogicalStructureProposalV1,
        *,
        d1: D2ValidatedD1ArtifactIdentityV1,
        kit: AgentKitIdentityV1 | None = None,
    ):
        return validate_d2_logical_structure_proposal(
            self.source,
            proposal,
            expected_d1_artifact=d1,
            expected_d1_table_map_artifact=self.table_map,
            expected_kit_identity=kit or self.kit,
        )

    @staticmethod
    def _fingerprint(proposal, receipt):  # type: ignore[no-untyped-def]
        return build_logical_structure_repeatability_fingerprint(proposal, receipt)


if __name__ == "__main__":
    unittest.main()

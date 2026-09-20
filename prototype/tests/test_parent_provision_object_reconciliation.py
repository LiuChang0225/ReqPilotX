from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

from dataclasses import dataclass
import hashlib
import unittest

from app.application.agent_workflow.d2_logical_structure import (
    D2LogicalObjectKind,
    D2LogicalObjectV1,
    D2SourceHandleKind,
    D2SourceSegmentV1,
    stable_d2_logical_object_id,
    stable_d2_logical_object_sha256,
    stable_d2_source_segment_id,
    stable_d2_source_segment_sha256,
)
from app.application.agent_workflow.d3_parent_provision_compiler import (
    D3CandidateSourceSpanV2,
    D3ParentBoundaryClassificationEntryV2,
)
from app.application.agent_workflow.d3_parent_provisions import (
    D3ExactSourceSegmentV1,
    D3ValidatedD2ArtifactIdentityV1,
    ParentProvisionStatus,
    ProvisionContentRole,
    RequirementKind,
    RequirementTechnicality,
    Requirementness,
    d3_source_scope_sha256,
)
from app.application.agent_workflow.extraction_runtime import AgentKitIdentityV1
from app.application.agent_workflow.parent_provision_object_batches import (
    ParentProvisionObjectBatchCompilationV1,
    ParentProvisionObjectBatchPlanV1,
    ParentProvisionObjectCandidateV1,
    ParentProvisionObjectExecutionMode,
    ParentProvisionObjectScopeStatus,
    build_parent_provision_object_batch_plan,
    compile_parent_provision_object_candidate,
)
from app.application.agent_workflow.parent_provision_object_reconciliation import (
    ParentProvisionObjectCoverageSource,
    ParentProvisionObjectReconciliationCandidateV1,
    ParentProvisionObjectReconciliationError,
    ParentProvisionObjectReconciliationStatus,
    aggregate_parent_provision_objects_with_reconciliation,
    build_parent_provision_object_reconciliation_plan,
    compile_parent_provision_object_reconciliation_candidate,
)


RUN_ID = "agent-run-" + "1" * 32
SOURCE_SHA256 = "a" * 64
STAGE_INPUT_BUNDLE_SHA256 = "b" * 64


@dataclass(frozen=True, slots=True)
class _Fixture:
    kit: AgentKitIdentityV1
    identity: D3ValidatedD2ArtifactIdentityV1
    objects: tuple[D2LogicalObjectV1, ...]
    segments: tuple[D3ExactSourceSegmentV1, ...]
    plan: ParentProvisionObjectBatchPlanV1
    model_targets: tuple


class ParentProvisionObjectReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = _fixture()

    def test_single_marked_object_gets_only_direct_model_neighbors(self) -> None:
        # B is separated from A by a Host-empty section.  The section may be
        # crossed but never becomes evidence or a replacement target.
        local_results = self._local_results(marked_model_indexes={1})
        reconciliation_plan = self._plan(local_results)

        self.assertEqual(len(reconciliation_plan.groups), 1)
        group = reconciliation_plan.groups[0]
        self.assertEqual(
            tuple(item.logical_object_id for item in group.replacements),
            tuple(
                target.logical_object_id for target in self.fixture.model_targets[:3]
            ),
        )
        self.assertEqual(group.global_segment_ordinals, (0, 1, 2))
        self.assertEqual(
            group.marked_batch_ids,
            (self.fixture.model_targets[1].batch_id,),
        )
        host_empty_ids = {
            target.logical_object_id
            for target in self.fixture.plan.targets
            if target.execution_mode
            is ParentProvisionObjectExecutionMode.HOST_EMPTY_CONTAINER
        }
        self.assertTrue(host_empty_ids)
        self.assertTrue(
            host_empty_ids.isdisjoint(
                item.logical_object_id for item in group.replacements
            )
        )
        self.assertEqual(
            reconciliation_plan.invalidated_local_result_sha256s,
            tuple(item.local_result_sha256 for item in group.replacements),
        )

    def test_adjacent_marked_objects_merge_into_one_minimal_group(self) -> None:
        local_results = self._local_results(marked_model_indexes={1, 2})
        reconciliation_plan = self._plan(local_results)

        self.assertEqual(len(reconciliation_plan.groups), 1)
        group = reconciliation_plan.groups[0]
        self.assertEqual(
            tuple(item.batch_id for item in group.replacements),
            tuple(target.batch_id for target in self.fixture.model_targets[:4]),
        )
        self.assertEqual(group.global_segment_ordinals, (0, 1, 2, 3))

    def test_group_compile_rejects_span_outside_group(self) -> None:
        local_results = self._local_results(marked_model_indexes={1})
        reconciliation_plan = self._plan(local_results)
        group = reconciliation_plan.groups[0]
        candidate = _resolved_candidate(_entry_for(self.fixture.segments[3], ordinal=3))

        with self.assertRaisesRegex(
            ParentProvisionObjectReconciliationError,
            "SPAN_ESCAPES_GROUP",
        ):
            self._compile(
                local_results=local_results,
                reconciliation_plan=reconciliation_plan,
                group_id=group.group_id,
                candidate=candidate,
            )

    def test_group_compile_accepts_zero_offset_boundary_after_group(self) -> None:
        local_results = self._local_results(marked_model_indexes={1})
        reconciliation_plan = self._plan(local_results)
        group = reconciliation_plan.groups[0]
        candidate = _resolved_candidate(
            _entry(
                D3CandidateSourceSpanV2(
                    start_segment_ordinal=0,
                    start_offset=0,
                    end_segment_ordinal=3,
                    end_offset=0,
                )
            )
        )

        compiled = self._compile(
            local_results=local_results,
            reconciliation_plan=reconciliation_plan,
            group_id=group.group_id,
            candidate=candidate,
        )

        self.assertEqual(
            tuple(
                source_range.segment_id
                for source_range in compiled.compiled_decision.entries[0].source_ranges
            ),
            tuple(item.segment_id for item in self.fixture.segments[:3]),
        )

    def test_group_compile_rejects_incomplete_object_disposition(self) -> None:
        local_results = self._local_results(marked_model_indexes={1})
        reconciliation_plan = self._plan(local_results)
        group = reconciliation_plan.groups[0]
        candidate = _resolved_candidate(
            _entry_for(self.fixture.segments[0], ordinal=0),
            _entry_for(self.fixture.segments[1], ordinal=1),
        )

        with self.assertRaisesRegex(ValueError, "every non-whitespace"):
            self._compile(
                local_results=local_results,
                reconciliation_plan=reconciliation_plan,
                group_id=group.group_id,
                candidate=candidate,
            )

    def test_replacement_rebuilds_a_globally_valid_whole_document(self) -> None:
        local_results = self._local_results(marked_model_indexes={1})
        reconciliation_plan = self._plan(local_results)
        group = reconciliation_plan.groups[0]
        # The first replacement parent deliberately crosses the A/B object
        # boundary; that is the authority which object-local turns never had.
        candidate = _resolved_candidate(
            _entry(
                D3CandidateSourceSpanV2(
                    start_segment_ordinal=0,
                    start_offset=0,
                    end_segment_ordinal=1,
                    end_offset=len(self.fixture.segments[1].source_text),
                )
            ),
            _entry_for(self.fixture.segments[2], ordinal=2),
        )
        reconciliation_result = self._compile(
            local_results=local_results,
            reconciliation_plan=reconciliation_plan,
            group_id=group.group_id,
            candidate=candidate,
        )

        aggregate = self._aggregate(
            local_results=tuple(reversed(local_results)),
            reconciliation_plan=reconciliation_plan,
            reconciliation_results=(reconciliation_result,),
        )

        self.assertEqual(
            aggregate.compiled_decision.status, ParentProvisionStatus.COMPLETE
        )
        self.assertEqual(len(aggregate.compiled_decision.entries), 4)
        self.assertEqual(
            aggregate.compiled_decision.entries[0].logical_object_ids,
            tuple(
                target.logical_object_id for target in self.fixture.model_targets[:2]
            ),
        )
        self.assertEqual(
            tuple(
                item.coverage_source for item in aggregate.ordered_model_target_coverage
            ),
            (
                ParentProvisionObjectCoverageSource.RECONCILIATION_REPLACEMENT,
                ParentProvisionObjectCoverageSource.RECONCILIATION_REPLACEMENT,
                ParentProvisionObjectCoverageSource.RECONCILIATION_REPLACEMENT,
                ParentProvisionObjectCoverageSource.LOCAL_RESULT,
                ParentProvisionObjectCoverageSource.LOCAL_RESULT,
            ),
        )
        self.assertEqual(
            aggregate.invalidated_local_result_sha256s,
            reconciliation_plan.invalidated_local_result_sha256s,
        )
        self.assertEqual(
            aggregate.global_candidate_sha256,
            aggregate.global_candidate.candidate_sha256,
        )

    def test_missing_reconciliation_result_keeps_aggregate_blocked(self) -> None:
        local_results = self._local_results(marked_model_indexes={1})
        reconciliation_plan = self._plan(local_results)

        with self.assertRaisesRegex(
            ParentProvisionObjectReconciliationError,
            "RESULT_SET_INCOMPLETE",
        ):
            self._aggregate(
                local_results=local_results,
                reconciliation_plan=reconciliation_plan,
                reconciliation_results=(),
            )

    def test_semantic_unresolved_is_scoped_and_compiled_by_the_host(self) -> None:
        local_results = self._local_results(marked_model_indexes={1})
        reconciliation_plan = self._plan(local_results)
        group = reconciliation_plan.groups[0]
        unresolved_object_id = self.fixture.model_targets[1].logical_object_id
        candidate = ParentProvisionObjectReconciliationCandidateV1(
            status=ParentProvisionObjectReconciliationStatus.UNRESOLVED,
            entries=(
                _entry_for(self.fixture.segments[0], ordinal=0),
                _entry_for(self.fixture.segments[2], ordinal=2),
            ),
            unresolved_logical_object_ids=(unresolved_object_id,),
            diagnostic_codes=("UNRESOLVED_PARENT_BOUNDARY",),
        )
        reconciliation_result = self._compile(
            local_results=local_results,
            reconciliation_plan=reconciliation_plan,
            group_id=group.group_id,
            candidate=candidate,
        )
        aggregate = self._aggregate(
            local_results=local_results,
            reconciliation_plan=reconciliation_plan,
            reconciliation_results=(reconciliation_result,),
        )

        self.assertEqual(
            reconciliation_result.compiled_decision.unresolved_logical_object_ids,
            (unresolved_object_id,),
        )
        self.assertEqual(
            aggregate.compiled_decision.status, ParentProvisionStatus.PARTIAL
        )
        self.assertEqual(
            aggregate.compiled_decision.unresolved_logical_object_ids,
            (unresolved_object_id,),
        )

    def test_overlapping_reconciliation_groups_are_rejected(self) -> None:
        local_results = self._local_results(marked_model_indexes={1})
        reconciliation_plan = self._plan(local_results)
        group = reconciliation_plan.groups[0]
        forged = reconciliation_plan.model_copy(
            update={
                "groups": (group, group),
                "invalidated_local_result_sha256s": (
                    *reconciliation_plan.invalidated_local_result_sha256s,
                    *reconciliation_plan.invalidated_local_result_sha256s,
                ),
            }
        )

        with self.assertRaisesRegex(
            ParentProvisionObjectReconciliationError,
            "GROUPS_OVERLAP",
        ):
            self._aggregate(
                local_results=local_results,
                reconciliation_plan=forged,
                reconciliation_results=(),
            )

    def test_completion_order_does_not_change_plan_or_aggregate(self) -> None:
        local_results = self._local_results(marked_model_indexes={1})
        forward_plan = self._plan(local_results)
        reverse_plan = self._plan(tuple(reversed(local_results)))
        self.assertEqual(forward_plan, reverse_plan)
        group = forward_plan.groups[0]
        candidate = _resolved_candidate(
            *(
                _entry_for(self.fixture.segments[index], ordinal=index)
                for index in group.global_segment_ordinals
            )
        )
        reconciliation_result = self._compile(
            local_results=local_results,
            reconciliation_plan=forward_plan,
            group_id=group.group_id,
            candidate=candidate,
        )

        forward = self._aggregate(
            local_results=local_results,
            reconciliation_plan=forward_plan,
            reconciliation_results=(reconciliation_result,),
        )
        reverse = self._aggregate(
            local_results=tuple(reversed(local_results)),
            reconciliation_plan=reverse_plan,
            reconciliation_results=(reconciliation_result,),
        )
        self.assertEqual(forward, reverse)

    def test_execution_failure_cannot_be_disguised_as_semantic_unresolved(self) -> None:
        for execution_failure in (
            "TRANSPORT_FAILED",
            "APP_SERVER_TIMEOUT",
            "AUTH_REQUIRED",
            "MODEL_UNAVAILABLE",
            "PROCESS_STATE_UNKNOWN",
        ):
            with self.subTest(execution_failure=execution_failure):
                with self.assertRaisesRegex(
                    ValueError,
                    "execution failure cannot be encoded",
                ):
                    ParentProvisionObjectReconciliationCandidateV1(
                        status=ParentProvisionObjectReconciliationStatus.UNRESOLVED,
                        unresolved_logical_object_ids=(
                            self.fixture.model_targets[0].logical_object_id,
                        ),
                        diagnostic_codes=(execution_failure,),
                    )

    def _local_results(
        self,
        *,
        marked_model_indexes: set[int],
    ) -> tuple[ParentProvisionObjectBatchCompilationV1, ...]:
        results: list[ParentProvisionObjectBatchCompilationV1] = []
        for model_index, target in enumerate(self.fixture.model_targets):
            if model_index in marked_model_indexes:
                candidate = ParentProvisionObjectCandidateV1(
                    scope_status=(
                        ParentProvisionObjectScopeStatus.ADJACENT_RECONCILIATION_REQUIRED
                    ),
                    diagnostic_codes=("CROSSES_ADJACENT_OBJECT",),
                )
            else:
                ordinal = target.segment_ordinals[0]
                candidate = ParentProvisionObjectCandidateV1(
                    scope_status=ParentProvisionObjectScopeStatus.CLOSED_WITHIN_OBJECT,
                    entries=(
                        _entry_for(self.fixture.segments[ordinal], ordinal=ordinal),
                    ),
                )
            results.append(
                compile_parent_provision_object_candidate(
                    plan=self.fixture.plan,
                    expected_plan_sha256=self.fixture.plan.plan_sha256,
                    batch_id=target.batch_id,
                    d2_identity=self.fixture.identity,
                    source_segments=self.fixture.segments,
                    kit_identity=self.fixture.kit,
                    candidate=candidate,
                )
            )
        return tuple(results)

    def _plan(
        self,
        local_results: tuple[ParentProvisionObjectBatchCompilationV1, ...],
    ):
        return build_parent_provision_object_reconciliation_plan(
            object_batch_plan=self.fixture.plan,
            expected_object_batch_plan_sha256=self.fixture.plan.plan_sha256,
            local_results=local_results,
            d2_identity=self.fixture.identity,
            source_segments=self.fixture.segments,
            kit_identity=self.fixture.kit,
        )

    def _compile(
        self,
        *,
        local_results: tuple[ParentProvisionObjectBatchCompilationV1, ...],
        reconciliation_plan,
        group_id: str,
        candidate: ParentProvisionObjectReconciliationCandidateV1,
    ):
        return compile_parent_provision_object_reconciliation_candidate(
            object_batch_plan=self.fixture.plan,
            expected_object_batch_plan_sha256=self.fixture.plan.plan_sha256,
            reconciliation_plan=reconciliation_plan,
            expected_reconciliation_plan_sha256=(
                reconciliation_plan.reconciliation_plan_sha256
            ),
            group_id=group_id,
            local_results=local_results,
            d2_identity=self.fixture.identity,
            source_segments=self.fixture.segments,
            kit_identity=self.fixture.kit,
            candidate=candidate,
        )

    def _aggregate(
        self,
        *,
        local_results: tuple[ParentProvisionObjectBatchCompilationV1, ...],
        reconciliation_plan,
        reconciliation_results,
    ):
        return aggregate_parent_provision_objects_with_reconciliation(
            object_batch_plan=self.fixture.plan,
            expected_object_batch_plan_sha256=self.fixture.plan.plan_sha256,
            reconciliation_plan=reconciliation_plan,
            expected_reconciliation_plan_sha256=(
                reconciliation_plan.reconciliation_plan_sha256
            ),
            local_results=local_results,
            reconciliation_results=reconciliation_results,
            d2_identity=self.fixture.identity,
            source_segments=self.fixture.segments,
            kit_identity=self.fixture.kit,
        )


def _resolved_candidate(
    *entries: D3ParentBoundaryClassificationEntryV2,
) -> ParentProvisionObjectReconciliationCandidateV1:
    return ParentProvisionObjectReconciliationCandidateV1(
        status=ParentProvisionObjectReconciliationStatus.RESOLVED_WITHIN_GROUP,
        entries=entries,
    )


def _entry_for(
    segment: D3ExactSourceSegmentV1,
    *,
    ordinal: int,
) -> D3ParentBoundaryClassificationEntryV2:
    return _entry(
        D3CandidateSourceSpanV2(
            start_segment_ordinal=ordinal,
            start_offset=0,
            end_segment_ordinal=ordinal,
            end_offset=len(segment.source_text),
        )
    )


def _entry(
    span: D3CandidateSourceSpanV2,
) -> D3ParentBoundaryClassificationEntryV2:
    return D3ParentBoundaryClassificationEntryV2(
        source_spans=(span,),
        requirementness=Requirementness.REQUIREMENT,
        requirement_kind=RequirementKind.FUNCTIONAL,
        technicality=RequirementTechnicality.TECHNICAL,
        content_role=ProvisionContentRole.PROVISION,
        confidence=0.93,
        reason_codes=("SOURCE_BOUND_CLASSIFICATION",),
    )


def _fixture() -> _Fixture:
    kit = AgentKitIdentityV1(
        kit_id="reqpilotx-document-extraction",
        kit_version="0.7.0",
        manifest_sha256="1" * 64,
        bundle_sha256="2" * 64,
        policy_id="reqpilotx-extraction-policy",
        policy_version="1.0.0",
        tool_registry_id="reqpilotx-document-evidence-tools",
        tool_registry_version="1.3.0",
    )
    source_texts = (
        "A shall initialize.\n",
        "B shall acquire.\n",
        "C shall validate.\n",
        "D shall publish.\n",
        "E shall retain.\n",
    )
    d2_segments = tuple(
        _d2_segment(index=index, source_text=source_text)
        for index, source_text in enumerate(source_texts)
    )
    object_ids = tuple(
        stable_d2_logical_object_id(
            source_sha256=SOURCE_SHA256,
            object_kind=D2LogicalObjectKind.PROVISION_SCOPE,
            source_segment_ids=(segment.segment_id,),
        )
        for segment in d2_segments
    )
    root_id = "d2-logical-object-" + "8" * 32
    section_id = "d2-logical-object-" + "9" * 32
    objects: list[D2LogicalObjectV1] = []
    root_values = {
        "logical_object_id": root_id,
        "object_kind": D2LogicalObjectKind.DOCUMENT_UNIT,
        "parent_logical_object_id": None,
        "source_order": 0,
        "page_numbers": (1, 2, 3, 4, 5),
        "source_segments": (),
        "logical_table_scope_ids": (),
        "reason_codes": ("synthetic_container",),
    }
    objects.append(
        D2LogicalObjectV1(
            **root_values,
            object_sha256=stable_d2_logical_object_sha256(**root_values),
        )
    )
    objects.append(_d2_object(d2_segments[0], object_ids[0], parent_id=root_id))
    section_values = {
        "logical_object_id": section_id,
        "object_kind": D2LogicalObjectKind.SECTION,
        "parent_logical_object_id": root_id,
        "source_order": 1,
        "page_numbers": (2,),
        "source_segments": (),
        "logical_table_scope_ids": (),
        "reason_codes": ("synthetic_container",),
    }
    objects.append(
        D2LogicalObjectV1(
            **section_values,
            object_sha256=stable_d2_logical_object_sha256(**section_values),
        )
    )
    objects.append(_d2_object(d2_segments[1], object_ids[1], parent_id=section_id))
    objects.extend(
        _d2_object(d2_segments[index], object_ids[index], parent_id=root_id)
        for index in range(2, len(d2_segments))
    )
    segments = tuple(
        _d3_segment(d2_segment, object_id, source_text)
        for d2_segment, object_id, source_text in zip(
            d2_segments,
            object_ids,
            source_texts,
            strict=True,
        )
    )
    identity = D3ValidatedD2ArtifactIdentityV1(
        artifact_id="agent-artifact-" + "4" * 32,
        artifact_sha256="5" * 64,
        validator_receipt_sha256="6" * 64,
        run_id=RUN_ID,
        source_sha256=SOURCE_SHA256,
        source_ir_id="source-ir-reconciliation",
        source_ir_canonical_sha256="7" * 64,
        kit_identity=kit,
        status=ParentProvisionStatus.COMPLETE,
        logical_object_ids=tuple(item.logical_object_id for item in objects),
        source_segment_ids=tuple(item.segment_id for item in segments),
        source_scope_sha256=d3_source_scope_sha256(segments),
    )
    plan = build_parent_provision_object_batch_plan(
        d2_identity=identity,
        logical_objects=tuple(objects),
        source_segments=segments,
        stage_input_bundle_sha256=STAGE_INPUT_BUNDLE_SHA256,
    )
    model_targets = tuple(
        item
        for item in plan.targets
        if item.execution_mode is ParentProvisionObjectExecutionMode.MODEL_TARGET
    )
    return _Fixture(
        kit=kit,
        identity=identity,
        objects=tuple(objects),
        segments=segments,
        plan=plan,
        model_targets=model_targets,
    )


def _d2_segment(*, index: int, source_text: str) -> D2SourceSegmentV1:
    source_handle = f"source-node-{index}"
    content_sha256 = _sha(source_text)
    segment_id = stable_d2_source_segment_id(
        source_sha256=SOURCE_SHA256,
        handle_kind=D2SourceHandleKind.SOURCE_NODE,
        source_handle=source_handle,
        start_offset=0,
        end_offset=len(source_text),
    )
    values = {
        "segment_id": segment_id,
        "source_sha256": SOURCE_SHA256,
        "handle_kind": D2SourceHandleKind.SOURCE_NODE,
        "source_handle": source_handle,
        "owner_source_node_id": source_handle,
        "source_order": index,
        "page_numbers": (index + 1,),
        "start_offset": 0,
        "end_offset": len(source_text),
        "source_content_sha256": content_sha256,
    }
    return D2SourceSegmentV1(
        **values,
        segment_sha256=stable_d2_source_segment_sha256(**values),
    )


def _d2_object(
    segment: D2SourceSegmentV1,
    logical_object_id: str,
    *,
    parent_id: str,
) -> D2LogicalObjectV1:
    values = {
        "logical_object_id": logical_object_id,
        "object_kind": D2LogicalObjectKind.PROVISION_SCOPE,
        "parent_logical_object_id": parent_id,
        "source_order": segment.source_order,
        "page_numbers": segment.page_numbers,
        "source_segments": (segment,),
        "logical_table_scope_ids": (),
        "reason_codes": ("synthetic_source",),
    }
    return D2LogicalObjectV1(
        **values,
        object_sha256=stable_d2_logical_object_sha256(**values),
    )


def _d3_segment(
    segment: D2SourceSegmentV1,
    logical_object_id: str,
    source_text: str,
) -> D3ExactSourceSegmentV1:
    return D3ExactSourceSegmentV1(
        segment_id=segment.segment_id,
        logical_object_id=logical_object_id,
        source_handle=segment.source_handle,
        handle_kind=segment.handle_kind.value,
        owner_source_node_id=segment.owner_source_node_id,
        source_order=segment.source_order,
        page_numbers=segment.page_numbers,
        handle_start_offset=segment.start_offset,
        handle_end_offset=segment.end_offset,
        source_text=source_text,
        source_text_sha256=_sha(source_text),
        source_content_sha256=segment.source_content_sha256,
        d2_segment_sha256=segment.segment_sha256,
        evidence_refs=(f"page-render-{segment.page_numbers[0]}",),
    )


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


if __name__ == "__main__":
    unittest.main()

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
    stable_d2_derived_container_id,
    stable_d2_logical_object_id,
    stable_d2_logical_object_sha256,
    stable_d2_source_segment_id,
    stable_d2_source_segment_sha256,
)
from app.application.agent_workflow.d3_parent_provision_compiler import (
    D3CandidateSourceSpanV2,
    D3ParentBoundaryClassificationCandidateV2,
    D3ParentBoundaryClassificationEntryV2,
    compile_d3_parent_provision_decision,
)
from app.application.agent_workflow.d3_parent_provisions import (
    D3ExactSourceSegmentV1,
    D3ValidatedD2ArtifactIdentityV1,
    ParentProvisionStatus,
    ProvisionContentRole,
    RequirementKind,
    RequirementTechnicality,
    Requirementness,
    canonical_json_sha256,
    d3_source_scope_sha256,
)
from app.application.agent_workflow.extraction_runtime import AgentKitIdentityV1
from app.application.agent_workflow.parent_provision_object_batches import (
    ParentProvisionObjectBatchError,
    ParentProvisionObjectBatchCompilationV1,
    ParentProvisionObjectBatchTargetV1,
    ParentProvisionObjectCandidateV1,
    ParentProvisionObjectExecutionMode,
    ParentProvisionObjectScopeStatus,
    aggregate_parent_provision_object_batches,
    build_parent_provision_object_batch_plan,
    compile_parent_provision_object_candidate,
    require_parent_provision_object_evidence_recovery,
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


class ParentProvisionObjectBatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = _fixture()
        self.plan = build_parent_provision_object_batch_plan(
            d2_identity=self.fixture.identity,
            logical_objects=self.fixture.objects,
            source_segments=self.fixture.segments,
            stage_input_bundle_sha256=STAGE_INPUT_BUNDLE_SHA256,
        )
        self.text_target = self.plan.targets[1]
        self.table_target = self.plan.targets[2]

    def test_plan_partitions_every_object_and_segment_deterministically(self) -> None:
        second = build_parent_provision_object_batch_plan(
            d2_identity=self.fixture.identity,
            logical_objects=self.fixture.objects,
            source_segments=self.fixture.segments,
            stage_input_bundle_sha256=STAGE_INPUT_BUNDLE_SHA256,
        )

        self.assertEqual(self.plan, second)
        legacy_payload = self.plan.model_dump(mode="json")
        self.assertNotIn("stage_input_manifest_sha256", legacy_payload)
        self.assertNotIn("runtime_adapter_sha256", legacy_payload)
        self.assertTrue(
            all(
                "stage_input_manifest_sha256" not in item
                and "runtime_adapter_sha256" not in item
                for item in legacy_payload["targets"]
            )
        )
        self.assertEqual(
            tuple(item.logical_object_id for item in self.plan.targets),
            self.fixture.identity.logical_object_ids,
        )
        self.assertEqual(
            tuple(item.batch_order for item in self.plan.targets),
            (0, 1, 2),
        )
        self.assertEqual(
            tuple(item.execution_mode for item in self.plan.targets),
            (
                ParentProvisionObjectExecutionMode.HOST_EMPTY_CONTAINER,
                ParentProvisionObjectExecutionMode.MODEL_TARGET,
                ParentProvisionObjectExecutionMode.MODEL_TARGET,
            ),
        )
        self.assertEqual(
            tuple(
                ordinal
                for target in self.plan.targets
                for ordinal in target.segment_ordinals
            ),
            (0, 1),
        )
        self.assertNotEqual(
            self.fixture.segments[0].source_order,
            self.text_target.segment_ordinals[0],
        )
        self.assertEqual(
            self.text_target.stage_input_bundle_sha256,
            STAGE_INPUT_BUNDLE_SHA256,
        )
        self.assertEqual(
            self.text_target.global_source_scope_sha256,
            self.fixture.identity.source_scope_sha256,
        )
        self.assertEqual(
            self.text_target.segment_bindings[0].source_text_sha256,
            self.fixture.segments[0].source_text_sha256,
        )
        self.assertEqual(
            self.text_target.segment_bindings[0].d2_segment_sha256,
            self.fixture.segments[0].d2_segment_sha256,
        )

        with self.assertRaisesRegex(
            ParentProvisionObjectBatchError,
            "PLAN_PIN_MISMATCH",
        ):
            self._compile(
                self.text_target.batch_id,
                _closed_candidate(_entry_for(self.fixture.segments[0], ordinal=0)),
                expected_plan_sha256="f" * 64,
            )

    def test_attachment_without_direct_evidence_requires_recovery(
        self,
    ) -> None:
        attachment_id = stable_d2_derived_container_id(
            source_sha256=SOURCE_SHA256,
            object_kind=D2LogicalObjectKind.ATTACHMENT,
            descendant_source_segment_ids=(),
            descendant_logical_table_scope_ids=(),
            parent_logical_object_id=self.fixture.objects[0].logical_object_id,
        )
        attachment_values = {
            "logical_object_id": attachment_id,
            "object_kind": D2LogicalObjectKind.ATTACHMENT,
            "parent_logical_object_id": self.fixture.objects[0].logical_object_id,
            "source_order": 42,
            "page_numbers": (2,),
            "source_segments": (),
            "logical_table_scope_ids": (),
            "reason_codes": ("synthetic_attachment",),
        }
        attachment = D2LogicalObjectV1(
            **attachment_values,
            object_sha256=stable_d2_logical_object_sha256(**attachment_values),
        )
        objects = (*self.fixture.objects, attachment)
        identity = self.fixture.identity.model_copy(
            update={
                "logical_object_ids": tuple(item.logical_object_id for item in objects)
            }
        )

        plan = build_parent_provision_object_batch_plan(
            d2_identity=identity,
            logical_objects=objects,
            source_segments=self.fixture.segments,
            stage_input_bundle_sha256=STAGE_INPUT_BUNDLE_SHA256,
        )

        self.assertEqual(
            plan.targets[-1].execution_mode,
            ParentProvisionObjectExecutionMode.RECOVERY_REQUIRED,
        )
        with self.assertRaisesRegex(
            ParentProvisionObjectBatchError,
            "EVIDENCE_RECOVERY_REQUIRED",
        ):
            aggregate_parent_provision_object_batches(
                plan=plan,
                expected_plan_sha256=plan.plan_sha256,
                results=(),
                d2_identity=identity,
                source_segments=self.fixture.segments,
                kit_identity=self.fixture.kit,
            )

    def test_ready_object_can_require_visual_recovery_without_losing_evidence(
        self,
    ) -> None:
        recovery_plan = require_parent_provision_object_evidence_recovery(
            plan=self.plan,
            expected_plan_sha256=self.plan.plan_sha256,
            logical_object_id=self.text_target.logical_object_id,
        )
        recovery_target = next(
            item
            for item in recovery_plan.targets
            if item.logical_object_id == self.text_target.logical_object_id
        )

        self.assertEqual(
            recovery_target.execution_mode,
            ParentProvisionObjectExecutionMode.RECOVERY_REQUIRED,
        )
        self.assertEqual(
            recovery_target.target_evidence_sha256,
            self.text_target.target_evidence_sha256,
        )
        self.assertNotEqual(
            recovery_target.target_scope_sha256,
            self.text_target.target_scope_sha256,
        )
        self.assertEqual(
            recovery_target.segment_bindings,
            self.text_target.segment_bindings,
        )
        with self.assertRaisesRegex(
            ParentProvisionObjectBatchError,
            "EVIDENCE_RECOVERY_REQUIRED",
        ):
            aggregate_parent_provision_object_batches(
                plan=recovery_plan,
                expected_plan_sha256=recovery_plan.plan_sha256,
                results=(),
                d2_identity=self.fixture.identity,
                source_segments=self.fixture.segments,
                kit_identity=self.fixture.kit,
            )

    def test_evidence_identity_survives_stage_input_republication(self) -> None:
        republished = build_parent_provision_object_batch_plan(
            d2_identity=self.fixture.identity,
            logical_objects=self.fixture.objects,
            source_segments=self.fixture.segments,
            stage_input_bundle_sha256="c" * 64,
        )

        self.assertEqual(
            tuple(item.target_evidence_sha256 for item in self.plan.targets),
            tuple(item.target_evidence_sha256 for item in republished.targets),
        )
        self.assertNotEqual(
            tuple(item.target_scope_sha256 for item in self.plan.targets),
            tuple(item.target_scope_sha256 for item in republished.targets),
        )

    def test_zero_evidence_attachment_figure_and_table_targets_require_recovery(
        self,
    ) -> None:
        parent_id = self.fixture.objects[0].logical_object_id
        object_ids = (
            "d2-logical-object-" + "8" * 32,
            "d2-logical-object-" + "9" * 32,
            "agent-logical-table-" + "a" * 32,
        )
        kinds = (
            D2LogicalObjectKind.ATTACHMENT,
            D2LogicalObjectKind.FIGURE,
            D2LogicalObjectKind.TABLE,
        )
        for batch_order, (object_kind, object_id) in enumerate(
            zip(kinds, object_ids, strict=True)
        ):
            evidence_sha256 = canonical_json_sha256(
                {
                    "object_kind": object_kind.value,
                    "parent_logical_object_id": parent_id,
                    "segment_bindings": [],
                }
            )
            scope_sha256 = canonical_json_sha256(
                {
                    "batch_order": batch_order,
                    "logical_object_id": object_id,
                    "stage_input_bundle_sha256": STAGE_INPUT_BUNDLE_SHA256,
                    "global_source_scope_sha256": (
                        self.fixture.identity.source_scope_sha256
                    ),
                    "target_evidence_sha256": evidence_sha256,
                    "execution_mode": (
                        ParentProvisionObjectExecutionMode.RECOVERY_REQUIRED.value
                    ),
                }
            )
            target = ParentProvisionObjectBatchTargetV1(
                batch_id=f"parent-provision-object-batch-{scope_sha256[:32]}",
                batch_order=batch_order,
                logical_object_id=object_id,
                object_kind=object_kind,
                parent_logical_object_id=parent_id,
                stage_input_bundle_sha256=STAGE_INPUT_BUNDLE_SHA256,
                global_source_scope_sha256=(self.fixture.identity.source_scope_sha256),
                segment_ordinals=(),
                segment_bindings=(),
                execution_mode=ParentProvisionObjectExecutionMode.RECOVERY_REQUIRED,
                target_evidence_sha256=evidence_sha256,
                target_scope_sha256=scope_sha256,
            )

            self.assertEqual(
                target.execution_mode,
                ParentProvisionObjectExecutionMode.RECOVERY_REQUIRED,
            )

    def test_local_compile_closes_one_object_or_defers_only_that_object(self) -> None:
        closed = self._compile(
            self.text_target.batch_id,
            _closed_candidate(_entry_for(self.fixture.segments[0], ordinal=0)),
        )
        unresolved = self._compile(
            self.table_target.batch_id,
            ParentProvisionObjectCandidateV1(
                scope_status=ParentProvisionObjectScopeStatus.UNRESOLVED,
                diagnostic_codes=("BOUNDARY_AMBIGUOUS",),
            ),
        )

        self.assertIsNotNone(closed.compiled_decision)
        assert closed.compiled_decision is not None
        self.assertEqual(
            closed.compiled_decision.declared_logical_object_ids,
            (self.text_target.logical_object_id,),
        )
        self.assertEqual(closed.compiled_decision.unresolved_logical_object_ids, ())
        self.assertEqual(
            closed.compiled_decision.entries[0].source_ranges[0].verbatim_text,
            self.fixture.segments[0].source_text,
        )
        self.assertIsNotNone(unresolved.compiled_decision)
        assert unresolved.compiled_decision is not None
        self.assertEqual(
            unresolved.compiled_decision.unresolved_logical_object_ids,
            (self.table_target.logical_object_id,),
        )
        self.assertEqual(unresolved.compiled_decision.entries, ())

    def test_local_compile_rejects_cross_object_and_incomplete_spans(self) -> None:
        cross_object = _closed_candidate(
            _entry(
                D3CandidateSourceSpanV2(
                    start_segment_ordinal=0,
                    start_offset=0,
                    end_segment_ordinal=1,
                    end_offset=len(self.fixture.segments[1].source_text),
                )
            )
        )
        with self.assertRaisesRegex(
            ParentProvisionObjectBatchError,
            "SPAN_ESCAPES_TARGET",
        ):
            self._compile(self.text_target.batch_id, cross_object)

        incomplete = _closed_candidate(
            _entry(
                D3CandidateSourceSpanV2(
                    start_segment_ordinal=0,
                    start_offset=1,
                    end_segment_ordinal=0,
                    end_offset=len(self.fixture.segments[0].source_text),
                )
            )
        )
        with self.assertRaisesRegex(ValueError, "every non-whitespace"):
            self._compile(self.text_target.batch_id, incomplete)

    def test_local_compile_accepts_zero_offset_boundary_at_next_object(self) -> None:
        half_open = _closed_candidate(
            _entry(
                D3CandidateSourceSpanV2(
                    start_segment_ordinal=0,
                    start_offset=0,
                    end_segment_ordinal=1,
                    end_offset=0,
                )
            )
        )

        compiled = self._compile(self.text_target.batch_id, half_open)

        assert compiled.compiled_decision is not None
        self.assertEqual(
            tuple(
                item.segment_id
                for item in compiled.compiled_decision.entries[0].source_ranges
            ),
            (self.fixture.segments[0].segment_id,),
        )
        self.assertEqual(
            compiled.compiled_decision.entries[0].source_ranges[0].verbatim_text,
            self.fixture.segments[0].source_text,
        )

    def test_aggregate_is_completion_order_independent_and_reuses_global_compiler(
        self,
    ) -> None:
        text_candidate = _closed_candidate(
            _entry_for(self.fixture.segments[0], ordinal=0)
        )
        table_candidate = _closed_candidate(
            _entry_for(
                self.fixture.segments[1],
                ordinal=1,
                content_role=ProvisionContentRole.TABLE_RECORD,
            )
        )
        text_result = self._compile(self.text_target.batch_id, text_candidate)
        table_result = self._compile(self.table_target.batch_id, table_candidate)

        aggregate = self._aggregate((table_result, text_result))
        direct = compile_d3_parent_provision_decision(
            d2_identity=self.fixture.identity,
            source_segments=self.fixture.segments,
            kit_identity=self.fixture.kit,
            candidate=D3ParentBoundaryClassificationCandidateV2(
                entries=(*text_candidate.entries, *table_candidate.entries),
            ),
        )

        self.assertEqual(aggregate.compiled_decision, direct)
        self.assertEqual(
            tuple(item.content_role for item in aggregate.compiled_decision.entries),
            (ProvisionContentRole.PROVISION, ProvisionContentRole.TABLE_RECORD),
        )
        self.assertEqual(
            aggregate.ordered_batch_result_sha256s,
            (text_result.result_sha256, table_result.result_sha256),
        )

    def test_reconciliation_required_blocks_global_finalization(self) -> None:
        reconciliation = self._compile(
            self.text_target.batch_id,
            ParentProvisionObjectCandidateV1(
                scope_status=(
                    ParentProvisionObjectScopeStatus.ADJACENT_RECONCILIATION_REQUIRED
                ),
                diagnostic_codes=("CROSSES_ADJACENT_OBJECT",),
            ),
        )
        table_result = self._compile(
            self.table_target.batch_id,
            _closed_candidate(
                _entry_for(
                    self.fixture.segments[1],
                    ordinal=1,
                    content_role=ProvisionContentRole.TABLE_RECORD,
                )
            ),
        )

        self.assertIsNone(reconciliation.compiled_decision)
        with self.assertRaisesRegex(
            ParentProvisionObjectBatchError,
            "ADJACENT_RECONCILIATION_REQUIRED",
        ):
            self._aggregate((reconciliation, table_result))

    def test_aggregate_rejects_missing_duplicate_and_stale_results(self) -> None:
        text_result = self._compile(
            self.text_target.batch_id,
            _closed_candidate(_entry_for(self.fixture.segments[0], ordinal=0)),
        )
        table_result = self._compile(
            self.table_target.batch_id,
            _closed_candidate(
                _entry_for(
                    self.fixture.segments[1],
                    ordinal=1,
                    content_role=ProvisionContentRole.TABLE_RECORD,
                )
            ),
        )

        with self.assertRaisesRegex(
            ParentProvisionObjectBatchError,
            "RESULT_SET_INCOMPLETE",
        ):
            self._aggregate((text_result,))
        with self.assertRaisesRegex(
            ParentProvisionObjectBatchError,
            "DUPLICATE_RESULT",
        ):
            self._aggregate((text_result, text_result))

        stale = table_result.model_copy(update={"plan_sha256": "f" * 64})
        with self.assertRaises(ValueError):
            self._aggregate((text_result, stale))

        cross_candidate = _closed_candidate(
            _entry(
                D3CandidateSourceSpanV2(
                    start_segment_ordinal=0,
                    start_offset=0,
                    end_segment_ordinal=1,
                    end_offset=len(self.fixture.segments[1].source_text),
                )
            )
        )
        forged_payload = text_result.model_dump(mode="json")
        forged_payload["model_candidate"] = cross_candidate.model_dump(mode="json")
        forged_payload["model_candidate_sha256"] = cross_candidate.candidate_sha256
        forged_payload["result_sha256"] = canonical_json_sha256(
            {
                key: value
                for key, value in forged_payload.items()
                if key != "result_sha256"
            }
        )
        forged = ParentProvisionObjectBatchCompilationV1.model_validate(forged_payload)
        with self.assertRaisesRegex(
            ParentProvisionObjectBatchError,
            "SPAN_ESCAPES_TARGET",
        ):
            self._aggregate((forged, table_result))

    def _compile(
        self,
        batch_id: str,
        candidate: ParentProvisionObjectCandidateV1,
        *,
        expected_plan_sha256: str | None = None,
    ):
        return compile_parent_provision_object_candidate(
            plan=self.plan,
            expected_plan_sha256=(
                self.plan.plan_sha256
                if expected_plan_sha256 is None
                else expected_plan_sha256
            ),
            batch_id=batch_id,
            d2_identity=self.fixture.identity,
            source_segments=self.fixture.segments,
            kit_identity=self.fixture.kit,
            candidate=candidate,
        )

    def _aggregate(self, results):  # type: ignore[no-untyped-def]
        return aggregate_parent_provision_object_batches(
            plan=self.plan,
            expected_plan_sha256=self.plan.plan_sha256,
            results=results,
            d2_identity=self.fixture.identity,
            source_segments=self.fixture.segments,
            kit_identity=self.fixture.kit,
        )


def _closed_candidate(
    entry: D3ParentBoundaryClassificationEntryV2,
) -> ParentProvisionObjectCandidateV1:
    return ParentProvisionObjectCandidateV1(
        scope_status=ParentProvisionObjectScopeStatus.CLOSED_WITHIN_OBJECT,
        entries=(entry,),
    )


def _entry_for(
    segment: D3ExactSourceSegmentV1,
    *,
    ordinal: int,
    content_role: ProvisionContentRole = ProvisionContentRole.PROVISION,
) -> D3ParentBoundaryClassificationEntryV2:
    return _entry(
        D3CandidateSourceSpanV2(
            start_segment_ordinal=ordinal,
            start_offset=0,
            end_segment_ordinal=ordinal,
            end_offset=len(segment.source_text),
        ),
        content_role=content_role,
    )


def _entry(
    span: D3CandidateSourceSpanV2,
    *,
    content_role: ProvisionContentRole = ProvisionContentRole.PROVISION,
) -> D3ParentBoundaryClassificationEntryV2:
    return D3ParentBoundaryClassificationEntryV2(
        source_spans=(span,),
        requirementness=Requirementness.REQUIREMENT,
        requirement_kind=RequirementKind.FUNCTIONAL,
        technicality=RequirementTechnicality.TECHNICAL,
        content_role=content_role,
        confidence=0.93,
        reason_codes=("SOURCE_BOUND_CLASSIFICATION",),
    )


def _fixture() -> _Fixture:
    kit = AgentKitIdentityV1(
        kit_id="reqpilotx-document-extraction",
        kit_version="0.6.0",
        manifest_sha256="1" * 64,
        bundle_sha256="2" * 64,
        policy_id="reqpilotx-extraction-policy",
        policy_version="1.0.0",
        tool_registry_id="reqpilotx-document-evidence-tools",
        tool_registry_version="1.3.0",
    )
    first_text = "😀 Controller shall stop.\r\n"
    second_text = "Table record A"
    first_segment = _d2_segment(
        source_handle="source-node-text",
        source_order=17,
        page_number=1,
        source_text=first_text,
    )
    second_segment = _d2_segment(
        source_handle="source-node-table",
        source_order=41,
        page_number=2,
        source_text=second_text,
    )
    first_object_id = stable_d2_logical_object_id(
        source_sha256=SOURCE_SHA256,
        object_kind=D2LogicalObjectKind.PROVISION_SCOPE,
        source_segment_ids=(first_segment.segment_id,),
    )
    table_object_id = "agent-logical-table-" + "3" * 32
    root_object_id = stable_d2_derived_container_id(
        source_sha256=SOURCE_SHA256,
        object_kind=D2LogicalObjectKind.DOCUMENT_UNIT,
        descendant_source_segment_ids=(
            first_segment.segment_id,
            second_segment.segment_id,
        ),
        descendant_logical_table_scope_ids=(table_object_id,),
        parent_logical_object_id=None,
    )
    first_values = {
        "logical_object_id": first_object_id,
        "object_kind": D2LogicalObjectKind.PROVISION_SCOPE,
        "parent_logical_object_id": root_object_id,
        "source_order": first_segment.source_order,
        "page_numbers": first_segment.page_numbers,
        "source_segments": (first_segment,),
        "logical_table_scope_ids": (),
        "reason_codes": ("synthetic_source",),
    }
    table_values = {
        "logical_object_id": table_object_id,
        "object_kind": D2LogicalObjectKind.TABLE,
        "parent_logical_object_id": root_object_id,
        "source_order": second_segment.source_order,
        "page_numbers": second_segment.page_numbers,
        "source_segments": (second_segment,),
        "logical_table_scope_ids": (table_object_id,),
        "reason_codes": ("synthetic_table",),
    }
    root_values = {
        "logical_object_id": root_object_id,
        "object_kind": D2LogicalObjectKind.DOCUMENT_UNIT,
        "parent_logical_object_id": None,
        "source_order": first_segment.source_order,
        "page_numbers": (1, 2),
        "source_segments": (),
        "logical_table_scope_ids": (),
        "reason_codes": ("synthetic_container",),
    }
    objects = (
        D2LogicalObjectV1(
            **root_values,
            object_sha256=stable_d2_logical_object_sha256(**root_values),
        ),
        D2LogicalObjectV1(
            **first_values,
            object_sha256=stable_d2_logical_object_sha256(**first_values),
        ),
        D2LogicalObjectV1(
            **table_values,
            object_sha256=stable_d2_logical_object_sha256(**table_values),
        ),
    )
    segments = (
        _d3_segment(first_segment, first_object_id, first_text),
        _d3_segment(second_segment, table_object_id, second_text),
    )
    identity = D3ValidatedD2ArtifactIdentityV1(
        artifact_id="agent-artifact-" + "4" * 32,
        artifact_sha256="5" * 64,
        validator_receipt_sha256="6" * 64,
        run_id=RUN_ID,
        source_sha256=SOURCE_SHA256,
        source_ir_id="source-ir-synthetic",
        source_ir_canonical_sha256="7" * 64,
        kit_identity=kit,
        status=ParentProvisionStatus.COMPLETE,
        logical_object_ids=tuple(item.logical_object_id for item in objects),
        source_segment_ids=tuple(item.segment_id for item in segments),
        source_scope_sha256=d3_source_scope_sha256(segments),
    )
    return _Fixture(kit=kit, identity=identity, objects=objects, segments=segments)


def _d2_segment(
    *,
    source_handle: str,
    source_order: int,
    page_number: int,
    source_text: str,
) -> D2SourceSegmentV1:
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
        "source_order": source_order,
        "page_numbers": (page_number,),
        "start_offset": 0,
        "end_offset": len(source_text),
        "source_content_sha256": content_sha256,
    }
    return D2SourceSegmentV1(
        **values,
        segment_sha256=stable_d2_source_segment_sha256(**values),
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

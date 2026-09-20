from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import json
import unittest

from pydantic import ValidationError

from app.adapters.agents.codex_app_server.canonical_stage_driver import (
    D3ParentProvisionDecisionCandidateV1 as DriverD3DecisionCandidateV1,
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
    d3_source_scope_sha256,
    parent_provision_evidence_refs,
    parent_provision_decision_candidate_sha256,
    parent_provision_decision_sha256,
    stable_parent_provision_id,
)
from app.application.agent_workflow.extraction_runtime import AgentKitIdentityV1


class D3ParentProvisionCompilerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.kit = _kit_identity()
        self.segments = _source_segments()
        self.d2 = _d2_identity(self.kit, self.segments)

    def test_model_wire_contains_only_ranges_and_professional_classification(
        self,
    ) -> None:
        schema = D3ParentBoundaryClassificationCandidateV2.model_json_schema()
        serialized = json.dumps(schema, sort_keys=True)

        self.assertEqual(
            set(schema["properties"]),
            {"schema_version", "entries", "unresolved_logical_object_ids"},
        )
        for forbidden in (
            "run_id",
            "source_sha256",
            "kit_identity",
            "parent_provision_id",
            "source_order",
            "logical_object_ids",
            "verbatim_text",
            "verbatim_text_sha256",
            "evidence_refs",
            "policy_attestation",
        ):
            self.assertNotIn(f'"{forbidden}"', serialized)

        payload = _candidate(self.segments).model_dump(mode="json")
        payload["run_id"] = self.d2.run_id
        with self.assertRaisesRegex(ValidationError, "Extra inputs"):
            D3ParentBoundaryClassificationCandidateV2.model_validate(payload)

        payload = _candidate(self.segments).model_dump(mode="json")
        payload["entries"][0]["parent_provision_id"] = "parent-provision-" + "0" * 32
        with self.assertRaisesRegex(ValidationError, "Extra inputs"):
            D3ParentBoundaryClassificationCandidateV2.model_validate(payload)

        payload = _candidate(self.segments).model_dump(mode="json")
        payload["entries"][0]["source_spans"][0]["verbatim_text"] = "invented"
        with self.assertRaisesRegex(ValidationError, "Extra inputs"):
            D3ParentBoundaryClassificationCandidateV2.model_validate(payload)

    def test_compiler_does_not_duplicate_high_cardinality_segment_evidence(
        self,
    ) -> None:
        first = self.segments[0].model_copy(
            update={
                "evidence_refs": tuple(
                    sorted(f"cell-evidence-{index}" for index in range(256))
                )
            }
        )
        segments = (first, self.segments[1])
        identity = _d2_identity(self.kit, segments)

        decision = compile_d3_parent_provision_decision(
            d2_identity=identity,
            source_segments=segments,
            kit_identity=self.kit,
            candidate=_candidate(segments),
        )

        self.assertEqual(decision.entries[0].evidence_refs, (first.logical_object_id,))
        refs = parent_provision_evidence_refs(
            declared_logical_object_ids=decision.declared_logical_object_ids,
            unresolved_logical_object_ids=decision.unresolved_logical_object_ids,
            entries=decision.entries,
        )
        self.assertIn(first.segment_id, refs)

    def test_host_derives_canonical_attestation_ready_decision(self) -> None:
        candidate = _candidate(self.segments)
        decision = compile_d3_parent_provision_decision(
            d2_identity=self.d2,
            source_segments=self.segments,
            kit_identity=self.kit,
            candidate=candidate,
        )

        self.assertEqual(decision.run_id, self.d2.run_id)
        self.assertEqual(decision.source_sha256, self.d2.source_sha256)
        self.assertEqual(decision.kit_identity, self.kit)
        self.assertEqual(decision.predecessor_d2_artifact_id, self.d2.artifact_id)
        self.assertEqual(
            decision.predecessor_d2_artifact_sha256,
            self.d2.artifact_sha256,
        )
        self.assertEqual(decision.status, ParentProvisionStatus.COMPLETE)
        self.assertEqual(
            decision.declared_logical_object_ids,
            self.d2.logical_object_ids,
        )
        self.assertEqual(decision.unresolved_logical_object_ids, ())
        self.assertEqual(decision.diagnostic_codes, ())
        self.assertEqual(
            tuple(entry.source_order for entry in decision.entries),
            (0, 1),
        )

        first = decision.entries[0]
        first_segment = self.segments[0]
        first_range = first.source_ranges[0]
        self.assertEqual(first.logical_object_ids, (first_segment.logical_object_id,))
        self.assertIsNone(first.explicit_label)
        self.assertEqual(first.ancestor_labels, ())
        self.assertEqual(first_range.verbatim_text, first_segment.source_text)
        self.assertEqual(
            first_range.verbatim_text_sha256,
            _sha(first_segment.source_text),
        )
        self.assertEqual(
            first.parent_provision_id,
            stable_parent_provision_id(
                source_sha256=self.d2.source_sha256,
                d2_artifact_sha256=self.d2.artifact_sha256,
                logical_object_ids=first.logical_object_ids,
                source_ranges=first.source_ranges,
            ),
        )
        self.assertEqual(
            first.evidence_refs,
            tuple(
                sorted(
                    {
                        first_segment.logical_object_id,
                        first_segment.segment_id,
                        first_segment.source_handle,
                        *first_segment.evidence_refs,
                    }
                )
            ),
        )
        professional_refs = parent_provision_evidence_refs(
            declared_logical_object_ids=decision.declared_logical_object_ids,
            unresolved_logical_object_ids=decision.unresolved_logical_object_ids,
            entries=decision.entries,
        )
        self.assertIn(first_segment.segment_id, professional_refs)
        self.assertIn(first_segment.logical_object_id, professional_refs)
        self.assertEqual(
            decision.decision_sha256,
            parent_provision_decision_sha256(
                run_id=decision.run_id,
                source_sha256=decision.source_sha256,
                kit_identity=decision.kit_identity,
                predecessor_d2_artifact_id=decision.predecessor_d2_artifact_id,
                predecessor_d2_artifact_sha256=(
                    decision.predecessor_d2_artifact_sha256
                ),
                status=decision.status,
                declared_logical_object_ids=decision.declared_logical_object_ids,
                unresolved_logical_object_ids=decision.unresolved_logical_object_ids,
                entries=decision.entries,
                diagnostic_codes=decision.diagnostic_codes,
            ),
        )
        self.assertEqual(
            decision.decision_candidate_sha256,
            parent_provision_decision_candidate_sha256(
                run_id=decision.run_id,
                source_sha256=decision.source_sha256,
                kit_identity=decision.kit_identity,
                predecessor_d2_artifact_id=decision.predecessor_d2_artifact_id,
                predecessor_d2_artifact_sha256=(
                    decision.predecessor_d2_artifact_sha256
                ),
                status=decision.status,
                declared_logical_object_ids=decision.declared_logical_object_ids,
                unresolved_logical_object_ids=decision.unresolved_logical_object_ids,
                entries=decision.entries,
                diagnostic_codes=decision.diagnostic_codes,
            ),
        )

        # The existing driver wire can consume the Host result without inventing
        # a second transformation or changing attestation hash semantics.
        driver_wire = DriverD3DecisionCandidateV1.model_validate(
            decision.model_dump(mode="json")
        )
        self.assertEqual(driver_wire.decision_sha256, decision.decision_sha256)

    def test_cross_segment_span_expands_to_exact_final_ranges(self) -> None:
        candidate = D3ParentBoundaryClassificationCandidateV2(
            entries=(
                _entry(
                    (
                        D3CandidateSourceSpanV2(
                            start_segment_ordinal=0,
                            start_offset=0,
                            end_segment_ordinal=1,
                            end_offset=len(self.segments[1].source_text),
                        ),
                    ),
                ),
            )
        )

        decision = self._compile(candidate)
        entry = decision.entries[0]

        self.assertEqual(len(entry.source_ranges), 2)
        self.assertEqual(
            tuple(item.verbatim_text for item in entry.source_ranges),
            tuple(item.source_text for item in self.segments),
        )
        self.assertEqual(entry.logical_object_ids, self.d2.logical_object_ids)

    def test_later_zero_end_offset_forms_a_half_open_parent_boundary(self) -> None:
        candidate = D3ParentBoundaryClassificationCandidateV2(
            entries=(
                _entry(
                    (
                        D3CandidateSourceSpanV2(
                            start_segment_ordinal=0,
                            start_offset=0,
                            end_segment_ordinal=1,
                            end_offset=0,
                        ),
                    ),
                ),
                _entry(
                    (_full_range(self.segments[1], ordinal=1),),
                    requirementness=Requirementness.NON_REQUIREMENT,
                    requirement_kind=None,
                    technicality=None,
                    content_role=ProvisionContentRole.CONTEXT,
                ),
            )
        )

        decision = self._compile(candidate)

        self.assertEqual(len(decision.entries[0].source_ranges), 1)
        self.assertEqual(
            decision.entries[0].source_ranges[0].segment_id,
            self.segments[0].segment_id,
        )

    def test_discontinuous_parent_can_surround_nested_table_entries(self) -> None:
        tail_text = "Requirement continuation after nested table."
        tail = self.segments[1].model_copy(
            update={
                "segment_id": "d2-source-segment-" + "3" * 32,
                "logical_object_id": "d2-logical-object-" + "3" * 32,
                "source_handle": "node-" + "3" * 32,
                "owner_source_node_id": "node-" + "3" * 32,
                "source_order": 30,
                "page_numbers": (3,),
                "handle_end_offset": len(tail_text),
                "source_text": tail_text,
                "source_text_sha256": _sha(tail_text),
                "source_content_sha256": _sha(tail_text),
                "d2_segment_sha256": "3" * 64,
                "evidence_refs": ("page-render-3",),
            }
        )
        segments = (*self.segments, tail)
        identity = _d2_identity(self.kit, segments)
        candidate = D3ParentBoundaryClassificationCandidateV2(
            entries=(
                _entry(
                    (
                        _full_range(segments[0], ordinal=0),
                        _full_range(segments[2], ordinal=2),
                    )
                ),
                _entry(
                    (_full_range(segments[1], ordinal=1),),
                    requirementness=Requirementness.NON_REQUIREMENT,
                    requirement_kind=None,
                    technicality=None,
                    content_role=ProvisionContentRole.TABLE_RECORD,
                ),
            )
        )

        decision = compile_d3_parent_provision_decision(
            d2_identity=identity,
            source_segments=segments,
            kit_identity=self.kit,
            candidate=candidate,
        )

        self.assertEqual(
            tuple(item.segment_id for item in decision.entries[0].source_ranges),
            (segments[0].segment_id, segments[2].segment_id),
        )
        self.assertEqual(
            decision.entries[1].source_ranges[0].segment_id,
            segments[1].segment_id,
        )

    def test_unknown_out_of_bounds_and_overlapping_spans_fail_closed(self) -> None:
        unknown = _candidate(
            self.segments,
            first_spans=(
                D3CandidateSourceSpanV2(
                    start_segment_ordinal=len(self.segments),
                    start_offset=0,
                    end_segment_ordinal=len(self.segments),
                    end_offset=1,
                ),
            ),
        )
        with self.assertRaisesRegex(ValueError, "unknown segment"):
            self._compile(unknown)

        out_of_bounds = _candidate(
            self.segments,
            first_spans=(
                D3CandidateSourceSpanV2(
                    start_segment_ordinal=0,
                    start_offset=0,
                    end_segment_ordinal=0,
                    end_offset=len(self.segments[0].source_text) + 1,
                ),
            ),
        )
        with self.assertRaisesRegex(ValueError, "exceeds"):
            self._compile(out_of_bounds)

        first = self.segments[0]
        overlapping = D3ParentBoundaryClassificationCandidateV2(
            entries=(
                _entry(
                    (
                        D3CandidateSourceSpanV2(
                            start_segment_ordinal=0,
                            start_offset=0,
                            end_segment_ordinal=0,
                            end_offset=len(first.source_text),
                        ),
                    ),
                ),
                _entry(
                    (
                        D3CandidateSourceSpanV2(
                            start_segment_ordinal=0,
                            start_offset=1,
                            end_segment_ordinal=0,
                            end_offset=len(first.source_text),
                        ),
                    ),
                    requirementness=Requirementness.NON_REQUIREMENT,
                    requirement_kind=None,
                    technicality=None,
                    content_role=ProvisionContentRole.CONTEXT,
                ),
                _entry(
                    (_full_range(self.segments[1], ordinal=1),),
                    requirementness=Requirementness.NON_REQUIREMENT,
                    requirement_kind=None,
                    technicality=None,
                    content_role=ProvisionContentRole.CONTEXT,
                ),
            )
        )
        with self.assertRaisesRegex(ValueError, "overlap"):
            self._compile(overlapping)

    def test_order_drift_and_incomplete_source_disposition_fail_closed(self) -> None:
        reversed_entries = D3ParentBoundaryClassificationCandidateV2(
            entries=(
                _entry(
                    (_full_range(self.segments[1], ordinal=1),),
                    requirementness=Requirementness.NON_REQUIREMENT,
                    requirement_kind=None,
                    technicality=None,
                    content_role=ProvisionContentRole.CONTEXT,
                ),
                _entry((_full_range(self.segments[0], ordinal=0),)),
            )
        )
        with self.assertRaisesRegex(ValueError, "global D2 source order"):
            self._compile(reversed_entries)

        first = self.segments[0]
        incomplete = _candidate(
            self.segments,
            first_spans=(
                D3CandidateSourceSpanV2(
                    start_segment_ordinal=0,
                    start_offset=0,
                    end_segment_ordinal=0,
                    end_offset=len(first.source_text) - 2,
                ),
            ),
        )
        with self.assertRaisesRegex(ValueError, "every non-whitespace"):
            self._compile(incomplete)

    def test_kit_and_validated_source_scope_drift_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "Kit identity drifted"):
            compile_d3_parent_provision_decision(
                d2_identity=self.d2,
                source_segments=self.segments,
                kit_identity=self.kit.model_copy(update={"bundle_sha256": "9" * 64}),
                candidate=_candidate(self.segments),
            )

        with self.assertRaisesRegex(ValueError, "validated D2 order"):
            compile_d3_parent_provision_decision(
                d2_identity=self.d2,
                source_segments=tuple(reversed(self.segments)),
                kit_identity=self.kit,
                candidate=_candidate(self.segments),
            )

        drifted = (
            self.segments[0].model_copy(update={"evidence_refs": ("changed",)}),
            self.segments[1],
        )
        with self.assertRaisesRegex(ValueError, "source scope drifted"):
            compile_d3_parent_provision_decision(
                d2_identity=self.d2,
                source_segments=drifted,
                kit_identity=self.kit,
                candidate=_candidate(self.segments),
            )

        text_drifted = (
            self.segments[0].model_copy(
                update={"source_text": "X" + self.segments[0].source_text[1:]}
            ),
            self.segments[1],
        )
        with self.assertRaisesRegex(ValidationError, "text hash mismatch"):
            compile_d3_parent_provision_decision(
                d2_identity=self.d2,
                source_segments=text_drifted,
                kit_identity=self.kit,
                candidate=_candidate(self.segments),
            )

    def test_unresolved_classification_and_partial_d2_are_host_diagnostics(
        self,
    ) -> None:
        unresolved_candidate = D3ParentBoundaryClassificationCandidateV2(
            entries=(
                _entry(
                    (_full_range(self.segments[0], ordinal=0),),
                    requirementness=Requirementness.UNRESOLVED,
                    requirement_kind=RequirementKind.UNRESOLVED,
                    technicality=RequirementTechnicality.UNRESOLVED,
                ),
                _entry(
                    (_full_range(self.segments[1], ordinal=1),),
                    requirementness=Requirementness.NON_REQUIREMENT,
                    requirement_kind=None,
                    technicality=None,
                    content_role=ProvisionContentRole.CONTEXT,
                ),
            )
        )
        unresolved = self._compile(unresolved_candidate)
        self.assertEqual(unresolved.status, ParentProvisionStatus.PARTIAL)
        self.assertEqual(
            unresolved.diagnostic_codes,
            ("UNRESOLVED_REQUIREMENTNESS",),
        )

        partial_d2 = self.d2.model_copy(
            update={"status": ParentProvisionStatus.PARTIAL}
        )
        partial = compile_d3_parent_provision_decision(
            d2_identity=partial_d2,
            source_segments=self.segments,
            kit_identity=self.kit,
            candidate=_candidate(self.segments),
        )
        self.assertEqual(partial.status, ParentProvisionStatus.PARTIAL)
        self.assertEqual(partial.diagnostic_codes, ("PREDECESSOR_D2_PARTIAL",))

    def test_explicit_unresolved_object_may_defer_only_its_exact_scope(self) -> None:
        candidate = D3ParentBoundaryClassificationCandidateV2(
            entries=(_candidate(self.segments).entries[0],),
            unresolved_logical_object_ids=(self.d2.logical_object_ids[1],),
        )

        compiled = self._compile(candidate)

        self.assertEqual(compiled.status, ParentProvisionStatus.PARTIAL)
        self.assertEqual(
            compiled.declared_logical_object_ids,
            (self.d2.logical_object_ids[0],),
        )
        self.assertEqual(
            compiled.unresolved_logical_object_ids,
            (self.d2.logical_object_ids[1],),
        )
        self.assertEqual(
            compiled.diagnostic_codes,
            ("UNRESOLVED_LOGICAL_OBJECT_SCOPE",),
        )

        unknown = candidate.model_copy(
            update={"unresolved_logical_object_ids": ("unknown-object",)}
        )
        with self.assertRaisesRegex(ValueError, "unknown object"):
            self._compile(unknown)

    def _compile(
        self,
        candidate: D3ParentBoundaryClassificationCandidateV2,
    ):
        return compile_d3_parent_provision_decision(
            d2_identity=self.d2,
            source_segments=self.segments,
            kit_identity=self.kit,
            candidate=candidate,
        )


def _candidate(
    segments: tuple[D3ExactSourceSegmentV1, ...],
    *,
    first_spans: tuple[D3CandidateSourceSpanV2, ...] | None = None,
) -> D3ParentBoundaryClassificationCandidateV2:
    return D3ParentBoundaryClassificationCandidateV2(
        entries=(
            _entry(first_spans or (_full_range(segments[0], ordinal=0),)),
            _entry(
                (_full_range(segments[1], ordinal=1),),
                requirementness=Requirementness.NON_REQUIREMENT,
                requirement_kind=None,
                technicality=None,
                content_role=ProvisionContentRole.CONTEXT,
            ),
        )
    )


def _entry(
    spans: tuple[D3CandidateSourceSpanV2, ...],
    *,
    requirementness: Requirementness = Requirementness.REQUIREMENT,
    requirement_kind: RequirementKind | None = RequirementKind.FUNCTIONAL,
    technicality: RequirementTechnicality | None = (RequirementTechnicality.TECHNICAL),
    content_role: ProvisionContentRole = ProvisionContentRole.PROVISION,
) -> D3ParentBoundaryClassificationEntryV2:
    return D3ParentBoundaryClassificationEntryV2(
        source_spans=spans,
        requirementness=requirementness,
        requirement_kind=requirement_kind,
        technicality=technicality,
        content_role=content_role,
        confidence=0.94,
        reason_codes=("SOURCE_BOUND_CLASSIFICATION",),
    )


def _full_range(
    segment: D3ExactSourceSegmentV1,
    *,
    ordinal: int,
) -> D3CandidateSourceSpanV2:
    return D3CandidateSourceSpanV2(
        start_segment_ordinal=ordinal,
        start_offset=0,
        end_segment_ordinal=ordinal,
        end_offset=len(segment.source_text),
    )


def _source_segments() -> tuple[D3ExactSourceSegmentV1, ...]:
    first_text = "5.1 The controller shall stop.\n"
    second_text = "Background only."
    first_object = "d2-logical-object-" + "1" * 32
    second_object = "d2-logical-object-" + "2" * 32
    return (
        D3ExactSourceSegmentV1(
            segment_id="d2-source-segment-" + "1" * 32,
            logical_object_id=first_object,
            source_handle="node-" + "1" * 32,
            handle_kind="SOURCE_NODE",
            owner_source_node_id="node-" + "1" * 32,
            source_order=10,
            page_numbers=(1,),
            handle_start_offset=0,
            handle_end_offset=len(first_text),
            source_text=first_text,
            source_text_sha256=_sha(first_text),
            source_content_sha256=_sha(first_text),
            d2_segment_sha256="1" * 64,
            evidence_refs=("page-render-1",),
        ),
        D3ExactSourceSegmentV1(
            segment_id="d2-source-segment-" + "2" * 32,
            logical_object_id=second_object,
            source_handle="node-" + "2" * 32,
            handle_kind="SOURCE_NODE",
            owner_source_node_id="node-" + "2" * 32,
            source_order=20,
            page_numbers=(2,),
            handle_start_offset=0,
            handle_end_offset=len(second_text),
            source_text=second_text,
            source_text_sha256=_sha(second_text),
            source_content_sha256=_sha(second_text),
            d2_segment_sha256="2" * 64,
            evidence_refs=("page-render-2",),
        ),
    )


def _d2_identity(
    kit: AgentKitIdentityV1,
    segments: tuple[D3ExactSourceSegmentV1, ...],
) -> D3ValidatedD2ArtifactIdentityV1:
    return D3ValidatedD2ArtifactIdentityV1(
        artifact_id="agent-artifact-" + "2" * 32,
        artifact_sha256="2" * 64,
        validator_receipt_sha256="3" * 64,
        run_id="agent-run-" + "1" * 32,
        source_sha256="a" * 64,
        source_ir_id="source-ir-1",
        source_ir_canonical_sha256="4" * 64,
        kit_identity=kit,
        status=ParentProvisionStatus.COMPLETE,
        logical_object_ids=tuple(item.logical_object_id for item in segments),
        source_segment_ids=tuple(item.segment_id for item in segments),
        source_scope_sha256=d3_source_scope_sha256(segments),
    )


def _kit_identity() -> AgentKitIdentityV1:
    return AgentKitIdentityV1(
        kit_id="reqpilotx-extraction-agent-kit",
        kit_version="0.3.0",
        manifest_sha256="5" * 64,
        bundle_sha256="6" * 64,
        policy_id="reqpilotx-extraction-policy",
        policy_version="1.0.0",
        tool_registry_id="reqpilotx-document-evidence-tools",
        tool_registry_version="1.0.0",
    )


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import ast
import hashlib
from pathlib import Path
import unittest

from pydantic import ValidationError

from app.application.agent_workflow.d3_parent_provision_attestation import (
    D3ParentProvisionExecutionReceiptV1,
    D3ParentProvisionPolicyAttestationV1,
    D3ParentProvisionPolicyClosureV1,
    D3ParentProvisionPolicyFileDigestV1,
    D3ParentProvisionRunnerAuthorityV1,
    attest_d3_parent_provision_policy,
    canonical_sha256,
)
from app.application.agent_workflow.d3_parent_provisions import (
    D3ExactSourceSegmentV1,
    D3ProfessionalPolicyAttestationIdentityV1,
    D3ValidatedD2ArtifactIdentityV1,
    ParentProvisionEntryV3,
    ParentProvisionProposalV3,
    ParentProvisionSourceRangeV3,
    ParentProvisionStatus,
    ParentProvisionValidationReceiptV3,
    ProvisionContentRole,
    RequirementKind,
    RequirementTechnicality,
    Requirementness,
    build_professional_policy_attestation_identity,
    d3_source_scope_sha256,
    parent_provision_decision_candidate_sha256,
    parent_provision_decision_sha256,
    parent_provision_evidence_refs,
    stable_parent_provision_id,
    validate_parent_provision_proposal,
)
from app.application.agent_workflow.extraction_runtime import (
    AGENT_BASELINE_STAGE_ORDER,
    AgentKitIdentityV1,
    AgentRunnerIdentityV1,
    AgentRunnerKind,
)
from app.adapters.agents.codex_app_server.canonical_stage_driver import (
    D3ParentProvisionDecisionCandidateV1 as WireD3DecisionCandidateV1,
)


class D3ParentProvisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.kit = _kit_identity()
        self.segments = _source_segments()
        self.d2 = _d2_identity(self.kit, self.segments)

    def test_complete_source_verbatim_baseline_validates(self) -> None:
        entries = _complete_entries(self.d2, self.segments)
        proposal, execution, attestation = _proposal_with_attestation(
            self.d2,
            entries=entries,
            status=ParentProvisionStatus.COMPLETE,
            declared=self.d2.logical_object_ids,
        )

        receipt = validate_parent_provision_proposal(
            d2_identity=self.d2,
            source_segments=self.segments,
            parent_execution_receipt=execution,
            parent_policy_attestation=attestation,
            proposal=proposal,
        )

        self.assertEqual(receipt.status, ParentProvisionStatus.COMPLETE)
        self.assertEqual(receipt.parent_provision_count, 2)
        self.assertEqual(receipt.source_segment_count, 2)
        self.assertEqual(receipt.unresolved_scope_count, 0)
        self.assertEqual(receipt.unresolved_entry_count, 0)
        self.assertEqual(
            receipt.artifact_sha256, canonical_sha256(proposal.model_dump(mode="json"))
        )

    def test_partial_may_omit_only_explicitly_unresolved_d2_scope(self) -> None:
        entry = _entry(
            self.d2,
            self.segments[0],
            source_order=0,
            requirementness=Requirementness.REQUIREMENT,
            requirement_kind=RequirementKind.FUNCTIONAL,
        )
        proposal, execution, attestation = _proposal_with_attestation(
            self.d2,
            entries=(entry,),
            status=ParentProvisionStatus.PARTIAL,
            declared=(self.d2.logical_object_ids[0],),
            unresolved=(self.d2.logical_object_ids[1],),
            diagnostics=("UNRESOLVED_LOGICAL_OBJECT_SCOPE",),
        )

        receipt = validate_parent_provision_proposal(
            d2_identity=self.d2,
            source_segments=self.segments,
            parent_execution_receipt=execution,
            parent_policy_attestation=attestation,
            proposal=proposal,
        )

        self.assertEqual(receipt.status, ParentProvisionStatus.PARTIAL)
        self.assertEqual(receipt.declared_scope_count, 1)
        self.assertEqual(receipt.unresolved_scope_count, 1)

    def test_declared_scope_requires_complete_non_whitespace_disposition(self) -> None:
        segment = self.segments[0]
        source_range = _source_range(segment, end=len(segment.source_text) - 2)
        entry = _entry_from_ranges(
            self.d2,
            (segment,),
            (source_range,),
            source_order=0,
            requirementness=Requirementness.REQUIREMENT,
            requirement_kind=RequirementKind.FUNCTIONAL,
        )
        proposal, execution, attestation = _proposal_with_attestation(
            self.d2,
            entries=(entry,),
            status=ParentProvisionStatus.PARTIAL,
            declared=(self.d2.logical_object_ids[0],),
            unresolved=(self.d2.logical_object_ids[1],),
            diagnostics=("UNRESOLVED_LOGICAL_OBJECT_SCOPE",),
        )

        with self.assertRaisesRegex(ValueError, "completely disposition"):
            validate_parent_provision_proposal(
                d2_identity=self.d2,
                source_segments=self.segments,
                parent_execution_receipt=execution,
                parent_policy_attestation=attestation,
                proposal=proposal,
            )

    def test_invented_text_and_overlapping_ranges_fail_closed(self) -> None:
        segment = self.segments[0]
        invented_range = ParentProvisionSourceRangeV3(
            segment_id=segment.segment_id,
            start_offset=0,
            end_offset=4,
            verbatim_text="NOPE",
            verbatim_text_sha256=_sha("NOPE"),
        )
        invented_entry = _entry_from_ranges(
            self.d2,
            (segment,),
            (invented_range,),
            source_order=0,
            requirementness=Requirementness.REQUIREMENT,
            requirement_kind=RequirementKind.FUNCTIONAL,
        )
        proposal, execution, attestation = _proposal_with_attestation(
            self.d2,
            entries=(invented_entry,),
            status=ParentProvisionStatus.PARTIAL,
            declared=(self.d2.logical_object_ids[0],),
            unresolved=(self.d2.logical_object_ids[1],),
            diagnostics=("UNRESOLVED_LOGICAL_OBJECT_SCOPE",),
        )
        with self.assertRaisesRegex(ValueError, "invented or rewrote"):
            validate_parent_provision_proposal(
                d2_identity=self.d2,
                source_segments=self.segments,
                parent_execution_receipt=execution,
                parent_policy_attestation=attestation,
                proposal=proposal,
            )

        first = _source_range(segment)
        second = _source_range(segment, start=1)
        entries = (
            _entry_from_ranges(
                self.d2,
                (segment,),
                (first,),
                source_order=0,
                requirementness=Requirementness.REQUIREMENT,
                requirement_kind=RequirementKind.FUNCTIONAL,
            ),
            _entry_from_ranges(
                self.d2,
                (segment,),
                (second,),
                source_order=1,
                requirementness=Requirementness.NON_REQUIREMENT,
                requirement_kind=None,
                technicality=None,
            ),
        )
        proposal, execution, attestation = _proposal_with_attestation(
            self.d2,
            entries=entries,
            status=ParentProvisionStatus.PARTIAL,
            declared=(self.d2.logical_object_ids[0],),
            unresolved=(self.d2.logical_object_ids[1],),
            diagnostics=("UNRESOLVED_LOGICAL_OBJECT_SCOPE",),
        )
        with self.assertRaisesRegex(ValueError, "ranges overlap"):
            validate_parent_provision_proposal(
                d2_identity=self.d2,
                source_segments=self.segments,
                parent_execution_receipt=execution,
                parent_policy_attestation=attestation,
                proposal=proposal,
            )

    def test_source_d2_kit_and_object_ownership_drift_fail_closed(self) -> None:
        entries = _complete_entries(self.d2, self.segments)
        proposal, execution, attestation = _proposal_with_attestation(
            self.d2,
            entries=entries,
            status=ParentProvisionStatus.COMPLETE,
            declared=self.d2.logical_object_ids,
        )
        cases = (
            (
                self.d2.model_copy(update={"source_sha256": "9" * 64}),
                self.segments,
                proposal,
                "source/D2 lineage",
            ),
            (
                self.d2,
                self.segments,
                proposal.model_copy(
                    update={
                        "kit_identity": proposal.kit_identity.model_copy(
                            update={"bundle_sha256": "8" * 64}
                        )
                    }
                ),
                "decision candidate|Kit identity",
            ),
            (
                self.d2,
                (
                    self.segments[0].model_copy(
                        update={"logical_object_id": self.d2.logical_object_ids[1]}
                    ),
                    self.segments[1],
                ),
                proposal,
                "scope drifted",
            ),
        )
        for d2, segments, candidate, message in cases:
            with (
                self.subTest(message=message),
                self.assertRaisesRegex(
                    ValueError,
                    message,
                ),
            ):
                validate_parent_provision_proposal(
                    d2_identity=d2,
                    source_segments=segments,
                    parent_execution_receipt=execution,
                    parent_policy_attestation=attestation,
                    proposal=candidate,
                )

    def test_classification_axes_are_orthogonal_but_consistent(self) -> None:
        base = _entry_payload(self.d2, self.segments[0])
        invalid = (
            (
                base
                | {
                    "requirementness": "REQUIREMENT",
                    "requirement_kind": None,
                },
                "must declare a requirement kind",
            ),
            (
                base
                | {
                    "requirementness": "NON_REQUIREMENT",
                    "requirement_kind": "FUNCTIONAL",
                },
                "cannot claim a requirement kind",
            ),
            (
                base
                | {
                    "requirementness": "UNRESOLVED",
                    "requirement_kind": "QUALITY",
                },
                "cannot claim a resolved kind",
            ),
            (
                base
                | {
                    "requirementness": "NON_REQUIREMENT",
                    "requirement_kind": None,
                    "technicality": "TECHNICAL",
                },
                "cannot claim resolved technicality",
            ),
        )
        for payload, message in invalid:
            with (
                self.subTest(message=message),
                self.assertRaisesRegex(
                    ValidationError,
                    message,
                ),
            ):
                ParentProvisionEntryV3.model_validate(payload)

        context = ParentProvisionEntryV3.model_validate(
            base
            | {
                "requirementness": "NON_REQUIREMENT",
                "requirement_kind": None,
                "technicality": None,
                "content_role": "REFERENCE",
            }
        )
        self.assertEqual(context.requirementness, Requirementness.NON_REQUIREMENT)
        self.assertEqual(context.content_role, ProvisionContentRole.REFERENCE)

    def test_complete_rejects_unresolved_requirementness_and_partial_discloses_it(
        self,
    ) -> None:
        unresolved = _entry(
            self.d2,
            self.segments[0],
            source_order=0,
            requirementness=Requirementness.UNRESOLVED,
            requirement_kind=RequirementKind.UNRESOLVED,
            technicality=RequirementTechnicality.UNRESOLVED,
        )
        other = _entry(
            self.d2,
            self.segments[1],
            source_order=1,
            requirementness=Requirementness.NON_REQUIREMENT,
            requirement_kind=None,
            technicality=None,
        )
        complete, execution, attestation = _proposal_with_attestation(
            self.d2,
            entries=(unresolved, other),
            status=ParentProvisionStatus.COMPLETE,
            declared=self.d2.logical_object_ids,
        )
        with self.assertRaisesRegex(ValueError, "status does not match"):
            validate_parent_provision_proposal(
                d2_identity=self.d2,
                source_segments=self.segments,
                parent_execution_receipt=execution,
                parent_policy_attestation=attestation,
                proposal=complete,
            )

        partial, execution, attestation = _proposal_with_attestation(
            self.d2,
            entries=(unresolved, other),
            status=ParentProvisionStatus.PARTIAL,
            declared=self.d2.logical_object_ids,
            diagnostics=("UNRESOLVED_REQUIREMENTNESS",),
        )
        receipt = validate_parent_provision_proposal(
            d2_identity=self.d2,
            source_segments=self.segments,
            parent_execution_receipt=execution,
            parent_policy_attestation=attestation,
            proposal=partial,
        )
        self.assertEqual(receipt.unresolved_entry_count, 1)

    def test_real_policy_attestation_must_bind_exact_parent_decision(self) -> None:
        entries = _complete_entries(self.d2, self.segments)
        proposal, execution, attestation = _proposal_with_attestation(
            self.d2,
            entries=entries,
            status=ParentProvisionStatus.COMPLETE,
            declared=self.d2.logical_object_ids,
        )
        tampered_execution = execution.model_copy(
            update={"parent_provision_decision_sha256": "7" * 64}
        )

        with self.assertRaisesRegex(ValueError, "execution receipt drifted"):
            validate_parent_provision_proposal(
                d2_identity=self.d2,
                source_segments=self.segments,
                parent_execution_receipt=tampered_execution,
                parent_policy_attestation=attestation,
                proposal=proposal,
            )

        identity = build_professional_policy_attestation_identity(
            parent_execution_receipt=execution,
            parent_policy_attestation=attestation,
            attested_decision_sha256=(execution.parent_provision_decision_sha256 or ""),
        )
        self.assertEqual(identity, proposal.policy_attestation)
        identity_payload = identity.model_dump(mode="json")
        identity_payload["parent_provision_attestation_sha256"] = "6" * 64
        with self.assertRaisesRegex(ValidationError, "identity SHA is stale"):
            D3ProfessionalPolicyAttestationIdentityV1.model_validate(identity_payload)

    def test_direct_parent_attestation_fails_closed_on_every_host_identity_drift(
        self,
    ) -> None:
        entries = _complete_entries(self.d2, self.segments)
        proposal, execution, _ = _proposal_with_attestation(
            self.d2,
            entries=entries,
            status=ParentProvisionStatus.COMPLETE,
            declared=self.d2.logical_object_ids,
        )
        closure = _policy_closure(self.kit)
        runner = _runner_authority()
        decision_sha256 = parent_provision_decision_sha256(
            run_id=proposal.run_id,
            source_sha256=proposal.source_sha256,
            kit_identity=proposal.kit_identity,
            predecessor_d2_artifact_id=proposal.predecessor_d2_artifact_id,
            predecessor_d2_artifact_sha256=(proposal.predecessor_d2_artifact_sha256),
            status=proposal.status,
            declared_logical_object_ids=proposal.declared_logical_object_ids,
            unresolved_logical_object_ids=proposal.unresolved_logical_object_ids,
            entries=proposal.entries,
            diagnostic_codes=proposal.diagnostic_codes,
        )
        cases = (
            (
                execution.model_copy(
                    update={
                        "runner_identity": execution.runner_identity.model_copy(
                            update={"runner_profile_sha256": "1" * 64}
                        )
                    }
                ),
                "RUNNER_IDENTITY_MISMATCH",
            ),
            (
                execution.model_copy(
                    update={
                        "kit_identity": execution.kit_identity.model_copy(
                            update={"bundle_sha256": "2" * 64}
                        )
                    }
                ),
                "KIT_POLICY_CLOSURE_MISMATCH",
            ),
            (
                execution.model_copy(update={"d2_artifact_sha256": "3" * 64}),
                "D2_SOURCE_LINEAGE_MISMATCH",
            ),
            (
                execution.model_copy(
                    update={"stage_session_attestation_sha256": "4" * 64}
                ),
                "STAGE_SESSION_ATTESTATION_MISMATCH",
            ),
            (
                execution.model_copy(update={"execution_trace_sha256": "5" * 64}),
                "EXECUTION_TRACE_MISMATCH",
            ),
            (
                execution.model_copy(update={"decision_candidate_sha256": "6" * 64}),
                "DECISION_CANDIDATE_MISMATCH",
            ),
            (
                execution.model_copy(update={"evidence_refs": ("unknown-ref",)}),
                "EVIDENCE_REFERENCE_CLOSURE_MISMATCH",
            ),
        )
        for candidate, diagnostic in cases:
            with self.subTest(diagnostic=diagnostic):
                attestation = attest_d3_parent_provision_policy(
                    expected_policy_closure=closure,
                    expected_runner=runner,
                    expected_run_id=self.d2.run_id,
                    expected_source_sha256=self.d2.source_sha256,
                    expected_d2_artifact_id=self.d2.artifact_id,
                    expected_d2_artifact_sha256=self.d2.artifact_sha256,
                    expected_stage_session_attestation_sha256="c" * 64,
                    expected_execution_trace_sha256="d" * 64,
                    expected_decision_candidate_sha256=(
                        execution.decision_candidate_sha256
                    ),
                    expected_parent_provision_decision_sha256=decision_sha256,
                    expected_evidence_refs=execution.evidence_refs,
                    execution_receipt=candidate,
                )
                self.assertFalse(attestation.policy_attested)
                self.assertIn(diagnostic, attestation.diagnostic_codes)

    def test_direct_parent_schemas_forbid_extras_and_legacy_imports(self) -> None:
        entries = _complete_entries(self.d2, self.segments)
        proposal, execution, attestation = _proposal_with_attestation(
            self.d2,
            entries=entries,
            status=ParentProvisionStatus.COMPLETE,
            declared=self.d2.logical_object_ids,
        )
        for model, value in (
            (D3ParentProvisionExecutionReceiptV1, execution),
            (D3ParentProvisionPolicyAttestationV1, attestation),
        ):
            payload = value.model_dump(mode="json")
            payload["legacy_working_tree_id"] = "forbidden"
            with (
                self.subTest(model=model.__name__),
                self.assertRaisesRegex(
                    ValidationError,
                    "Extra inputs are not permitted",
                ),
            ):
                model.model_validate(payload)

        app_root = Path(__file__).resolve().parents[1] / "app" / "application"
        paths = (
            app_root / "agent_workflow" / "d3_parent_provision_attestation.py",
            app_root / "agent_workflow" / "d3_parent_provisions.py",
            app_root.parent
            / "adapters"
            / "agents"
            / "d3_parent_provision_attestation.py",
        )
        forbidden_modules = {
            "app.application.agent_workflow.d3_classification_attestation",
            "app.domain.entry_classification",
        }
        for path in paths:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imported = {
                node.module
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.module is not None
            }
            self.assertTrue(imported.isdisjoint(forbidden_modules), path.name)

    def test_direct_receipt_rejects_late_stages_and_planned_policy_cannot_attest(
        self,
    ) -> None:
        entries = _complete_entries(self.d2, self.segments)
        proposal, execution, _ = _proposal_with_attestation(
            self.d2,
            entries=entries,
            status=ParentProvisionStatus.COMPLETE,
            declared=self.d2.logical_object_ids,
        )
        payload = execution.model_dump(mode="json")
        payload["requested_stages"] = [
            stage.value for stage in AGENT_BASELINE_STAGE_ORDER
        ]
        with self.assertRaisesRegex(
            ValidationError, "exact baseline prefix through D3"
        ):
            D3ParentProvisionExecutionReceiptV1.model_validate(payload)

        planned_closure = _policy_closure(self.kit).model_copy(
            update={"stage_implementation_status": "PLANNED"}
        )
        planned_execution = execution.model_copy(
            update={"loaded_policy_closure": planned_closure}
        )
        attestation = attest_d3_parent_provision_policy(
            expected_policy_closure=planned_closure,
            expected_runner=_runner_authority(),
            expected_run_id=self.d2.run_id,
            expected_source_sha256=self.d2.source_sha256,
            expected_d2_artifact_id=self.d2.artifact_id,
            expected_d2_artifact_sha256=self.d2.artifact_sha256,
            expected_stage_session_attestation_sha256="c" * 64,
            expected_execution_trace_sha256="d" * 64,
            expected_decision_candidate_sha256=(execution.decision_candidate_sha256),
            expected_parent_provision_decision_sha256=(
                parent_provision_decision_sha256(
                    run_id=proposal.run_id,
                    source_sha256=proposal.source_sha256,
                    kit_identity=proposal.kit_identity,
                    predecessor_d2_artifact_id=(proposal.predecessor_d2_artifact_id),
                    predecessor_d2_artifact_sha256=(
                        proposal.predecessor_d2_artifact_sha256
                    ),
                    status=proposal.status,
                    declared_logical_object_ids=(proposal.declared_logical_object_ids),
                    unresolved_logical_object_ids=(
                        proposal.unresolved_logical_object_ids
                    ),
                    entries=proposal.entries,
                    diagnostic_codes=proposal.diagnostic_codes,
                )
            ),
            expected_evidence_refs=execution.evidence_refs,
            execution_receipt=planned_execution,
        )
        self.assertFalse(attestation.policy_attested)
        self.assertIn(
            "STAGE_IMPLEMENTATION_NOT_AVAILABLE",
            attestation.diagnostic_codes,
        )

    def test_wire_candidate_digest_and_evidence_match_host_reconstruction(self) -> None:
        entries = _complete_entries(self.d2, self.segments)
        proposal, execution, _ = _proposal_with_attestation(
            self.d2,
            entries=entries,
            status=ParentProvisionStatus.COMPLETE,
            declared=self.d2.logical_object_ids,
        )
        candidate = WireD3DecisionCandidateV1(
            run_id=proposal.run_id,
            source_sha256=proposal.source_sha256,
            kit_identity=proposal.kit_identity,
            predecessor_d2_artifact_id=proposal.predecessor_d2_artifact_id,
            predecessor_d2_artifact_sha256=(proposal.predecessor_d2_artifact_sha256),
            status=proposal.status,
            declared_logical_object_ids=proposal.declared_logical_object_ids,
            unresolved_logical_object_ids=proposal.unresolved_logical_object_ids,
            entries=proposal.entries,
            diagnostic_codes=proposal.diagnostic_codes,
        )
        reconstructed_candidate_sha256 = parent_provision_decision_candidate_sha256(
            run_id=proposal.run_id,
            source_sha256=proposal.source_sha256,
            kit_identity=proposal.kit_identity,
            predecessor_d2_artifact_id=proposal.predecessor_d2_artifact_id,
            predecessor_d2_artifact_sha256=(proposal.predecessor_d2_artifact_sha256),
            status=proposal.status,
            declared_logical_object_ids=proposal.declared_logical_object_ids,
            unresolved_logical_object_ids=proposal.unresolved_logical_object_ids,
            entries=proposal.entries,
            diagnostic_codes=proposal.diagnostic_codes,
        )
        self.assertEqual(
            reconstructed_candidate_sha256,
            canonical_sha256(candidate.model_dump(mode="json")),
        )
        self.assertEqual(
            execution.evidence_refs,
            parent_provision_evidence_refs(
                declared_logical_object_ids=proposal.declared_logical_object_ids,
                unresolved_logical_object_ids=(proposal.unresolved_logical_object_ids),
                entries=proposal.entries,
            ),
        )
        payload = candidate.model_dump(mode="json")
        payload["policy_attestation"] = proposal.policy_attestation.model_dump(
            mode="json"
        )
        with self.assertRaisesRegex(ValidationError, "Extra inputs are not permitted"):
            WireD3DecisionCandidateV1.model_validate(payload)

    def test_atomization_fields_are_not_part_of_d3_schema(self) -> None:
        entries = _complete_entries(self.d2, self.segments)
        proposal, _, _ = _proposal_with_attestation(
            self.d2,
            entries=entries,
            status=ParentProvisionStatus.COMPLETE,
            declared=self.d2.logical_object_ids,
        )
        payload = proposal.model_dump(mode="json")
        payload["atoms"] = [{"text": "derived"}]
        with self.assertRaisesRegex(ValidationError, "Extra inputs are not permitted"):
            ParentProvisionProposalV3.model_validate(payload)

    def test_receipt_rejects_artifact_or_count_tampering(self) -> None:
        proposal, execution, attestation = _proposal_with_attestation(
            self.d2,
            entries=_complete_entries(self.d2, self.segments),
            status=ParentProvisionStatus.COMPLETE,
            declared=self.d2.logical_object_ids,
        )
        receipt = validate_parent_provision_proposal(
            d2_identity=self.d2,
            source_segments=self.segments,
            parent_execution_receipt=execution,
            parent_policy_attestation=attestation,
            proposal=proposal,
        )
        payload = receipt.model_dump(mode="json")
        payload["artifact_id"] = "agent-artifact-" + "f" * 32
        payload["receipt_sha256"] = _receipt_sha(payload)
        with self.assertRaisesRegex(ValidationError, "artifact ID does not bind"):
            ParentProvisionValidationReceiptV3.model_validate(payload)

        payload = receipt.model_dump(mode="json")
        payload["unresolved_entry_count"] = 1
        payload["receipt_sha256"] = _receipt_sha(payload)
        with self.assertRaisesRegex(ValidationError, "cannot claim unresolved"):
            ParentProvisionValidationReceiptV3.model_validate(payload)


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
            source_order=0,
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
            source_order=1,
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


def _complete_entries(
    d2: D3ValidatedD2ArtifactIdentityV1,
    segments: tuple[D3ExactSourceSegmentV1, ...],
) -> tuple[ParentProvisionEntryV3, ...]:
    return (
        _entry(
            d2,
            segments[0],
            source_order=0,
            requirementness=Requirementness.REQUIREMENT,
            requirement_kind=RequirementKind.FUNCTIONAL,
        ),
        _entry(
            d2,
            segments[1],
            source_order=1,
            requirementness=Requirementness.NON_REQUIREMENT,
            requirement_kind=None,
            technicality=None,
            content_role=ProvisionContentRole.CONTEXT,
        ),
    )


def _entry(
    d2: D3ValidatedD2ArtifactIdentityV1,
    segment: D3ExactSourceSegmentV1,
    *,
    source_order: int,
    requirementness: Requirementness,
    requirement_kind: RequirementKind | None,
    technicality: RequirementTechnicality | None = RequirementTechnicality.TECHNICAL,
    content_role: ProvisionContentRole = ProvisionContentRole.PROVISION,
) -> ParentProvisionEntryV3:
    return _entry_from_ranges(
        d2,
        (segment,),
        (_source_range(segment),),
        source_order=source_order,
        requirementness=requirementness,
        requirement_kind=requirement_kind,
        technicality=technicality,
        content_role=content_role,
    )


def _entry_from_ranges(
    d2: D3ValidatedD2ArtifactIdentityV1,
    segments: tuple[D3ExactSourceSegmentV1, ...],
    ranges: tuple[ParentProvisionSourceRangeV3, ...],
    *,
    source_order: int,
    requirementness: Requirementness,
    requirement_kind: RequirementKind | None,
    technicality: RequirementTechnicality | None = RequirementTechnicality.TECHNICAL,
    content_role: ProvisionContentRole = ProvisionContentRole.PROVISION,
) -> ParentProvisionEntryV3:
    logical_object_ids = tuple(
        dict.fromkeys(item.logical_object_id for item in segments)
    )
    return ParentProvisionEntryV3(
        parent_provision_id=stable_parent_provision_id(
            source_sha256=d2.source_sha256,
            d2_artifact_sha256=d2.artifact_sha256,
            logical_object_ids=logical_object_ids,
            source_ranges=ranges,
        ),
        source_order=source_order,
        logical_object_ids=logical_object_ids,
        source_ranges=ranges,
        requirementness=requirementness,
        requirement_kind=requirement_kind,
        technicality=technicality,
        content_role=content_role,
        confidence=0.91,
        reason_codes=("SOURCE_BOUND_PROFESSIONAL_JUDGMENT",),
        evidence_refs=tuple(sorted({item.segment_id for item in segments})),
    )


def _entry_payload(
    d2: D3ValidatedD2ArtifactIdentityV1,
    segment: D3ExactSourceSegmentV1,
) -> dict[str, object]:
    return _entry(
        d2,
        segment,
        source_order=0,
        requirementness=Requirementness.REQUIREMENT,
        requirement_kind=RequirementKind.FUNCTIONAL,
    ).model_dump(mode="json")


def _source_range(
    segment: D3ExactSourceSegmentV1,
    *,
    start: int = 0,
    end: int | None = None,
) -> ParentProvisionSourceRangeV3:
    resolved_end = len(segment.source_text) if end is None else end
    text = segment.source_text[start:resolved_end]
    return ParentProvisionSourceRangeV3(
        segment_id=segment.segment_id,
        start_offset=start,
        end_offset=resolved_end,
        verbatim_text=text,
        verbatim_text_sha256=_sha(text),
    )


def _proposal_with_attestation(
    d2: D3ValidatedD2ArtifactIdentityV1,
    *,
    entries: tuple[ParentProvisionEntryV3, ...],
    status: ParentProvisionStatus,
    declared: tuple[str, ...],
    unresolved: tuple[str, ...] = (),
    diagnostics: tuple[str, ...] = (),
) -> tuple[
    ParentProvisionProposalV3,
    D3ParentProvisionExecutionReceiptV1,
    D3ParentProvisionPolicyAttestationV1,
]:
    decision_sha256 = parent_provision_decision_sha256(
        run_id=d2.run_id,
        source_sha256=d2.source_sha256,
        kit_identity=d2.kit_identity,
        predecessor_d2_artifact_id=d2.artifact_id,
        predecessor_d2_artifact_sha256=d2.artifact_sha256,
        status=status,
        declared_logical_object_ids=declared,
        unresolved_logical_object_ids=unresolved,
        entries=entries,
        diagnostic_codes=diagnostics,
    )
    execution, attestation = _parent_attestation(
        d2,
        decision_sha256,
        entries,
        status=status,
        declared=declared,
        unresolved=unresolved,
        diagnostics=diagnostics,
    )
    policy_identity = build_professional_policy_attestation_identity(
        parent_execution_receipt=execution,
        parent_policy_attestation=attestation,
        attested_decision_sha256=decision_sha256,
    )
    return (
        ParentProvisionProposalV3(
            run_id=d2.run_id,
            source_sha256=d2.source_sha256,
            kit_identity=d2.kit_identity,
            predecessor_d2_artifact_id=d2.artifact_id,
            predecessor_d2_artifact_sha256=d2.artifact_sha256,
            policy_attestation=policy_identity,
            status=status,
            declared_logical_object_ids=declared,
            unresolved_logical_object_ids=unresolved,
            entries=entries,
            diagnostic_codes=diagnostics,
        ),
        execution,
        attestation,
    )


def _parent_attestation(
    d2: D3ValidatedD2ArtifactIdentityV1,
    decision_sha256: str,
    entries: tuple[ParentProvisionEntryV3, ...],
    *,
    status: ParentProvisionStatus,
    declared: tuple[str, ...],
    unresolved: tuple[str, ...],
    diagnostics: tuple[str, ...],
) -> tuple[
    D3ParentProvisionExecutionReceiptV1,
    D3ParentProvisionPolicyAttestationV1,
]:
    closure = _policy_closure(d2.kit_identity)
    runner = _runner_authority()

    stage_session_sha256 = "c" * 64
    execution_trace_sha256 = "d" * 64
    candidate_sha256 = parent_provision_decision_candidate_sha256(
        run_id=d2.run_id,
        source_sha256=d2.source_sha256,
        kit_identity=d2.kit_identity,
        predecessor_d2_artifact_id=d2.artifact_id,
        predecessor_d2_artifact_sha256=d2.artifact_sha256,
        status=status,
        declared_logical_object_ids=declared,
        unresolved_logical_object_ids=unresolved,
        entries=entries,
        diagnostic_codes=diagnostics,
    )
    evidence_refs = parent_provision_evidence_refs(
        declared_logical_object_ids=declared,
        unresolved_logical_object_ids=unresolved,
        entries=entries,
    )
    execution = D3ParentProvisionExecutionReceiptV1(
        run_id=d2.run_id,
        source_sha256=d2.source_sha256,
        d2_artifact_id=d2.artifact_id,
        d2_artifact_sha256=d2.artifact_sha256,
        kit_identity=d2.kit_identity,
        agent_id=closure.agent_id,
        runner_identity=AgentRunnerIdentityV1(
            runner_profile_id=runner.runner_profile_id,
            runner_profile_sha256=runner.runner_profile_sha256,
            runner_kind=runner.runner_kind,
            adapter_id=runner.adapter_id,
            adapter_version=runner.adapter_version,
            runtime_version="codex-app-server-test",
        ),
        model_id=runner.model_id,
        reasoning_effort=runner.reasoning_effort,
        requested_stages=AGENT_BASELINE_STAGE_ORDER[:4],
        loaded_policy_closure=closure,
        stage_session_attestation_sha256=stage_session_sha256,
        execution_trace_sha256=execution_trace_sha256,
        decision_candidate_sha256=candidate_sha256,
        parent_provision_decision_sha256=decision_sha256,
        evidence_refs=evidence_refs,
    )
    return execution, attest_d3_parent_provision_policy(
        expected_policy_closure=closure,
        expected_runner=runner,
        expected_run_id=d2.run_id,
        expected_source_sha256=d2.source_sha256,
        expected_d2_artifact_id=d2.artifact_id,
        expected_d2_artifact_sha256=d2.artifact_sha256,
        expected_stage_session_attestation_sha256=stage_session_sha256,
        expected_execution_trace_sha256=execution_trace_sha256,
        expected_decision_candidate_sha256=candidate_sha256,
        expected_parent_provision_decision_sha256=decision_sha256,
        expected_evidence_refs=evidence_refs,
        execution_receipt=execution,
    )


def _runner_authority() -> D3ParentProvisionRunnerAuthorityV1:
    return D3ParentProvisionRunnerAuthorityV1(
        runner_profile_id="app-server-product",
        runner_profile_sha256="b" * 64,
        runner_kind=AgentRunnerKind.APP_SERVER_PRODUCT,
        adapter_id="codex-app-server",
        adapter_version="1.0.0",
        model_id="gpt-5.6-sol",
        reasoning_effort="high",
    )


def _policy_closure(kit: AgentKitIdentityV1) -> D3ParentProvisionPolicyClosureV1:
    return D3ParentProvisionPolicyClosureV1(
        kit_identity=kit,
        agent_id="reqpilotx-requirement-baseline-curator",
        agent_profile=D3ParentProvisionPolicyFileDigestV1(
            path="agents/requirement-baseline-curator.json",
            sha256="1" * 64,
        ),
        stage_implementation_status="AVAILABLE",
        skill_id="reqpilotx-parent-provision-recognition",
        skill=D3ParentProvisionPolicyFileDigestV1(
            path=(".agents/skills/reqpilotx-parent-provision-recognition/SKILL.md"),
            sha256="2" * 64,
        ),
        policy_id=kit.policy_id,
        policy_version=kit.policy_version,
        policy=D3ParentProvisionPolicyFileDigestV1(
            path="policy/extraction-policy.v1.json",
            sha256="3" * 64,
        ),
        references=(
            D3ParentProvisionPolicyFileDigestV1(
                path="references/requirements-engineering-baseline.md",
                sha256="4" * 64,
            ),
        ),
    )


def _kit_identity() -> AgentKitIdentityV1:
    return AgentKitIdentityV1(
        kit_id="reqpilotx-extraction-agent-kit",
        kit_version="0.4.0",
        manifest_sha256="5" * 64,
        bundle_sha256="6" * 64,
        policy_id="reqpilotx-extraction-policy",
        policy_version="1.0.0",
        tool_registry_id="reqpilotx-document-evidence-tools",
        tool_registry_version="1.0.0",
    )


def _receipt_sha(payload: dict[str, object]) -> str:
    return canonical_sha256(
        {key: value for key, value in payload.items() if key != "receipt_sha256"}
    )


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


if __name__ == "__main__":
    unittest.main()

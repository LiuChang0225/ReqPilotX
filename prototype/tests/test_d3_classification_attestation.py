from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

from pathlib import Path
import tempfile
import unittest

from pydantic import ValidationError

from app.adapters.agents.agent_kit_materializer import materialize_agent_kit
from app.adapters.agents.d3_classification_attestation import (
    attest_d3_classification_policy_from_kit,
    compile_d3_classification_policy_closure,
    compile_d3_runner_authority,
)
from app.adapters.agents.portable_kit import load_portable_agent_kit
from app.application.agent_workflow.d3_classification_attestation import (
    D3ClassificationExecutionReceiptV1,
    canonical_sha256,
)
from app.application.agent_workflow.extraction_runtime import (
    AGENT_BASELINE_STAGE_ORDER,
    AgentExtractionStage,
    AgentRunnerIdentityV1,
)
from app.domain.entry_classification import (
    EntryClassificationAgentSuggestionSetV1,
    EntryClassificationAgentSuggestionV1,
    EntryClassificationType,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
AGENT_KIT_ROOT = REPOSITORY_ROOT / "agent-kit"


class D3ClassificationAttestationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.kit = load_portable_agent_kit(AGENT_KIT_ROOT)
        self.policy_closure = compile_d3_classification_policy_closure(self.kit)
        self.runner = compile_d3_runner_authority(
            self.kit,
            "app-server-product",
        )
        self.suggestions = _suggestions(self.kit.identity.bundle_sha256)
        self.receipt = _receipt(
            self.policy_closure,
            self.runner,
            self.suggestions,
        )

    def test_canonical_kit_compiles_exact_d3_professional_policy_closure(self) -> None:
        closure = self.policy_closure

        self.assertEqual(closure.kit_identity.kit_version, "0.15.2")
        self.assertEqual(
            closure.agent_id,
            "reqpilotx-requirement-baseline-curator",
        )
        self.assertEqual(
            closure.skill_id,
            "reqpilotx-parent-provision-recognition",
        )
        self.assertEqual(closure.stage_implementation_status, "AVAILABLE")
        self.assertIn(
            "references/requirements-engineering-baseline.md",
            tuple(item.path for item in closure.references),
        )
        self.assertEqual(len(closure.policy.sha256), 64)
        self.assertEqual(len(closure.agent_profile.sha256), 64)
        self.assertEqual(len(closure.skill.sha256), 64)

    def test_materialized_runner_compiles_the_same_policy_closure(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            materialized = materialize_agent_kit(
                self.kit,
                Path(raw) / "app-server-kit",
                runner_profile_id="app-server-product",
            )
            copied = compile_d3_classification_policy_closure(
                load_portable_agent_kit(materialized.root)
            )

        self.assertEqual(copied, self.policy_closure)

    def test_exact_execution_attests_policy_without_claiming_stage_availability(
        self,
    ) -> None:
        attestation = attest_d3_classification_policy_from_kit(
            self.kit,
            execution_receipt=self.receipt,
            suggestions=self.suggestions,
        )

        self.assertTrue(attestation.classification_policy_attested)
        self.assertEqual(attestation.diagnostic_codes, ())
        self.assertEqual(
            attestation.kit_bundle_sha256,
            self.kit.identity.bundle_sha256,
        )
        self.assertEqual(
            attestation.source_artifact_id,
            self.receipt.source_artifact_id,
        )
        self.assertEqual(attestation.evidence_refs, self.receipt.evidence_refs)
        self.assertEqual(
            self.policy_closure.stage_implementation_status,
            "AVAILABLE",
        )

    def test_kit_agent_runner_source_and_evidence_drift_all_fail_closed(self) -> None:
        cases = (
            (
                self.receipt.model_copy(
                    update={
                        "kit_identity": self.receipt.kit_identity.model_copy(
                            update={"bundle_sha256": "f" * 64}
                        )
                    }
                ),
                self.suggestions,
                "KIT_POLICY_CLOSURE_MISMATCH",
            ),
            (
                self.receipt.model_copy(update={"agent_id": "legacy-parent-agent"}),
                self.suggestions,
                "AGENT_IDENTITY_MISMATCH",
            ),
            (
                self.receipt.model_copy(
                    update={
                        "runner_identity": self.receipt.runner_identity.model_copy(
                            update={"runner_profile_sha256": "e" * 64}
                        )
                    }
                ),
                self.suggestions,
                "RUNNER_IDENTITY_MISMATCH",
            ),
            (
                self.receipt,
                self.suggestions.model_copy(update={"source_sha256": "d" * 64}),
                "SOURCE_IDENTITY_MISMATCH",
            ),
            (
                self.receipt.model_copy(
                    update={"evidence_refs": ("node-" + "9" * 32,)}
                ),
                self.suggestions,
                "EVIDENCE_REFERENCE_CLOSURE_MISMATCH",
            ),
        )

        for receipt, suggestions, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                attestation = attest_d3_classification_policy_from_kit(
                    self.kit,
                    execution_receipt=receipt,
                    suggestions=suggestions,
                )
                self.assertFalse(attestation.classification_policy_attested)
                self.assertIn(expected_code, attestation.diagnostic_codes)

    def test_receipt_rejects_legacy_stage_order_that_automatically_runs_d4(
        self,
    ) -> None:
        payload = self.receipt.model_dump(mode="python")
        payload["requested_stages"] = tuple(AgentExtractionStage)

        with self.assertRaisesRegex(
            ValidationError,
            "baseline workflow that reached D3",
        ):
            D3ClassificationExecutionReceiptV1.model_validate(payload)

    def test_persisted_attestation_rejects_identity_or_evidence_tampering(self) -> None:
        attestation = attest_d3_classification_policy_from_kit(
            self.kit,
            execution_receipt=self.receipt,
            suggestions=self.suggestions,
        )
        payload = attestation.model_dump(mode="json")
        payload["source_artifact_sha256"] = "9" * 64
        with self.assertRaisesRegex(ValidationError, "identity does not match"):
            type(attestation).model_validate(payload)

        payload = attestation.model_dump(mode="json")
        payload["evidence_refs"] = [
            payload["evidence_refs"][1],
            payload["evidence_refs"][0],
        ]
        with self.assertRaisesRegex(ValidationError, "unique, and sorted"):
            type(attestation).model_validate(payload)


def _suggestions(bundle_sha256: str) -> EntryClassificationAgentSuggestionSetV1:
    return EntryClassificationAgentSuggestionSetV1(
        source_sha256="a" * 64,
        working_tree_sha256="b" * 64,
        effective_structure_id="effective-structure-1",
        policy_version="entry-classification-policy/v1",
        agent_id="reqpilotx-requirement-baseline-curator",
        kit_bundle_sha256=bundle_sha256,
        entries=(
            EntryClassificationAgentSuggestionV1(
                workflow_node_id="entry-1",
                suggested_type=EntryClassificationType.TECHNICAL,
                reason_codes=("SYSTEM_CAPABILITY_OR_CONSTRAINT",),
                evidence_refs=("node-" + "1" * 32,),
                confidence=0.94,
            ),
            EntryClassificationAgentSuggestionV1(
                workflow_node_id="entry-2",
                suggested_type=EntryClassificationType.INFORMATION,
                reason_codes=("DESCRIPTIVE_CONTEXT_ONLY",),
                evidence_refs=("node-" + "2" * 32,),
                confidence=0.87,
            ),
        ),
    )


def _receipt(
    policy_closure,  # type: ignore[no-untyped-def]
    runner,  # type: ignore[no-untyped-def]
    suggestions: EntryClassificationAgentSuggestionSetV1,
) -> D3ClassificationExecutionReceiptV1:
    return D3ClassificationExecutionReceiptV1(
        run_id="agent-run-" + "3" * 32,
        source_sha256=suggestions.source_sha256,
        source_artifact_id="agent-artifact-" + "2" * 32,
        source_artifact_sha256="c" * 64,
        working_tree_sha256=suggestions.working_tree_sha256,
        effective_structure_id=suggestions.effective_structure_id,
        classification_policy_version=suggestions.policy_version,
        kit_identity=policy_closure.kit_identity,
        agent_id=policy_closure.agent_id,
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
        requested_stages=AGENT_BASELINE_STAGE_ORDER,
        loaded_policy_closure=policy_closure,
        suggestion_set_sha256=canonical_sha256(suggestions.model_dump(mode="json")),
        evidence_refs=tuple(
            sorted(
                {
                    evidence_ref
                    for entry in suggestions.entries
                    for evidence_ref in entry.evidence_refs
                }
            )
        ),
    )


if __name__ == "__main__":
    unittest.main()

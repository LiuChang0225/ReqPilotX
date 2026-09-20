from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

if __package__:
    from .test_primary_runtime_pdf import _runtime_fixture
else:
    from test_primary_runtime_pdf import _runtime_fixture

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from pydantic import ValidationError

from app.adapters.agents.codex_app_server.canonical_document_session import (
    CanonicalAppServerDocumentSessionAttestationV1,
    CanonicalAppServerStageAttestationV1,
    CanonicalProductEvidenceRequest,
    CanonicalSourceRequirementStageInputRequest,
    CanonicalStageFileDigestV1,
    CanonicalStageRunnerOutputSchemaVariantV1,
    compile_canonical_app_server_stage_binding,
    prepare_canonical_app_server_document_session,
)
from app.adapters.agents.codex_app_server.document_map_scope_stage_input import (
    DOCUMENT_MAP_SCOPE_EXECUTION_MODE,
)
from app.adapters.agents.codex_app_server.synthetic_pdf_fixture import (
    build_synthetic_pdf,
)
from app.adapters.agents.codex_app_server.lifecycle import (
    CanaryRuntimeState,
    LifecycleProbeError,
)
from app.adapters.agents.portable_kit import (
    AgentKitRunnerOutputSchemaVariantV1,
    AgentKitValidationError,
    load_portable_agent_kit,
)
from app.application.agent_workflow.extraction_runtime import AgentExtractionStage
from app.application.agent_workflow.document_map_scope import (
    DocumentMapDetailScopeKind,
    DocumentMapHeadingCandidateRefV1,
    DocumentMapHeadingDecisionV1,
    DocumentMapHeadingDisposition,
    DocumentMapHeadingScopeActionV2,
    DocumentMapHeadingScopeCandidateV2,
    DocumentMapScopeRequestV1,
    DocumentMapSourceEndpointV1,
    DocumentMapSourceSpanV1,
    compile_document_map_heading_scope,
)
from app.application.agent_workflow.document_map_replacement import (
    DOCUMENT_MAP_SCOPE_REPLACEMENT_EXECUTION_MODE,
    build_document_map_scope_replacement_request,
)
from app.application.agent_workflow.native_heading_skeleton import (
    NativeHeadingCandidateState,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
AGENT_KIT_ROOT = REPOSITORY_ROOT / "agent-kit"


class CanonicalAppServerDocumentSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="canonical-primary-runtime-")
        self.addCleanup(directory.cleanup)
        self.primary_runtime_root = _runtime_fixture(Path(directory.name))

    def test_materializes_exact_product_kit_and_emits_content_free_attestation(
        self,
    ) -> None:
        canonical = load_portable_agent_kit(AGENT_KIT_ROOT)
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            runtime = _runtime(state)
            original_workspace = runtime.sterile_cwd
            original_marker = original_workspace / "host-owned-marker"
            original_marker.write_text("preserve", encoding="utf-8")
            session = prepare_canonical_app_server_document_session(
                primary_runtime_root=self.primary_runtime_root,
                base_runtime=runtime,
                canonical_kit_root=AGENT_KIT_ROOT,
                stages=(
                    AgentExtractionStage.DOCUMENT_UNDERSTANDING,
                    AgentExtractionStage.LOGICAL_STRUCTURE,
                    AgentExtractionStage.PARENT_PROVISION_RECOGNITION,
                ),
            )
            private_root = session.session_root
            try:
                self.assertEqual(session.kit.identity, canonical.identity)
                self.assertEqual(session.materialized.identity, canonical.identity)
                self.assertTrue(
                    session.runtime.sterile_cwd.resolve().is_relative_to(
                        state.resolve()
                    )
                )
                self.assertTrue(
                    session.runtime.sterile_cwd.resolve().is_relative_to(
                        private_root.resolve()
                    )
                )
                self.assertNotIn("runtime_assets", str(session.runtime.sterile_cwd))
                self.assertEqual(
                    session.runtime.sterile_cwd,
                    session.materialized.root,
                )
                self.assertEqual(
                    session.materialized.root.stat().st_mode & 0o777,
                    0o700,
                )
                self.assertEqual(private_root.stat().st_mode & 0o777, 0o700)
                self.assertEqual(private_root.parent.stat().st_mode & 0o777, 0o700)

                attestation = session.attest()
                serialized = json.dumps(
                    attestation.model_dump(mode="json"),
                    ensure_ascii=False,
                    sort_keys=True,
                )
                self.assertTrue(attestation.content_free)
                self.assertTrue(attestation.exact_bundle_materialized)
                self.assertTrue(attestation.private_runtime_state)
                self.assertFalse(attestation.runtime_assets_copied)
                self.assertEqual(
                    attestation.agent_native_read_capability.status,
                    "BLOCKED",
                )
                self.assertIsNone(attestation.document_evidence)
                self.assertFalse(
                    attestation.agent_native_read_capability.real_transport_eligible
                )
                self.assertIn(
                    "CANONICAL_NATIVE_READ_LIFECYCLE_FEATURES_DISABLED",
                    attestation.agent_native_read_capability.diagnostic_codes,
                )
                self.assertEqual(
                    attestation.kit_identity.bundle_sha256,
                    canonical.identity.bundle_sha256,
                )
                self.assertEqual(attestation.adapter_id, "codex-app-server-product")
                self.assertEqual(attestation.adapter_version, "0.12.1")
                self.assertIsNotNone(attestation.d3_policy_closure_sha256)
                self.assertNotIn(str(state), serialized)
                self.assertNotIn("prototype/runtime_assets", serialized)
                self.assertNotIn(
                    "requirements are documented representations", serialized
                )

                for binding in session.bindings:
                    relative_skill = binding.attestation.skill.path
                    self.assertEqual(
                        binding.skill_path,
                        (session.materialized.root / relative_skill).resolve(),
                    )
                    self.assertEqual(binding.skill_path.stat().st_mode & 0o777, 0o600)
                    self.assertEqual(len(binding.attestation.context_sha256), 64)
                    self.assertEqual(
                        tuple(item.name for item in binding.attestation.required_tools),
                        tuple(
                            item.name
                            for item in session.kit.required_tools_for_stage(
                                binding.attestation.stage
                            )
                        ),
                    )
            finally:
                session.close()
            self.assertFalse(private_root.exists())
            self.assertEqual(original_marker.read_text(encoding="utf-8"), "preserve")

    def test_product_evidence_enables_session_root_native_read_profile(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            corpus = state / "authorized-corpus"
            corpus.mkdir(mode=0o700)
            source = corpus / "source.pdf"
            build_synthetic_pdf(source, include_prompt_injection=True)
            source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
            session = prepare_canonical_app_server_document_session(
                primary_runtime_root=self.primary_runtime_root,
                base_runtime=_runtime(state),
                canonical_kit_root=AGENT_KIT_ROOT,
                stages=(AgentExtractionStage.DOCUMENT_UNDERSTANDING,),
                evidence_request=CanonicalProductEvidenceRequest(
                    source_path=source,
                    expected_source_sha256=source_sha256,
                    document_role="CUSTOMER_SOR",
                    authorized_corpus_root=corpus,
                    run_id="agent-run-" + "1" * 32,
                    opaque_source_handle="source-handle-" + "2" * 32,
                ),
            )
            private_root = session.session_root
            try:
                attestation = session.attest()
                self.assertEqual(
                    session.runtime.sterile_cwd,
                    session.materialized.root,
                )
                self.assertEqual(
                    attestation.agent_native_read_capability.status,
                    "ATTESTED",
                )
                self.assertTrue(
                    attestation.agent_native_read_capability.real_transport_eligible
                )
                self.assertIsNotNone(attestation.native_runtime_policy)
                self.assertIsNotNone(attestation.pdf_primary_runtime)
                self.assertIsNotNone(attestation.document_evidence)
                assert attestation.document_evidence is not None
                self.assertEqual(
                    attestation.document_evidence.source_sha256,
                    source_sha256,
                )
                self.assertEqual(
                    attestation.document_evidence.run_id,
                    "agent-run-" + "1" * 32,
                )
                self.assertTrue(
                    (private_root / "evidence-workspace/source/document.pdf").is_file()
                )
                activation = session.require_inference_stage(
                    AgentExtractionStage.DOCUMENT_UNDERSTANDING
                )
                self.assertEqual(
                    activation.evidence_workspace_path,
                    private_root / "evidence-workspace",
                )
                self.assertEqual(
                    activation.visual_input_paths,
                    (
                        (
                            private_root / "evidence-workspace/contact-sheet.png"
                        ).resolve(),
                    ),
                )
                self.assertEqual(
                    activation.turn_sandbox_policy["type"],
                    "readOnly",
                )
                readable_roots = activation.turn_sandbox_policy["access"][
                    "readableRoots"
                ]
                self.assertEqual(readable_roots[0], str(private_root.resolve()))
                self.assertEqual(len(readable_roots), 2)
                assert attestation.native_runtime_policy is not None
                assert attestation.pdf_primary_runtime is not None
                self.assertEqual(
                    attestation.native_runtime_policy.additional_readable_root_path_sha256s,
                    (attestation.pdf_primary_runtime.dependency_root_path_sha256,),
                )
                self.assertEqual(
                    attestation.pdf_primary_runtime.distribution_id,
                    "pdf@openai-primary-runtime",
                )
                self.assertEqual(
                    attestation.pdf_primary_runtime.runtime_bundle_version,
                    session.kit.skill_provenance["pdf"].runtime_bundle_version,
                )
                serialized = attestation.model_dump_json()
                self.assertNotIn(str(private_root), serialized)
                self.assertNotIn(str(source), serialized)
            finally:
                session.close()
            self.assertFalse(private_root.exists())

    def test_document_map_scope_variant_binds_private_exact_scope_input(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            runtime = _runtime(state)
            corpus = state / "authorized-corpus"
            corpus.mkdir(mode=0o700)
            source = corpus / "source.pdf"
            build_synthetic_pdf(source, include_prompt_injection=True)
            source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
            evidence_request = CanonicalProductEvidenceRequest(
                source_path=source,
                expected_source_sha256=source_sha256,
                document_role="CUSTOMER_SOR",
                authorized_corpus_root=corpus,
                run_id="agent-run-" + "6" * 32,
                opaque_source_handle="source-handle-" + "7" * 32,
            )
            bootstrap = prepare_canonical_app_server_document_session(
                primary_runtime_root=self.primary_runtime_root,
                base_runtime=runtime,
                canonical_kit_root=AGENT_KIT_ROOT,
                stages=(AgentExtractionStage.DOCUMENT_UNDERSTANDING,),
                evidence_request=evidence_request,
            )
            try:
                evidence = bootstrap.attest().document_evidence
                assert evidence is not None
                scope_request = _document_map_scope_request(evidence)
            finally:
                bootstrap.close()

            session = prepare_canonical_app_server_document_session(
                primary_runtime_root=self.primary_runtime_root,
                base_runtime=runtime,
                canonical_kit_root=AGENT_KIT_ROOT,
                stages=(AgentExtractionStage.DOCUMENT_UNDERSTANDING,),
                evidence_request=evidence_request,
                document_map_scope_stage_input_request=scope_request,
            )
            private_root = session.session_root
            try:
                attestation = session.attest()
                self.assertIsNotNone(attestation.document_map_scope_stage_input)
                activation = session.require_inference_stage(
                    AgentExtractionStage.DOCUMENT_UNDERSTANDING,
                    execution_mode=DOCUMENT_MAP_SCOPE_EXECUTION_MODE,
                )
                self.assertEqual(
                    activation.runner_output_schema_id,
                    "document-map-heading-scope-candidate/v2",
                )
                self.assertEqual(
                    activation.document_map_scope_stage_input_attestation,
                    attestation.document_map_scope_stage_input,
                )
                self.assertEqual(
                    activation.stage_input_manifest_path,
                    private_root
                    / "document-map-scope-stage-input"
                    / "manifest.json",
                )
                self.assertTrue(activation.stage_input_manifest_path.is_file())
            finally:
                session.close()
            self.assertFalse(private_root.exists())

    def test_document_map_replacement_variant_binds_accepted_heading_predecessor(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            runtime = _runtime(state)
            corpus = state / "authorized-corpus"
            corpus.mkdir(mode=0o700)
            source = corpus / "source.pdf"
            build_synthetic_pdf(source)
            evidence_request = CanonicalProductEvidenceRequest(
                source_path=source,
                expected_source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                document_role="CUSTOMER_SOR",
                authorized_corpus_root=corpus,
                run_id="agent-run-" + "6" * 32,
                opaque_source_handle="source-handle-" + "7" * 32,
            )
            bootstrap = prepare_canonical_app_server_document_session(
                primary_runtime_root=self.primary_runtime_root,
                base_runtime=runtime,
                canonical_kit_root=AGENT_KIT_ROOT,
                stages=(AgentExtractionStage.DOCUMENT_UNDERSTANDING,),
                evidence_request=evidence_request,
            )
            try:
                evidence = bootstrap.attest().document_evidence
                assert evidence is not None
                scope = _document_map_scope_request(evidence)
            finally:
                bootstrap.close()
            candidate = DocumentMapHeadingScopeCandidateV2(
                request_id=scope.scope_id,
                request_sha256=scope.request_sha256,
                action=DocumentMapHeadingScopeActionV2.ACCEPT_SCOPE,
                accepted_source_span=scope.suggested_source_span,
                heading_decisions=(
                    DocumentMapHeadingDecisionV1(
                        candidate_id=scope.heading_candidates[0].candidate_id,
                        disposition=DocumentMapHeadingDisposition.HEADING,
                        parent_candidate_id=None,
                        confidence=0.99,
                        public_summary="Source heading confirmed.",
                    ),
                ),
                heading_expansion_proposals=(),
                findings=(),
                public_summary="Heading scope confirmed.",
            )
            replacement_request = build_document_map_scope_replacement_request(
                scope_request=scope,
                heading_compilation=compile_document_map_heading_scope(request=scope, candidate=candidate),
            )
            session = prepare_canonical_app_server_document_session(
                primary_runtime_root=self.primary_runtime_root,
                base_runtime=runtime,
                canonical_kit_root=AGENT_KIT_ROOT,
                stages=(AgentExtractionStage.DOCUMENT_UNDERSTANDING,),
                evidence_request=evidence_request,
                document_map_replacement_stage_input_request=replacement_request,
            )
            try:
                activation = session.require_inference_stage(
                    AgentExtractionStage.DOCUMENT_UNDERSTANDING,
                    execution_mode=DOCUMENT_MAP_SCOPE_REPLACEMENT_EXECUTION_MODE,
                )
                self.assertEqual(activation.runner_output_schema_id, "document-map-scope-replacement-candidate/v2")
                self.assertIsNotNone(activation.document_map_replacement_stage_input_attestation)
                self.assertEqual(
                    activation.document_map_replacement_stage_input_attestation.replacement_request_sha256,
                    replacement_request.request_sha256,
                )
                self.assertEqual(activation.stage_input_manifest_path.parent.name, "document-map-replacement-stage-input")
            finally:
                session.close()

    def test_product_evidence_cannot_override_missing_restricted_read_protocol(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            corpus = state / "authorized-corpus"
            corpus.mkdir(mode=0o700)
            source = corpus / "source.pdf"
            build_synthetic_pdf(source, include_prompt_injection=False)
            source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
            runtime = replace(
                _runtime(state),
                protocol={
                    "codex_version": "current-without-restricted-read",
                    "restricted_read_access": "false",
                },
            )
            session = prepare_canonical_app_server_document_session(
                primary_runtime_root=self.primary_runtime_root,
                base_runtime=runtime,
                canonical_kit_root=AGENT_KIT_ROOT,
                stages=(AgentExtractionStage.DOCUMENT_UNDERSTANDING,),
                evidence_request=CanonicalProductEvidenceRequest(
                    source_path=source,
                    expected_source_sha256=source_sha256,
                    document_role="CUSTOMER_SOR",
                    authorized_corpus_root=corpus,
                    run_id="agent-run-" + "3" * 32,
                    opaque_source_handle="source-handle-" + "4" * 32,
                ),
            )
            try:
                attestation = session.attest()
                self.assertIsNotNone(attestation.document_evidence)
                self.assertIsNone(attestation.native_runtime_policy)
                self.assertEqual(
                    attestation.agent_native_read_capability.status,
                    "BLOCKED",
                )
                self.assertIn(
                    "CANONICAL_NATIVE_READ_RESTRICTED_ROOT_UNSUPPORTED",
                    attestation.agent_native_read_capability.diagnostic_codes,
                )
            finally:
                session.close()

    def test_available_d1_returns_domain_and_pdf_capability_skills(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            session = prepare_canonical_app_server_document_session(
                primary_runtime_root=self.primary_runtime_root,
                base_runtime=_runtime(Path(raw)),
                canonical_kit_root=AGENT_KIT_ROOT,
                stages=(AgentExtractionStage.DOCUMENT_UNDERSTANDING,),
            )
            try:
                activation = session.require_inference_stage(
                    AgentExtractionStage.DOCUMENT_UNDERSTANDING
                )
                self.assertEqual(
                    tuple(activation.skill_paths),
                    ("reqpilotx-document-understanding", "pdf"),
                )
                self.assertEqual(
                    tuple(
                        item.skill_id
                        for item in session.bindings[0].attestation.capability_skills
                    ),
                    ("pdf",),
                )
                self.assertEqual(
                    tuple(
                        name
                        for name, _path in session.bindings[0].capability_skill_paths
                    ),
                    ("pdf",),
                )
                self.assertEqual(
                    activation.context_sha256,
                    session.bindings[0].attestation.context_sha256,
                )
                self.assertEqual(
                    activation.artifact_schema_sha256,
                    session.bindings[0].attestation.artifact_schema.sha256,
                )
                self.assertIsNone(activation.d3_policy_closure)
            finally:
                session.close()

    def test_parent_stage_attests_locked_runner_schema_variants(self) -> None:
        kit = load_portable_agent_kit(AGENT_KIT_ROOT)
        binding = compile_canonical_app_server_stage_binding(
            kit,
            AgentExtractionStage.PARENT_PROVISION_RECOGNITION,
        )

        variants = binding.attestation.runner_output_schema_variants
        self.assertEqual(
            tuple(item.execution_mode for item in variants),
            (
                "OBJECT_BATCH",
                "OBJECT_RECONCILIATION",
                "OBJECT_WINDOW",
                "SOURCE_ITEM_TEXT_BATCH",
                "SOURCE_ITEM_TEXT_BOUNDARY_PLAN",
                "SOURCE_ITEM_TEXT_CLASSIFICATION_WINDOW",
                "SOURCE_REQUIREMENT_BOUNDARY_PLAN",
                "SOURCE_REQUIREMENT_CLASSIFICATION_WINDOW",
                "SOURCE_REQUIREMENT_SLICE",
            ),
        )
        by_mode = {item.execution_mode: item for item in variants}
        variant = by_mode["OBJECT_BATCH"]
        self.assertTrue(variant.content_free)
        self.assertEqual(variant.schema_id, "parent-provision-object-candidate/v1")
        self.assertEqual(
            variant.schema_file.path,
            "schemas/parent-provision-object-candidate.v1.schema.json",
        )
        self.assertEqual(
            variant.schema_file.sha256,
            hashlib.sha256(
                (kit.root / variant.schema_file.path).read_bytes()
            ).hexdigest(),
        )
        self.assertEqual(
            variant.variant_sha256,
            _canonical_sha256(
                variant.model_dump(mode="json", exclude={"variant_sha256"})
            ),
        )
        reconciliation = by_mode["OBJECT_RECONCILIATION"]
        self.assertEqual(
            reconciliation.schema_id,
            "parent-provision-object-reconciliation-candidate/v1",
        )
        self.assertEqual(
            reconciliation.schema_file.path,
            "schemas/parent-provision-object-reconciliation-candidate.v1.schema.json",
        )
        self.assertEqual(
            reconciliation.schema_file.sha256,
            hashlib.sha256(
                (kit.root / reconciliation.schema_file.path).read_bytes()
            ).hexdigest(),
        )
        self.assertEqual(
            binding.attestation.runner_output_schema_variants_sha256,
            _canonical_sha256([item.model_dump(mode="json") for item in variants]),
        )

    def test_source_requirement_variant_binds_private_stage_input(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            corpus = state / "authorized-corpus"
            corpus.mkdir(mode=0o700)
            source = corpus / "source.pdf"
            build_synthetic_pdf(source)
            source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
            request = CanonicalSourceRequirementStageInputRequest(
                execution_mode="SOURCE_REQUIREMENT_BOUNDARY_PLAN",
                source_sha256=source_sha256,
                document_map_artifact_id="document-map-" + "1" * 32,
                document_map_artifact_sha256="2" * 64,
                request_sha256="3" * 64,
                scope_id="table-a",
                json_files=(
                    ("document-context.json", {"scope": "table-a"}),
                    ("document-map.full.json", {"artifact": "map"}),
                    ("phase-input.json", {"phase": "boundary"}),
                    ("scope.full.json", {"request": "scope"}),
                    ("scope.json", {"rows": []}),
                ),
            )
            session = prepare_canonical_app_server_document_session(
                primary_runtime_root=self.primary_runtime_root,
                base_runtime=_runtime(state),
                canonical_kit_root=AGENT_KIT_ROOT,
                stages=(AgentExtractionStage.PARENT_PROVISION_RECOGNITION,),
                evidence_request=CanonicalProductEvidenceRequest(
                    source_path=source,
                    expected_source_sha256=source_sha256,
                    document_role="CUSTOMER_SOR",
                    authorized_corpus_root=corpus,
                    run_id="agent-run-" + "4" * 32,
                    opaque_source_handle="source-handle-" + "5" * 32,
                ),
                source_requirement_stage_input_request=request,
            )
            private_root = session.session_root
            try:
                activation = session.require_inference_stage(
                    AgentExtractionStage.PARENT_PROVISION_RECOGNITION,
                    execution_mode="SOURCE_REQUIREMENT_BOUNDARY_PLAN",
                )
                self.assertEqual(
                    activation.runner_output_schema_id,
                    "source-requirement-boundary-agent-candidate/v1",
                )
                self.assertIsNotNone(
                    activation.source_requirement_stage_input_attestation
                )
                self.assertEqual(
                    activation.stage_input_manifest_path,
                    session.source_requirement_stage_input.manifest_path,
                )
                attestation = session.attest()
                self.assertEqual(
                    attestation.source_requirement_stage_input.request_sha256,
                    "3" * 64,
                )
                self.assertTrue(activation.stage_input_manifest_path.is_file())
                self.assertEqual(
                    activation.stage_input_manifest_path.stat().st_mode & 0o777,
                    0o400,
                )
            finally:
                session.close()
            self.assertFalse(private_root.exists())

    def test_activation_selects_variant_without_changing_primary_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            session = prepare_canonical_app_server_document_session(
                primary_runtime_root=self.primary_runtime_root,
                base_runtime=_runtime(Path(raw)),
                canonical_kit_root=AGENT_KIT_ROOT,
                stages=(AgentExtractionStage.DOCUMENT_UNDERSTANDING,),
            )
            try:
                binding = session.bindings[0]
                variant_path = (
                    "schemas/d2-logical-structure-decision-candidate.v2.schema.json"
                )
                variant = _runner_schema_variant(
                    execution_mode="TEST_VARIANT",
                    schema_id="test-runner-output/v1",
                    schema_path=variant_path,
                    schema_sha256=hashlib.sha256(
                        (session.kit.root / variant_path).read_bytes()
                    ).hexdigest(),
                )
                variants_digest = _canonical_sha256([variant.model_dump(mode="json")])
                attestation_payload = binding.attestation.model_dump(mode="json")
                attestation_payload["runner_output_schema_variants"] = [
                    variant.model_dump(mode="json")
                ]
                attestation_payload["runner_output_schema_variants_sha256"] = (
                    variants_digest
                )
                attestation = CanonicalAppServerStageAttestationV1.model_validate(
                    attestation_payload
                )
                session.bindings = (replace(binding, attestation=attestation),)

                primary = session.require_inference_stage(
                    AgentExtractionStage.DOCUMENT_UNDERSTANDING
                )
                selected = session.require_inference_stage(
                    AgentExtractionStage.DOCUMENT_UNDERSTANDING,
                    execution_mode="TEST_VARIANT",
                )

                self.assertIsNone(primary.runner_output_execution_mode)
                self.assertEqual(
                    primary.runner_output_schema_id,
                    binding.attestation.runner_output_schema_id,
                )
                self.assertEqual(selected.runner_output_execution_mode, "TEST_VARIANT")
                self.assertEqual(selected.runner_output_schema_id, variant.schema_id)
                self.assertEqual(
                    selected.runner_output_schema_path,
                    (session.kit.root / variant.schema_file.path).resolve(),
                )
                self.assertEqual(
                    selected.runner_output_schema_sha256,
                    variant.schema_file.sha256,
                )
                self.assertEqual(
                    selected.runner_output_schema_variants_sha256,
                    variants_digest,
                )
                with self.assertRaisesRegex(
                    LifecycleProbeError,
                    "CANONICAL_STAGE_RUNNER_SCHEMA_VARIANT_NOT_FOUND",
                ):
                    session.require_inference_stage(
                        AgentExtractionStage.DOCUMENT_UNDERSTANDING,
                        execution_mode="UNKNOWN_MODE",
                    )
            finally:
                session.close()

    def test_variant_attestation_rejects_digest_duplicate_mode_and_unlocked_path(
        self,
    ) -> None:
        good = _runner_schema_variant(
            execution_mode="OBJECT_BATCH",
            schema_id="object-batch-output/v1",
            schema_path="schemas/d2-logical-structure-decision-candidate.v2.schema.json",
            schema_sha256="1" * 64,
        )
        payload = good.model_dump(mode="json")
        payload["variant_sha256"] = "f" * 64
        with self.assertRaisesRegex(ValidationError, "variant digest drifted"):
            CanonicalStageRunnerOutputSchemaVariantV1.model_validate(payload)

        kit = load_portable_agent_kit(AGENT_KIT_ROOT)
        base = compile_canonical_app_server_stage_binding(
            kit,
            AgentExtractionStage.DOCUMENT_UNDERSTANDING,
        ).attestation
        second = _runner_schema_variant(
            execution_mode="OBJECT_BATCH",
            schema_id="object-batch-output-two/v1",
            schema_path="schemas/d3-parent-boundary-classification-candidate.v2.schema.json",
            schema_sha256="2" * 64,
        )
        stage_payload = base.model_dump(mode="json")
        stage_payload["runner_output_schema_variants"] = [
            good.model_dump(mode="json"),
            second.model_dump(mode="json"),
        ]
        stage_payload["runner_output_schema_variants_sha256"] = _canonical_sha256(
            stage_payload["runner_output_schema_variants"]
        )
        with self.assertRaisesRegex(ValidationError, "modes must be unique"):
            CanonicalAppServerStageAttestationV1.model_validate(stage_payload)

        parent = next(
            item
            for item in kit.manifest.stages
            if item.stage is AgentExtractionStage.PARENT_PROVISION_RECOGNITION
        )
        missing = AgentKitRunnerOutputSchemaVariantV1(
            execution_mode="UNLOCKED_TEST",
            schema_id="unlocked-output/v1",
            schema_path="schemas/not-locked.schema.json",
        )
        changed_parent = parent.model_copy(
            update={
                "runner_output_schema_variants": (
                    *parent.runner_output_schema_variants,
                    missing,
                )
            }
        )
        changed_manifest = kit.manifest.model_copy(
            update={
                "stages": tuple(
                    changed_parent if item.stage is changed_parent.stage else item
                    for item in kit.manifest.stages
                )
            }
        )
        with self.assertRaisesRegex(AgentKitValidationError, "not locked"):
            compile_canonical_app_server_stage_binding(
                replace(kit, manifest=changed_manifest),
                AgentExtractionStage.PARENT_PROVISION_RECOGNITION,
            )

    def test_planned_stage_and_required_tool_closure_fail_before_inference(
        self,
    ) -> None:
        kit = load_portable_agent_kit(AGENT_KIT_ROOT)
        original = next(
            item
            for item in kit.manifest.stages
            if item.stage is AgentExtractionStage.LOGICAL_STRUCTURE
        )
        planned = original.model_copy(
            update={
                "implementation_status": "PLANNED",
                "artifact_schema_path": None,
                "validator_id": None,
                "validator_version": None,
            }
        )
        manifest = kit.manifest.model_copy(
            update={
                "stages": tuple(
                    planned if item.stage is planned.stage else item
                    for item in kit.manifest.stages
                )
            }
        )
        planned_d2_tool_name = next(
            item.name
            for item in kit.tool_registry.tools
            if AgentExtractionStage.LOGICAL_STRUCTURE in item.stages
        )
        planned_tool_registry = kit.tool_registry.model_copy(
            update={
                "tools": tuple(
                    item.model_copy(
                        update={
                            "implementation_status": "PLANNED",
                            "implementation_id": None,
                            "implementation_version": None,
                            "input_schema_path": None,
                            "output_schema_path": None,
                            "required_by_stages": (
                                AgentExtractionStage.LOGICAL_STRUCTURE,
                            ),
                        }
                    )
                    if item.name == planned_d2_tool_name
                    else item
                    for item in kit.tool_registry.tools
                )
            }
        )
        planned_kit = replace(
            kit,
            manifest=manifest,
            tool_registry=planned_tool_registry,
        )

        binding = compile_canonical_app_server_stage_binding(
            planned_kit,
            AgentExtractionStage.LOGICAL_STRUCTURE,
        )

        self.assertFalse(binding.attestation.inference_available)
        self.assertEqual(binding.attestation.context_kind, "POLICY_CLOSURE_ONLY")
        self.assertIn(
            "CANONICAL_STAGE_IMPLEMENTATION_PLANNED",
            binding.attestation.diagnostic_codes,
        )
        self.assertIn(
            "CANONICAL_STAGE_ARTIFACT_SCHEMA_MISSING",
            binding.attestation.diagnostic_codes,
        )
        self.assertIn(
            "CANONICAL_STAGE_VALIDATOR_MISSING",
            binding.attestation.diagnostic_codes,
        )
        self.assertIn(
            "CANONICAL_STAGE_REQUIRED_TOOL_PLANNED",
            binding.attestation.diagnostic_codes,
        )

    def test_available_d2_still_requires_product_evidence_and_native_runtime(
        self,
    ) -> None:
        kit = load_portable_agent_kit(AGENT_KIT_ROOT)
        d2 = next(
            item
            for item in kit.manifest.stages
            if item.stage is AgentExtractionStage.LOGICAL_STRUCTURE
        )
        self.assertEqual(d2.implementation_status, "AVAILABLE")
        with tempfile.TemporaryDirectory() as raw:
            session = prepare_canonical_app_server_document_session(
                primary_runtime_root=self.primary_runtime_root,
                base_runtime=_runtime(Path(raw)),
                canonical_kit_root=AGENT_KIT_ROOT,
                stages=(AgentExtractionStage.LOGICAL_STRUCTURE,),
            )
            try:
                with self.assertRaises(LifecycleProbeError) as raised:
                    session.require_inference_stage(
                        AgentExtractionStage.LOGICAL_STRUCTURE
                    )
                self.assertEqual(
                    raised.exception.reason_code,
                    "CANONICAL_NATIVE_READ_LIFECYCLE_FEATURES_DISABLED",
                )
            finally:
                session.close()

    def test_available_d3_policy_closure_still_requires_evidence_and_stage_input(
        self,
    ) -> None:
        kit = load_portable_agent_kit(AGENT_KIT_ROOT)
        d3 = next(
            item
            for item in kit.manifest.stages
            if item.stage is AgentExtractionStage.PARENT_PROVISION_RECOGNITION
        )
        self.assertEqual(d3.implementation_status, "AVAILABLE")
        with tempfile.TemporaryDirectory() as raw:
            session = prepare_canonical_app_server_document_session(
                primary_runtime_root=self.primary_runtime_root,
                base_runtime=_runtime(Path(raw)),
                canonical_kit_root=AGENT_KIT_ROOT,
                stages=(AgentExtractionStage.PARENT_PROVISION_RECOGNITION,),
            )
            try:
                attestation = session.attest()
                self.assertIsNotNone(session.d3_policy_closure)
                self.assertIsNotNone(attestation.d3_policy_closure_sha256)
                self.assertTrue(attestation.stages[0].inference_available)
                self.assertIsNotNone(attestation.stages[0].artifact_schema)
                self.assertEqual(
                    attestation.stages[0].validator_id,
                    "reqpilotx-parent-provision-validator",
                )
                self.assertNotIn(
                    "CANONICAL_STAGE_ARTIFACT_SCHEMA_MISSING",
                    attestation.stages[0].diagnostic_codes,
                )
                self.assertNotIn(
                    "CANONICAL_STAGE_VALIDATOR_MISSING",
                    attestation.stages[0].diagnostic_codes,
                )
                self.assertIn(
                    "references/requirements-engineering-baseline.md",
                    tuple(item.path for item in attestation.stages[0].references),
                )
                with self.assertRaisesRegex(
                    LifecycleProbeError,
                    "CANONICAL_NATIVE_READ_LIFECYCLE_FEATURES_DISABLED",
                ):
                    session.require_inference_stage(
                        AgentExtractionStage.PARENT_PROVISION_RECOGNITION
                    )
            finally:
                session.close()

    def test_malformed_available_stage_still_fails_closed(self) -> None:
        kit = load_portable_agent_kit(AGENT_KIT_ROOT)
        stage = next(
            item
            for item in kit.manifest.stages
            if item.stage is AgentExtractionStage.PARENT_PROVISION_RECOGNITION
        ).model_copy(
            update={
                "implementation_status": "AVAILABLE",
                "artifact_schema_path": None,
                "validator_id": None,
                "validator_version": None,
            }
        )
        manifest = kit.manifest.model_copy(
            update={
                "stages": tuple(
                    stage if item.stage is stage.stage else item
                    for item in kit.manifest.stages
                )
            }
        )

        binding = compile_canonical_app_server_stage_binding(
            replace(kit, manifest=manifest),
            AgentExtractionStage.PARENT_PROVISION_RECOGNITION,
        )

        self.assertFalse(binding.attestation.inference_available)
        self.assertIn(
            "CANONICAL_STAGE_ARTIFACT_SCHEMA_MISSING",
            binding.attestation.diagnostic_codes,
        )
        self.assertIn(
            "CANONICAL_STAGE_VALIDATOR_MISSING",
            binding.attestation.diagnostic_codes,
        )

    def test_workspace_tamper_fails_attestation_and_cleanup_still_completes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            session = prepare_canonical_app_server_document_session(
                primary_runtime_root=self.primary_runtime_root,
                base_runtime=_runtime(Path(raw)),
                canonical_kit_root=AGENT_KIT_ROOT,
                stages=(AgentExtractionStage.DOCUMENT_UNDERSTANDING,),
            )
            private_root = session.session_root
            binding = session.bindings[0]
            binding.skill_path.write_text("tampered", encoding="utf-8")
            with self.assertRaisesRegex(
                LifecycleProbeError,
                "MODEL_WORKSPACE_MUTATED",
            ):
                session.attest()
            with self.assertRaisesRegex(
                LifecycleProbeError,
                "MODEL_WORKSPACE_MUTATED",
            ):
                session.close()
            self.assertTrue(session.closed)
            self.assertFalse(private_root.exists())

    def test_attestation_identity_tamper_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            session = prepare_canonical_app_server_document_session(
                primary_runtime_root=self.primary_runtime_root,
                base_runtime=_runtime(Path(raw)),
                canonical_kit_root=AGENT_KIT_ROOT,
                stages=(AgentExtractionStage.DOCUMENT_UNDERSTANDING,),
            )
            try:
                payload = session.attest().model_dump(mode="json")
            finally:
                session.close()
        payload["workspace_manifest_sha256"] = "f" * 64
        with self.assertRaisesRegex(
            ValidationError,
            "identity does not match",
        ):
            CanonicalAppServerDocumentSessionAttestationV1.model_validate(payload)

    def test_host_stage_and_non_private_workspace_are_rejected_without_residue(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            runtime = _runtime(state)
            with self.assertRaisesRegex(AgentKitValidationError, "Host-owned D0"):
                prepare_canonical_app_server_document_session(
                    primary_runtime_root=self.primary_runtime_root,
                    base_runtime=runtime,
                    canonical_kit_root=AGENT_KIT_ROOT,
                    stages=(AgentExtractionStage.SOURCE_PREFLIGHT,),
                )
            self.assertFalse(
                (state / "canonical-app-server-document-sessions").exists()
            )

            outside = state.parent / f"outside-{state.name}"
            outside.mkdir()
            try:
                invalid_runtime = replace(runtime, sterile_cwd=outside)
                with self.assertRaisesRegex(
                    AgentKitValidationError,
                    "inside private runtime state",
                ):
                    prepare_canonical_app_server_document_session(
                        primary_runtime_root=self.primary_runtime_root,
                        base_runtime=invalid_runtime,
                        canonical_kit_root=AGENT_KIT_ROOT,
                        stages=(AgentExtractionStage.DOCUMENT_UNDERSTANDING,),
                    )
            finally:
                outside.rmdir()


def _runtime(state: Path) -> CanaryRuntimeState:
    workspace = state / "workspace"
    workspace.mkdir(mode=0o700)
    codex_home = state / "codex-home"
    codex_home.mkdir(mode=0o700)
    sqlite_home = state / "sqlite"
    sqlite_home.mkdir(mode=0o700)
    log_dir = state / "logs"
    log_dir.mkdir(mode=0o700)
    tmp_dir = state / "tmp"
    tmp_dir.mkdir(mode=0o700)
    return CanaryRuntimeState(
        codex_home=codex_home,
        command=("codex", "app-server"),
        environment={},
        log_dir=log_dir,
        protocol={"version": "test", "restricted_read_access": "true"},
        sqlite_home=sqlite_home,
        state_path=state,
        sterile_cwd=workspace,
        tmp_dir=tmp_dir,
    )


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _document_map_scope_request(evidence) -> DocumentMapScopeRequestV1:  # type: ignore[no-untyped-def]
    root_node = "node-" + "1" * 32
    next_node = "node-" + "2" * 32
    span = DocumentMapSourceSpanV1(
        start=DocumentMapSourceEndpointV1(source_node_id=root_node, offset=0),
        end=DocumentMapSourceEndpointV1(source_node_id=next_node, offset=0),
    )
    values = {
        "run_id": evidence.run_id,
        "source_sha256": evidence.source_sha256,
        "source_ir_id": evidence.source_ir_id,
        "source_ir_canonical_sha256": evidence.source_ir_canonical_sha256,
        "evidence_workspace_bundle_sha256": evidence.workspace_bundle_sha256,
        "survey_compilation_sha256": "3" * 64,
        "visual_coverage_receipt_sha256": "4" * 64,
        "heading_skeleton_sha256": "5" * 64,
        "scope_id": "document-map-detail-scope-" + "6" * 32,
        "kind": DocumentMapDetailScopeKind.CHAPTER,
        "advisory_number": "1",
        "advisory_title": "范围",
        "suggested_source_span": span,
        "source_node_ids": (root_node,),
        "page_numbers": (1,),
        "physical_table_ids": (),
        "asset_ids": (),
        "heading_candidates": (
            DocumentMapHeadingCandidateRefV1(
                candidate_id="heading-candidate-" + "7" * 32,
                source_node_id=root_node,
                source_order=1,
                page_number=1,
                number="1",
                title="范围",
                level=1,
                advisory_state=NativeHeadingCandidateState.PROPOSED,
            ),
        ),
        "preceding_context_source_node_ids": (),
        "following_context_source_node_ids": (next_node,),
    }
    draft = DocumentMapScopeRequestV1.model_construct(
        **values,
        request_sha256="0" * 64,
    )
    return DocumentMapScopeRequestV1(
        **values,
        request_sha256=_canonical_sha256(
            draft.model_dump(mode="json", exclude={"request_sha256"})
        ),
    )


def _runner_schema_variant(
    *,
    execution_mode: str,
    schema_id: str,
    schema_path: str,
    schema_sha256: str,
) -> CanonicalStageRunnerOutputSchemaVariantV1:
    schema_file = CanonicalStageFileDigestV1(
        path=schema_path,
        sha256=schema_sha256,
    )
    values = {
        "content_free": True,
        "execution_mode": execution_mode,
        "schema_id": schema_id,
        "schema_file": schema_file,
    }
    return CanonicalStageRunnerOutputSchemaVariantV1(
        **values,
        variant_sha256=_canonical_sha256(
            {**values, "schema_file": schema_file.model_dump(mode="json")}
        ),
    )


if __name__ == "__main__":
    unittest.main()

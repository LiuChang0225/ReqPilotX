from __future__ import annotations

# Test runtime isolation must be activated before importing application modules.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import json
import unittest
from unittest.mock import patch

from app.adapters.agents.codex_app_server.semantic_app_server import (
    CODEX_APP_SERVER_SEMANTIC_PROVIDER_ID,
    CodexAppServerConsensusSemanticProvider,
    CodexSemanticCandidateInvalid,
    LocalCodexAppServerSemanticDriver,
    SemanticDriverResult,
)
from app.domain.extraction.requirement_assembly import RequirementAssemblyStatus
from app.domain.extraction.requirement_view import RequirementProjectionRole
from app.domain.extraction.semantic_provider import SemanticProviderRunStatus
from app.domain.extraction.semantics import RequirementSubtype, SemanticClass
from app.domain.extraction.structure import (
    StructureNodeKind,
    StructureSourceRef,
    StructureSourceRefKind,
)
from app.application.extraction.native_parent_boundary_reference import (
    NativeParentBoundaryReferenceV1,
)
from app.ports.semantic_provider import (
    RequirementAssemblyDecision,
    RequirementAssemblyNodeInput,
    RequirementAssemblyRequest,
    SemanticClassificationRequest,
    SemanticDecision,
    SemanticEvidenceInput,
    SemanticImageInput,
    SemanticNodeInput,
    SemanticProviderRequestRejected,
    SemanticProviderRequestRejectionCode,
)
from tests.test_codex_semantic_mcp import (
    _document_decomposition_pair,
    _parent_boundary_candidate,
    _parent_boundary_request,
)
from tests.test_native_codex_atomization_reference import (
    _reference as _atomization_reference,
    _request as _atomization_request,
)


TARGET = "structure-node-" + "1" * 32
SOURCE_REF = StructureSourceRef(
    kind=StructureSourceRefKind.SOURCE_NODE,
    source_id="node-" + "2" * 32,
)


def _evidence() -> SemanticEvidenceInput:
    text = "The component shall satisfy the specified limit."
    return SemanticEvidenceInput(
        source_ref=SOURCE_REF,
        text=text,
        text_sha256=hashlib.sha256(text.encode()).hexdigest(),
    )


def _classification_request(
    *, with_image: bool = False
) -> SemanticClassificationRequest:
    png = (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + (1).to_bytes(4, "big")
        + (1).to_bytes(4, "big")
    )
    images = (
        (
            SemanticImageInput(
                source_sha256="3" * 64,
                page_number=1,
                width_px=1,
                height_px=1,
                image_bytes=png,
                image_sha256=hashlib.sha256(png).hexdigest(),
            ),
        )
        if with_image
        else ()
    )
    return SemanticClassificationRequest(
        source_ir_id="source-ir-test",
        source_sha256="3" * 64,
        structure_snapshot_id="structure-test",
        scope_structure_node_ids=(TARGET,),
        nodes=(
            SemanticNodeInput(
                target_structure_node_id=TARGET,
                source_order=0,
                kind=StructureNodeKind.CLAUSE,
                image_page_numbers=(1,) if with_image else (),
                target_source_refs=(SOURCE_REF,),
                evidence=(_evidence(),),
            ),
        ),
        images=images,
    )


def _classification_decision(semantic_class: SemanticClass) -> SemanticDecision:
    return SemanticDecision(
        target_structure_node_id=TARGET,
        semantic_class=semantic_class,
        requirement_subtype=(
            RequirementSubtype.PRODUCT_REQUIREMENT
            if semantic_class is SemanticClass.REQUIREMENT
            else None
        ),
        evidence_source_refs=(SOURCE_REF,),
        reason_codes=("source_bound_decision",),
    )


def _assembly_request() -> RequirementAssemblyRequest:
    return RequirementAssemblyRequest(
        source_ir_id="source-ir-test",
        structure_snapshot_id="structure-test",
        semantic_snapshot_id="semantic-test",
        scope_structure_node_ids=(TARGET,),
        nodes=(
            RequirementAssemblyNodeInput(
                target_structure_node_id=TARGET,
                source_order=0,
                kind=StructureNodeKind.CLAUSE,
                semantic_annotation_ids=("semantic-annotation-1",),
                semantic_class=SemanticClass.REQUIREMENT,
                requirement_subtype=RequirementSubtype.PRODUCT_REQUIREMENT,
                evidence=(_evidence(),),
            ),
        ),
    )


def _assembly_decision(include: bool) -> RequirementAssemblyDecision:
    return RequirementAssemblyDecision(
        target_structure_node_id=TARGET,
        include_in_view=include,
        role=RequirementProjectionRole.PRIMARY if include else None,
        status=RequirementAssemblyStatus.CANDIDATE,
        reason_codes=("source_bound_assembly",),
    )


class _FakeDriver:
    model_id = "gpt-5.6-sol"

    def __init__(
        self,
        *,
        classification: list[SemanticDecision] | None = None,
        assembly: list[RequirementAssemblyDecision] | None = None,
    ) -> None:
        self.classification = classification or []
        self.assembly = assembly or []
        self.cancellation_callback = None

    def set_cancellation_requested(self, callback):  # type: ignore[no-untyped-def]
        self.cancellation_callback = callback

    def classify_once(
        self,
        request: SemanticClassificationRequest,
    ) -> SemanticDriverResult:
        del request
        decision = self.classification.pop(0)
        return _result(
            "CLASSIFICATION",
            {"decisions": [decision.model_dump(mode="json")]},
        )

    def assemble_once(
        self,
        request: RequirementAssemblyRequest,
    ) -> SemanticDriverResult:
        del request
        decision = self.assembly.pop(0)
        return _result(
            "ASSEMBLY",
            {"decisions": [decision.model_dump(mode="json")]},
        )


class _RetryingFakeDriver(_FakeDriver):
    def __init__(self, *, classification: list[SemanticDecision]) -> None:
        super().__init__(classification=classification)
        self.invalid_attempts_remaining = 1

    def classify_once(
        self,
        request: SemanticClassificationRequest,
    ) -> SemanticDriverResult:
        if self.invalid_attempts_remaining:
            self.invalid_attempts_remaining -= 1
            raise CodexSemanticCandidateInvalid("semantic_schema_invalid")
        return super().classify_once(request)


class _JointFakeDriver(_FakeDriver):
    def classify_and_assemble_once(
        self,
        request: SemanticClassificationRequest,
    ) -> SemanticDriverResult:
        del request
        classification = self.classification.pop(0)
        assembly = self.assembly.pop(0)
        return _result(
            "JOINT",
            {
                "classifications": [classification.model_dump(mode="json")],
                "assemblies": [assembly.model_dump(mode="json")],
            },
        )


class _AlwaysInvalidFakeDriver(_FakeDriver):
    def classify_once(
        self,
        request: SemanticClassificationRequest,
    ) -> SemanticDriverResult:
        del request
        raise CodexSemanticCandidateInvalid("semantic_schema_invalid")


class _DecompositionFakeDriver(_FakeDriver):
    def decompose_parent_once(self, request):  # type: ignore[no-untyped-def]
        del request
        return _result(
            "DECOMPOSITION",
            _atomization_reference().model_dump(mode="json"),
        )

    def decompose_document_parents_once(self, request):  # type: ignore[no-untyped-def]
        _, reference = _document_decomposition_pair()
        if tuple(item.parent_provision_id for item in request.items) != tuple(
            item.parent_provision_id for item in reference.items
        ):
            raise AssertionError("unexpected decomposition test parent")
        return _result(
            "DOCUMENT_DECOMPOSITION",
            reference.model_dump(mode="json"),
        )


class _ParentBoundaryFakeDriver(_FakeDriver):
    def __init__(self, candidates: list[dict[str, object]]) -> None:
        super().__init__()
        self.candidates = candidates

    def identify_parent_boundaries_once(self, request):  # type: ignore[no-untyped-def]
        del request
        return _result("PARENT_BOUNDARY", self.candidates.pop(0))


def _result(operation: str, payload: dict[str, object]) -> SemanticDriverResult:
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return SemanticDriverResult(
        operation=operation,  # type: ignore[arg-type]
        candidate_payload=payload,
        candidate_sha256=digest,
        runtime_thread_id="thread-test",
        request_sha256="4" * 64,
        tool_receipt_sha256="5" * 64,
        output_schema_sha256="6" * 64,
    )


class CodexSemanticAppServerTests(unittest.TestCase):
    def test_parent_boundary_consensus_uses_host_derived_range_identity(self) -> None:
        first = _parent_boundary_candidate()
        second = json.loads(json.dumps(first))
        second["provisions"][0]["provision_id"] = "native-provision-" + "9" * 32
        second["provisions"][0]["reason_codes"] = ["same_range_second_wording"]
        provider = CodexAppServerConsensusSemanticProvider(
            _ParentBoundaryFakeDriver([first, second]),
            repetition_count=1,
            max_concurrent_repetitions=1,
        )

        result = provider.identify_parent_boundaries(_parent_boundary_request())

        self.assertEqual(result.run.request_count, 2)
        self.assertNotEqual(
            result.reference.provisions[0].provision_id,
            NativeParentBoundaryReferenceV1.model_validate(first)
            .provisions[0]
            .provision_id,
        )

    def test_document_semantic_provider_batches_parent_decomposition(self) -> None:
        provider = CodexAppServerConsensusSemanticProvider(
            _DecompositionFakeDriver(),
            repetition_count=1,
            max_concurrent_repetitions=1,
        )
        request, _ = _document_decomposition_pair()

        result = provider.decompose_document_parents(request)

        self.assertEqual(len(result.reference.items), 1)
        self.assertEqual(result.run.operation, "decompose_document_parents")
        self.assertEqual(result.run.request_count, 2)

    def test_document_semantic_provider_decomposes_parent_with_same_adapter(
        self,
    ) -> None:
        provider = CodexAppServerConsensusSemanticProvider(
            _DecompositionFakeDriver(),
            repetition_count=1,
            max_concurrent_repetitions=1,
        )

        result = provider.atomize(_atomization_request())

        self.assertEqual(len(result.obligations), 1)
        self.assertEqual(result.run.operation, "atomize_requirement_obligation")
        self.assertEqual(result.run.request_count, 1)

    def test_product_driver_reuses_one_document_session_until_explicit_close(
        self,
    ) -> None:
        classification = _classification_decision(SemanticClass.REQUIREMENT)
        assembly = _assembly_decision(True)
        candidate = _result(
            "JOINT",
            {
                "classifications": [classification.model_dump(mode="json")],
                "assemblies": [assembly.model_dump(mode="json")],
            },
        )

        class _Session:
            def __init__(self) -> None:
                self.calls = 0
                self.closed = 0

            def run(self, operation, request):  # type: ignore[no-untyped-def]
                del operation, request
                self.calls += 1
                return candidate

            def close(self):
                self.closed += 1

        session = _Session()
        driver = LocalCodexAppServerSemanticDriver(
            codex_bin=__file__,  # type: ignore[arg-type]
            codex_home=__file__,  # type: ignore[arg-type]
            repository_root=__file__,  # type: ignore[arg-type]
            reuse_document_session=True,
        )
        with patch(
            "app.adapters.agents.codex_app_server.semantic_app_server."
            "_PersistentSemanticDocumentSession",
            return_value=session,
        ) as factory:
            driver.classify_and_assemble_once(_classification_request())
            driver.classify_and_assemble_once(_classification_request())
            driver.identify_parent_boundaries_once(_parent_boundary_request())
            driver.decompose_parent_once(_atomization_request())
            driver.close_document_session()

        self.assertEqual(factory.call_count, 1)
        self.assertEqual(session.calls, 4)
        self.assertEqual(session.closed, 1)

    def test_single_primary_turn_is_supported_for_document_session_mode(self) -> None:
        classification = _classification_decision(SemanticClass.REQUIREMENT)
        assembly = _assembly_decision(True)
        provider = CodexAppServerConsensusSemanticProvider(
            _JointFakeDriver(
                classification=[classification],
                assembly=[assembly],
            ),
            repetition_count=1,
            max_concurrent_repetitions=1,
        )

        classified = provider.classify_nodes(_classification_request())
        assembled = provider.assemble_requirements(_assembly_request())

        self.assertEqual(classified.run.request_count, 1)
        self.assertEqual(
            classified.decisions[0].semantic_class, SemanticClass.REQUIREMENT
        )
        self.assertEqual(assembled.run.request_count, 0)

    def test_provider_forwards_durable_cancellation_callback(self) -> None:
        driver = _FakeDriver()
        provider = CodexAppServerConsensusSemanticProvider(driver)

        def callback() -> bool:
            return True

        provider.set_cancellation_requested(callback)

        self.assertIs(driver.cancellation_callback, callback)

    def test_classification_consensus_accepts_source_bound_page_images(self) -> None:
        requirement = _classification_decision(SemanticClass.REQUIREMENT)
        heading = _classification_decision(SemanticClass.HEADING)
        provider = CodexAppServerConsensusSemanticProvider(
            _FakeDriver(classification=[requirement, heading, requirement])
        )

        result = provider.classify_nodes(_classification_request(with_image=True))

        self.assertEqual(provider.provider_id, CODEX_APP_SERVER_SEMANTIC_PROVIDER_ID)
        self.assertEqual(provider.document_chunk_node_limit, 32)
        self.assertEqual(result.decisions[0].semantic_class, SemanticClass.REQUIREMENT)
        self.assertEqual(result.run.status, SemanticProviderRunStatus.SUCCESS)
        self.assertEqual(result.run.request_count, 3)
        self.assertEqual(result.run.operation, "classify_nodes")

    def test_invalid_vote_is_replaced_without_entering_consensus(self) -> None:
        requirement = _classification_decision(SemanticClass.REQUIREMENT)
        provider = CodexAppServerConsensusSemanticProvider(
            _RetryingFakeDriver(classification=[requirement, requirement, requirement])
        )

        result = provider.classify_nodes(_classification_request())

        self.assertEqual(result.decisions[0].semantic_class, SemanticClass.REQUIREMENT)
        self.assertEqual(result.run.request_count, 4)
        self.assertEqual(result.run.retry_count, 1)

    def test_exhausted_valid_votes_return_adaptive_bisection_code(self) -> None:
        provider = CodexAppServerConsensusSemanticProvider(_AlwaysInvalidFakeDriver())

        with self.assertRaises(SemanticProviderRequestRejected) as caught:
            provider.classify_nodes(_classification_request())

        self.assertIs(
            caught.exception.code,
            SemanticProviderRequestRejectionCode.PROVIDER_OUTPUT_INVALID,
        )

    def test_classification_no_majority_fails_closed_as_partial(self) -> None:
        provider = CodexAppServerConsensusSemanticProvider(
            _FakeDriver(
                classification=[
                    _classification_decision(SemanticClass.REQUIREMENT),
                    _classification_decision(SemanticClass.HEADING),
                    _classification_decision(SemanticClass.BACKGROUND),
                ]
            )
        )

        result = provider.classify_nodes(_classification_request())

        self.assertEqual(result.decisions[0].semantic_class, SemanticClass.UNRESOLVED)
        self.assertEqual(result.run.status, SemanticProviderRunStatus.PARTIAL)
        self.assertEqual(result.run.error_code, "semantic_consensus_unresolved")

    def test_assembly_uses_strict_majority(self) -> None:
        included = _assembly_decision(True)
        excluded = _assembly_decision(False)
        provider = CodexAppServerConsensusSemanticProvider(
            _FakeDriver(assembly=[included, excluded, included])
        )

        result = provider.assemble_requirements(_assembly_request())

        self.assertTrue(result.decisions[0].include_in_view)
        self.assertEqual(result.decisions[0].role, RequirementProjectionRole.PRIMARY)
        self.assertEqual(result.run.status, SemanticProviderRunStatus.SUCCESS)
        self.assertEqual(result.run.operation, "assemble_requirements")

    def test_joint_turn_reuses_remote_requests_for_assembly_receipt(self) -> None:
        classification = _classification_decision(SemanticClass.REQUIREMENT)
        assembly = _assembly_decision(True)
        provider = CodexAppServerConsensusSemanticProvider(
            _JointFakeDriver(
                classification=[classification, classification, classification],
                assembly=[assembly, assembly, assembly],
            )
        )

        classified = provider.classify_nodes(_classification_request())
        assembled = provider.assemble_requirements(_assembly_request())

        self.assertEqual(classified.run.operation, "classify_nodes")
        self.assertEqual(classified.run.request_count, 3)
        self.assertEqual(assembled.run.operation, "assemble_requirements")
        self.assertEqual(assembled.run.request_count, 0)
        self.assertEqual(
            assembled.run.shared_request_provider_run_id,
            classified.run.provider_run_id,
        )
        self.assertTrue(assembled.decisions[0].include_in_view)


if __name__ == "__main__":
    unittest.main()

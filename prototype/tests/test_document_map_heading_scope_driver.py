from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.adapters.agents.codex_app_server.canonical_stage_driver import (
    CanonicalStageTransportResult,
    canonical_sha256,
)
from app.adapters.agents.codex_app_server.document_map_heading_scope_driver import (
    CanonicalDocumentMapHeadingScopeStageDriver,
    DocumentMapHeadingScopeCandidateValidationError,
    DocumentMapHeadingScopeHostCompilationError,
)
from app.adapters.agents.codex_app_server.document_map_scope_stage_input import (
    DOCUMENT_MAP_SCOPE_EXECUTION_MODE,
)
from app.adapters.agents.codex_app_server.lifecycle import LifecycleProbeError
from app.application.agent_workflow.document_map_scope import (
    DocumentMapDetailScopeKind,
    DocumentMapHeadingCandidateRefV1,
    DocumentMapScopeRequestV1,
    DocumentMapSourceEndpointV1,
    DocumentMapSourceSpanV1,
)
from app.application.agent_workflow.extraction_runtime import AgentExtractionStage
from app.application.agent_workflow.native_heading_skeleton import (
    NativeHeadingCandidateState,
)


class DocumentMapHeadingScopeDriverTests(unittest.TestCase):
    def test_compiles_scope_only_after_real_high_resolution_page_views(self) -> None:
        request = _request()
        session = _FakeSession(request)
        transport = _FakeTransport(_candidate(request), pages=(9, 10))

        with _driver_patches():
            result = CanonicalDocumentMapHeadingScopeStageDriver(
                transport=transport
            ).run(
                session,
                request=request,
                task_binding_sha256="f" * 64,
            )

        self.assertEqual(
            session.requests,
            [
                (
                    AgentExtractionStage.DOCUMENT_UNDERSTANDING,
                    DOCUMENT_MAP_SCOPE_EXECUTION_MODE,
                )
            ],
        )
        self.assertIsNotNone(result.compilation)
        self.assertFalse(result.coordination_required)
        self.assertEqual(result.opened_high_resolution_page_numbers, (9, 10))

    def test_missing_visual_page_fails_without_publishing_checkpoint(self) -> None:
        request = _request()
        session = _FakeSession(request)
        transport = _FakeTransport(_candidate(request), pages=(9,))

        with _driver_patches(), self.assertRaisesRegex(
            LifecycleProbeError,
            "DOCUMENT_MAP_HEADING_SCOPE_VISUAL_COVERAGE_INCOMPLETE",
        ):
            CanonicalDocumentMapHeadingScopeStageDriver(transport=transport).run(
                session,
                request=request,
                task_binding_sha256="f" * 64,
            )

    def test_boundary_coordination_is_returned_not_misreported_as_completion(self) -> None:
        request = _request()
        session = _FakeSession(request)
        candidate = _candidate(request)
        candidate["action"] = "COORDINATE_WITH_NEXT"
        candidate["heading_decisions"] = []
        transport = _FakeTransport(candidate, pages=(9, 10))

        with _driver_patches():
            result = CanonicalDocumentMapHeadingScopeStageDriver(
                transport=transport
            ).run(
                session,
                request=request,
                task_binding_sha256="f" * 64,
            )

        self.assertTrue(result.coordination_required)
        self.assertIsNone(result.compilation)

    def test_missing_heading_returns_source_bound_expansion_for_host_coordination(
        self,
    ) -> None:
        request = _request()
        session = _FakeSession(request)
        candidate = _candidate(request)
        candidate["action"] = "EXPAND_HEADING_CANDIDATES"
        candidate["heading_decisions"] = []
        candidate["heading_expansion_proposals"] = [
            {
                "source_node_id": request.source_node_ids[2],
                "number": None,
                "title": "遗漏标题",
                "level": 2,
                "parent_source_node_id": request.source_node_ids[0],
                "confidence": 0.95,
                "public_summary": "该原文节点具有独立标题层级。",
            }
        ]
        transport = _FakeTransport(candidate, pages=(9, 10))

        with _driver_patches():
            result = CanonicalDocumentMapHeadingScopeStageDriver(
                transport=transport
            ).run(
                session,
                request=request,
                task_binding_sha256="f" * 64,
            )

        self.assertTrue(result.coordination_required)
        self.assertEqual(
            result.candidate.heading_expansion_proposals[0].source_node_id,
            request.source_node_ids[2],
        )

    def test_invalid_private_candidate_exposes_only_field_paths(self) -> None:
        request = _request()
        session = _FakeSession(request)
        candidate = _candidate(request)
        candidate["public_summary"] = ""
        transport = _FakeTransport(candidate, pages=(9, 10))

        with _driver_patches(), self.assertRaises(
            DocumentMapHeadingScopeCandidateValidationError
        ) as raised:
            CanonicalDocumentMapHeadingScopeStageDriver(transport=transport).run(
                session,
                request=request,
                task_binding_sha256="f" * 64,
            )

        self.assertIn(
            "public_summary:string_too_short",
            raised.exception.safe_issue_codes,
        )
        self.assertEqual(raised.exception.private_candidate_payload, candidate)

    def test_host_rejection_retains_schema_valid_candidate_for_recovery(self) -> None:
        request = _request()
        session = _FakeSession(request)
        candidate = _candidate(request)
        candidate["heading_decisions"][1]["parent_candidate_id"] = None  # type: ignore[index]
        transport = _FakeTransport(candidate, pages=(9, 10))

        with _driver_patches(), self.assertRaises(
            DocumentMapHeadingScopeHostCompilationError
        ) as raised:
            CanonicalDocumentMapHeadingScopeStageDriver(transport=transport).run(
                session,
                request=request,
                task_binding_sha256="f" * 64,
            )

        self.assertEqual(raised.exception.private_candidate_payload, candidate)
        self.assertEqual(
            raised.exception.safe_issue_code,
            "non_root_heading_requires_a_decided_parent",
        )


class _FakeSession:
    def __init__(self, request: DocumentMapScopeRequestV1) -> None:
        self.requests: list[tuple[AgentExtractionStage, str | None]] = []
        self.activation = SimpleNamespace(
            session_attestation=SimpleNamespace(
                document_evidence=SimpleNamespace(
                    run_id=request.run_id,
                    source_sha256=request.source_sha256,
                )
            ),
            document_map_scope_stage_input_attestation=SimpleNamespace(
                request_sha256=request.request_sha256,
                scope_id=request.scope_id,
            ),
        )

    def require_inference_stage(
        self,
        stage: AgentExtractionStage,
        *,
        execution_mode: str | None = None,
    ):
        self.requests.append((stage, execution_mode))
        return self.activation


class _FakeTransport:
    def __init__(self, candidate: dict[str, object], *, pages: tuple[int, ...]):
        self.candidate = candidate
        self.pages = pages

    def invoke(self, _invocation) -> CanonicalStageTransportResult:  # type: ignore[no-untyped-def]
        summary = {
            "schema_version": "reqpilotx-canonical-stage-event-ledger/v1",
            "visual_inspection": {
                "contact_sheet_opened": False,
                "high_resolution_page_numbers": list(self.pages),
                "other_image_view_count": 0,
            },
        }
        return CanonicalStageTransportResult(
            stage=AgentExtractionStage.DOCUMENT_UNDERSTANDING,
            structured_output=self.candidate,
            output_schema_sha256="4" * 64,
            response_sha256=canonical_sha256(self.candidate),
            tool_receipt_sha256="5" * 64,
            runtime_thread_id="thread-scope",
            runtime_turn_id="turn-scope",
            tool_activity_summary=summary,
        )


class _driver_patches:
    def __enter__(self):
        self._invocation = patch(
            "app.adapters.agents.codex_app_server.document_map_heading_scope_driver."
            "build_canonical_stage_invocation",
            return_value=SimpleNamespace(
                stage=AgentExtractionStage.DOCUMENT_UNDERSTANDING,
                turn_output_schema_sha256="4" * 64,
            ),
        )
        self._schema = patch(
            "app.adapters.agents.codex_app_server.document_map_heading_scope_driver."
            "_load_runner_output_schema",
            return_value={"type": "object"},
        )
        self._invocation.start()
        self._schema.start()
        return self

    def __exit__(self, exc_type, exc, traceback):  # type: ignore[no-untyped-def]
        self._schema.stop()
        self._invocation.stop()


def _request() -> DocumentMapScopeRequestV1:
    root_node = "node-" + "1" * 32
    child_node = "node-" + "2" * 32
    body_node = "node-" + "e" * 32
    next_node = "node-" + "3" * 32
    span = DocumentMapSourceSpanV1(
        start=DocumentMapSourceEndpointV1(source_node_id=root_node, offset=0),
        end=DocumentMapSourceEndpointV1(source_node_id=next_node, offset=0),
    )
    values = {
        "run_id": "agent-run-" + "4" * 32,
        "source_sha256": "5" * 64,
        "source_ir_id": "source-ir-driver-test",
        "source_ir_canonical_sha256": "6" * 64,
        "evidence_workspace_bundle_sha256": "7" * 64,
        "survey_compilation_sha256": "8" * 64,
        "visual_coverage_receipt_sha256": "9" * 64,
        "heading_skeleton_sha256": "a" * 64,
        "scope_id": "document-map-detail-scope-" + "b" * 32,
        "kind": DocumentMapDetailScopeKind.CHAPTER,
        "advisory_number": "1",
        "advisory_title": "范围",
        "suggested_source_span": span,
        "source_node_ids": (root_node, child_node, body_node),
        "page_numbers": (9, 10),
        "physical_table_ids": (),
        "asset_ids": (),
        "heading_candidates": (
            DocumentMapHeadingCandidateRefV1(
                candidate_id="heading-candidate-" + "c" * 32,
                source_node_id=root_node,
                source_order=1,
                page_number=9,
                number="1",
                title="范围",
                level=1,
                advisory_state=NativeHeadingCandidateState.PROPOSED,
            ),
            DocumentMapHeadingCandidateRefV1(
                candidate_id="heading-candidate-" + "d" * 32,
                source_node_id=child_node,
                source_order=2,
                page_number=10,
                number="1.1",
                title="目的",
                level=2,
                advisory_parent_candidate_id="heading-candidate-" + "c" * 32,
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
        request_sha256=canonical_sha256(
            draft.model_dump(mode="json", exclude={"request_sha256"})
        ),
    )


def _candidate(request: DocumentMapScopeRequestV1) -> dict[str, object]:
    root, child = request.heading_candidates
    return {
        "schema_version": "document-map-heading-scope-candidate/v2",
        "request_id": request.scope_id,
        "request_sha256": request.request_sha256,
        "action": "ACCEPT_SCOPE",
        "accepted_source_span": request.suggested_source_span.model_dump(mode="json"),
        "heading_decisions": [
            {
                "candidate_id": root.candidate_id,
                "disposition": "HEADING",
                "parent_candidate_id": None,
                "confidence": 0.99,
                "public_summary": "这是当前章节的标题。",
            },
            {
                "candidate_id": child.candidate_id,
                "disposition": "HEADING",
                "parent_candidate_id": root.candidate_id,
                "confidence": 0.98,
                "public_summary": "这是当前章节的二级标题。",
            },
        ],
        "heading_expansion_proposals": [],
        "findings": [],
        "public_summary": "本章标题层级已完成原文核对。",
    }


if __name__ == "__main__":
    unittest.main()

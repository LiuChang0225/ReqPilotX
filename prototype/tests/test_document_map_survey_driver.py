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
from app.adapters.agents.codex_app_server.document_map_survey_driver import (
    CanonicalDocumentMapSurveyStageDriver,
    DOCUMENT_MAP_SURVEY_EXECUTION_MODE,
    DocumentMapSurveyCandidateValidationError,
)
from app.adapters.agents.codex_app_server.lifecycle import LifecycleProbeError
from app.application.agent_workflow.document_map_exploration import (
    DocumentMapVisualCoverageMode,
)
from app.application.agent_workflow.extraction_runtime import (
    AgentExtractionStage,
    AgentKitIdentityV1,
)


class DocumentMapSurveyDriverTests(unittest.TestCase):
    def test_compiles_private_survey_from_real_visual_event_summary(self) -> None:
        kit = _kit()
        session = _FakeSession(kit)
        transport = _FakeTransport(_candidate(kit), pages=(1, 2))

        with patch(
            "app.adapters.agents.codex_app_server.document_map_survey_driver."
            "build_canonical_stage_invocation",
            return_value=SimpleNamespace(
                stage=AgentExtractionStage.DOCUMENT_UNDERSTANDING,
                turn_output_schema_sha256="4" * 64,
            ),
        ), patch(
            "app.adapters.agents.codex_app_server.document_map_survey_driver."
            "_load_runner_output_schema",
            return_value={"type": "object"},
        ):
            result = CanonicalDocumentMapSurveyStageDriver(
                transport=transport,
                visual_coverage_mode=DocumentMapVisualCoverageMode.REFERENCE_FULL_PAGE,
            ).run(session, task_binding_sha256="7" * 64)

        self.assertEqual(
            session.requests,
            [
                (
                    AgentExtractionStage.DOCUMENT_UNDERSTANDING,
                    DOCUMENT_MAP_SURVEY_EXECUTION_MODE,
                )
            ],
        )
        self.assertEqual(len(result.compilation.scopes), 2)
        self.assertEqual(
            result.visual_coverage.high_resolution_page_numbers,
            (1, 2),
        )

    def test_fails_closed_when_model_did_not_open_every_reference_page(self) -> None:
        kit = _kit()
        session = _FakeSession(kit)
        transport = _FakeTransport(_candidate(kit), pages=(1,))

        with patch(
            "app.adapters.agents.codex_app_server.document_map_survey_driver."
            "build_canonical_stage_invocation",
            return_value=SimpleNamespace(
                stage=AgentExtractionStage.DOCUMENT_UNDERSTANDING,
                turn_output_schema_sha256="4" * 64,
            ),
        ), patch(
            "app.adapters.agents.codex_app_server.document_map_survey_driver."
            "_load_runner_output_schema",
            return_value={"type": "object"},
        ), self.assertRaisesRegex(
            LifecycleProbeError,
            "DOCUMENT_MAP_SURVEY_HOST_COMPILATION_FAILED",
        ):
            CanonicalDocumentMapSurveyStageDriver(
                transport=transport,
                visual_coverage_mode=DocumentMapVisualCoverageMode.REFERENCE_FULL_PAGE,
            ).run(session, task_binding_sha256="7" * 64)

    def test_invalid_candidate_retains_private_payload_and_safe_field_paths(self) -> None:
        kit = _kit()
        session = _FakeSession(kit)
        candidate = _candidate(kit)
        candidate["ranges"][0]["evidence_refs"] = ["PRIVATE CUSTOMER TEXT"]  # type: ignore[index]
        transport = _FakeTransport(candidate, pages=(1, 2))

        with patch(
            "app.adapters.agents.codex_app_server.document_map_survey_driver."
            "build_canonical_stage_invocation",
            return_value=SimpleNamespace(
                stage=AgentExtractionStage.DOCUMENT_UNDERSTANDING,
                turn_output_schema_sha256="4" * 64,
            ),
        ), patch(
            "app.adapters.agents.codex_app_server.document_map_survey_driver."
            "_load_runner_output_schema",
            return_value={"type": "object"},
        ), self.assertRaises(DocumentMapSurveyCandidateValidationError) as raised:
            CanonicalDocumentMapSurveyStageDriver(
                transport=transport,
                visual_coverage_mode=DocumentMapVisualCoverageMode.REFERENCE_FULL_PAGE,
            ).run(session, task_binding_sha256="7" * 64)

        self.assertIn(
            "ranges.0.evidence_refs.0:string_pattern_mismatch",
            raised.exception.safe_issue_codes,
        )
        self.assertNotIn(
            "PRIVATE CUSTOMER TEXT",
            " ".join(raised.exception.safe_issue_codes),
        )
        self.assertEqual(
            raised.exception.private_candidate_payload["ranges"][0][
                "evidence_refs"
            ],
            ["PRIVATE CUSTOMER TEXT"],
        )
        self.assertTrue(
            raised.exception.tool_activity_summary["visual_inspection"][
                "contact_sheet_opened"
            ]
        )


class _FakeSession:
    def __init__(self, kit: AgentKitIdentityV1) -> None:
        self.requests: list[tuple[AgentExtractionStage, str | None]] = []
        self.activation = SimpleNamespace(
            session_attestation=SimpleNamespace(
                kit_identity=kit,
                document_evidence=SimpleNamespace(
                    run_id="agent-run-" + "1" * 32,
                    source_sha256="2" * 64,
                    page_count=2,
                ),
            )
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
    def __init__(
        self,
        candidate: dict[str, object],
        *,
        pages: tuple[int, ...],
    ) -> None:
        self.candidate = candidate
        self.pages = pages

    def invoke(self, _invocation) -> CanonicalStageTransportResult:  # type: ignore[no-untyped-def]
        summary = {
            "schema_version": "reqpilotx-canonical-stage-event-ledger/v1",
            "visual_inspection": {
                "contact_sheet_opened": True,
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
            runtime_thread_id="thread-survey",
            runtime_turn_id="turn-survey",
            tool_activity_summary=summary,
        )


def _kit() -> AgentKitIdentityV1:
    return AgentKitIdentityV1(
        kit_id="reqpilotx-document-extraction",
        kit_version="0.15.0",
        manifest_sha256="6" * 64,
        bundle_sha256="3" * 64,
        policy_id="reqpilotx-extraction-policy",
        policy_version="1.0.0",
        tool_registry_id="reqpilotx-document-evidence-tools",
        tool_registry_version="1.3.0",
    )


def _candidate(kit: AgentKitIdentityV1) -> dict[str, object]:
    return {
        "schema_version": "document-map-survey-candidate/v1",
        "run_id": "agent-run-" + "1" * 32,
        "source_sha256": "2" * 64,
        "kit_identity": kit.model_dump(mode="json"),
        "page_count": 2,
        "status": "COMPLETE",
        "document_summary": "一份包含封面和正文的测试文档。",
        "ranges": [
            {
                "proposal_key": "front",
                "kind": "FRONT_MATTER",
                "label": "封面",
                "page_numbers": [1],
                "evidence_refs": ["page:1"],
                "public_summary": "封面范围。",
            },
            {
                "proposal_key": "body",
                "kind": "MAIN_DOCUMENT",
                "label": "正文",
                "page_numbers": [2],
                "evidence_refs": ["page:2"],
                "public_summary": "正文范围。",
            },
        ],
        "findings": [],
    }


if __name__ == "__main__":
    unittest.main()

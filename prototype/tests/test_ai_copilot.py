from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.ai_copilot import (
    AICopilotConfig,
    build_assessment_context,
    fallback_need_clarification,
    load_ai_copilot_config,
    reconcile_assessment_output,
    run_ai_copilot_assessment,
    sync_knowledge_document_index,
)


class AICopilotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmpdir.name)
        self.trace_dir = self.tmp_path / "traces"
        self.index_dir = self.tmp_path / "index"
        self.config = AICopilotConfig(
            assessment_mode="heuristic",
            retrieval_backend="local",
            top_k=5,
            trace_dir=self.trace_dir,
            azure_auth_mode="auto",
            azure_allow_interactive_login=False,
            azure_search_endpoint="",
            azure_search_api_key="",
            azure_search_index_name="reqpilot-knowledge",
            azure_openai_endpoint="",
            azure_openai_api_key="",
            azure_openai_api_version="2024-10-21",
            azure_openai_chat_deployment="",
            azure_openai_embedding_deployment="",
            azure_openai_embedding_dimensions=1536,
            azure_search_semantic_configuration="",
            llm_timeout_seconds=45.0,
        )

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_build_assessment_context_includes_section_and_table_projection(self) -> None:
        row = {
            "id": "req-1",
            "req_code": "REQ0001",
            "display_text": "ECU shall support wake-up time less than 100ms.",
            "requirement_type": "FUNCTIONAL",
            "parent_section_label": "5.1 Startup",
            "content_blocks": [{"kind": "paragraph", "text": "Applies to cold start validation."}],
            "table_refs": [{"table_id": "tbl-1", "slice_ai_projection": "|Voltage|12V|"}],
        }

        context = build_assessment_context(row)

        self.assertIn("5.1 Startup", context["retrieval_query"])
        self.assertIn("Applies to cold start validation.", context["content_summary"])
        self.assertIn("|Voltage|12V|", context["table_context"])

    def test_run_ai_copilot_assessment_returns_extended_fields_and_trace(self) -> None:
        row = {
            "id": "req-1",
            "req_code": "REQ0001",
            "display_text": "ECU shall support wake-up time less than 100ms.",
            "requirement_type": "FUNCTIONAL",
            "parent_section_label": "5.1 Startup",
            "content_blocks": [{"kind": "paragraph", "text": "Applies to cold start validation."}],
            "table_refs": [{"table_id": "tbl-1", "slice_ai_projection": "|Voltage|12V|"}],
        }
        knowledge_docs = [
            {
                "id": "kb-1",
                "project_id": "project-1",
                "file_name": "platform-spec.txt",
                "doc_version": "v1.2",
                "extracted_text": (
                    "5.1 Startup\n"
                    "The ECU shall support wake-up time less than 100ms.\n"
                    "Platform validation shows wake-up time of 80ms at -20C.\n"
                ),
            }
        ]

        with patch("app.services.ai_copilot.TRACE_DIR", self.trace_dir), patch(
            "app.services.ai_copilot.INDEX_PAYLOAD_DIR", self.index_dir
        ):
            result = run_ai_copilot_assessment(row, knowledge_docs, config=self.config)

        assessment = result["assessment"]
        self.assertEqual(assessment["assessment_mode"], "heuristic")
        self.assertEqual(assessment["analysis_label"], "REQUIREMENT")
        self.assertTrue(assessment["normalized_requirement"])
        self.assertIn(assessment["risk_level"], {"LOW", "MEDIUM", "HIGH"})
        self.assertEqual(result["runs"][0]["mode"], "screening_heuristic")
        self.assertEqual(result["runs"][-1]["mode"], "heuristic")
        self.assertTrue(Path(result["trace_path"]).exists())
        self.assertEqual(result["evidences"][0]["source_doc_version"], "v1.2")

    def test_run_ai_copilot_assessment_marks_info_rows_and_skips_evidence(self) -> None:
        row = {
            "id": "req-info-1",
            "req_code": "REQ0002",
            "display_text": "Functional status A",
            "requirement_type": "FUNCTIONAL",
            "parent_section_label": "4.5 Status",
        }
        knowledge_docs = [
            {
                "id": "kb-1",
                "project_id": "project-1",
                "file_name": "platform-spec.txt",
                "doc_version": "v1.2",
                "extracted_text": "Any knowledge is fine here.",
            }
        ]

        result = run_ai_copilot_assessment(row, knowledge_docs, config=self.config)

        assessment = result["assessment"]
        self.assertEqual(assessment["analysis_label"], "INFO")
        self.assertEqual(assessment["ai_decision"], "")
        self.assertEqual(assessment["ai_reason"], "")
        self.assertIn("信息", assessment["analysis_basis"])
        self.assertEqual(result["evidences"], [])
        self.assertEqual(result["runs"][0]["mode"], "screening_heuristic")

    def test_run_ai_copilot_assessment_without_knowledge_docs_only_screens_requirement(self) -> None:
        row = {
            "id": "req-no-kb-1",
            "req_code": "REQ0003",
            "display_text": "ECU shall support wake-up time less than 100ms.",
            "requirement_type": "FUNCTIONAL",
            "parent_section_label": "5.1 Startup",
        }

        result = run_ai_copilot_assessment(row, [], config=self.config)

        assessment = result["assessment"]
        self.assertEqual(assessment["analysis_label"], "REQUIREMENT")
        self.assertEqual(assessment["assessment_mode"], "screening_only")
        self.assertEqual(assessment["ai_decision"], "")
        self.assertEqual(assessment["ai_reason"], "")
        self.assertTrue(assessment["normalized_requirement"])
        self.assertIn("需求分析", assessment["analysis_basis"])
        self.assertEqual(result["evidences"], [])
        self.assertEqual(len(result["runs"]), 1)
        self.assertEqual(result["runs"][0]["mode"], "screening_heuristic")

    def test_run_ai_copilot_assessment_prefers_active_chunked_knowledge_over_archived_match(self) -> None:
        row = {
            "id": "req-active-1",
            "req_code": "REQ0004",
            "display_text": "ECU shall support wake-up time less than 100ms.",
            "requirement_type": "FUNCTIONAL",
            "parent_section_label": "5.1 Startup",
        }
        knowledge_docs = [
            {
                "id": "kb-archived",
                "project_id": "project-1",
                "file_name": "archived-spec.txt",
                "doc_version": "v0.8",
                "status": "ARCHIVED",
                "extracted_text": "ECU shall support wake-up time less than 100ms.",
            },
            {
                "id": "kb-active",
                "project_id": "project-1",
                "file_name": "active-spec.txt",
                "doc_version": "v1.2",
                "status": "ACTIVE",
                "extracted_text": (
                    "5.1 Startup\n"
                    "The ECU shall support wake-up time less than 100ms.\n"
                    "Measured wake-up time is 80ms at -20C.\n"
                ),
            },
        ]

        result = run_ai_copilot_assessment(row, knowledge_docs, config=self.config)

        self.assertEqual(result["evidences"][0]["source_doc_id"], "kb-active")
        self.assertEqual(result["assessment"]["analysis_label"], "REQUIREMENT")

    def test_run_ai_copilot_assessment_regulation_without_compliance_signal_falls_back(self) -> None:
        row = {
            "id": "req-reg-1",
            "req_code": "REQ0005",
            "display_text": "Component shall comply with Regulation X section 5.",
            "requirement_type": "REGULATION",
            "parent_section_label": "6.2 Compliance",
        }
        knowledge_docs = [
            {
                "id": "kb-reg-1",
                "project_id": "project-1",
                "file_name": "regulation-note.txt",
                "doc_version": "v1.0",
                "status": "ACTIVE",
                "extracted_text": (
                    "Regulation X section 5 requirement applies to this component.\n"
                    "Section 5 boundary and applicable scope are listed below for the component.\n"
                ),
            }
        ]

        result = run_ai_copilot_assessment(row, knowledge_docs, config=self.config)

        self.assertEqual(result["assessment"]["ai_decision"], "NEED_CLARIFICATION")
        self.assertEqual(result["runs"][-1]["validation_status"], "accepted")

    def test_run_ai_copilot_assessment_conflicting_evidence_falls_back(self) -> None:
        row = {
            "id": "req-conflict-1",
            "req_code": "REQ0006",
            "display_text": "Valve shall support pressure drop less than 10kPa.",
            "requirement_type": "FUNCTIONAL",
            "parent_section_label": "7.2 Fluid Test",
        }
        knowledge_docs = [
            {
                "id": "kb-pos-1",
                "project_id": "project-1",
                "file_name": "positive.txt",
                "doc_version": "v1.0",
                "status": "ACTIVE",
                "extracted_text": "Valve pressure drop is less than 10kPa and passed validation.",
            },
            {
                "id": "kb-neg-1",
                "project_id": "project-1",
                "file_name": "negative.txt",
                "doc_version": "v1.1",
                "status": "ACTIVE",
                "extracted_text": "Valve does not meet pressure drop less than 10kPa and failed validation.",
            },
        ]

        result = run_ai_copilot_assessment(row, knowledge_docs, config=self.config)

        self.assertEqual(result["assessment"]["ai_decision"], "NEED_CLARIFICATION")
        self.assertIn("conflicting_evidences", result["runs"][-1]["fallback_reason"])

    def test_reconcile_assessment_output_reject_without_negative_signal_falls_back(self) -> None:
        context = {
            "requirement_text": "The component shall support wake-up time less than 100ms.",
            "normalized_requirement": "The component shall support wake-up time less than 100ms.",
            "requirement_type": "FUNCTIONAL",
        }
        candidate = {
            "normalized_requirement": context["normalized_requirement"],
            "proposed_decision": "REJECT",
            "confidence": 0.91,
            "reason": "Rejected.",
            "risk_level": "HIGH",
            "clarification_question": "",
            "evidences": [
                {
                    "source_doc_id": "kb-1",
                    "source_doc_name": "spec.txt",
                    "source_location": "Chunk 1",
                    "excerpt": "The component supports wake-up time of 80ms at -20C.",
                    "relevance": 0.88,
                    "source_doc_version": "v1",
                }
            ],
        }
        retrieved = [
            {
                "source_doc_id": "kb-1",
                "source_doc_name": "spec.txt",
                "source_location": "Chunk 1",
                "excerpt": "The component supports wake-up time of 80ms at -20C.",
                "relevance": 0.88,
                "source_doc_version": "v1",
            }
        ]

        result = reconcile_assessment_output(
            context=context,
            candidate=candidate,
            retrieved_evidences=retrieved,
            fallback_reason="",
        )

        self.assertEqual(result["ai_decision"], "NEED_CLARIFICATION")
        self.assertIn("reject_without_negative_signal", result["fallback_reason"])

    def test_reconcile_assessment_output_negative_evidence_overrides_accept(self) -> None:
        context = {
            "requirement_text": "The component shall support wake-up time less than 100ms.",
            "normalized_requirement": "The component shall support wake-up time less than 100ms.",
            "requirement_type": "FUNCTIONAL",
        }
        candidate = {
            "normalized_requirement": context["normalized_requirement"],
            "proposed_decision": "ACCEPT",
            "confidence": 0.81,
            "reason": "Accepted.",
            "risk_level": "LOW",
            "clarification_question": "",
            "evidences": [
                {
                    "source_doc_id": "kb-2",
                    "source_doc_name": "failure.txt",
                    "source_location": "Chunk 2",
                    "excerpt": "The component does not meet wake-up time requirement and failed validation.",
                    "relevance": 0.79,
                    "source_doc_version": "v2",
                }
            ],
        }
        retrieved = [
            {
                "source_doc_id": "kb-2",
                "source_doc_name": "failure.txt",
                "source_location": "Chunk 2",
                "excerpt": "The component does not meet wake-up time requirement and failed validation.",
                "relevance": 0.79,
                "source_doc_version": "v2",
            }
        ]

        result = reconcile_assessment_output(
            context=context,
            candidate=candidate,
            retrieved_evidences=retrieved,
            fallback_reason="",
        )

        self.assertEqual(result["ai_decision"], "REJECT")
        self.assertEqual(result["validation_status"], "hard_rule_override")
        self.assertIn("hard_rule_negative_reject", result["fallback_reason"])

    def test_sync_knowledge_document_index_writes_local_payload(self) -> None:
        knowledge_doc = {
            "id": "kb-2",
            "file_name": "knowledge.txt",
            "doc_version": "2026.03",
            "doc_type": "STANDARD",
            "status": "ACTIVE",
            "effective_from": "2026-01-01",
            "effective_to": "",
            "extracted_text": (
                "1 概述\n"
                "用于整车试验规范。\n"
                "|Item|Condition|\n"
                "|---|---|\n"
                "|Voltage|12V|\n"
            ),
        }

        with patch("app.services.ai_copilot.INDEX_PAYLOAD_DIR", self.index_dir):
            sync = sync_knowledge_document_index("project-1", knowledge_doc, config=self.config)

        self.assertEqual(sync["status"], "READY")
        self.assertGreater(sync["chunk_count"], 0)
        payload_path = Path(sync["payload_path"])
        self.assertTrue(payload_path.exists())
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
        content_kinds = {item["content_kind"] for item in payload["chunks"]}
        self.assertIn("paragraph", content_kinds)
        self.assertIn("table_slice", content_kinds)

    def test_fallback_need_clarification_keeps_candidate_evidence(self) -> None:
        context = {
            "normalized_requirement": "The component shall comply with regulation X.",
            "requirement_type": "REGULATION",
        }
        evidences = [
            {
                "source_doc_id": "kb-3",
                "source_doc_name": "regulation.txt",
                "source_doc_version": "v3",
                "source_location": "Chunk 1",
                "excerpt": "Regulation X applies to this component.",
                "relevance": 0.88,
            }
        ]

        result = fallback_need_clarification(context=context, evidences=evidences, fallback_reason="invalid_output")

        self.assertEqual(result["ai_decision"], "NEED_CLARIFICATION")
        self.assertEqual(result["evidences"][0]["source_doc_id"], "kb-3")
        self.assertEqual(result["fallback_reason"], "invalid_output")

    def test_load_ai_copilot_config_prefers_rbac_sdk_backend_without_keys(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "REQPILOT_AI_ASSESSMENT_MODE": "llm_primary",
                "REQPILOT_AZURE_AUTH_MODE": "rbac",
                "AZURE_AI_SEARCH_ENDPOINT": "https://search.example.search.windows.net",
                "AZURE_AI_SEARCH_INDEX_NAME": "reqpilot-knowledge",
                "AZURE_OPENAI_ENDPOINT": "https://openai.example.openai.azure.com",
                "AZURE_OPENAI_CHAT_DEPLOYMENT": "gpt-4.1-mini",
                "AZURE_OPENAI_EMBEDDING_DEPLOYMENT": "text-embedding-3-large",
            },
            clear=False,
        ):
            config = load_ai_copilot_config()

        self.assertEqual(config.azure_auth_mode, "rbac")
        self.assertEqual(config.retrieval_backend, "azure_sdk")
        self.assertTrue(config.uses_rbac_for_openai)
        self.assertTrue(config.uses_rbac_for_search)
        self.assertTrue(config.azure_ready)

    def test_load_ai_copilot_config_prefers_haystack_backend_with_api_keys(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "REQPILOT_AI_ASSESSMENT_MODE": "llm_primary",
                "REQPILOT_AZURE_AUTH_MODE": "api_key",
                "AZURE_AI_SEARCH_ENDPOINT": "https://search.example.search.windows.net",
                "AZURE_AI_SEARCH_API_KEY": "search-key",
                "AZURE_AI_SEARCH_INDEX_NAME": "reqpilot-knowledge",
                "AZURE_OPENAI_ENDPOINT": "https://openai.example.openai.azure.com",
                "AZURE_OPENAI_API_KEY": "openai-key",
                "AZURE_OPENAI_CHAT_DEPLOYMENT": "gpt-4.1-mini",
                "AZURE_OPENAI_EMBEDDING_DEPLOYMENT": "text-embedding-3-large",
            },
            clear=False,
        ):
            config = load_ai_copilot_config()

        self.assertEqual(config.azure_auth_mode, "api_key")
        self.assertEqual(config.retrieval_backend, "azure_haystack")
        self.assertTrue(config.uses_api_key_for_openai)
        self.assertTrue(config.uses_api_key_for_search)
        self.assertTrue(config.azure_ready)

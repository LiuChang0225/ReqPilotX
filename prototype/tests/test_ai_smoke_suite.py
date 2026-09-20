from __future__ import annotations

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.ai_copilot import AICopilotConfig
from scripts import ai_smoke_suite


class AISmokeSuiteTests(unittest.TestCase):
    def test_run_smoke_suite_skips_azure_profile_when_runtime_is_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cases_path = root / "cases.json"
            out_dir = root / "out"
            cases_path.write_text(
                json.dumps(
                    {
                        "cases": [
                            {
                                "id": "azure-skip-1",
                                "title": "Azure skip",
                                "row": {"id": "req-1", "display_text": "Supplier shall provide EMC evidence."},
                                "knowledge_docs": [{"id": "kb-1"}],
                                "expect": {"assessment_mode": "llm_primary"},
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            self.assertTrue(hasattr(ai_smoke_suite, "run_smoke_suite"), "run_smoke_suite API is required")
            incomplete_config = AICopilotConfig(
                assessment_mode="llm_primary",
                retrieval_backend="azure_sdk",
                top_k=5,
                trace_dir=root / "traces",
                azure_auth_mode="rbac",
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
            with (
                patch("scripts.ai_smoke_suite.load_ai_copilot_config", return_value=incomplete_config) as load_config,
                patch("scripts.ai_smoke_suite.run_ai_copilot_assessment") as run_assessment,
            ):
                summary = ai_smoke_suite.run_smoke_suite(
                    cases_path=cases_path,
                    out_dir=out_dir,
                    mode="llm_primary",
                    profile="azure",
                    allow_skip=True,
                )

            run_assessment.assert_not_called()
            load_config.assert_called_once_with("azure")
            self.assertTrue(summary["skipped"])
            self.assertIn("Azure runtime is not configured", summary["skip_reason"])
            self.assertEqual(summary["profile"], "azure")
            self.assertEqual(summary["provider"], "azure")
            self.assertEqual(summary["retrieval_backend"], "azure_sdk")
            self.assertEqual(summary["cases_total"], 1)
            self.assertTrue((out_dir / "ai_smoke_summary.json").exists())

    def test_run_smoke_suite_uses_selected_llm_mode_for_azure_expectations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cases_path = root / "cases.json"
            out_dir = root / "out"
            cases_path.write_text(
                json.dumps(
                    {
                        "cases": [
                            {
                                "id": "azure-primary-1",
                                "title": "Azure primary mode",
                                "row": {"id": "req-1", "display_text": "Supplier shall provide EMC evidence."},
                                "knowledge_docs": [{"id": "kb-1"}],
                                "expect": {"assessment_mode": "heuristic"},
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            ready_config = AICopilotConfig(
                assessment_mode="llm_primary",
                retrieval_backend="azure_sdk",
                top_k=5,
                trace_dir=root / "traces",
                azure_auth_mode="rbac",
                azure_allow_interactive_login=False,
                azure_search_endpoint="https://example.search.windows.net",
                azure_search_api_key="",
                azure_search_index_name="reqpilot-knowledge",
                azure_openai_endpoint="https://example.openai.azure.com",
                azure_openai_api_key="",
                azure_openai_api_version="2024-10-21",
                azure_openai_chat_deployment="chat",
                azure_openai_embedding_deployment="embedding",
                azure_openai_embedding_dimensions=1536,
                azure_search_semantic_configuration="",
                llm_timeout_seconds=45.0,
            )
            result = {
                "assessment": {
                    "analysis_label": "REQUIREMENT",
                    "ai_decision": "ACCEPT",
                    "assessment_mode": "llm_primary",
                },
                "evidences": [],
                "runs": [{"mode": "assessment_llm_primary"}],
            }
            with (
                patch("scripts.ai_smoke_suite.load_ai_copilot_config", return_value=ready_config),
                patch("scripts.ai_smoke_suite.run_ai_copilot_assessment", return_value=result),
            ):
                summary = ai_smoke_suite.run_smoke_suite(
                    cases_path=cases_path,
                    out_dir=out_dir,
                    mode="llm_primary",
                    profile="azure",
                )

            self.assertTrue(summary["overall_pass"])
            self.assertFalse(summary["heuristic_run_detected"])

    def test_main_returns_nonzero_when_smoke_cases_fail(self) -> None:
        summary = {
            "cases_passed": 0,
            "cases_total": 1,
            "overall_pass": False,
            "skipped": False,
        }
        with (
            patch.object(sys, "argv", ["ai_smoke_suite.py"]),
            patch("scripts.ai_smoke_suite.run_smoke_suite", return_value=summary),
            patch("builtins.print"),
        ):
            with self.assertRaises(SystemExit) as error:
                ai_smoke_suite.main()

        self.assertEqual(error.exception.code, 1)

    def test_run_smoke_suite_syncs_and_cleans_azure_evidence_cases_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cases_path = root / "cases.json"
            out_dir = root / "out"
            cases_path.write_text(
                json.dumps(
                    {
                        "cases": [
                            {
                                "id": "azure-evidence-1",
                                "title": "Azure evidence",
                                "row": {"id": "req-1", "display_text": "Supplier shall provide EMC evidence."},
                                "knowledge_docs": [
                                    {
                                        "id": "kb-1",
                                        "file_name": "emc.txt",
                                        "doc_version": "v1",
                                        "status": "ACTIVE",
                                        "extracted_text": "Supplier EMC evidence is available.",
                                    }
                                ],
                                "expect": {"assessment_mode": "heuristic", "requires_evidence": True},
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            ready_config = AICopilotConfig(
                assessment_mode="llm_primary",
                retrieval_backend="azure_sdk",
                top_k=5,
                trace_dir=root / "traces",
                azure_auth_mode="rbac",
                azure_allow_interactive_login=False,
                azure_search_endpoint="https://example.search.windows.net",
                azure_search_api_key="",
                azure_search_index_name="reqpilot-knowledge",
                azure_openai_endpoint="https://example.openai.azure.com",
                azure_openai_api_key="",
                azure_openai_api_version="2024-10-21",
                azure_openai_chat_deployment="chat",
                azure_openai_embedding_deployment="embedding",
                azure_openai_embedding_dimensions=1536,
                azure_search_semantic_configuration="",
                llm_timeout_seconds=45.0,
            )
            result = {
                "assessment": {
                    "analysis_label": "REQUIREMENT",
                    "ai_decision": "ACCEPT",
                    "assessment_mode": "llm_primary",
                },
                "evidences": [{"chunk_id": "chunk-1"}],
                "runs": [{"mode": "assessment_llm_primary"}],
            }
            with (
                patch("scripts.ai_smoke_suite.load_ai_copilot_config", return_value=ready_config),
                patch(
                    "scripts.ai_smoke_suite.sync_knowledge_document_index",
                    return_value={"status": "READY"},
                ) as sync_knowledge,
                patch("scripts.ai_smoke_suite.delete_azure_search_chunks_for_source_docs") as delete_chunks,
                patch("scripts.ai_smoke_suite.run_ai_copilot_assessment", return_value=result),
            ):
                summary = ai_smoke_suite.run_smoke_suite(
                    cases_path=cases_path,
                    out_dir=out_dir,
                    mode="llm_primary",
                    profile="azure",
                    sync_knowledge=True,
                )

            self.assertTrue(summary["overall_pass"])
            sync_knowledge.assert_called_once()
            synced_doc = sync_knowledge.call_args.args[1]
            self.assertNotEqual(synced_doc["id"], "kb-1")
            self.assertTrue(synced_doc["project_id"].startswith("ai-smoke-"))
            delete_chunks.assert_called_once()
            self.assertEqual(delete_chunks.call_args.kwargs["source_doc_ids"], [synced_doc["id"]])

    def test_run_smoke_suite_cleans_synced_azure_chunks_after_assessment_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cases_path = root / "cases.json"
            cases_path.write_text(
                json.dumps(
                    {
                        "cases": [
                            {
                                "id": "azure-evidence-1",
                                "row": {"id": "req-1", "display_text": "Supplier shall provide EMC evidence."},
                                "knowledge_docs": [{"id": "kb-1", "file_name": "emc.txt", "extracted_text": "EMC evidence."}],
                                "expect": {},
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            ready_config = AICopilotConfig(
                assessment_mode="llm_primary",
                retrieval_backend="azure_sdk",
                top_k=5,
                trace_dir=root / "traces",
                azure_auth_mode="rbac",
                azure_allow_interactive_login=False,
                azure_search_endpoint="https://example.search.windows.net",
                azure_search_api_key="",
                azure_search_index_name="reqpilot-knowledge",
                azure_openai_endpoint="https://example.openai.azure.com",
                azure_openai_api_key="",
                azure_openai_api_version="2024-10-21",
                azure_openai_chat_deployment="chat",
                azure_openai_embedding_deployment="embedding",
                azure_openai_embedding_dimensions=1536,
                azure_search_semantic_configuration="",
                llm_timeout_seconds=45.0,
            )
            with (
                patch("scripts.ai_smoke_suite.load_ai_copilot_config", return_value=ready_config),
                patch(
                    "scripts.ai_smoke_suite.sync_knowledge_document_index",
                    return_value={"status": "READY"},
                ) as sync_knowledge,
                patch("scripts.ai_smoke_suite.delete_azure_search_chunks_for_source_docs") as delete_chunks,
                patch(
                    "scripts.ai_smoke_suite.run_ai_copilot_assessment",
                    side_effect=RuntimeError("assessment failed"),
                ),
                self.assertRaisesRegex(RuntimeError, "assessment failed"),
            ):
                ai_smoke_suite.run_smoke_suite(
                    cases_path=cases_path,
                    out_dir=root / "out",
                    mode="llm_primary",
                    profile="azure",
                    sync_knowledge=True,
                )

            synced_doc = sync_knowledge.call_args.args[1]
            self.assertEqual(delete_chunks.call_args.kwargs["source_doc_ids"], [synced_doc["id"]])


if __name__ == "__main__":
    unittest.main()

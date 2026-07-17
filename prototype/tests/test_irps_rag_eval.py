from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.services.ai_copilot import AICopilotConfig, build_knowledge_chunks
from scripts import irps_rag_eval


class IRPSRAGEvalTests(unittest.TestCase):
    def test_irps_manifest_keeps_docx_searchable_and_pairs_duplicate_pdfs(self) -> None:
        manifest_path = Path(__file__).resolve().parents[1] / "eval" / "irps_rag" / "irps_deepal_edh3c_v1.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        documents_by_id = {document["id"]: document for document in manifest["documents"]}

        self.assertEqual(documents_by_id["irps-product-information-ae"]["ingestion"], "searchable_knowledge")
        self.assertEqual(documents_by_id["irps-product-information-ae-pdf"]["ingestion"], "do_not_index")
        self.assertEqual(documents_by_id["irps-application-note-ae"]["ingestion"], "do_not_index")
        self.assertEqual(
            documents_by_id["irps-application-note-ae"]["duplicate_of"],
            "irps-product-information-ae-pdf",
        )

    def test_prepare_index_documents_generates_indexable_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source_root = Path(tmp)
            (source_root / "product.docx").write_text("placeholder", encoding="utf-8")
            searchable_documents = [
                {
                    "id": "product",
                    "relative_path": "product.docx",
                    "role": "knowledge_primary",
                    "doc_type": "REFERENCE",
                }
            ]
            with patch(
                "scripts.irps_rag_eval.parse_document_with_diagnostics",
                return_value={"text": "Product information\nThe sensor supports analog output.", "segments": []},
            ):
                indexed_docs, _ = irps_rag_eval._prepare_index_documents(
                    searchable_documents=searchable_documents,
                    source_root=source_root,
                    project_id="isolated-evaluation",
                )

            chunks = build_knowledge_chunks("isolated-evaluation", indexed_docs[0])

            self.assertGreater(len(chunks), 0)
            self.assertEqual(chunks[0]["meta"]["source_doc_id"], indexed_docs[0]["source_doc_id"])

    def test_run_corpus_preflight_reports_index_scope_and_verified_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "corpus"
            source_root.mkdir()
            (source_root / "product.docx").write_text("product reference", encoding="utf-8")
            (source_root / "duplicate.pdf").write_text("product reference", encoding="utf-8")
            (source_root / "other-project.pdf").write_text("other project", encoding="utf-8")
            manifest_path = root / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "corpus_id": "test-corpus",
                        "documents": [
                            {
                                "id": "product",
                                "relative_path": "product.docx",
                                "ingestion": "searchable_knowledge",
                            },
                            {
                                "id": "duplicate",
                                "relative_path": "duplicate.pdf",
                                "duplicate_of": "product",
                                "ingestion": "do_not_index",
                            },
                            {
                                "id": "other-project",
                                "relative_path": "other-project.pdf",
                                "ingestion": "searchable_hard_negative",
                            },
                        ],
                        "cases": [{"id": "case", "query": "A requirement."}],
                    }
                ),
                encoding="utf-8",
            )

            report = irps_rag_eval.run_corpus_preflight(
                manifest_path=manifest_path,
                source_root=source_root,
            )

            self.assertEqual(report["status"], "READY")
            self.assertEqual(report["indexable_document_ids"], ["product", "other-project"])
            self.assertEqual(report["summary"]["do_not_index_documents"], 1)
            self.assertTrue(report["documents"][1]["duplicate_verified"])
            self.assertFalse(report["documents"][1]["indexable"])

    def test_main_preflight_only_writes_report_without_loading_azure_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "corpus"
            source_root.mkdir()
            (source_root / "label.xlsx").write_text("labels", encoding="utf-8")
            manifest_path = root / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "corpus_id": "test-corpus",
                        "documents": [
                            {
                                "id": "labels",
                                "relative_path": "label.xlsx",
                                "ingestion": "do_not_index",
                            }
                        ],
                        "cases": [{"id": "case", "query": "A requirement."}],
                    }
                ),
                encoding="utf-8",
            )
            out_dir = root / "out"
            with (
                patch.object(
                    sys,
                    "argv",
                    [
                        "irps_rag_eval.py",
                        "--manifest",
                        str(manifest_path),
                        "--source-root",
                        str(source_root),
                        "--out-dir",
                        str(out_dir),
                        "--preflight-only",
                    ],
                ),
                patch("scripts.irps_rag_eval.load_ai_copilot_config") as load_config,
            ):
                irps_rag_eval.main()

            report = json.loads((out_dir / "irps_rag_corpus_preflight.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "READY")
            load_config.assert_not_called()

    def test_run_corpus_preflight_rejects_paths_outside_source_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "corpus"
            source_root.mkdir()
            (root / "outside.docx").write_text("not corpus content", encoding="utf-8")
            manifest_path = root / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "corpus_id": "test-corpus",
                        "documents": [
                            {
                                "id": "outside",
                                "relative_path": "..\\outside.docx",
                                "ingestion": "searchable_knowledge",
                            }
                        ],
                        "cases": [{"id": "case", "query": "A requirement."}],
                    }
                ),
                encoding="utf-8",
            )

            report = irps_rag_eval.run_corpus_preflight(
                manifest_path=manifest_path,
                source_root=source_root,
            )

            self.assertEqual(report["status"], "BLOCKED")
            self.assertIn("path_outside_source_root", [issue["code"] for issue in report["issues"]])
            self.assertFalse(report["documents"][0]["exists"])

    def test_run_evaluation_fails_preflight_before_loading_azure_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path = root / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "corpus_id": "test-corpus",
                        "documents": [
                            {
                                "id": "missing",
                                "relative_path": "missing.docx",
                                "ingestion": "searchable_knowledge",
                            }
                        ],
                        "cases": [{"id": "case", "query": "A requirement."}],
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch("scripts.irps_rag_eval.load_ai_copilot_config") as load_config,
                self.assertRaisesRegex(ValueError, "missing source file"),
            ):
                irps_rag_eval.run_evaluation(
                    manifest_path=manifest_path,
                    source_root=root / "corpus",
                    out_dir=root / "out",
                )

            load_config.assert_not_called()

    def test_run_evaluation_loads_azure_profile_when_config_is_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "corpus"
            source_root.mkdir()
            (source_root / "ignored.xlsx").write_text("labels", encoding="utf-8")
            manifest_path = root / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "corpus_id": "test-corpus",
                        "documents": [
                            {
                                "id": "ignored",
                                "relative_path": "ignored.xlsx",
                                "ingestion": "do_not_index",
                            }
                        ],
                        "cases": [{"id": "case", "query": "A requirement."}],
                    }
                ),
                encoding="utf-8",
            )
            config = Mock()
            with (
                patch("scripts.irps_rag_eval.load_ai_copilot_config", return_value=config) as load_config,
                patch("scripts.irps_rag_eval._validate_azure_runtime") as validate_runtime,
                self.assertRaisesRegex(ValueError, "does not contain searchable knowledge documents"),
            ):
                irps_rag_eval.run_evaluation(
                    manifest_path=manifest_path,
                    source_root=source_root,
                    out_dir=root / "out",
                )

            load_config.assert_called_once_with("azure")
            validate_runtime.assert_called_once_with(config)

    def test_run_evaluation_reports_gold_and_hard_negative_metrics_and_cleans_up(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "corpus"
            source_root.mkdir()
            (source_root / "product.docx").write_text("placeholder", encoding="utf-8")
            (source_root / "other.pdf").write_text("placeholder", encoding="utf-8")
            manifest_path = root / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "corpus_id": "test-corpus",
                        "documents": [
                            {
                                "id": "product",
                                "relative_path": "product.docx",
                                "role": "knowledge_primary",
                                "doc_type": "REFERENCE",
                                "ingestion": "searchable_knowledge",
                            },
                            {
                                "id": "other-project",
                                "relative_path": "other.pdf",
                                "role": "hard_negative",
                                "doc_type": "REFERENCE",
                                "ingestion": "searchable_hard_negative",
                            },
                        ],
                        "cases": [
                            {
                                "id": "gold-case",
                                "query": "Product shall support expected output.",
                                "gold_source_doc_ids": ["product"],
                                "forbidden_source_doc_ids": ["other-project"],
                                "expected_retrieval_grade": "direct",
                            },
                            {
                                "id": "visual-case",
                                "query": "Follow the customer drawing.",
                                "gold_source_doc_ids": [],
                                "forbidden_source_doc_ids": ["product", "other-project"],
                                "expected_retrieval_grade": "visual_only",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            config = AICopilotConfig(
                assessment_mode="llm_primary",
                retrieval_backend="azure_sdk",
                top_k=3,
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
            retriever = Mock()

            def retrieve(context: dict, docs: list[dict], _top_k: int) -> list[dict]:
                if context["requirement_id"] == "visual-case":
                    return []
                return [
                    {
                        "source_doc_id": docs[0]["source_doc_id"],
                        "chunk_id": f"{docs[0]['source_doc_id']}_1",
                        "relevance": 0.032,
                        "score_source": "azure_search_score",
                    },
                    {
                        "source_doc_id": docs[1]["source_doc_id"],
                        "chunk_id": f"{docs[1]['source_doc_id']}_1",
                        "relevance": 0.031,
                        "score_source": "azure_search_score",
                    },
                ]

            retriever.retrieve.side_effect = retrieve
            with (
                patch(
                    "scripts.irps_rag_eval.parse_document_with_diagnostics",
                    return_value={"text": "indexed text", "segments": [], "file_type": "document"},
                ),
                patch(
                    "scripts.irps_rag_eval.sync_knowledge_document_index",
                    return_value={"status": "READY"},
                ),
                patch("scripts.irps_rag_eval.AzureSearchSDKEvidenceRetriever", return_value=retriever),
                patch("scripts.irps_rag_eval.delete_azure_search_chunks_for_source_docs") as cleanup,
            ):
                report = irps_rag_eval.run_evaluation(
                    manifest_path=manifest_path,
                    source_root=source_root,
                    out_dir=root / "out",
                    config=config,
                )

            self.assertEqual(report["metrics"]["recall_at_1"], 1.0)
            self.assertEqual(report["metrics"]["recall_at_3"], 1.0)
            self.assertEqual(report["metrics"]["visual_only_false_positive_rate"], 0.0)
            self.assertEqual(report["cases"][0]["gold_rank"], 1)
            self.assertEqual(report["cases"][0]["forbidden_hits"], ["other-project"])
            self.assertTrue(report["cleanup_completed"])
            self.assertEqual(report["metrics"]["score_distribution"]["gold"]["median"], 0.032)
            self.assertEqual(report["metrics"]["score_distribution"]["hard_negative"]["median"], 0.031)
            cleanup.assert_called_once()
            self.assertEqual(len(cleanup.call_args.kwargs["source_doc_ids"]), 2)


if __name__ == "__main__":
    unittest.main()

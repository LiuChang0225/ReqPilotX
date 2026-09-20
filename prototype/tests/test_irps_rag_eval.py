from __future__ import annotations

# Test isolation intentionally runs before application imports.
# ruff: noqa: E402

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
from unittest.mock import Mock, patch

from app.services.ai_copilot import AICopilotConfig, build_knowledge_chunks
from scripts import irps_rag_eval


class IRPSRAGEvalTests(unittest.TestCase):
    def test_manifest_reference_contract_rejects_quality_gate_fake_green_inputs(
        self,
    ) -> None:
        base_manifest = {
            "corpus_id": "controlled-corpus",
            "documents": [
                {
                    "id": "knowledge",
                    "ingestion": "searchable_knowledge",
                },
                {
                    "id": "hard-negative",
                    "ingestion": "searchable_hard_negative",
                },
            ],
            "cases": [
                {
                    "id": "case-1",
                    "query": "A controlled requirement.",
                    "expected_retrieval_grade": "direct",
                    "gold_source_doc_ids": ["knowledge"],
                    "forbidden_source_doc_ids": ["hard-negative"],
                }
            ],
        }
        irps_rag_eval._validate_manifest_reference_contract(base_manifest)

        mutations = [
            (
                "unknown documents",
                lambda payload: payload["cases"][0]["gold_source_doc_ids"].append(
                    "missing-document"
                ),
            ),
            (
                "non-knowledge document as Gold",
                lambda payload: payload["cases"][0].update(
                    gold_source_doc_ids=["hard-negative"],
                    forbidden_source_doc_ids=[],
                ),
            ),
            (
                "does not cover every hard-negative",
                lambda payload: payload["cases"][0].update(
                    forbidden_source_doc_ids=[]
                ),
            ),
        ]
        for expected_error, mutate in mutations:
            with self.subTest(expected_error=expected_error):
                payload = json.loads(json.dumps(base_manifest))
                mutate(payload)
                with self.assertRaisesRegex(ValueError, expected_error):
                    irps_rag_eval._validate_manifest_reference_contract(payload)

    def test_visual_only_manifest_case_must_forbid_every_searchable_document(
        self,
    ) -> None:
        manifest = {
            "corpus_id": "controlled-corpus",
            "documents": [
                {
                    "id": "knowledge",
                    "ingestion": "searchable_knowledge",
                },
                {
                    "id": "hard-negative",
                    "ingestion": "searchable_hard_negative",
                },
            ],
            "cases": [
                {
                    "id": "visual-case",
                    "query": "Use the controlled drawing.",
                    "expected_retrieval_grade": "visual_only",
                    "gold_source_doc_ids": [],
                    "forbidden_source_doc_ids": ["hard-negative"],
                }
            ],
        }

        with self.assertRaisesRegex(ValueError, "forbid every searchable document"):
            irps_rag_eval._validate_manifest_reference_contract(manifest)

    def test_synthetic_manifest_keeps_docx_searchable_and_pairs_duplicate_pdfs(self) -> None:
        manifest_path = Path(__file__).resolve().parents[1] / "eval/public/retrieval_manifest.json"
        manifest = irps_rag_eval._load_manifest(manifest_path)
        documents_by_id = {document["id"]: document for document in manifest["documents"]}

        self.assertEqual(documents_by_id["product-reference"]["ingestion"], "searchable_knowledge")
        self.assertEqual(documents_by_id["product-pdf"]["ingestion"], "do_not_index")
        self.assertEqual(documents_by_id["product-copy"]["ingestion"], "do_not_index")
        self.assertEqual(
            documents_by_id["product-copy"]["duplicate_of"],
            "product-pdf",
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

            self.assertEqual(indexed_docs[0]["status"], "ACTIVE")
            self.assertGreater(len(chunks), 0)
            self.assertEqual(chunks[0]["meta"]["source_doc_id"], indexed_docs[0]["source_doc_id"])

    def test_prepare_index_documents_fails_closed_when_chunks_are_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source_root = Path(tmp)
            (source_root / "product.docx").write_text("placeholder", encoding="utf-8")
            searchable_documents = [
                {
                    "id": "product",
                    "relative_path": "product.docx",
                    "doc_type": "REFERENCE",
                }
            ]
            with (
                patch(
                    "scripts.irps_rag_eval.parse_document_with_diagnostics",
                    return_value={"text": "Parsed but not chunkable.", "segments": []},
                ),
                patch("scripts.irps_rag_eval.build_knowledge_chunks", return_value=[]),
                self.assertRaisesRegex(ValueError, "produced zero knowledge chunks before indexing"),
            ):
                irps_rag_eval._prepare_index_documents(
                    searchable_documents=searchable_documents,
                    source_root=source_root,
                    project_id="isolated-evaluation",
                )

    def test_pre_index_guard_rejects_missing_lifecycle_status(self) -> None:
        knowledge_doc = {
            "id": "missing-status",
            "source_doc_id": "missing-status",
            "doc_type": "REFERENCE",
            "extracted_text": "The product supports an analog output.",
        }

        with self.assertRaisesRegex(ValueError, "status_not_active"):
            irps_rag_eval._require_indexable_knowledge_document(
                project_id="isolated-evaluation",
                knowledge_doc=knowledge_doc,
                phase="before indexing",
            )

    def test_post_index_guard_rejects_ready_sync_with_zero_chunks(self) -> None:
        knowledge_doc = {
            "id": "active-document",
            "source_doc_id": "active-document",
            "doc_type": "REFERENCE",
            "status": "ACTIVE",
            "extracted_text": "The product supports an analog output.",
        }

        with self.assertRaisesRegex(RuntimeError, "READY sync reported zero knowledge chunks"):
            irps_rag_eval._require_ready_index_sync(
                knowledge_doc=knowledge_doc,
                sync_result={"status": "READY", "chunk_count": 0},
            )

    def test_index_guards_accept_active_document_with_nonzero_chunks(self) -> None:
        knowledge_doc = {
            "id": "active-document",
            "source_doc_id": "active-document",
            "doc_type": "REFERENCE",
            "status": "ACTIVE",
            "extracted_text": "The product supports an analog output.",
        }

        chunk_count = irps_rag_eval._require_indexable_knowledge_document(
            project_id="isolated-evaluation",
            knowledge_doc=knowledge_doc,
            phase="before indexing",
        )
        irps_rag_eval._require_ready_index_sync(
            knowledge_doc=knowledge_doc,
            sync_result={"status": "READY", "chunk_count": chunk_count},
        )

        self.assertGreater(chunk_count, 0)

    def test_metrics_exclude_cases_requiring_human_review(self) -> None:
        quality_case = {
            "quality_eligible": True,
            "expected_retrieval_grade": "direct",
            "gold_document_keys": ["gold"],
            "ranked_document_keys": ["gold"],
            "gold_rank": 1,
            "forbidden_hit_document_keys": [],
            "top1_margin": None,
            "top3_score_spread": None,
            "evidences": [
                {
                    "score": 0.8,
                    "score_source": "test",
                    "relevance_class": "gold",
                }
            ],
        }
        pending_case = {
            "quality_eligible": False,
            "expected_retrieval_grade": "direct",
            "gold_document_keys": ["gold"],
            "ranked_document_keys": ["forbidden"],
            "gold_rank": None,
            "forbidden_hit_document_keys": ["forbidden"],
            "top1_margin": None,
            "top3_score_spread": None,
            "evidences": [
                {
                    "score": 0.9,
                    "score_source": "test",
                    "relevance_class": "hard_negative",
                }
            ],
        }

        metrics = irps_rag_eval._build_metrics([quality_case, pending_case])

        self.assertEqual(metrics["quality_case_count"], 1)
        self.assertEqual(metrics["excluded_requires_human_review_count"], 1)
        self.assertEqual(metrics["visual_only_quality_case_count"], 0)
        self.assertEqual(metrics["recall_at_1"], 1.0)
        self.assertEqual(metrics["wrong_evidence_rate"], 0.0)
        self.assertNotIn("hard_negative_citation_rate", metrics)
        self.assertEqual(metrics["score_distribution"]["gold"]["count"], 1)
        self.assertNotIn("hard_negative", metrics["score_distribution"])

    def test_case_report_marks_pending_label_and_omits_query(self) -> None:
        retriever = Mock()
        retriever.retrieve.return_value = []
        result = irps_rag_eval._evaluate_case(
            case={
                "id": "sensitive-case-id",
                "query": "Sensitive customer requirement text.",
                "status": "requires_human_review",
                "expected_retrieval_grade": "direct",
                "gold_source_doc_ids": ["sensitive-document-id"],
            },
            retriever=retriever,
            indexed_docs=[],
            source_id_to_manifest_id={},
            top_k=3,
        )

        self.assertFalse(result["quality_eligible"])
        self.assertEqual(result["exclusion_reason"], "REQUIRES_HUMAN_REVIEW")
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("Sensitive customer requirement text.", serialized)
        self.assertNotIn("sensitive-case-id", serialized)
        self.assertNotIn("sensitive-document-id", serialized)

    def test_quality_gate_requires_frozen_approved_manifest_and_thresholds(self) -> None:
        metrics = {
            "quality_case_count": 25,
            "excluded_requires_human_review_count": 0,
            "retrieval_cases_with_gold": 24,
            "visual_only_quality_case_count": 1,
            "recall_at_1": 0.9,
            "recall_at_3": 1.0,
            "mrr": 0.95,
            "wrong_evidence_rate": 0.0,
            "hard_negative_retrieval_rate": 0.0,
            "visual_only_false_positive_rate": 0.0,
        }
        manifest = {
            "status": "FROZEN",
            "thresholds": dict(irps_rag_eval.QUALITY_THRESHOLD_FLOORS),
        }
        manifest["approval"] = {
            "status": "APPROVED",
            "content_sha256": irps_rag_eval._manifest_approval_content_sha256(
                manifest
            ),
        }

        passing_gate = irps_rag_eval._build_quality_gate(
            manifest=manifest,
            metrics=metrics,
        )
        self.assertTrue(passing_gate["overall_pass"])
        irps_rag_eval._ensure_quality_pass({"quality_gate": passing_gate})

        draft_gate = irps_rag_eval._build_quality_gate(
            manifest={**manifest, "status": "DRAFT"},
            metrics=metrics,
        )
        self.assertFalse(draft_gate["overall_pass"])
        self.assertIn("manifest_not_frozen", draft_gate["blockers"])
        with self.assertRaisesRegex(RuntimeError, "manifest_not_frozen"):
            irps_rag_eval._ensure_quality_pass({"quality_gate": draft_gate})

        unresolved_gate = irps_rag_eval._build_quality_gate(
            manifest=manifest,
            metrics={**metrics, "excluded_requires_human_review_count": 1},
        )
        self.assertFalse(unresolved_gate["overall_pass"])
        self.assertIn("unresolved_human_review_cases", unresolved_gate["blockers"])

    def test_quality_gate_rejects_policy_that_weakens_code_floor(self) -> None:
        metrics = {
            "quality_case_count": 25,
            "excluded_requires_human_review_count": 0,
            "retrieval_cases_with_gold": 24,
            "visual_only_quality_case_count": 1,
            "recall_at_1": 1.0,
            "recall_at_3": 1.0,
            "mrr": 1.0,
            "wrong_evidence_rate": 0.0,
            "hard_negative_retrieval_rate": 0.0,
            "visual_only_false_positive_rate": 0.0,
        }
        thresholds = dict(irps_rag_eval.QUALITY_THRESHOLD_FLOORS)
        thresholds["recall_at_3_min"] = 0.5
        manifest = {
            "status": "FROZEN",
            "thresholds": thresholds,
        }
        manifest["approval"] = {
            "status": "APPROVED",
            "content_sha256": irps_rag_eval._manifest_approval_content_sha256(
                manifest
            ),
        }
        gate = irps_rag_eval._build_quality_gate(
            manifest=manifest,
            metrics=metrics,
        )

        self.assertFalse(gate["overall_pass"])
        self.assertIn(
            "threshold_below_floor:recall_at_3_min",
            gate["blockers"],
        )
        self.assertEqual(gate["effective_thresholds"]["recall_at_3_min"], 0.95)

    def test_quality_gate_rejects_stale_approval_content_hash(self) -> None:
        manifest = {
            "status": "FROZEN",
            "thresholds": dict(irps_rag_eval.QUALITY_THRESHOLD_FLOORS),
            "cases": [{"id": "case-a"}],
        }
        manifest["approval"] = {
            "status": "APPROVED",
            "content_sha256": irps_rag_eval._manifest_approval_content_sha256(
                manifest
            ),
        }
        manifest["cases"].append({"id": "case-b"})
        metrics = {
            "quality_case_count": 25,
            "excluded_requires_human_review_count": 0,
            "retrieval_cases_with_gold": 24,
            "visual_only_quality_case_count": 1,
            "recall_at_1": 1.0,
            "recall_at_3": 1.0,
            "mrr": 1.0,
            "wrong_evidence_rate": 0.0,
            "hard_negative_retrieval_rate": 0.0,
            "visual_only_false_positive_rate": 0.0,
        }

        gate = irps_rag_eval._build_quality_gate(
            manifest=manifest,
            metrics=metrics,
        )

        self.assertFalse(gate["overall_pass"])
        self.assertFalse(gate["approval_content_hash_valid"])
        self.assertIn("approval_content_hash_mismatch", gate["blockers"])

    def test_cleanup_verification_fails_closed_when_chunks_remain(self) -> None:
        config = Mock(
            azure_search_endpoint="https://example.search.windows.net",
            azure_search_index_name="test-index",
        )
        search_client = Mock()
        search_client.search.return_value = [{"chunk_id": "still-present"}]
        with (
            patch(
                "azure.search.documents.SearchClient",
                return_value=search_client,
            ),
            patch(
                "scripts.irps_rag_eval._azure_search_credential",
                return_value=object(),
            ),
            patch("scripts.irps_rag_eval.time.sleep") as sleep,
        ):
            confirmed = irps_rag_eval._verify_azure_search_cleanup(
                config,
                source_doc_ids=["isolated-source"],
            )

        self.assertFalse(confirmed)
        self.assertEqual(
            search_client.search.call_count,
            irps_rag_eval.CLEANUP_VERIFICATION_ATTEMPTS,
        )
        self.assertEqual(
            sleep.call_count,
            irps_rag_eval.CLEANUP_VERIFICATION_ATTEMPTS - 1,
        )

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
                        "cases": [
                            {
                                "id": "case",
                                "query": "A requirement.",
                                "expected_retrieval_grade": "direct",
                                "gold_source_doc_ids": ["product"],
                                "forbidden_source_doc_ids": ["other-project"],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            report = irps_rag_eval.run_corpus_preflight(
                manifest_path=manifest_path,
                source_root=source_root,
            )

            self.assertEqual(report["status"], "READY")
            self.assertEqual(
                report["indexable_document_keys"],
                [
                    irps_rag_eval._stable_report_identifier("document", "product"),
                    irps_rag_eval._stable_report_identifier("document", "other-project"),
                ],
            )
            self.assertEqual(report["summary"]["do_not_index_documents"], 1)
            self.assertTrue(report["documents"][1]["duplicate_verified"])
            self.assertFalse(report["documents"][1]["indexable"])
            serialized = json.dumps(report, ensure_ascii=False)
            self.assertNotIn(str(source_root), serialized)
            self.assertNotIn("product.docx", serialized)
            self.assertNotIn("other-project.pdf", serialized)
            self.assertNotIn('"id": "product"', serialized)

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
                        "cases": [
                            {
                                "id": "case",
                                "query": "A requirement.",
                                "expected_retrieval_grade": "visual_only",
                                "gold_source_doc_ids": [],
                                "forbidden_source_doc_ids": [],
                            }
                        ],
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
            serialized = json.dumps(report, ensure_ascii=False)
            self.assertNotIn("label.xlsx", serialized)
            self.assertNotIn(str(source_root), serialized)
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
                        "cases": [
                            {
                                "id": "case",
                                "query": "A requirement.",
                                "expected_retrieval_grade": "direct",
                                "gold_source_doc_ids": ["outside"],
                                "forbidden_source_doc_ids": [],
                            }
                        ],
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
                        "cases": [
                            {
                                "id": "case",
                                "query": "A requirement.",
                                "expected_retrieval_grade": "direct",
                                "gold_source_doc_ids": ["missing"],
                                "forbidden_source_doc_ids": [],
                            }
                        ],
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
                        "cases": [
                            {
                                "id": "case",
                                "query": "A requirement.",
                                "expected_retrieval_grade": "visual_only",
                                "gold_source_doc_ids": [],
                                "forbidden_source_doc_ids": [],
                            }
                        ],
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
                    return_value={
                        "text": "Product information\nThe sensor supports analog output.",
                        "segments": [],
                        "file_type": "document",
                    },
                ),
                patch(
                    "scripts.irps_rag_eval.sync_knowledge_document_index",
                    return_value={"status": "READY", "chunk_count": 1},
                ),
                patch("scripts.irps_rag_eval.AzureSearchSDKEvidenceRetriever", return_value=retriever),
                patch("scripts.irps_rag_eval.delete_azure_search_chunks_for_source_docs") as cleanup,
                patch(
                    "scripts.irps_rag_eval._verify_azure_search_cleanup",
                    return_value=True,
                ) as verify_cleanup,
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
            self.assertEqual(
                report["cases"][0]["forbidden_hit_document_keys"],
                [
                    irps_rag_eval._stable_report_identifier(
                        "document",
                        "other-project",
                    )
                ],
            )
            self.assertTrue(report["cleanup_completed"])
            self.assertTrue(report["cleanup"]["confirmed"])
            self.assertEqual(report["metrics"]["score_distribution"]["gold"]["median"], 0.032)
            self.assertEqual(report["metrics"]["score_distribution"]["hard_negative"]["median"], 0.031)
            self.assertNotIn("hard_negative_citation_rate", report["metrics"])
            self.assertEqual(report["metrics"]["hard_negative_retrieval_rate"], 0.5)
            self.assertFalse(report["quality_gate"]["overall_pass"])
            serialized = json.dumps(report, ensure_ascii=False)
            self.assertNotIn("Product shall support expected output.", serialized)
            self.assertNotIn("product.docx", serialized)
            self.assertNotIn("other.pdf", serialized)
            self.assertNotIn(str(source_root), serialized)
            self.assertNotIn("reqpilot-knowledge", serialized)
            self.assertNotIn('"expected_decision"', serialized)
            cleanup.assert_called_once()
            verify_cleanup.assert_called_once()
            self.assertEqual(len(cleanup.call_args.kwargs["source_doc_ids"]), 2)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import tempfile
import unittest
import uuid
import dataclasses
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, call, patch, sentinel

from app.services.ai_copilot import (
    ALIYUN_API_KEY_NON_ASCII_REASON,
    ALIYUN_QDRANT_API_KEY_NON_ASCII_REASON,
    AIAnalysisCancelledError,
    apply_runtime_ai_provider_setup,
    ALIYUN_RETRIEVAL_DISABLED_REASON,
    AICopilotConfig,
    AssessmentOutputSchema,
    _default_azure_credential,
    _azure_json_completion,
    _dashscope_json_completion,
    _ensure_qdrant_collection,
    _embed_texts,
    _qdrant_point_id,
    _azure_search_item_to_evidence,
    _validate_evidence_references,
    AssessmentEvidenceSchema,
    build_requirement_normalization_detail,
    build_knowledge_chunks,
    build_assessment_context,
    build_runtime_ai_provider_setup,
    clear_runtime_ai_provider_overrides,
    fallback_need_clarification,
    get_ai_profile_missing_config,
    is_ascii_runtime_secret,
    load_ai_copilot_config,
    QdrantEvidenceRetriever,
    reconcile_assessment_output,
    run_ai_copilot_assessment,
    sync_knowledge_document_index,
)


class AICopilotTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_runtime_ai_provider_overrides()
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
        clear_runtime_ai_provider_overrides()
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

    def test_build_assessment_context_creates_structured_temperature_summary(self) -> None:
        row = {
            "id": "req-temp-1",
            "req_code": "REQ-TEMP-1",
            "display_text": (
                "3.3\n"
                "temperature Tmin /Tmax\n"
                "系统/组件能够连续运行时的周围环境的最低或最高值。\n"
                "Lowest/highest value of the ambient temperature at which the systems/components can be operated continuously"
            ),
            "requirement_type": "PERFORMANCE",
            "parent_section_code": "3.3",
            "parent_section_title": "最低工作温度和最高工作温度 lowest operating temperature and highest operating",
            "parent_section_label": "3.3 最低工作温度和最高工作温度 lowest operating temperature and highest operating",
        }

        context = build_assessment_context(row)

        self.assertEqual(
            context["normalized_requirement"],
            "系统/组件应定义并满足可连续运行的环境最低温度Tmin和最高温度Tmax。",
        )
        self.assertIn("Tmin", context["retrieval_requirement"])
        self.assertIn("Tmax", context["retrieval_requirement"])
        self.assertTrue(context["normalization_facets"])
        facet_values = " | ".join(str(item.get("value") or "") for item in context["normalization_facets"])
        self.assertNotIn("CAN", facet_values)

    def test_build_requirement_normalization_detail_refreshes_stale_persisted_summary(self) -> None:
        row = {
            "id": "req-temp-2",
            "req_code": "REQ-TEMP-2",
            "display_text": (
                "3.3\n"
                "temperature Tmin /Tmax\n"
                "系统/组件能够连续运行时的周围环境的最低或最高值。\n"
                "Lowest/highest value of the ambient temperature at which the systems/components can be operated continuously"
            ),
            "requirement_type": "PERFORMANCE",
            "parent_section_code": "3.3",
            "parent_section_title": "最低工作温度和最高工作温度 lowest operating temperature and highest operating",
            "parent_section_label": "3.3 最低工作温度和最高工作温度 lowest operating temperature and highest operating",
        }

        detail = build_requirement_normalization_detail(
            row,
            persisted_summary=(
                "3.3 temperature Tmin /Tmax 系统/组件能够连续运行时的周围环境的最低或最高值。 "
                "Lowest/highest value of the ambient temperature at which the systems/components can be operated continuously"
            ),
        )

        self.assertEqual(
            detail["summary"],
            "系统/组件应定义并满足可连续运行的环境最低温度Tmin和最高温度Tmax。",
        )

    def test_build_requirement_normalization_detail_keeps_text_when_heading_strip_would_overtruncate(self) -> None:
        row = {
            "id": "req-camds-1",
            "req_code": "REQ-0025",
            "display_text": "5.4 材料数据申报要求 供应商需满足禁限用物质要求，同时在规定期限内如实填报 CAMDS 数据。",
            "requirement_type": "REGULATION",
            "parent_section_code": "5.4",
            "parent_section_title": "材料数据申报要求",
            "parent_section_label": "5.4 材料数据申报要求",
            "heading_text": "材料数据申报要求",
        }

        detail = build_requirement_normalization_detail(row)

        self.assertIn("供应商需满足禁限用物质要求", detail["summary"])
        self.assertIn("CAMDS", detail["summary"])

    def test_build_requirement_normalization_detail_exposes_table_facets(self) -> None:
        row = {
            "id": "req-table-1",
            "req_code": "REQ-TABLE-1",
            "display_text": "跳跃启动的试验参数见表16和图6。",
            "requirement_type": "TEST",
            "parent_section_code": "5.1.11.2",
            "parent_section_title": "试验参数 Test parameters",
            "table_refs": [
                {
                    "table_title": "表16 试验参数--跳跃启动",
                    "slice_rows": [
                        ["项目 Item", "要求 Requirement"],
                        ["Umin", "10.8V"],
                        ["Umax", "26 V"],
                        ["循环次数 Number cycles", "1"],
                    ],
                }
            ],
        }

        detail = build_requirement_normalization_detail(row)

        self.assertIn("Umin=10.8V", detail["summary"])
        self.assertIn("Umax=26 V", detail["summary"])
        self.assertEqual(detail["facets"][0]["label"], "Umin")
        self.assertEqual(detail["facets"][0]["value"], "10.8V")

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

    def test_build_knowledge_chunks_uses_azure_search_safe_chunk_ids(self) -> None:
        knowledge_doc = {
            "id": "kb-2",
            "file_name": "knowledge.txt",
            "doc_version": "2026.03",
            "doc_type": "STANDARD",
            "status": "ACTIVE",
            "effective_from": "2026-01-01",
            "effective_to": "",
            "extracted_text": (
                "启动时延要求如下。\n"
                "|Item|Condition|\n"
                "|---|---|\n"
                "|Latency|150ms|\n"
            ),
        }

        chunks = build_knowledge_chunks("project-1", knowledge_doc)

        self.assertGreater(len(chunks), 0)
        for chunk in chunks:
            self.assertNotIn(":", chunk["chunk_id"])
            self.assertTrue(chunk["chunk_id"].startswith("kb-2_"))

    def test_qdrant_point_id_is_stable_uuid_for_non_uuid_chunk_ids(self) -> None:
        point_id = _qdrant_point_id("kb-2_paragraph_1")

        self.assertEqual(point_id, _qdrant_point_id("kb-2_paragraph_1"))
        self.assertNotEqual(point_id, "kb-2_paragraph_1")
        self.assertEqual(str(uuid.UUID(point_id)), point_id)

    def test_ensure_qdrant_collection_tolerates_existing_collection_race(self) -> None:
        class FakeAlreadyExistsError(Exception):
            def __init__(self) -> None:
                super().__init__("Unexpected Response: 409 (Conflict)")
                self.status_code = 409
                self.content = (
                    b'{"status":{"error":"Wrong input: Collection `reqpilot-knowledge-aliyun-local` already exists!"}}'
                )

        config = AICopilotConfig(
            assessment_mode="llm_shadow",
            retrieval_backend="qdrant",
            top_k=5,
            trace_dir=self.trace_dir,
            azure_auth_mode="",
            azure_allow_interactive_login=False,
            azure_search_endpoint="",
            azure_search_api_key="",
            azure_search_index_name="",
            azure_openai_endpoint="",
            azure_openai_api_key="",
            azure_openai_api_version="2024-10-21",
            azure_openai_chat_deployment="",
            azure_openai_embedding_deployment="",
            azure_openai_embedding_dimensions=0,
            azure_search_semantic_configuration="",
            llm_timeout_seconds=45.0,
            profile_id="aliyun",
            provider="aliyun",
            profile_label="Aliyun",
            chat_model="qwen3.5-plus",
            embedding_model="text-embedding-v4",
            dashscope_api_key="aliyun-key",
            dashscope_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            qdrant_url="http://localhost:6333",
            qdrant_collection_name="reqpilot-knowledge-aliyun-local",
        )
        collection_info = type("CollectionInfo", (), {"size": 1024})()
        client = Mock()
        client.get_collection.side_effect = [RuntimeError("not found"), collection_info]
        client.create_collection.side_effect = FakeAlreadyExistsError()

        _ensure_qdrant_collection(
            client,
            config,
            dimension=1024,
            create_if_missing=True,
        )

        self.assertEqual(client.get_collection.call_count, 2)
        client.create_collection.assert_called_once()

    def test_qdrant_retriever_returns_no_evidence_for_regulation_without_authoritative_docs(self) -> None:
        config = AICopilotConfig(
            assessment_mode="llm_shadow",
            retrieval_backend="qdrant",
            top_k=5,
            trace_dir=self.trace_dir,
            azure_auth_mode="",
            azure_allow_interactive_login=False,
            azure_search_endpoint="",
            azure_search_api_key="",
            azure_search_index_name="",
            azure_openai_endpoint="",
            azure_openai_api_key="",
            azure_openai_api_version="2024-10-21",
            azure_openai_chat_deployment="",
            azure_openai_embedding_deployment="",
            azure_openai_embedding_dimensions=0,
            azure_search_semantic_configuration="",
            llm_timeout_seconds=45.0,
            profile_id="aliyun",
            provider="aliyun",
            profile_label="Aliyun",
            chat_model="qwen3.5-plus",
            embedding_model="text-embedding-v4",
            dashscope_api_key="aliyun-key",
            dashscope_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            qdrant_url="http://localhost:6333",
            qdrant_collection_name="reqpilot-knowledge-aliyun-local",
        )
        retriever = QdrantEvidenceRetriever(config)
        context = {
            "requirement_type": "REGULATION",
            "requirement_text": "Supplier shall comply with Regulation X section 5.",
            "dense_retrieval_query": "Supplier shall comply with Regulation X section 5.",
            "sparse_retrieval_query": "Supplier shall comply with Regulation X section 5.",
            "retrieval_gate_reason": "",
        }

        evidences = retriever.retrieve(
            context,
            [
                {
                    "id": "kb-1",
                    "project_id": "project-1",
                    "file_name": "emc-reference.txt",
                    "doc_type": "REFERENCE",
                    "status": "ACTIVE",
                    "extracted_text": "EMC cover page only.",
                }
            ],
            top_k=5,
        )

        self.assertEqual(evidences, [])
        self.assertIn("缺少法规类知识文档", context["retrieval_gate_reason"])

    def test_azure_search_item_to_evidence_normalizes_reranker_score(self) -> None:
        evidence = _azure_search_item_to_evidence(
            {
                "source_doc_id": "kb-1",
                "source_doc_name": "knowledge.txt",
                "source_doc_version": "2026.03",
                "source_location": "Chunk 1",
                "content": "Latency target is 150ms.",
                "@search.reranker_score": 2.0,
            }
        )

        self.assertEqual(evidence["source_doc_id"], "kb-1")
        self.assertEqual(evidence["relevance"], 0.5)

    def test_validate_evidence_references_allows_excerpt_substrings_from_same_chunk(self) -> None:
        cited = [
            AssessmentEvidenceSchema(
                source_doc_id="kb-1",
                source_doc_name="knowledge.txt",
                source_location="Chunk 1",
                excerpt="OTA update module supports rollback after validation failure.",
                relevance=0.5564,
                source_doc_version="2026.03",
            )
        ]
        retrieved = [
            {
                "source_doc_id": "kb-1",
                "source_doc_name": "knowledge.txt",
                "source_location": "Chunk 1",
                "excerpt": (
                    "Platform startup latency in cold condition is 120ms P95.\n"
                    "OTA update module supports rollback after validation failure."
                ),
                "relevance": 0.6181,
                "source_doc_version": "2026.03",
            }
        ]

        self.assertEqual(_validate_evidence_references(cited, retrieved), "")

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

    def test_run_ai_copilot_assessment_llm_shadow_prefers_llm_final_result(self) -> None:
        llm_shadow_config = AICopilotConfig(
            assessment_mode="llm_shadow",
            retrieval_backend="local",
            top_k=5,
            trace_dir=self.trace_dir,
            azure_auth_mode="auto",
            azure_allow_interactive_login=False,
            azure_search_endpoint="https://search.example.search.windows.net",
            azure_search_api_key="",
            azure_search_index_name="reqpilot-knowledge",
            azure_openai_endpoint="https://openai.example.openai.azure.com",
            azure_openai_api_key="",
            azure_openai_api_version="2024-10-21",
            azure_openai_chat_deployment="gpt-5.4",
            azure_openai_embedding_deployment="text-embedding-3-large",
            azure_openai_embedding_dimensions=1536,
            azure_search_semantic_configuration="",
            llm_timeout_seconds=45.0,
        )
        row = {
            "id": "req-shadow-1",
            "req_code": "REQ-SHADOW-1",
            "display_text": "System shall define Tmin and Tmax for continuous operation.",
            "requirement_type": "PERFORMANCE",
            "parent_section_label": "3.3 Temperature",
        }
        retrieved_evidences = [
            {
                "source_doc_id": "kb-1",
                "source_doc_name": "temperature-spec.txt",
                "source_location": "Line 124",
                "excerpt": "Tmin = -40 C Minimum operating temperature Tmax = 125 C Maximum operating temperature",
                "relevance": 0.6246,
                "source_doc_version": "2026.03",
            }
        ]

        heuristic_screen = Mock()
        heuristic_assess = Mock()
        with patch(
            "app.services.ai_copilot.HeuristicRequirementScreeningEngine.screen",
            heuristic_screen,
        ), patch(
            "app.services.ai_copilot.AzureOpenAIScreeningEngine.screen",
            return_value={"analysis_label": "REQUIREMENT", "reason": "该条目是需要判定的性能需求。"},
        ), patch(
            "app.services.ai_copilot.HeuristicEvidenceRetriever.retrieve",
            return_value=retrieved_evidences,
        ), patch(
            "app.services.ai_copilot.HeuristicAssessmentEngine.assess",
            heuristic_assess,
        ), patch(
            "app.services.ai_copilot.AzureOpenAIAssessmentEngine.assess",
            return_value={
                "normalized_requirement": "系统/组件应定义并满足可连续运行的环境最低温度Tmin和最高温度Tmax。",
                "proposed_decision": "NEED_CLARIFICATION",
                "confidence": 0.72,
                "reason": "候选证据给出了 Tmin/Tmax 的明确边界，但仍缺少连续运行验证结果，因此需要澄清判定标准。",
                "risk_level": "MEDIUM",
                "clarification_question": "该要求是否仅要求定义温度边界，还是要求已完成连续运行验证？",
                "evidences": [
                    {
                        "source_doc_id": "kb-1",
                        "source_doc_name": "temperature-spec.txt",
                        "source_location": "Line 124",
                        "excerpt": "Tmin = -40 C Minimum operating temperature Tmax = 125 C Maximum operating temperature",
                        "relevance": 0.6246,
                        "source_doc_version": "2026.03",
                    }
                ],
            },
        ):
            result = run_ai_copilot_assessment(row, [{"id": "kb-1"}], config=llm_shadow_config)

        assessment = result["assessment"]
        self.assertEqual(assessment["assessment_mode"], "llm_shadow")
        self.assertEqual(assessment["ai_decision"], "NEED_CLARIFICATION")
        self.assertIn("仍缺少连续运行验证结果", assessment["ai_reason"])
        self.assertIn("最终结论采用 LLM 输出", assessment["analysis_basis"])
        self.assertIn("检索后端 local", assessment["analysis_basis"])
        self.assertEqual({run["mode"] for run in result["runs"]}, {"screening_llm_shadow", "llm_shadow"})
        heuristic_screen.assert_not_called()
        heuristic_assess.assert_not_called()

    def test_run_ai_copilot_assessment_llm_shadow_uses_llm_screening_result(self) -> None:
        llm_shadow_config = AICopilotConfig(
            assessment_mode="llm_shadow",
            retrieval_backend="local",
            top_k=5,
            trace_dir=self.trace_dir,
            azure_auth_mode="auto",
            azure_allow_interactive_login=False,
            azure_search_endpoint="https://search.example.search.windows.net",
            azure_search_api_key="",
            azure_search_index_name="reqpilot-knowledge",
            azure_openai_endpoint="https://openai.example.openai.azure.com",
            azure_openai_api_key="",
            azure_openai_api_version="2024-10-21",
            azure_openai_chat_deployment="gpt-5.4",
            azure_openai_embedding_deployment="text-embedding-3-large",
            azure_openai_embedding_dimensions=1536,
            azure_search_semantic_configuration="",
            llm_timeout_seconds=45.0,
        )
        row = {
            "id": "req-shadow-info-1",
            "req_code": "REQ-SHADOW-INFO-1",
            "display_text": "temperature Tmin / Tmax lowest and highest ambient values",
            "requirement_type": "PERFORMANCE",
            "parent_section_label": "3.3 Temperature",
        }

        heuristic_screen = Mock()
        with patch(
            "app.services.ai_copilot.HeuristicRequirementScreeningEngine.screen",
            heuristic_screen,
        ), patch(
            "app.services.ai_copilot.AzureOpenAIScreeningEngine.screen",
            return_value={"analysis_label": "INFO", "reason": "该条目仅在定义 Tmin/Tmax 的含义，不构成待判定义务。"},
        ):
            result = run_ai_copilot_assessment(row, [{"id": "kb-1"}], config=llm_shadow_config)

        assessment = result["assessment"]
        self.assertEqual(assessment["analysis_label"], "INFO")
        self.assertEqual(assessment["ai_decision"], "")
        self.assertIn("信息条目", assessment["analysis_basis"])
        self.assertEqual({run["mode"] for run in result["runs"]}, {"screening_llm_shadow"})
        heuristic_screen.assert_not_called()

    def test_run_ai_copilot_assessment_llm_shadow_respects_llm_screening_result(self) -> None:
        llm_shadow_config = AICopilotConfig(
            assessment_mode="llm_shadow",
            retrieval_backend="local",
            top_k=5,
            trace_dir=self.trace_dir,
            azure_auth_mode="auto",
            azure_allow_interactive_login=False,
            azure_search_endpoint="https://search.example.search.windows.net",
            azure_search_api_key="",
            azure_search_index_name="reqpilot-knowledge",
            azure_openai_endpoint="https://openai.example.openai.azure.com",
            azure_openai_api_key="",
            azure_openai_api_version="2024-10-21",
            azure_openai_chat_deployment="gpt-5.4",
            azure_openai_embedding_deployment="text-embedding-3-large",
            azure_openai_embedding_dimensions=1536,
            azure_search_semantic_configuration="",
            llm_timeout_seconds=45.0,
        )
        row = {
            "id": "req-shadow-temp-1",
            "req_code": "REQ-SHADOW-TEMP-1",
            "display_text": (
                "3.3\n"
                "temperature Tmin /Tmax\n"
                "系统/组件能够连续运行时的周围环境的最低或最高值。\n"
                "Lowest/highest value of the ambient temperature at which the systems/components can be operated continuously"
            ),
            "requirement_type": "PERFORMANCE",
            "parent_section_code": "3.3",
            "parent_section_title": "最低工作温度和最高工作温度 lowest operating temperature and highest operating",
            "parent_section_label": "3.3 最低工作温度和最高工作温度 lowest operating temperature and highest operating",
        }

        heuristic_screen = Mock()
        with patch(
            "app.services.ai_copilot.HeuristicRequirementScreeningEngine.screen",
            heuristic_screen,
        ), patch(
            "app.services.ai_copilot.AzureOpenAIScreeningEngine.screen",
            return_value={"analysis_label": "INFO", "reason": "该条目仅在定义 Tmin/Tmax 的含义，不构成待判定义务。"},
        ):
            result = run_ai_copilot_assessment(row, [], config=llm_shadow_config)

        assessment = result["assessment"]
        self.assertEqual(assessment["analysis_label"], "INFO")
        self.assertEqual(assessment["assessment_mode"], "screening_only")
        self.assertEqual(assessment["ai_decision"], "")
        self.assertIn("信息条目", assessment["analysis_basis"])
        self.assertEqual({run["mode"] for run in result["runs"]}, {"screening_llm_shadow"})
        heuristic_screen.assert_not_called()

    def test_run_ai_copilot_assessment_llm_shadow_fails_closed_when_screening_errors(self) -> None:
        llm_shadow_config = dataclasses.replace(
            self.config,
            assessment_mode="llm_shadow",
            azure_search_endpoint="https://search.example.search.windows.net",
            azure_search_index_name="reqpilot-knowledge",
            azure_openai_endpoint="https://openai.example.openai.azure.com",
            azure_openai_chat_deployment="gpt-5.4",
            azure_openai_embedding_deployment="text-embedding-3-large",
            azure_openai_embedding_dimensions=1536,
        )
        row = {
            "id": "req-shadow-failed-screening-1",
            "req_code": "REQ-SHADOW-FAILED-SCREENING-1",
            "display_text": "Supplier shall provide EMC validation evidence.",
            "requirement_type": "REGULATION",
            "parent_section_label": "5.1 Compliance",
        }

        with patch(
            "app.services.ai_copilot.HeuristicRequirementScreeningEngine.screen",
            side_effect=AssertionError("heuristic screening should not run in live llm_shadow mode"),
        ), patch(
            "app.services.ai_copilot.AzureOpenAIScreeningEngine.screen",
            side_effect=TimeoutError("Request timed out."),
        ), patch(
            "app.services.ai_copilot.HeuristicEvidenceRetriever.retrieve",
            side_effect=AssertionError("retrieval should not run after screening failure"),
        ), patch(
            "app.services.ai_copilot.AzureOpenAIAssessmentEngine.assess",
            side_effect=AssertionError("assessment should not run after screening failure"),
        ):
            result = run_ai_copilot_assessment(row, [{"id": "kb-1"}], config=llm_shadow_config)

        assessment = result["assessment"]
        self.assertEqual(assessment["assessment_mode"], "screening_failed")
        self.assertEqual(assessment["analysis_label"], "REQUIREMENT")
        self.assertEqual(assessment["ai_decision"], "")
        self.assertEqual(result["evidences"], [])
        self.assertIn("LLM screening failed; analysis stopped.", assessment["analysis_basis"])
        self.assertEqual(len(result["runs"]), 1)
        self.assertEqual(result["runs"][0]["mode"], "screening_llm_shadow")
        self.assertEqual(result["runs"][0]["validation_status"], "failed")

    def test_run_ai_copilot_assessment_llm_shadow_fails_closed_when_assessment_errors(self) -> None:
        llm_shadow_config = dataclasses.replace(
            self.config,
            assessment_mode="llm_shadow",
            azure_search_endpoint="https://search.example.search.windows.net",
            azure_search_index_name="reqpilot-knowledge",
            azure_openai_endpoint="https://openai.example.openai.azure.com",
            azure_openai_chat_deployment="gpt-5.4",
            azure_openai_embedding_deployment="text-embedding-3-large",
            azure_openai_embedding_dimensions=1536,
        )
        row = {
            "id": "req-shadow-failed-analysis-1",
            "req_code": "REQ-SHADOW-FAILED-ANALYSIS-1",
            "display_text": "System shall support wake-up time less than 100ms.",
            "requirement_type": "FUNCTIONAL",
            "parent_section_label": "5.1 Startup",
        }
        retrieved_evidences = [
            {
                "source_doc_id": "kb-1",
                "source_doc_name": "platform-spec.txt",
                "source_location": "Chunk 1",
                "excerpt": "System wake-up time is 80ms in validation runs.",
                "relevance": 0.71,
                "source_doc_version": "v1.0",
            }
        ]

        with patch(
            "app.services.ai_copilot.HeuristicRequirementScreeningEngine.screen",
            side_effect=AssertionError("heuristic screening should not run in live llm_shadow mode"),
        ), patch(
            "app.services.ai_copilot.HeuristicAssessmentEngine.assess",
            side_effect=AssertionError("heuristic assessment should not run in live llm_shadow mode"),
        ), patch(
            "app.services.ai_copilot.AzureOpenAIScreeningEngine.screen",
            return_value={"analysis_label": "REQUIREMENT", "reason": "该条目表达了明确可验证义务。"},
        ), patch(
            "app.services.ai_copilot.HeuristicEvidenceRetriever.retrieve",
            return_value=retrieved_evidences,
        ), patch(
            "app.services.ai_copilot.AzureOpenAIAssessmentEngine.assess",
            side_effect=ConnectionError("Connection error."),
        ):
            result = run_ai_copilot_assessment(row, [{"id": "kb-1"}], config=llm_shadow_config)

        assessment = result["assessment"]
        self.assertEqual(assessment["assessment_mode"], "analysis_failed")
        self.assertEqual(assessment["analysis_label"], "REQUIREMENT")
        self.assertEqual(assessment["ai_decision"], "")
        self.assertEqual(result["evidences"], [])
        self.assertIn("LLM requirement analysis failed; analysis stopped.", assessment["analysis_basis"])
        self.assertEqual({run["mode"] for run in result["runs"]}, {"screening_llm_shadow", "llm_shadow"})
        llm_run = next(run for run in result["runs"] if run["mode"] == "llm_shadow")
        self.assertEqual(llm_run["validation_status"], "failed")

    def test_run_ai_copilot_assessment_propagates_cancelled_screening(self) -> None:
        llm_shadow_config = dataclasses.replace(
            self.config,
            assessment_mode="llm_shadow",
            azure_search_endpoint="https://search.example.search.windows.net",
            azure_search_index_name="reqpilot-knowledge",
            azure_openai_endpoint="https://openai.example.openai.azure.com",
            azure_openai_chat_deployment="gpt-5.4",
            azure_openai_embedding_deployment="text-embedding-3-large",
            azure_openai_embedding_dimensions=1536,
        )
        row = {
            "id": "req-shadow-cancel-screening-1",
            "req_code": "REQ-SHADOW-CANCEL-SCREENING-1",
            "display_text": "Supplier shall provide EMC validation evidence.",
            "requirement_type": "REGULATION",
        }

        with patch(
            "app.services.ai_copilot.AzureOpenAIScreeningEngine.screen",
            side_effect=AIAnalysisCancelledError("cancelled"),
        ):
            with self.assertRaises(AIAnalysisCancelledError):
                run_ai_copilot_assessment(row, [{"id": "kb-1"}], config=llm_shadow_config)

    def test_run_ai_copilot_assessment_propagates_cancelled_assessment(self) -> None:
        llm_shadow_config = dataclasses.replace(
            self.config,
            assessment_mode="llm_shadow",
            azure_search_endpoint="https://search.example.search.windows.net",
            azure_search_index_name="reqpilot-knowledge",
            azure_openai_endpoint="https://openai.example.openai.azure.com",
            azure_openai_chat_deployment="gpt-5.4",
            azure_openai_embedding_deployment="text-embedding-3-large",
            azure_openai_embedding_dimensions=1536,
        )
        row = {
            "id": "req-shadow-cancel-analysis-1",
            "req_code": "REQ-SHADOW-CANCEL-ANALYSIS-1",
            "display_text": "System shall support wake-up time less than 100ms.",
            "requirement_type": "FUNCTIONAL",
        }
        retrieved_evidences = [
            {
                "source_doc_id": "kb-1",
                "source_doc_name": "platform-spec.txt",
                "source_location": "Chunk 1",
                "excerpt": "System wake-up time is 80ms in validation runs.",
                "relevance": 0.71,
                "source_doc_version": "v1.0",
            }
        ]

        with patch(
            "app.services.ai_copilot.AzureOpenAIScreeningEngine.screen",
            return_value={"analysis_label": "REQUIREMENT", "reason": "该条目表达了明确可验证义务。"},
        ), patch(
            "app.services.ai_copilot.HeuristicEvidenceRetriever.retrieve",
            return_value=retrieved_evidences,
        ), patch(
            "app.services.ai_copilot.AzureOpenAIAssessmentEngine.assess",
            side_effect=AIAnalysisCancelledError("cancelled"),
        ):
            with self.assertRaises(AIAnalysisCancelledError):
                run_ai_copilot_assessment(row, [{"id": "kb-1"}], config=llm_shadow_config)

    def test_load_ai_copilot_config_defaults_to_llm_shadow_when_azure_runtime_is_configured(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "AZURE_AI_SEARCH_ENDPOINT": "https://search.example.search.windows.net",
                "AZURE_AI_SEARCH_INDEX_NAME": "reqpilot-knowledge",
                "AZURE_OPENAI_ENDPOINT": "https://openai.example.openai.azure.com",
                "AZURE_OPENAI_CHAT_DEPLOYMENT": "gpt-5.4",
                "AZURE_OPENAI_EMBEDDING_DEPLOYMENT": "text-embedding-3-large",
            },
            clear=True,
        ):
            config = load_ai_copilot_config()

        self.assertEqual(config.assessment_mode, "llm_shadow")

    def test_load_ai_copilot_config_keeps_heuristic_local_azure_profile_runnable(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "REQPILOT_AI_ASSESSMENT_MODE": "heuristic",
                "REQPILOT_RETRIEVAL_BACKEND": "local",
            },
            clear=True,
        ):
            config = load_ai_copilot_config()

        self.assertEqual(config.profile_id, "azure")
        self.assertEqual(config.assessment_mode, "heuristic")
        self.assertEqual(config.retrieval_backend, "local")
        self.assertTrue(config.provider_ready)

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
            clear=True,
        ):
            config = load_ai_copilot_config()

        self.assertEqual(config.azure_auth_mode, "rbac")
        self.assertEqual(config.retrieval_backend, "azure_sdk")
        self.assertTrue(config.uses_rbac_for_openai)
        self.assertTrue(config.uses_rbac_for_search)
        self.assertTrue(config.azure_ready)

    def test_load_ai_copilot_config_treats_managed_identity_as_rbac(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "REQPILOT_AI_ASSESSMENT_MODE": "llm_primary",
                "REQPILOT_AZURE_AUTH_MODE": "managed_identity",
                "AZURE_AI_SEARCH_ENDPOINT": "https://search.example.search.windows.net",
                "AZURE_AI_SEARCH_INDEX_NAME": "reqpilot-knowledge",
                "AZURE_OPENAI_ENDPOINT": "https://openai.example.openai.azure.com",
                "AZURE_OPENAI_CHAT_DEPLOYMENT": "gpt-4.1-mini",
                "AZURE_OPENAI_EMBEDDING_DEPLOYMENT": "text-embedding-3-large",
            },
            clear=True,
        ):
            config = load_ai_copilot_config()

        self.assertEqual(config.azure_auth_mode, "rbac")
        self.assertEqual(config.retrieval_backend, "azure_sdk")
        self.assertTrue(config.uses_rbac_for_openai)
        self.assertTrue(config.uses_rbac_for_search)

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
            clear=True,
        ):
            config = load_ai_copilot_config()

        self.assertEqual(config.azure_auth_mode, "api_key")
        self.assertEqual(config.retrieval_backend, "azure_haystack")
        self.assertTrue(config.uses_api_key_for_openai)
        self.assertTrue(config.uses_api_key_for_search)
        self.assertTrue(config.azure_ready)

    def test_load_ai_copilot_config_disables_aliyun_retrieval_backends(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "REQPILOT_ALIYUN_API_KEY": "aliyun-key",
                "REQPILOT_DASHSCOPE_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "REQPILOT_ALIYUN_CHAT_MODEL": "qwen3.5-plus",
                "REQPILOT_ALIYUN_EMBEDDING_MODEL": "text-embedding-v4",
            },
            clear=True,
        ):
            config = load_ai_copilot_config("aliyun")

        self.assertEqual(config.profile_id, "aliyun")
        self.assertEqual(config.provider, "aliyun")
        self.assertEqual(config.retrieval_backend, "disabled")
        self.assertFalse(config.retrieval_runtime_ready)
        self.assertFalse(config.provider_ready)
        self.assertIn(ALIYUN_RETRIEVAL_DISABLED_REASON, get_ai_profile_missing_config(config))

    def test_load_ai_copilot_config_enables_aliyun_local_qdrant_poc(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "REQPILOT_ALIYUN_API_KEY": "aliyun-key",
                "REQPILOT_DASHSCOPE_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "REQPILOT_ALIYUN_CHAT_MODEL": "qwen3.5-plus",
                "REQPILOT_ALIYUN_EMBEDDING_MODEL": "text-embedding-v4",
                "REQPILOT_ALIYUN_RETRIEVAL_BACKEND": "qdrant",
                "REQPILOT_ALIYUN_QDRANT_URL": "http://localhost:6333",
                "REQPILOT_ALIYUN_QDRANT_COLLECTION": "reqpilot-knowledge-aliyun-local",
            },
            clear=True,
        ):
            config = load_ai_copilot_config("aliyun")

        self.assertEqual(config.profile_id, "aliyun")
        self.assertEqual(config.retrieval_backend, "qdrant")
        self.assertEqual(config.qdrant_url, "http://localhost:6333")
        self.assertEqual(config.qdrant_collection_name, "reqpilot-knowledge-aliyun-local")
        self.assertTrue(config.retrieval_runtime_ready)
        self.assertTrue(config.provider_ready)
        self.assertNotIn(ALIYUN_RETRIEVAL_DISABLED_REASON, get_ai_profile_missing_config(config))
        self.assertEqual(config.openai_max_retries, 0)
        self.assertEqual(config.embedding_batch_size, 10)
        self.assertEqual(config.screening_model, "qwen3.5-flash")

    def test_embed_texts_batches_aliyun_embedding_requests(self) -> None:
        config = AICopilotConfig(
            assessment_mode="llm_shadow",
            retrieval_backend="qdrant",
            top_k=5,
            trace_dir=self.trace_dir,
            azure_auth_mode="",
            azure_allow_interactive_login=False,
            azure_search_endpoint="",
            azure_search_api_key="",
            azure_search_index_name="",
            azure_openai_endpoint="",
            azure_openai_api_key="",
            azure_openai_api_version="2024-10-21",
            azure_openai_chat_deployment="",
            azure_openai_embedding_deployment="",
            azure_openai_embedding_dimensions=0,
            azure_search_semantic_configuration="",
            llm_timeout_seconds=45.0,
            provider="aliyun",
            profile_id="aliyun",
            chat_model="qwen3.5-plus",
            screening_model="qwen3.5-flash",
            embedding_model="text-embedding-v4",
            dashscope_api_key="aliyun-key",
            dashscope_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            embedding_batch_size=10,
        )
        create = Mock(
            side_effect=[
                SimpleNamespace(
                    data=[SimpleNamespace(index=index, embedding=[float(index)]) for index in range(10)]
                ),
                SimpleNamespace(
                    data=[SimpleNamespace(index=index, embedding=[float(index + 10)]) for index in range(2)]
                ),
            ]
        )
        fake_client = SimpleNamespace(embeddings=SimpleNamespace(create=create))

        with patch("app.services.ai_copilot._aliyun_openai_client", return_value=fake_client):
            vectors = _embed_texts([f"chunk-{index}" for index in range(12)], config=config)

        self.assertEqual(create.call_count, 2)
        self.assertEqual(len(create.call_args_list[0].kwargs["input"]), 10)
        self.assertEqual(len(create.call_args_list[1].kwargs["input"]), 2)
        self.assertEqual(len(vectors), 12)
        self.assertEqual(vectors[0], [0.0])
        self.assertEqual(vectors[-1], [11.0])

    def test_dashscope_json_completion_retries_without_response_format_on_bad_request(self) -> None:
        class FakeBadRequest(Exception):
            def __init__(self) -> None:
                super().__init__("response_format json_object is not supported")
                self.status_code = 400

        create = Mock(
            side_effect=[
                FakeBadRequest(),
                SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))]),
            ]
        )
        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        aliyun_config = dataclasses.replace(
            self.config,
            provider="aliyun",
            profile_id="aliyun",
            chat_model="qwen3.5-plus",
            dashscope_api_key="aliyun-key",
            dashscope_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        )

        with patch("app.services.ai_copilot._aliyun_openai_client", return_value=fake_client):
            payload = _dashscope_json_completion(
                aliyun_config,
                [{"role": "user", "content": "return json"}],
            )

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(create.call_count, 2)

    def test_dashscope_json_completion_does_not_retry_on_timeout(self) -> None:
        create = Mock(side_effect=TimeoutError("Request timed out."))
        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        aliyun_config = dataclasses.replace(
            self.config,
            provider="aliyun",
            profile_id="aliyun",
            chat_model="qwen3.5-plus",
            dashscope_api_key="aliyun-key",
            dashscope_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        )

        with patch("app.services.ai_copilot._aliyun_openai_client", return_value=fake_client):
            with self.assertRaises(TimeoutError):
                _dashscope_json_completion(
                    aliyun_config,
                    [{"role": "user", "content": "return json"}],
                )

        self.assertEqual(create.call_count, 1)

    def test_azure_json_completion_streams_structured_output(self) -> None:
        streamed_events: list[dict[str, object]] = []

        class FakeStream:
            def __init__(self, events: list[object], completion: object) -> None:
                self._events = events
                self._completion = completion

            def __enter__(self) -> "FakeStream":
                return self

            def __exit__(self, exc_type, exc, tb) -> bool:
                return False

            def __iter__(self):
                return iter(self._events)

            def get_final_completion(self) -> object:
                return self._completion

        parsed_payload = AssessmentOutputSchema(
            normalized_requirement="Supplier shall submit CAMDS data before SOP.",
            proposed_decision="NEED_CLARIFICATION",
            confidence=0.81,
            reason="Evidence does not directly prove CAMDS submission.",
            risk_level="HIGH",
            clarification_question="请补充 CAMDS 数据提交记录。",
            evidences=[],
        )
        completion = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(parsed=parsed_payload, refusal=""),
                    finish_reason="stop",
                )
            ],
            usage=SimpleNamespace(prompt_tokens=120, completion_tokens=48, total_tokens=168),
        )
        fake_stream = FakeStream(
            [
                SimpleNamespace(type="content.delta", delta='{"normalized_requirement":"Supplier shall submit CAMDS data before SOP.",'),
                SimpleNamespace(type="content.delta", delta='"proposed_decision":"NEED_CLARIFICATION"}'),
            ],
            completion,
        )
        stream = Mock(return_value=fake_stream)
        parse = Mock()
        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(stream=stream, parse=parse)))
        azure_config = dataclasses.replace(
            self.config,
            provider="azure",
            profile_id="azure",
            azure_openai_endpoint="https://openai.example.openai.azure.com",
            azure_openai_chat_deployment="gpt-5.4",
            azure_openai_embedding_deployment="text-embedding-3-large",
            azure_openai_embedding_dimensions=1536,
        )

        with patch("app.services.ai_copilot._azure_openai_client", return_value=fake_client):
            payload = _azure_json_completion(
                azure_config,
                [{"role": "user", "content": "return json"}],
                model_name="gpt-5.4",
                response_format=AssessmentOutputSchema,
                progress_callback=streamed_events.append,
            )

        self.assertEqual(payload["proposed_decision"], "NEED_CLARIFICATION")
        self.assertEqual(stream.call_count, 1)
        self.assertEqual(parse.call_count, 0)
        self.assertTrue(any(item.get("type") == "answer" for item in streamed_events))
        self.assertTrue(any(item.get("type") == "complete" for item in streamed_events))

    def test_run_ai_copilot_assessment_emits_stage_progress_for_screening_and_assessment(self) -> None:
        llm_shadow_config = dataclasses.replace(
            self.config,
            assessment_mode="llm_shadow",
            retrieval_backend="local",
            azure_search_endpoint="https://search.example.search.windows.net",
            azure_search_index_name="reqpilot-knowledge",
            azure_openai_endpoint="https://openai.example.openai.azure.com",
            azure_openai_chat_deployment="gpt-5.4",
            azure_openai_embedding_deployment="text-embedding-3-large",
            azure_openai_embedding_dimensions=1536,
        )
        row = {
            "id": "req-progress-1",
            "req_code": "REQ-PROGRESS-1",
            "display_text": "Supplier shall submit CAMDS data before SOP.",
            "requirement_type": "REGULATION",
            "parent_section_label": "5.4 Compliance",
        }
        retrieved_evidences = [
            {
                "source_doc_id": "kb-1",
                "source_doc_name": "baseline.txt",
                "source_location": "Chunk 1",
                "excerpt": "No direct CAMDS submission record is attached in this baseline.",
                "relevance": 0.42,
                "source_doc_version": "v1.0",
            }
        ]
        progress_events: list[dict[str, object]] = []

        def fake_screen(
            _self: object,
            context: dict[str, object],
            *,
            progress_callback=None,
        ) -> dict[str, object]:
            if progress_callback:
                progress_callback(
                    {
                        "type": "reasoning",
                        "delta": "先判断该条是否属于真实 requirement。",
                        "content": "先判断该条是否属于真实 requirement。",
                        "characters_emitted": 18,
                    }
                )
                progress_callback(
                    {
                        "type": "answer",
                        "delta": "{\"analysis_label\":\"REQUIREMENT\"}",
                        "content": "{\"analysis_label\":\"REQUIREMENT\"}",
                        "characters_emitted": 30,
                    }
                )
            return {"analysis_label": "REQUIREMENT", "reason": "该条表达了明确的 CAMDS 数据提交义务。"}

        def fake_assess(
            _self: object,
            context: dict[str, object],
            evidences: list[dict[str, object]],
            *,
            progress_callback=None,
        ) -> dict[str, object]:
            if progress_callback:
                progress_callback(
                    {
                        "type": "answer",
                        "delta": "{\"proposed_decision\":\"NEED_CLARIFICATION\"}",
                        "content": "{\"proposed_decision\":\"NEED_CLARIFICATION\"}",
                        "characters_emitted": 42,
                    }
                )
            return {
                "normalized_requirement": "Supplier shall submit CAMDS data before SOP.",
                "proposed_decision": "NEED_CLARIFICATION",
                "confidence": 0.77,
                "reason": "Evidence is indirect and cannot prove CAMDS submission.",
                "risk_level": "HIGH",
                "clarification_question": "请补充 CAMDS 数据提交记录。",
                "evidences": [],
            }

        with patch(
            "app.services.ai_copilot.AzureOpenAIScreeningEngine.screen",
            autospec=True,
            side_effect=fake_screen,
        ), patch(
            "app.services.ai_copilot.HeuristicEvidenceRetriever.retrieve",
            return_value=retrieved_evidences,
        ), patch(
            "app.services.ai_copilot.AzureOpenAIAssessmentEngine.assess",
            autospec=True,
            side_effect=fake_assess,
        ):
            result = run_ai_copilot_assessment(
                row,
                [{"id": "kb-1"}],
                config=llm_shadow_config,
                progress_callback=progress_events.append,
            )

        self.assertEqual(result["assessment"]["assessment_mode"], "llm_shadow")
        self.assertTrue(any(item.get("type") == "stage" and item.get("stage") == "screening" for item in progress_events))
        self.assertTrue(any(item.get("type") == "stage" and item.get("stage") == "retrieval" for item in progress_events))
        self.assertTrue(any(item.get("type") == "stage" and item.get("stage") == "assessment" for item in progress_events))
        self.assertTrue(
            any(
                item.get("type") == "reasoning"
                and item.get("stage") == "screening"
                and item.get("mode") == "screening_llm_shadow"
                for item in progress_events
            )
        )
        self.assertTrue(
            any(
                item.get("type") == "answer"
                and item.get("stage") == "assessment"
                and item.get("mode") == "llm_shadow"
                for item in progress_events
            )
        )

    def test_default_profile_becomes_aliyun_when_aliyun_local_qdrant_poc_is_ready(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "REQPILOT_AI_ASSESSMENT_MODE": "llm_shadow",
                "REQPILOT_RETRIEVAL_BACKEND": "azure_sdk",
                "AZURE_OPENAI_ENDPOINT": "https://openai.example.openai.azure.com",
                "AZURE_OPENAI_CHAT_DEPLOYMENT": "gpt-5.4",
                "AZURE_OPENAI_EMBEDDING_DEPLOYMENT": "text-embedding-3-large",
                "AZURE_AI_SEARCH_ENDPOINT": "https://search.example.search.windows.net",
                "AZURE_AI_SEARCH_INDEX_NAME": "reqpilot-knowledge",
                "REQPILOT_ALIYUN_API_KEY": "aliyun-key",
                "REQPILOT_DASHSCOPE_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "REQPILOT_ALIYUN_CHAT_MODEL": "qwen3.5-plus",
                "REQPILOT_ALIYUN_EMBEDDING_MODEL": "text-embedding-v4",
                "REQPILOT_ALIYUN_RETRIEVAL_BACKEND": "qdrant",
                "REQPILOT_ALIYUN_QDRANT_URL": "http://localhost:6333",
                "REQPILOT_ALIYUN_QDRANT_COLLECTION": "reqpilot-knowledge-aliyun-local",
            },
            clear=True,
        ):
            config = load_ai_copilot_config()

        self.assertEqual(config.profile_id, "aliyun")
        self.assertEqual(config.provider, "aliyun")

    def test_apply_runtime_ai_provider_setup_can_switch_default_profile_to_aliyun(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "AZURE_OPENAI_ENDPOINT": "https://openai.example.openai.azure.com",
                "AZURE_OPENAI_CHAT_DEPLOYMENT": "gpt-5.4",
                "AZURE_OPENAI_EMBEDDING_DEPLOYMENT": "text-embedding-3-large",
                "AZURE_AI_SEARCH_ENDPOINT": "https://search.example.search.windows.net",
                "AZURE_AI_SEARCH_INDEX_NAME": "reqpilot-knowledge",
            },
            clear=True,
        ):
            payload = apply_runtime_ai_provider_setup(
                {
                    "default_profile_id": "aliyun",
                    "aliyun": {
                        "api_key": "aliyun-key",
                        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                        "chat_model": "qwen3.5-plus",
                        "embedding_model": "text-embedding-v4",
                        "qdrant_url": "http://localhost:6333",
                        "qdrant_collection": "reqpilot-knowledge-aliyun-local",
                    },
                }
            )
            config = load_ai_copilot_config()

        self.assertEqual(payload["default_profile_id"], "aliyun")
        self.assertEqual(config.profile_id, "aliyun")
        self.assertEqual(config.retrieval_backend, "qdrant")
        self.assertEqual(config.qdrant_url, "http://localhost:6333")
        self.assertEqual(config.embedding_model, "text-embedding-v4")

    def test_explicit_default_profile_prefers_aliyun_even_when_not_ready(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "REQPILOT_AI_DEFAULT_PROFILE": "aliyun",
                "AZURE_OPENAI_ENDPOINT": "https://openai.example.openai.azure.com",
                "AZURE_OPENAI_CHAT_DEPLOYMENT": "gpt-5.4",
                "AZURE_OPENAI_EMBEDDING_DEPLOYMENT": "text-embedding-3-large",
                "AZURE_AI_SEARCH_ENDPOINT": "https://search.example.search.windows.net",
                "AZURE_AI_SEARCH_INDEX_NAME": "reqpilot-knowledge",
            },
            clear=True,
        ):
            config = load_ai_copilot_config()

        self.assertEqual(config.profile_id, "aliyun")
        self.assertFalse(config.provider_ready)

    def test_build_runtime_ai_provider_setup_exposes_masked_provider_form_defaults(self) -> None:
        apply_runtime_ai_provider_setup(
            {
                "default_profile_id": "aliyun",
                "aliyun": {
                    "api_key": "aliyun-key",
                    "chat_model": "qwen3.5-plus",
                    "embedding_model": "text-embedding-v4",
                    "qdrant_url": "http://localhost:6333",
                    "qdrant_collection": "reqpilot-knowledge-aliyun-local",
                },
            }
        )

        payload = build_runtime_ai_provider_setup()

        self.assertEqual(payload["default_profile_id"], "aliyun")
        self.assertTrue(payload["aliyun"]["api_key_configured"])
        self.assertEqual(payload["aliyun"]["chat_model"], "qwen3.5-plus")
        self.assertEqual(payload["aliyun"]["embedding_model"], "text-embedding-v4")
        self.assertEqual(payload["aliyun"]["qdrant_url"], "http://localhost:6333")

    def test_get_ai_profile_missing_config_flags_non_ascii_aliyun_keys(self) -> None:
        config = AICopilotConfig(
            assessment_mode="llm_shadow",
            retrieval_backend="qdrant",
            top_k=5,
            trace_dir=self.trace_dir,
            azure_auth_mode="",
            azure_allow_interactive_login=False,
            azure_search_endpoint="",
            azure_search_api_key="",
            azure_search_index_name="",
            azure_openai_endpoint="",
            azure_openai_api_key="",
            azure_openai_api_version="2024-10-21",
            azure_openai_chat_deployment="",
            azure_openai_embedding_deployment="",
            azure_openai_embedding_dimensions=0,
            azure_search_semantic_configuration="",
            llm_timeout_seconds=45.0,
            profile_id="aliyun",
            provider="aliyun",
            profile_label="Aliyun",
            chat_model="qwen3.5-plus",
            embedding_model="text-embedding-v4",
            dashscope_api_key="sk-测试key",
            dashscope_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            qdrant_url="http://localhost:6333",
            qdrant_api_key="测试-qdrant",
            qdrant_collection_name="reqpilot-knowledge-aliyun-local",
        )

        missing = get_ai_profile_missing_config(config)

        self.assertIn(ALIYUN_API_KEY_NON_ASCII_REASON, missing)
        self.assertIn(ALIYUN_QDRANT_API_KEY_NON_ASCII_REASON, missing)
        self.assertFalse(is_ascii_runtime_secret("sk-测试key"))
        self.assertTrue(is_ascii_runtime_secret("sk-test"))

    def test_sync_knowledge_document_index_marks_aliyun_retrieval_as_disabled(self) -> None:
        config = AICopilotConfig(
            assessment_mode="llm_shadow",
            retrieval_backend="disabled",
            top_k=5,
            trace_dir=self.trace_dir,
            azure_auth_mode="",
            azure_allow_interactive_login=False,
            azure_search_endpoint="",
            azure_search_api_key="",
            azure_search_index_name="",
            azure_openai_endpoint="",
            azure_openai_api_key="",
            azure_openai_api_version="2024-10-21",
            azure_openai_chat_deployment="",
            azure_openai_embedding_deployment="",
            azure_openai_embedding_dimensions=0,
            azure_search_semantic_configuration="",
            llm_timeout_seconds=45.0,
            profile_id="aliyun",
            provider="aliyun",
            profile_label="Aliyun",
            chat_model="qwen3.5-plus",
            embedding_model="text-embedding-v4",
            dashscope_api_key="aliyun-key",
        )
        knowledge_doc = {
            "id": "kb-aliyun-1",
            "file_name": "knowledge-aliyun.txt",
            "doc_version": "2026.04",
            "doc_type": "STANDARD",
            "status": "ACTIVE",
            "effective_from": "2026-01-01",
            "effective_to": "",
            "extracted_text": (
                "1 Overview\n"
                "Aliyun knowledge sync.\n"
                "|Item|Condition|\n"
                "|---|---|\n"
                "|Voltage|12V|\n"
            ),
        }

        with patch("app.services.ai_copilot.INDEX_PAYLOAD_DIR", self.index_dir):
            sync = sync_knowledge_document_index("project-1", knowledge_doc, config=config)

        self.assertEqual(sync["status"], "ERROR")
        self.assertEqual(sync["targets"]["aliyun"]["retrieval_backend"], "disabled")
        self.assertIn("local Qdrant POC is disabled", sync["targets"]["aliyun"]["error"])

    def test_sync_knowledge_document_index_uses_qdrant_for_aliyun_local_poc(self) -> None:
        config = AICopilotConfig(
            assessment_mode="llm_shadow",
            retrieval_backend="qdrant",
            top_k=5,
            trace_dir=self.trace_dir,
            azure_auth_mode="",
            azure_allow_interactive_login=False,
            azure_search_endpoint="",
            azure_search_api_key="",
            azure_search_index_name="",
            azure_openai_endpoint="",
            azure_openai_api_key="",
            azure_openai_api_version="2024-10-21",
            azure_openai_chat_deployment="",
            azure_openai_embedding_deployment="",
            azure_openai_embedding_dimensions=0,
            azure_search_semantic_configuration="",
            llm_timeout_seconds=45.0,
            profile_id="aliyun",
            provider="aliyun",
            profile_label="Aliyun",
            chat_model="qwen3.5-plus",
            embedding_model="text-embedding-v4",
            dashscope_api_key="aliyun-key",
            dashscope_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            qdrant_url="http://localhost:6333",
            qdrant_collection_name="reqpilot-knowledge-aliyun-local",
        )
        knowledge_doc = {
            "id": "kb-aliyun-qdrant-1",
            "file_name": "knowledge-aliyun-qdrant.txt",
            "doc_version": "2026.04",
            "doc_type": "STANDARD",
            "status": "ACTIVE",
            "effective_from": "2026-01-01",
            "effective_to": "",
            "extracted_text": "Aliyun Qdrant sync test.\nThe system shall support wake-up behavior.",
        }
        fake_indexer = Mock()
        fake_indexer.upsert_chunks.return_value = 2

        with patch("app.services.ai_copilot.INDEX_PAYLOAD_DIR", self.index_dir), patch(
            "app.services.ai_copilot._select_indexer",
            return_value=fake_indexer,
        ):
            sync = sync_knowledge_document_index("project-1", knowledge_doc, config=config)

        self.assertEqual(sync["status"], "READY")
        self.assertEqual(sync["targets"]["aliyun"]["retrieval_backend"], "qdrant")
        self.assertEqual(sync["targets"]["aliyun"]["status"], "READY")
        fake_indexer.upsert_chunks.assert_called_once()

    def test_default_azure_credential_prefers_cli_over_local_cached_tokens(self) -> None:
        fake_azure = ModuleType("azure")
        fake_azure.__path__ = []
        fake_identity = ModuleType("azure.identity")
        credential_ctor = Mock(side_effect=[sentinel.non_interactive, sentinel.interactive])
        fake_identity.DefaultAzureCredential = credential_ctor
        fake_azure.identity = fake_identity

        _default_azure_credential.cache_clear()
        try:
            with patch.dict(
                "sys.modules",
                {"azure": fake_azure, "azure.identity": fake_identity},
                clear=False,
            ):
                self.assertIs(_default_azure_credential(False), sentinel.non_interactive)
                self.assertIs(_default_azure_credential(True), sentinel.interactive)
        finally:
            _default_azure_credential.cache_clear()

        self.assertEqual(
            credential_ctor.call_args_list,
            [
                call(
                    exclude_interactive_browser_credential=True,
                    exclude_shared_token_cache_credential=True,
                    exclude_visual_studio_code_credential=True,
                ),
                call(
                    exclude_interactive_browser_credential=False,
                    exclude_shared_token_cache_credential=True,
                    exclude_visual_studio_code_credential=True,
                ),
            ],
        )

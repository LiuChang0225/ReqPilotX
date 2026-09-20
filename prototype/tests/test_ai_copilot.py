from __future__ import annotations

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import json
import hashlib
import sqlite3
import ssl
import stat
import sys
import tempfile
import unittest
import uuid
import dataclasses
from datetime import date
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, call, patch, sentinel

from app.domain.knowledge import (
    EVIDENCE_INDEX_CONTRACT_VERSION,
    compute_evidence_content_hash,
    evaluate_knowledge_evidence_eligibility,
)
from app.services import ai_copilot as ai_copilot_module
from app.ports.visual_text_provider import (
    VisualTextProviderRejected,
    VisualTextProviderRejectionCode,
)
from app.services.ai_copilot import (
    ALIYUN_API_KEY_NON_ASCII_REASON,
    ALIYUN_QDRANT_API_KEY_NON_ASCII_REASON,
    AIAnalysisCancelledError,
    apply_runtime_ai_provider_setup,
    ALIYUN_RETRIEVAL_DISABLED_REASON,
    AICopilotConfig,
    AliyunDashScopeAssessmentEngine,
    AssessmentOutputSchema,
    AzureHaystackEvidenceRetriever,
    AzureHaystackIndexer,
    AzureOpenAIAssessmentEngine,
    AzureSearchSDKIndexer,
    DashVectorIndexer,
    DEFAULT_DASHSCOPE_CHAT_MODEL,
    DEFAULT_DASHSCOPE_SCREENING_MODEL,
    DEFAULT_PROMPT_VERSION,
    DASHVECTOR_INDEX_MIGRATION_ACTION,
    EVIDENCE_INDEX_RESYNC_ACTION,
    EVIDENCE_INDEX_RESYNC_REQUIRED_REASON,
    _default_azure_credential,
    _decision_evidence_threshold_for_backend,
    _azure_json_completion,
    _dashscope_json_completion,
    _ensure_qdrant_collection,
    _embed_texts,
    _ensure_azure_index_contract,
    _ensure_azure_search_index,
    _azure_filter_to_odata,
    _azure_metadata_fields,
    _build_dashvector_filter,
    _build_qdrant_filter,
    _dashvector_metadata_fields,
    _dashvector_contract_config,
    _dashvector_contract_collection_name,
    _filter_retrieved_evidence_candidates,
    _extend_evidence_with_adjacent_chunks,
    _is_evidence_contract_schema_error,
    _remote_evidence_index_contract_ready,
    _reconciliation_policy_for_config,
    _flatten_azure_chunk_meta,
    _flatten_qdrant_chunk_meta,
    _qdrant_point_id,
    _azure_search_item_to_evidence,
    _validate_evidence_references,
    AssessmentEvidenceSchema,
    build_azure_filter,
    build_requirement_normalization_detail,
    build_knowledge_chunks,
    build_llm_messages,
    build_assessment_context,
    build_runtime_ai_provider_setup,
    clear_runtime_ai_provider_overrides,
    fallback_need_clarification,
    get_ai_profile_missing_config,
    is_ascii_runtime_secret,
    load_ai_copilot_config,
    probe_runtime_aliyun_models,
    HeuristicEvidenceRetriever,
    QdrantEvidenceRetriever,
    QdrantIndexer,
    _delete_existing_azure_chunks_for_source_docs,
    reconcile_assessment_output,
    run_ai_copilot_assessment,
    sync_knowledge_document_index,
)
from scripts.ai_eval_readiness import _read_only_connection, build_readiness_report
from scripts.ai_gold_eval import (
    _coverage_from_cases,
    assessment_contract_sha256,
    build_evaluation_metrics,
    evaluate_cases,
    sha256_bytes,
    validate_gold_contract,
)


def _with_evidence_contract(
    evidence: dict,
    *,
    evidence_id: str | None = None,
    content: str | None = None,
) -> dict:
    canonical_content = str(content if content is not None else evidence.get("excerpt") or evidence.get("content") or "")
    source_doc_id = str(evidence.get("source_doc_id") or "test-doc")
    source_location = str(evidence.get("source_location") or "test-location")
    resolved_evidence_id = evidence_id or f"{source_doc_id}_{source_location.replace(' ', '_')}"
    return {
        **evidence,
        "authority": str(
            evidence.get("authority") or "INTERNAL_CONTROLLED"
        ),
        "claim_role": str(
            evidence.get("claim_role") or "VALIDATION_RESULT"
        ),
        "applicability": str(evidence.get("applicability") or "PROJECT_SCOPE"),
        "evidence_id": resolved_evidence_id,
        "content_hash": compute_evidence_content_hash(canonical_content),
        "evidence_contract_version": EVIDENCE_INDEX_CONTRACT_VERSION,
        "evidence_contract_verified": True,
    }


def _assessment_candidate(
    evidence: dict,
    *,
    decision: str,
    reason: str,
    confidence: float,
    risk_level: str,
) -> dict:
    evidence_ref = {
        key: evidence.get(key)
        for key in (
            "evidence_id",
            "content_hash",
            "evidence_contract_version",
            "source_doc_id",
            "source_doc_name",
            "source_location",
            "excerpt",
            "relevance",
            "source_doc_version",
        )
    }
    return {
        "normalized_requirement": "",
        "proposed_decision": decision,
        "confidence": confidence,
        "reason": reason,
        "risk_level": risk_level,
        "clarification_question": "" if decision != "NEED_CLARIFICATION" else "Please provide direct evidence.",
        "evidences": [evidence_ref],
    }


def _llm_assessment_candidate(
    evidence: dict,
    *,
    statement: str,
    decision: str,
    reason: str,
    confidence: float,
    risk_level: str,
) -> dict:
    candidate = _assessment_candidate(
        evidence,
        decision=decision,
        reason=reason,
        confidence=confidence,
        risk_level=risk_level,
    )
    compliance_status = {
        "ACCEPT": "COMPLIANT",
        "DEVIATION_ACCEPT": "PARTIALLY_COMPLIANT",
        "REJECT": "NON_COMPLIANT",
        "NEED_CLARIFICATION": "INSUFFICIENT_INFORMATION",
    }[decision]
    finding = {
        "COMPLIANT": "SUPPORTS",
        "PARTIALLY_COMPLIANT": "PARTIALLY_SUPPORTS",
        "NON_COMPLIANT": "CONTRADICTS",
        "INSUFFICIENT_INFORMATION": "CONTEXT_ONLY",
    }[compliance_status]
    evidence_id = str(evidence.get("evidence_id") or "")
    gap = (
        {
            "schema_version": "assessment-information-gap/v1",
            "found_evidence": [
                {
                    "retrieval_evidence_id": evidence_id,
                    "proves": "The candidate is relevant but incomplete.",
                }
            ],
            "missing_items": [
                {
                    "item": statement,
                    "blocking_reason": "Direct evidence is missing.",
                    "required_input": "Provide direct validation evidence.",
                }
            ],
            "conflicts": [],
            "clarification_questions": ["Please provide direct validation evidence."],
        }
        if compliance_status == "INSUFFICIENT_INFORMATION"
        else {}
    )
    candidate.update(
        {
            "normalized_requirement": statement,
            "compliance_status": compliance_status,
            "evidence_findings": [
                {
                    "retrieval_evidence_id": evidence_id,
                    "finding": finding,
                    "supported_claims": (
                        [statement]
                        if finding in {"SUPPORTS", "PARTIALLY_SUPPORTS"}
                        else []
                    ),
                    "unsupported_claims": (
                        [statement]
                        if finding in {"CONTRADICTS", "CONTEXT_ONLY"}
                        else []
                    ),
                    "rationale": reason,
                }
            ],
            "sub_judgments": [
                {
                    "sub_judgment_id": "sj-1",
                    "source_ranges": ["atomic_source_text"],
                    "statement": statement,
                    "compliance_status": compliance_status,
                    "reason": reason,
                    "retrieval_evidence_ids": [evidence_id],
                    "structured_information_gap": gap,
                }
            ],
            "structured_information_gap": gap,
        }
    )
    return candidate


class AICopilotTests(unittest.TestCase):
    def test_all_llm_assessment_engines_publish_the_current_prompt_contract(self) -> None:
        self.assertEqual(
            AzureOpenAIAssessmentEngine.prompt_version,
            DEFAULT_PROMPT_VERSION,
        )
        self.assertEqual(
            AliyunDashScopeAssessmentEngine.prompt_version,
            DEFAULT_PROMPT_VERSION,
        )

    def test_all_llm_profiles_use_guarded_reconciliation(self) -> None:
        azure = dataclasses.replace(
            self.config,
            provider="azure",
            assessment_mode="llm_primary",
        )
        aliyun = dataclasses.replace(
            self.config,
            provider="aliyun",
            profile_id="aliyun",
            assessment_mode="llm_shadow",
        )

        self.assertEqual(_reconciliation_policy_for_config(azure), "llm_guarded")
        self.assertEqual(_reconciliation_policy_for_config(aliyun), "llm_guarded")

    def test_current_aliyun_model_defaults_are_explicit(self) -> None:
        self.assertEqual(DEFAULT_DASHSCOPE_CHAT_MODEL, "qwen3.8-max")
        self.assertEqual(DEFAULT_DASHSCOPE_SCREENING_MODEL, "qwen3.7-flash")

    def test_dashscope_compatible_base_url_is_strictly_allowlisted(self) -> None:
        validate = ai_copilot_module.validate_dashscope_compatible_base_url
        for value in (
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "https://dashscope-intl.aliyuncs.com:443/compatible-mode/v1/",
            "https://workspace-1.maas.aliyuncs.com/compatible-mode/v1",
        ):
            self.assertTrue(validate(value).endswith("/compatible-mode/v1"))
        for value in (
            "http://dashscope.aliyuncs.com/compatible-mode/v1",
            "https://evil.example/compatible-mode/v1",
            "https://user@dashscope.aliyuncs.com/compatible-mode/v1",
            "https://dashscope.aliyuncs.com:8443/compatible-mode/v1",
            "https://dashscope.aliyuncs.com/compatible-mode/v1?target=evil",
            "https://dashscope.aliyuncs.com/compatible-mode/v1#secret",
            "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate(value)

    def test_aliyun_client_rejects_untrusted_base_url_before_client_creation(
        self,
    ) -> None:
        constructor = Mock()
        unsafe = dataclasses.replace(
            self.config,
            provider="aliyun",
            dashscope_api_key="synthetic-key",
            dashscope_base_url="https://evil.example/compatible-mode/v1",
        )
        with patch.dict(
            sys.modules, {"openai": SimpleNamespace(OpenAI=constructor)}
        ), self.assertRaises(ValueError):
            ai_copilot_module._aliyun_openai_client(unsafe)
        constructor.assert_not_called()

    def test_runtime_config_save_is_private_atomic_and_preserves_unknown_fields(
        self,
    ) -> None:
        storage = self.tmp_path / "private-runtime"
        storage.mkdir(mode=0o755)
        config_path = storage / "config.json"
        config_path.write_text(
            json.dumps({"UNKNOWN_OWNER_FIELD": {"keep": True}}), encoding="utf-8"
        )
        config_path.chmod(0o644)

        with patch.object(ai_copilot_module, "STORAGE_DIR", storage):
            apply_runtime_ai_provider_setup(
                {
                    "default_profile_id": "aliyun",
                    "aliyun": {
                        "api_key": "synthetic-key",
                        "base_url": ai_copilot_module.DEFAULT_DASHSCOPE_BASE_URL,
                    },
                }
            )

        saved = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["UNKNOWN_OWNER_FIELD"], {"keep": True})
        self.assertEqual(stat.S_IMODE(storage.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(config_path.stat().st_mode), 0o600)
        self.assertEqual(list(storage.glob(".config.json.*.tmp")), [])

    def test_failed_atomic_replace_keeps_previous_runtime_config(self) -> None:
        storage = self.tmp_path / "atomic-runtime"
        storage.mkdir(mode=0o700)
        config_path = storage / "config.json"
        previous_payload = {
            "UNKNOWN_OWNER_FIELD": "old",
            "REQPILOT_AI_DEFAULT_PROFILE": "aliyun",
            "REQPILOT_ALIYUN_API_KEY": "old-synthetic-key",
            "REQPILOT_DASHSCOPE_BASE_URL": (
                ai_copilot_module.DEFAULT_DASHSCOPE_BASE_URL
            ),
            "REQPILOT_ALIYUN_CHAT_MODEL": "old-model",
            "REQPILOT_RUNTIME_CONNECTION_REVISION": "23",
        }
        previous = (json.dumps(previous_payload) + "\n").encode("utf-8")
        config_path.write_bytes(previous)
        config_path.chmod(0o600)
        ai_copilot_module._RUNTIME_AI_ENV_OVERRIDES.update(
            {key: str(value) for key, value in previous_payload.items()}
        )

        with patch.object(ai_copilot_module, "STORAGE_DIR", storage), patch.object(
            ai_copilot_module.os, "replace", side_effect=OSError("replace failed")
        ):
            with self.assertRaises(RuntimeError):
                apply_runtime_ai_provider_setup(
                    {
                        "default_profile_id": "aliyun",
                        "aliyun": {
                            "api_key": "new-synthetic-key",
                            "base_url": ai_copilot_module.DEFAULT_DASHSCOPE_BASE_URL,
                            "chat_model": "new-model",
                        },
                    }
                )

        self.assertEqual(config_path.read_bytes(), previous)
        self.assertEqual(list(storage.glob(".config.json.*.tmp")), [])
        restored = load_ai_copilot_config("aliyun")
        self.assertEqual(restored.chat_model, "old-model")
        self.assertEqual(restored.dashscope_api_key, "old-synthetic-key")
        self.assertEqual(ai_copilot_module.get_runtime_connection_revision(), 23)

    def test_worker_idle_reload_replaces_config_atomically(self) -> None:
        storage = self.tmp_path / "worker-reload"
        storage.mkdir(mode=0o700)
        config_path = storage / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "REQPILOT_RUNTIME_CONNECTION_REVISION": "41",
                    "REQPILOT_ALIYUN_CHAT_MODEL": "fresh-model",
                }
            ),
            encoding="utf-8",
        )
        ai_copilot_module._RUNTIME_AI_ENV_OVERRIDES.update(
            {
                "REQPILOT_RUNTIME_CONNECTION_REVISION": "40",
                "REQPILOT_ALIYUN_CHAT_MODEL": "stale-model",
                "REMOVED_FROM_PERSISTED_CONFIG": "must-disappear",
            }
        )

        with patch.object(ai_copilot_module, "STORAGE_DIR", storage), patch.object(
            ai_copilot_module._aliyun_openai_client, "cache_clear"
        ) as clear_aliyun, patch.object(
            ai_copilot_module._default_azure_credential, "cache_clear"
        ) as clear_azure:
            revision = ai_copilot_module.reload_runtime_ai_config_from_file()

        self.assertEqual(revision, 41)
        self.assertEqual(
            ai_copilot_module._RUNTIME_AI_ENV_OVERRIDES,
            {
                "REQPILOT_RUNTIME_CONNECTION_REVISION": "41",
                "REQPILOT_ALIYUN_CHAT_MODEL": "fresh-model",
            },
        )
        clear_aliyun.assert_called_once_with()
        clear_azure.assert_called_once_with()

    def test_worker_idle_reload_keeps_previous_snapshot_when_json_is_invalid(
        self,
    ) -> None:
        storage = self.tmp_path / "worker-reload-invalid"
        storage.mkdir(mode=0o700)
        (storage / "config.json").write_text("{invalid", encoding="utf-8")
        previous = {
            "REQPILOT_RUNTIME_CONNECTION_REVISION": "44",
            "REQPILOT_ALIYUN_CHAT_MODEL": "stable-model",
        }
        ai_copilot_module._RUNTIME_AI_ENV_OVERRIDES.update(previous)

        with patch.object(ai_copilot_module, "STORAGE_DIR", storage):
            with self.assertRaises(RuntimeError):
                ai_copilot_module.reload_runtime_ai_config_from_file()

        self.assertEqual(ai_copilot_module._RUNTIME_AI_ENV_OVERRIDES, previous)

    def test_noop_provider_save_does_not_advance_connection_revision(self) -> None:
        storage = self.tmp_path / "noop-save"
        storage.mkdir(mode=0o700)
        payload = {
            "default_profile_id": "aliyun",
            "aliyun": {
                "api_key": "synthetic-key",
                "base_url": ai_copilot_module.DEFAULT_DASHSCOPE_BASE_URL,
            },
        }

        with patch.object(ai_copilot_module, "STORAGE_DIR", storage):
            first = apply_runtime_ai_provider_setup(payload)
            first_bytes = (storage / "config.json").read_bytes()
            second = apply_runtime_ai_provider_setup(payload)
            second_bytes = (storage / "config.json").read_bytes()

        self.assertEqual(first["connection_revision"], 1)
        self.assertEqual(second["connection_revision"], 1)
        self.assertEqual(second_bytes, first_bytes)

    def test_windows_skips_runtime_config_directory_fsync(self) -> None:
        with patch.object(ai_copilot_module.os, "name", "nt"), patch.object(
            ai_copilot_module.os, "open"
        ) as open_directory:
            ai_copilot_module._fsync_directory(self.tmp_path)
        open_directory.assert_not_called()

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

    def _azure_mode_config(self, assessment_mode: str) -> AICopilotConfig:
        return dataclasses.replace(
            self.config,
            assessment_mode=assessment_mode,
            retrieval_backend="local",
            azure_search_endpoint="https://search.example.search.windows.net",
            azure_search_index_name="reqpilot-knowledge",
            azure_openai_endpoint="https://openai.example.openai.azure.com",
            azure_openai_chat_deployment="gpt-5.4",
            azure_openai_embedding_deployment="text-embedding-3-large",
            azure_openai_embedding_dimensions=1536,
        )

    def _reconcile_numeric_case(
        self,
        *,
        requirement: str,
        evidence_excerpt: str,
        candidate_requirement: str | None = None,
        decision: str = "ACCEPT",
    ) -> dict:
        canonical = _with_evidence_contract(
            {
                "source_doc_id": "kb-numeric-1",
                "source_doc_name": "numeric-validation.txt",
                "source_location": "Chunk 1",
                "excerpt": evidence_excerpt,
                "relevance": 0.91,
                "source_doc_version": "v1",
            },
            evidence_id="kb-numeric-1_chunk_1",
        )
        candidate = _assessment_candidate(
            canonical,
            decision=decision,
            reason="The canonical evidence demonstrates compliance.",
            confidence=0.91,
            risk_level="LOW",
        )
        candidate["normalized_requirement"] = candidate_requirement or requirement
        return reconcile_assessment_output(
            context={
                "requirement_text": requirement,
                "normalized_requirement": requirement,
                "requirement_type": "FUNCTIONAL",
            },
            candidate=candidate,
            retrieved_evidences=[canonical],
            fallback_reason="",
        )

    def _reconcile_numeric_evidence_set(
        self,
        *,
        requirement: str,
        evidence_excerpts: list[str],
        cited_indexes: tuple[int, ...],
        normalized_requirement: str | None = None,
        candidate_requirement: str | None = None,
        decision: str = "ACCEPT",
    ) -> dict:
        canonical = [
            _with_evidence_contract(
                {
                    "source_doc_id": f"kb-numeric-{index + 1}",
                    "source_doc_name": "numeric-validation.txt",
                    "source_location": f"Chunk {index + 1}",
                    "excerpt": excerpt,
                    "relevance": 0.95 - (index * 0.01),
                    "source_doc_version": "v1",
                },
                evidence_id=f"kb-numeric-{index + 1}_chunk_{index + 1}",
            )
            for index, excerpt in enumerate(evidence_excerpts)
        ]
        candidate = _assessment_candidate(
            canonical[cited_indexes[0]],
            decision=decision,
            reason="The canonical evidence demonstrates compliance.",
            confidence=0.91,
            risk_level="LOW",
        )
        candidate["evidences"] = [
            _assessment_candidate(
                canonical[index],
                decision="ACCEPT",
                reason="Supported.",
                confidence=0.91,
                risk_level="LOW",
            )["evidences"][0]
            for index in cited_indexes
        ]
        server_normalized = normalized_requirement or requirement
        candidate["normalized_requirement"] = candidate_requirement or server_normalized
        return reconcile_assessment_output(
            context={
                "requirement_text": requirement,
                "normalized_requirement": server_normalized,
                "requirement_type": "FUNCTIONAL",
            },
            candidate=candidate,
            retrieved_evidences=canonical,
            fallback_reason="",
        )

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

    def test_build_assessment_context_exposes_explicit_hierarchy_and_completeness(self) -> None:
        row = {
            "id": "req-context-1",
            "req_code": "REQ-CONTEXT-1",
            "display_text": "控制器应在100ms内完成唤醒。",
            "parent_section_code": "5.1.2",
            "parent_section_title": "唤醒性能",
            "full_parent_clause": "5.1 控制器在所有规定工况下均应满足唤醒性能要求。",
            "ancestor_path": [
                {"section_code": "5", "section_title": "性能要求"},
                {"section_code": "5.1", "section_title": "启动"},
            ],
            "direct_parent": {"text": "控制器应支持唤醒。"},
            "direct_children": [{"text": "低温工况同样适用。"}],
            "adjacent_context": [{"text": "测试电压为12V。"}],
            "content_blocks": [
                {"kind": "paragraph", "text": "适用于冷启动验证。"}
            ],
            "table_refs": [
                {
                    "table_id": "table-1",
                    "table_title": "测试条件",
                    "slice_rows": [["电压", "12V"], ["温度", "-20°C"]],
                    "units": ["V", "°C"],
                }
            ],
        }

        context = build_assessment_context(row)
        source_context = context["source_context"]

        self.assertEqual(
            source_context["atomic_source_text"],
            "控制器应在100ms内完成唤醒。",
        )
        self.assertEqual(
            source_context["full_parent_clause"],
            "5.1 控制器在所有规定工况下均应满足唤醒性能要求。",
        )
        self.assertEqual(len(source_context["ancestor_path"]), 2)
        self.assertTrue(source_context["context_completeness"]["direct_parent"])
        self.assertTrue(source_context["context_completeness"]["table_context"])
        self.assertNotIn(
            "full_parent_clause_unavailable",
            source_context["context_warnings"],
        )

        prompt = json.loads(build_llm_messages(context, [])[1]["content"])
        self.assertEqual(
            prompt["source_context"]["direct_children"][0]["text"],
            "低温工况同样适用。",
        )

    def test_build_assessment_context_marks_unavailable_context_without_inventing_it(self) -> None:
        context = build_assessment_context(
            {
                "id": "req-context-missing",
                "display_text": "系统应支持安全诊断。",
            }
        )

        self.assertEqual(context["source_context"]["full_parent_clause"], "")
        self.assertIsNone(context["source_context"]["direct_parent"])
        self.assertIn(
            "full_parent_clause_unavailable",
            context["context_warnings"],
        )
        self.assertIn("ancestor_path_unavailable", context["context_warnings"])

    def test_preview_rows_supply_traceable_parent_child_and_adjacent_context(self) -> None:
        from app.application.requirements import (
            _attach_assessment_source_context,
        )

        rows = _attach_assessment_source_context(
            [
                {
                    "entry_id": "parent-1",
                    "req_code": "REQ-P",
                    "raw_text": "控制器应支持诊断。",
                    "parent_section_code": "5.1",
                    "parent_section_title": "诊断",
                    "source_page": 3,
                },
                {
                    "entry_id": "child-1",
                    "parent_id": "parent-1",
                    "req_code": "REQ-C1",
                    "raw_text": "控制器应记录故障码。",
                    "parent_section_code": "5.1",
                    "parent_section_title": "诊断",
                    "source_page": 3,
                },
                {
                    "entry_id": "child-2",
                    "parent_id": "parent-1",
                    "req_code": "REQ-C2",
                    "raw_text": "控制器应支持故障码清除。",
                    "parent_section_code": "5.1",
                    "parent_section_title": "诊断",
                    "source_page": 4,
                },
            ]
        )

        child_context = build_assessment_context(rows[1])["source_context"]
        self.assertEqual(
            child_context["direct_parent"]["entry_id"],
            "parent-1",
        )
        self.assertEqual(
            child_context["adjacent_context"][0]["entry_id"],
            "parent-1",
        )
        self.assertEqual(
            child_context["context_completeness_status"][
                "ancestor_path"
            ],
            "PARTIAL",
        )
        self.assertIn(
            "ancestor_path_partial",
            child_context["context_warnings"],
        )
        self.assertEqual(len(rows[0]["direct_children"]), 2)

    def test_llm_prompt_separates_compliance_from_business_disposition(self) -> None:
        context = build_assessment_context(
            {
                "id": "req-prompt-contract",
                "display_text": "系统应支持安全诊断。",
                "requirement_type": "FUNCTIONAL",
            }
        )
        evidence = _with_evidence_contract(
            {
                "source_doc_id": "kb-prompt",
                "source_doc_name": "validation.txt",
                "source_location": "Chunk 1",
                "excerpt": "安全诊断功能已通过验证。",
                "relevance": 0.81,
                "source_doc_version": "v2",
                "doc_type": "TEST_REPORT",
                "status": "ACTIVE",
                "effective_from": "2026-01-01",
                "effective_to": "",
                "content_kind": "paragraph",
                "section_code": "4.2",
                "section_title": "诊断验证",
                "authority": "UNKNOWN",
            }
        )

        messages = build_llm_messages(context, [evidence])
        system_prompt = messages[0]["content"]
        payload = json.loads(messages[1]["content"])

        self.assertIn("符合性事实", system_prompt)
        self.assertIn("检索相关度不能证明符合", system_prompt)
        self.assertNotIn("ETC12", system_prompt)
        self.assertEqual(
            payload["candidate_evidences"][0]["doc_type"],
            "TEST_REPORT",
        )
        self.assertEqual(
            payload["candidate_evidences"][0]["authority"],
            "UNKNOWN",
        )
        self.assertTrue(
            payload["decision_rules"]["must_analyze_each_independent_constraint"]
        )

    def test_ai_eval_readiness_fails_closed_without_independent_gold(self) -> None:
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.executescript(
            """
            CREATE TABLE requirements (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                sor_document_id TEXT NOT NULL
            );
            CREATE TABLE assessments (
                requirement_id TEXT NOT NULL,
                final_decision TEXT,
                engineer_reason TEXT
            );
            CREATE TABLE review_logs (
                requirement_id TEXT NOT NULL,
                action TEXT NOT NULL,
                operator TEXT NOT NULL
            );
            INSERT INTO requirements VALUES ('r1', 'p1', 'd1');
            INSERT INTO requirements VALUES ('r2', 'p2', 'd2');
            INSERT INTO assessments VALUES ('r1', 'ACCEPT', 'reviewed');
            INSERT INTO assessments VALUES ('r2', 'DEVIATION_ACCEPT', 'reviewed');
            INSERT INTO review_logs VALUES ('r1', 'EDIT_DECISION', 'engineer');
            INSERT INTO review_logs VALUES ('r2', 'EDIT_DECISION', 'engineer');
            """
        )

        report = build_readiness_report(connection)

        self.assertFalse(report["overall_ready"])
        self.assertEqual(report["decision_gold"]["human_labeled"], 2)
        self.assertEqual(report["decision_gold"]["dual_reviewed"], 0)
        self.assertIn(
            "no_independent_explicit_requirement_gold_field",
            report["explicit_requirement_gold"]["blockers"],
        )
        self.assertFalse(report["synthetic_regression_is_gold"])

    def test_ai_eval_readiness_reads_committed_wal_and_remains_read_only(self) -> None:
        database_path = self.tmp_path / "gold-readiness.db"
        writer = sqlite3.connect(database_path)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode = WAL")
        writer.execute("CREATE TABLE requirements (id TEXT PRIMARY KEY)")
        writer.execute("INSERT INTO requirements VALUES ('r1')")
        writer.commit()

        with _read_only_connection(database_path) as reader:
            self.assertEqual(reader.execute("SELECT COUNT(*) FROM requirements").fetchone()[0], 1)
            with self.assertRaises(sqlite3.OperationalError):
                reader.execute("INSERT INTO requirements VALUES ('r2')")

    def test_ai_eval_readiness_rejects_empty_gold_field_and_disputed_dual_review(self) -> None:
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.executescript(
            """
            CREATE TABLE requirements (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                sor_document_id TEXT NOT NULL,
                gold_analysis_label TEXT
            );
            CREATE TABLE assessments (
                requirement_id TEXT NOT NULL,
                final_decision TEXT,
                engineer_reason TEXT
            );
            CREATE TABLE review_logs (
                id TEXT PRIMARY KEY,
                requirement_id TEXT NOT NULL,
                action TEXT NOT NULL,
                operator TEXT NOT NULL,
                new_value TEXT,
                operated_at TEXT NOT NULL
            );
            INSERT INTO requirements VALUES ('r1', 'p1', 'd1', NULL);
            INSERT INTO requirements VALUES ('r2', 'p1', 'd1', NULL);
            INSERT INTO assessments VALUES ('r1', 'ACCEPT', 'reviewed');
            INSERT INTO assessments VALUES ('r2', 'APPROVE', 'invalid label');
            INSERT INTO review_logs VALUES
                ('l1', 'r1', 'EDIT_DECISION', 'reviewer-a', '{"final_decision":"ACCEPT"}', '2026-01-01'),
                ('l2', 'r1', 'EDIT_DECISION', 'reviewer-b', '{"final_decision":"REJECT"}', '2026-01-02'),
                ('l3', 'r1', 'EDIT_DECISION', 'reviewer-c', '[]', '2026-01-03');
            """
        )

        report = build_readiness_report(connection)

        self.assertFalse(report["explicit_requirement_gold"]["ready"])
        self.assertEqual(report["explicit_requirement_gold"]["labeled"], 0)
        self.assertIn(
            "no_nonempty_explicit_requirement_labels",
            report["explicit_requirement_gold"]["blockers"],
        )
        self.assertEqual(report["decision_gold"]["dual_reviewed"], 0)
        self.assertEqual(report["decision_gold"]["human_labeled"], 1)
        self.assertEqual(report["decision_gold"]["unknown_distribution"], {"APPROVE": 1})
        self.assertIn(
            "insufficient_dual_reviewed_decisions:0/1",
            report["decision_gold"]["blockers"],
        )
        self.assertIn(
            "unknown_decision_classes:APPROVE=1",
            report["decision_gold"]["blockers"],
        )

    def test_ai_gold_contract_accepts_only_frozen_independently_reviewed_minimum_set(self) -> None:
        def pseudonym(value: str) -> str:
            return hashlib.sha256(value.encode("utf-8")).hexdigest()

        decision_labels = ("ACCEPT", "REJECT", "DEVIATION_ACCEPT", "NEED_CLARIFICATION")
        cases = []
        for index in range(150):
            gold = {"analysis_label": "REQUIREMENT" if index < 100 else "INFO"}
            if index < 100:
                decision = decision_labels[index % len(decision_labels)]
                compliance_status = {
                    "ACCEPT": "COMPLIANT",
                    "REJECT": "NON_COMPLIANT",
                    "DEVIATION_ACCEPT": "PARTIALLY_COMPLIANT",
                    "NEED_CLARIFICATION": "INSUFFICIENT_INFORMATION",
                }[decision]
                gold.update(
                    {
                        "final_decision": decision,
                        "compliance_status": compliance_status,
                        "expected_evidence_ids": (
                            []
                            if compliance_status
                            == "INSUFFICIENT_INFORMATION"
                            else [f"evidence-{index}"]
                        ),
                        "required_missing_items": (
                            [
                                {
                                    "missing_item_id": pseudonym(
                                        f"missing-{index}"
                                    ),
                                    "item": "需要直接覆盖证据",
                                }
                            ]
                            if compliance_status
                            == "INSUFFICIENT_INFORMATION"
                            else []
                        ),
                        "expected_sub_judgments": [
                            {
                                "gold_sub_judgment_id": pseudonym(
                                    f"sub-{index}"
                                ),
                                "statement": "controlled external row",
                                "compliance_status": compliance_status,
                            }
                        ],
                    }
                )
                gold["assessment_contract_sha256"] = (
                    assessment_contract_sha256(gold)
                )
            review_gold = {
                key: gold[key]
                for key in (
                    "analysis_label",
                    "final_decision",
                    "compliance_status",
                    "assessment_contract_sha256",
                )
                if key in gold
            }
            cases.append(
                {
                    "case_id": pseudonym(f"case-{index}"),
                    "project_id": pseudonym(f"project-{index % 2}"),
                    "document_id": pseudonym(f"document-{index % 5}"),
                    "input": {
                        "requirement_row": {"display_text": "controlled external row"},
                        "knowledge_docs": [],
                    },
                    "gold": gold,
                    "reviews": [
                        {"reviewer_id": pseudonym("reviewer-a"), **review_gold},
                        {"reviewer_id": pseudonym("reviewer-b"), **review_gold},
                    ],
                    "adjudication": {"status": "NOT_REQUIRED"},
                }
            )
        payload = {
            "schema_version": "ai_gold_payload.v2",
            "dataset_id": "controlled-gold",
            "version": "v1",
            "cases": cases,
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        coverage = _coverage_from_cases(cases)
        manifest = {
            "schema_version": "ai_gold_manifest.v2",
            "dataset_id": "controlled-gold",
            "version": "v1",
            "status": "FROZEN",
            "payload": {"location_class": "CONTROLLED_EXTERNAL", "sha256": sha256_bytes(raw)},
            "label_policy": {
                "version": "v1",
                "approval_status": "APPROVED",
                "reviewers_per_case": 2,
                "adjudication_required": True,
            },
            "evaluation_policy": {
                "version": "v1",
                "approval_status": "APPROVED",
                "explicit_requirement": {"min_recall": 1.0, "min_info_specificity": 0.8},
                "decision_safety": {
                    "min_macro_recall": 0.7,
                    "min_per_class_recall": 0.5,
                    "max_unsafe_accepts": 0,
                },
                "assessment_contract": {
                    "min_compliance_macro_recall": 0.7,
                    "min_citation_precision": 0.8,
                    "min_citation_recall": 0.8,
                    "min_information_gap_completeness": 0.9,
                    "min_sub_judgment_exact_match": 0.75,
                },
            },
            "coverage": coverage,
            "split_strategy": "PROJECT_DOCUMENT_GROUP",
            "revisions": {
                "code_sha": "a" * 40,
                "prompt_versions": ["prompt-v1"],
                "model_versions": ["model-v1"],
                "config_sha256": "b" * 64,
            },
            "privacy": {
                "contains_customer_data": True,
                "hmac_pseudonymized_ids": True,
                "text_committed_to_repo": False,
            },
        }

        blockers, reported_coverage = validate_gold_contract(
            manifest=manifest,
            payload=payload,
            payload_sha256=sha256_bytes(raw),
            code_sha="a" * 40,
            config_sha256="b" * 64,
        )

        self.assertEqual(blockers, [])
        self.assertEqual(reported_coverage, coverage)
        self.assertEqual(coverage["explicit_requirement"]["label_counts"], {"REQUIREMENT": 100, "INFO": 50})
        self.assertEqual(coverage["decision"]["class_counts"], {label: 25 for label in decision_labels})

    def test_ai_gold_contract_rejects_proposed_empty_templates(self) -> None:
        payload = {
            "schema_version": "ai_gold_payload.v2",
            "dataset_id": "reqpilotx-ai-gold",
            "version": "v0-proposed",
            "cases": [],
        }
        raw = json.dumps(payload).encode("utf-8")
        manifest = {
            "schema_version": "ai_gold_manifest.v2",
            "dataset_id": "reqpilotx-ai-gold",
            "version": "v0-proposed",
            "status": "PROPOSED",
            "payload": {"location_class": "CONTROLLED_EXTERNAL", "sha256": sha256_bytes(raw)},
            "label_policy": {
                "approval_status": "PROPOSED",
                "reviewers_per_case": 2,
                "adjudication_required": True,
            },
            "evaluation_policy": {
                "approval_status": "PROPOSED",
                "explicit_requirement": {"min_recall": 1.0, "min_info_specificity": 0.8},
                "decision_safety": {
                    "min_macro_recall": 0.7,
                    "min_per_class_recall": 0.5,
                    "max_unsafe_accepts": 0,
                },
                "assessment_contract": {
                    "min_compliance_macro_recall": 0.7,
                    "min_citation_precision": 0.8,
                    "min_citation_recall": 0.8,
                    "min_information_gap_completeness": 0.9,
                    "min_sub_judgment_exact_match": 0.75,
                },
            },
            "coverage": _coverage_from_cases([]),
            "split_strategy": "PROJECT_DOCUMENT_GROUP",
            "revisions": {
                "code_sha": "",
                "prompt_versions": [],
                "model_versions": [],
                "config_sha256": "",
            },
            "privacy": {
                "contains_customer_data": True,
                "hmac_pseudonymized_ids": True,
                "text_committed_to_repo": False,
            },
        }

        blockers, _ = validate_gold_contract(
            manifest=manifest,
            payload=payload,
            payload_sha256=sha256_bytes(raw),
            code_sha="a" * 40,
            config_sha256="b" * 64,
        )

        self.assertIn("manifest_not_frozen", blockers)
        self.assertIn("label_policy_not_approved", blockers)
        self.assertIn("evaluation_policy_not_approved", blockers)
        self.assertIn("insufficient_explicit_positive_labels:0/100", blockers)
        self.assertIn("insufficient_decision_cases:0/100", blockers)

    def test_ai_gold_metrics_count_false_negative_and_unsafe_accept_without_raw_input(self) -> None:
        metrics = build_evaluation_metrics(
            [
                {
                    "gold_analysis_label": "REQUIREMENT",
                    "predicted_analysis_label": "REQUIREMENT",
                    "gold_final_decision": "ACCEPT",
                    "predicted_final_decision": "ACCEPT",
                    "gold_compliance_status": "COMPLIANT",
                    "predicted_compliance_status": "COMPLIANT",
                    "citation_tp": 1,
                    "citation_fp": 0,
                    "citation_fn": 0,
                    "gold_requires_information_gap": False,
                    "sub_judgment_evaluated": True,
                    "sub_judgment_exact_match": True,
                    "execution_status": "OK",
                },
                {
                    "gold_analysis_label": "REQUIREMENT",
                    "predicted_analysis_label": "INFO",
                    "gold_final_decision": "REJECT",
                    "predicted_final_decision": "ACCEPT",
                    "gold_compliance_status": "NON_COMPLIANT",
                    "predicted_compliance_status": "COMPLIANT",
                    "citation_tp": 0,
                    "citation_fp": 1,
                    "citation_fn": 1,
                    "gold_requires_information_gap": False,
                    "sub_judgment_evaluated": True,
                    "sub_judgment_exact_match": False,
                    "execution_status": "OK",
                },
                {
                    "gold_analysis_label": "INFO",
                    "predicted_analysis_label": "INFO",
                    "gold_final_decision": "",
                    "predicted_final_decision": "",
                    "execution_status": "OK",
                },
            ]
        )

        self.assertEqual(metrics["explicit_requirement"]["recall"], 0.5)
        self.assertEqual(metrics["explicit_requirement"]["info_specificity"], 1.0)
        self.assertEqual(metrics["decision_safety"]["unsafe_accepts"], 1)
        self.assertEqual(
            metrics["assessment_contract"]["compliance_macro_recall"],
            0.5,
        )
        self.assertEqual(
            metrics["assessment_contract"]["citation"]["precision"],
            0.5,
        )
        self.assertEqual(
            metrics["assessment_contract"]["sub_judgments"][
                "exact_match_rate"
            ],
            0.5,
        )
        self.assertNotIn("customer secret sentence", json.dumps(metrics))

    def test_ai_gold_evaluation_uses_screening_and_isolated_decision_paths(self) -> None:
        calls = []

        def fake_runner(row, knowledge_docs, *, config, forced_analysis_label, forced_analysis_basis):
            calls.append((row, knowledge_docs, forced_analysis_label, forced_analysis_basis))
            is_decision = bool(forced_analysis_label)
            return {
                "assessment": {
                    "analysis_label": "REQUIREMENT",
                    "ai_decision": "ACCEPT" if is_decision else "",
                    "compliance_status": "COMPLIANT" if is_decision else "",
                    "structured_information_gap": {},
                    "evidence_findings": (
                        [
                            {
                                "retrieval_evidence_id": "evidence-1",
                                "finding": "SUPPORTS",
                            }
                        ]
                        if is_decision
                        else []
                    ),
                    "sub_judgments": (
                        [
                            {
                                "statement": "decide",
                                "compliance_status": "COMPLIANT",
                            }
                        ]
                        if is_decision
                        else []
                    ),
                    "assessment_mode": "heuristic" if is_decision else "screening_only",
                    "model_name": "model-v1",
                    "prompt_version": "prompt-v1",
                },
                "evidences": (
                    [{"retrieval_evidence_id": "evidence-1"}]
                    if is_decision
                    else []
                ),
                "runs": [],
            }

        cases = [
            {
                "input": {"requirement_row": {"display_text": "screen"}, "knowledge_docs": [{"x": 1}]},
                "gold": {"analysis_label": "REQUIREMENT"},
            },
            {
                "input": {"requirement_row": {"display_text": "decide"}, "knowledge_docs": [{"x": 2}]},
                "gold": {
                    "final_decision": "ACCEPT",
                    "compliance_status": "COMPLIANT",
                    "expected_evidence_ids": ["evidence-1"],
                    "required_missing_items": [],
                    "expected_sub_judgments": [
                        {
                            "gold_sub_judgment_id": "a" * 64,
                            "statement": "decide",
                            "compliance_status": "COMPLIANT",
                        }
                    ],
                },
            },
        ]

        records, models, prompts = evaluate_cases(
            cases=cases,
            config=self.config,
            scope="all",
            assessment_runner=fake_runner,
        )

        self.assertEqual(len(records), 2)
        self.assertEqual(calls[0][1], [])
        self.assertIsNone(calls[0][2])
        self.assertEqual(calls[1][1], [{"x": 2}])
        self.assertEqual(calls[1][2], "REQUIREMENT")
        self.assertEqual(models, {"model-v1"})
        self.assertEqual(prompts, {"prompt-v1"})
        self.assertEqual(records[1]["citation_tp"], 1)
        self.assertTrue(records[1]["sub_judgment_exact_match"])

    def test_ai_gold_does_not_count_returned_candidates_or_unrelated_structure(self) -> None:
        def fake_runner(
            row,
            knowledge_docs,
            *,
            config,
            forced_analysis_label,
            forced_analysis_basis,
        ):
            return {
                "assessment": {
                    "analysis_label": "REQUIREMENT",
                    "ai_decision": "NEED_CLARIFICATION",
                    "compliance_status": "INSUFFICIENT_INFORMATION",
                    "structured_information_gap": {
                        "missing_items": [
                            {
                                "item": "完全无关的输入",
                                "blocking_reason": "缺失",
                                "required_input": "提供无关材料",
                            }
                        ],
                        "clarification_questions": ["请提供无关材料？"],
                    },
                    "evidence_findings": [],
                    "sub_judgments": [
                        {
                            "statement": "另一个约束",
                            "compliance_status": "INSUFFICIENT_INFORMATION",
                        }
                    ],
                    "assessment_mode": "heuristic",
                    "model_name": "model-v1",
                    "prompt_version": "prompt-v1",
                },
                "evidences": [
                    {"retrieval_evidence_id": "evidence-1"}
                ],
                "runs": [],
            }

        records, _, _ = evaluate_cases(
            cases=[
                {
                    "input": {
                        "requirement_row": {"display_text": "目标约束"},
                        "knowledge_docs": [{"id": "doc-1"}],
                    },
                    "gold": {
                        "final_decision": "NEED_CLARIFICATION",
                        "compliance_status": "INSUFFICIENT_INFORMATION",
                        "expected_evidence_ids": ["evidence-1"],
                        "required_missing_items": [
                            {
                                "missing_item_id": "b" * 64,
                                "item": "目标证据",
                            }
                        ],
                        "expected_sub_judgments": [
                            {
                                "gold_sub_judgment_id": "c" * 64,
                                "statement": "目标约束",
                                "compliance_status": "INSUFFICIENT_INFORMATION",
                            }
                        ],
                    },
                }
            ],
            config=self.config,
            scope="decision",
            assessment_runner=fake_runner,
        )

        self.assertEqual(records[0]["citation_tp"], 0)
        self.assertEqual(records[0]["citation_fn"], 1)
        self.assertFalse(records[0]["information_gap_complete"])
        self.assertFalse(records[0]["sub_judgment_exact_match"])

    def test_ai_gold_evaluation_rejects_nonserviceable_primary_fallback(self) -> None:
        def fake_runner(row, knowledge_docs, *, config, forced_analysis_label, forced_analysis_basis):
            return {
                "assessment": {
                    "analysis_label": "REQUIREMENT",
                    "ai_decision": "",
                    "assessment_mode": "screening_only",
                    "model_name": "rules-rag-prototype",
                    "prompt_version": "v1.0-screening-heuristic",
                },
                "runs": [
                    {
                        "mode": "screening_llm_primary",
                        "model_name": "provider-model",
                        "prompt_version": "provider-prompt",
                        "validation_status": "failed",
                        "fallback_reason": "provider unavailable",
                    },
                    {
                        "mode": "screening_heuristic_fallback",
                        "model_name": "rules-rag-prototype",
                        "prompt_version": "v1.0-screening-heuristic",
                        "validation_status": "accepted",
                        "fallback_reason": "llm_primary_screening_failed",
                    },
                ],
            }

        records, _, _ = evaluate_cases(
            cases=[
                {
                    "input": {"requirement_row": {"display_text": "shall"}, "knowledge_docs": []},
                    "gold": {"analysis_label": "REQUIREMENT"},
                }
            ],
            config=dataclasses.replace(self.config, assessment_mode="llm_primary"),
            scope="explicit",
            assessment_runner=fake_runner,
        )

        self.assertEqual(records[0]["execution_status"], "ERROR")
        self.assertEqual(records[0]["predicted_analysis_label"], "EXECUTION_ERROR")
        self.assertEqual(build_evaluation_metrics(records)["execution_errors"], 1)

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
                "doc_type": "REFERENCE",
                "status": "ACTIVE",
                "effective_from": "",
                "effective_to": "",
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
                "doc_type": "REFERENCE",
                "status": "ARCHIVED",
                "effective_from": "",
                "effective_to": "",
                "extracted_text": "ECU shall support wake-up time less than 100ms.",
            },
            {
                "id": "kb-active",
                "project_id": "project-1",
                "file_name": "active-spec.txt",
                "doc_version": "v1.2",
                "doc_type": "REFERENCE",
                "status": "ACTIVE",
                "effective_from": "",
                "effective_to": "",
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

    def test_knowledge_evidence_lifecycle_policy_fails_closed(self) -> None:
        as_of = date(2026, 7, 10)
        base = {
            "status": "ACTIVE",
            "doc_type": "REFERENCE",
            "effective_from": "2026-01-01",
            "effective_to": "2026-12-31",
        }
        cases = [
            ("active_reference", {}, True, "eligible"),
            ("active_case", {"doc_type": "CASE"}, True, "eligible"),
            ("missing_status", {"status": ""}, False, "status_not_active"),
            ("missing_doc_type", {"doc_type": ""}, False, "doc_type_not_allowed"),
            ("draft", {"status": "DRAFT"}, False, "status_not_active"),
            ("superseded", {"status": "SUPERSEDED"}, False, "status_not_active"),
            ("archived", {"status": "ARCHIVED"}, False, "status_not_active"),
            ("future", {"effective_from": "2026-07-11"}, False, "not_yet_effective"),
            ("expired", {"effective_to": "2026-07-09"}, False, "expired"),
            ("unknown_type", {"doc_type": "UNKNOWN"}, False, "doc_type_not_allowed"),
            ("invalid_date", {"effective_from": "not-a-date"}, False, "invalid_effective_from"),
            ("invalid_end_date", {"effective_to": "2026-99-99"}, False, "invalid_effective_to"),
            (
                "reversed_window",
                {"effective_from": "2026-08-01", "effective_to": "2026-07-01"},
                False,
                "invalid_effective_window",
            ),
        ]

        for name, overrides, expected_allowed, expected_reason in cases:
            with self.subTest(name=name):
                decision = evaluate_knowledge_evidence_eligibility(
                    {**base, **overrides},
                    as_of=as_of,
                )
                self.assertEqual(decision.allowed, expected_allowed)
                self.assertEqual(decision.reason, expected_reason)

    def test_local_retrieval_excludes_inactive_and_out_of_window_documents(self) -> None:
        common = {
            "project_id": "project-1",
            "file_name": "wake-up-spec.txt",
            "doc_version": "v1",
            "doc_type": "REFERENCE",
            "effective_from": "2000-01-01",
            "effective_to": "",
            "extracted_text": "ECU wake-up latency is 80ms and validation passed.",
        }
        knowledge_docs = [
            {**common, "id": "kb-active", "status": "ACTIVE"},
            {**common, "id": "kb-draft", "status": "DRAFT"},
            {**common, "id": "kb-superseded", "status": "SUPERSEDED"},
            {**common, "id": "kb-archived", "status": "ARCHIVED"},
            {**common, "id": "kb-future", "status": "ACTIVE", "effective_from": "2999-01-01"},
            {**common, "id": "kb-expired", "status": "ACTIVE", "effective_to": "2000-01-02"},
            {**common, "id": "kb-unknown", "status": "ACTIVE", "doc_type": "UNKNOWN"},
        ]

        evidences = HeuristicEvidenceRetriever().retrieve(
            {"retrieval_query": "ECU wake-up latency 80ms"},
            knowledge_docs,
            top_k=10,
        )

        self.assertEqual({item["source_doc_id"] for item in evidences}, {"kb-active"})

    def test_remote_filters_only_include_currently_eligible_document_ids(self) -> None:
        as_of = date(2026, 7, 10)
        common = {
            "project_id": "project-1",
            "file_name": "knowledge.txt",
            "doc_version": "v1",
            "doc_type": "REFERENCE",
            "effective_from": "2026-01-01",
            "effective_to": "2026-12-31",
        }
        knowledge_docs = [
            {**common, "id": "kb-active", "status": "ACTIVE"},
            {**common, "id": "kb-draft", "status": "DRAFT"},
            {**common, "id": "kb-future", "status": "ACTIVE", "effective_from": "2026-07-11"},
            {**common, "id": "kb-expired", "status": "ACTIVE", "effective_to": "2026-07-09"},
        ]

        azure_filter = build_azure_filter(
            project_id="project-1",
            knowledge_docs=knowledge_docs,
            as_of=as_of,
        )
        azure_odata = _azure_filter_to_odata(azure_filter) or ""
        dashvector_filter = _build_dashvector_filter(
            project_id="project-1",
            knowledge_docs=knowledge_docs,
            as_of=as_of,
        ) or ""
        qdrant_filter = _build_qdrant_filter(
            project_id="project-1",
            knowledge_docs=knowledge_docs,
            as_of=as_of,
        )
        qdrant_payload = json.dumps(qdrant_filter.model_dump(), ensure_ascii=False)

        for rendered_filter in (azure_odata, dashvector_filter, qdrant_payload):
            self.assertIn("kb-active", rendered_filter)
            self.assertIn("ACTIVE", rendered_filter)
            self.assertIn(EVIDENCE_INDEX_CONTRACT_VERSION, rendered_filter)
            self.assertNotIn("kb-draft", rendered_filter)
            self.assertNotIn("kb-future", rendered_filter)
            self.assertNotIn("kb-expired", rendered_filter)

        ineligible_only = [{**common, "id": "kb-draft", "status": "DRAFT"}]
        closed_azure = _azure_filter_to_odata(
            build_azure_filter(
                project_id="project-1",
                knowledge_docs=ineligible_only,
                as_of=as_of,
            )
        ) or ""
        closed_dashvector = _build_dashvector_filter(
            project_id="project-1",
            knowledge_docs=ineligible_only,
            as_of=as_of,
        ) or ""
        closed_qdrant = _build_qdrant_filter(
            project_id="project-1",
            knowledge_docs=ineligible_only,
            as_of=as_of,
        )
        closed_qdrant_payload = json.dumps(closed_qdrant.model_dump(), ensure_ascii=False)
        self.assertIn("__reqpilot_no_eligible_evidence__", closed_azure)
        self.assertIn("__reqpilot_no_eligible_evidence__", closed_dashvector)
        self.assertIn("__reqpilot_no_eligible_evidence__", closed_qdrant_payload)

    def test_dashvector_evidence_contract_uses_versioned_collection(self) -> None:
        base_name = "reqpilot-knowledge"
        versioned_name = f"{base_name}-{EVIDENCE_INDEX_CONTRACT_VERSION}"

        self.assertEqual(_dashvector_contract_collection_name(base_name), versioned_name)
        self.assertEqual(_dashvector_contract_collection_name(versioned_name), versioned_name)
        config = dataclasses.replace(
            self.config,
            retrieval_backend="dashvector",
            dashvector_collection_name=base_name,
        )
        self.assertEqual(
            _dashvector_contract_config(config).dashvector_collection_name,
            versioned_name,
        )
        self.assertEqual(
            DASHVECTOR_INDEX_MIGRATION_ACTION,
            "CREATE_VERSIONED_DASHVECTOR_COLLECTION_AND_RESYNC",
        )
        self.assertTrue(
            _is_evidence_contract_schema_error(
                RuntimeError("Unknown field evidence_contract_version in existing Azure index")
            )
        )
        self.assertFalse(_is_evidence_contract_schema_error(RuntimeError("network timeout")))
        with self.assertRaisesRegex(ValueError, "at most"):
            _dashvector_contract_collection_name("a" * 21)
        with self.assertRaisesRegex(ValueError, "must start with a letter"):
            _dashvector_contract_collection_name("1invalid")

    def test_retrieval_post_filter_rejects_stale_remote_lifecycle_metadata(self) -> None:
        as_of = date(2026, 7, 10)
        knowledge_doc = {
            "id": "kb-active",
            "project_id": "project-1",
            "file_name": "standard.txt",
            "doc_version": "v1",
            "doc_type": "STANDARD",
            "status": "ACTIVE",
            "effective_from": "2026-01-01",
            "effective_to": "2026-12-31",
            "authority": "INTERNAL_CONTROLLED",
            "claim_role": "VALIDATION_RESULT",
            "applicability": "PROJECT_SCOPE",
            "extracted_text": "Wake-up latency is 80ms and validation passed.",
        }
        canonical_chunk = build_knowledge_chunks("project-1", knowledge_doc)[0]
        canonical = _with_evidence_contract(
            {
                "source_doc_id": "kb-active",
                "source_doc_name": "standard.txt",
                "source_location": canonical_chunk["meta"]["source_location"],
                "source_doc_version": "v1",
                "excerpt": canonical_chunk["content"],
                "relevance": 0.72,
                "doc_type": "STANDARD",
                "status": "ACTIVE",
                "effective_from": "2026-01-01",
                "effective_to": "2026-12-31",
            },
            evidence_id=canonical_chunk["evidence_id"],
        )
        candidates = [
            canonical,
            {**canonical, "status": "ARCHIVED"},
            {**canonical, "effective_to": "2026-07-09"},
            {**canonical, "source_doc_id": "kb-unscoped"},
        ]

        filtered = _filter_retrieved_evidence_candidates(
            candidates,
            [knowledge_doc],
            as_of=as_of,
        )

        self.assertEqual(len(filtered), 1)
        for field in (
            "evidence_id",
            "content_hash",
            "source_doc_id",
            "source_doc_name",
            "source_location",
            "excerpt",
            "authority",
            "claim_role",
            "applicability",
        ):
            self.assertEqual(filtered[0][field], canonical[field])

        forged_provider_provenance = {
            **canonical,
            "source_doc_name": "forged.txt",
            "source_doc_version": "forged-version",
            "source_location": "Forged location",
        }
        canonicalized = _filter_retrieved_evidence_candidates(
            [forged_provider_provenance],
            [knowledge_doc],
            as_of=as_of,
        )
        self.assertEqual(len(canonicalized), 1)
        self.assertEqual(canonicalized[0]["source_doc_name"], "standard.txt")
        self.assertEqual(canonicalized[0]["source_doc_version"], "v1")
        self.assertEqual(canonicalized[0]["source_location"], canonical_chunk["meta"]["source_location"])
        self.assertNotIn("Forged", json.dumps(canonicalized, ensure_ascii=False))

        legacy_remote = {
            key: value
            for key, value in canonical.items()
            if key
            not in {
                "evidence_id",
                "content_hash",
                "evidence_contract_version",
                "evidence_contract_verified",
            }
        }
        context: dict[str, object] = {}
        self.assertEqual(
            _filter_retrieved_evidence_candidates(
                [legacy_remote],
                [knowledge_doc],
                as_of=as_of,
                context=context,
            ),
            [],
        )
        self.assertEqual(context["retrieval_gate_reason"], EVIDENCE_INDEX_RESYNC_REQUIRED_REASON)
        self.assertEqual(context["retrieval_migration_action"], EVIDENCE_INDEX_RESYNC_ACTION)

        hash_mismatch_context: dict[str, object] = {}
        self.assertEqual(
            _filter_retrieved_evidence_candidates(
                [
                    {
                        **canonical,
                        "content_hash": "0" * 64,
                        "evidence_contract_verified": False,
                    }
                ],
                [knowledge_doc],
                as_of=as_of,
                context=hash_mismatch_context,
            ),
            [],
        )
        self.assertEqual(
            hash_mismatch_context["retrieval_migration_action"],
            EVIDENCE_INDEX_RESYNC_ACTION,
        )

        stale_remote = _with_evidence_contract(
            {
                **canonical,
                "excerpt": "Wake-up latency is 180ms and validation failed.",
            },
            evidence_id=canonical_chunk["evidence_id"],
            content="Wake-up latency is 180ms and validation failed.",
        )
        removed_remote = _with_evidence_contract(
            {
                **canonical,
                "excerpt": "Removed historical chunk.",
            },
            evidence_id="kb-active_paragraph_99",
            content="Removed historical chunk.",
        )
        stale_context: dict[str, object] = {}
        self.assertEqual(
            _filter_retrieved_evidence_candidates(
                [stale_remote, removed_remote],
                [knowledge_doc],
                as_of=as_of,
                context=stale_context,
            ),
            [],
        )
        self.assertEqual(stale_context["retrieval_migration_action"], EVIDENCE_INDEX_RESYNC_ACTION)

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
                "doc_type": "REFERENCE",
                "status": "ACTIVE",
                "effective_from": "",
                "effective_to": "",
                "extracted_text": "Valve pressure drop is less than 10kPa and passed validation.",
            },
            {
                "id": "kb-neg-1",
                "project_id": "project-1",
                "file_name": "negative.txt",
                "doc_version": "v1.1",
                "doc_type": "REFERENCE",
                "status": "ACTIVE",
                "effective_from": "",
                "effective_to": "",
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
        retrieved = [_with_evidence_contract(item) for item in retrieved]

        result = reconcile_assessment_output(
            context=context,
            candidate=candidate,
            retrieved_evidences=retrieved,
            fallback_reason="",
        )

        self.assertEqual(result["ai_decision"], "NEED_CLARIFICATION")
        self.assertIn("numeric_constraint_unsupported", result["fallback_reason"])

    def test_numeric_guard_rejects_strict_upper_bound_at_boundary(self) -> None:
        result = self._reconcile_numeric_case(
            requirement="Wake-up time shall be <100ms.",
            evidence_excerpt="Wake-up time is 100ms and passed validation.",
        )

        self.assertEqual(result["ai_decision"], "NEED_CLARIFICATION")
        self.assertIn("numeric_evidence_contradiction", result["fallback_reason"])

    def test_numeric_guard_rejects_value_above_inclusive_upper_bound(self) -> None:
        result = self._reconcile_numeric_case(
            requirement="Wake-up time shall be ≤100ms.",
            evidence_excerpt="Wake-up time is 120ms and passed validation.",
        )

        self.assertEqual(result["ai_decision"], "NEED_CLARIFICATION")
        self.assertIn("numeric_evidence_contradiction", result["fallback_reason"])

    def test_numeric_guard_rejects_value_below_inclusive_lower_bound(self) -> None:
        result = self._reconcile_numeric_case(
            requirement="Operating voltage shall be ≥12V.",
            evidence_excerpt="Operating voltage is 10.8V and passed validation.",
        )

        self.assertEqual(result["ai_decision"], "NEED_CLARIFICATION")
        self.assertIn("numeric_evidence_contradiction", result["fallback_reason"])

    def test_numeric_guard_normalizes_seconds_before_comparison(self) -> None:
        result = self._reconcile_numeric_case(
            requirement="Wake-up time shall be ≤100ms.",
            evidence_excerpt="Wake-up time is 0.12s and passed validation.",
        )

        self.assertEqual(result["ai_decision"], "NEED_CLARIFICATION")
        self.assertIn("numeric_evidence_contradiction", result["fallback_reason"])

    def test_numeric_guard_fails_closed_for_nonmatching_voltage_ranges(self) -> None:
        result = self._reconcile_numeric_case(
            requirement="Operating voltage range shall be 9–16V.",
            evidence_excerpt="Operating voltage range is 8–18V and passed validation.",
        )

        self.assertEqual(result["ai_decision"], "NEED_CLARIFICATION")
        self.assertIn("numeric_evidence_unsafe_correspondence", result["fallback_reason"])

    def test_numeric_guard_rejects_model_mutation_of_normalized_requirement(self) -> None:
        result = self._reconcile_numeric_case(
            requirement="Wake-up time shall be ≤100 ms.",
            candidate_requirement="Wake-up time shall be ≥100 s.",
            evidence_excerpt="Wake-up time is 80ms and passed validation.",
        )

        self.assertEqual(result["ai_decision"], "NEED_CLARIFICATION")
        self.assertEqual(result["normalized_requirement"], "Wake-up time shall be ≤100 ms.")
        self.assertIn("numeric_requirement_mismatch", result["fallback_reason"])

    def test_numeric_guard_accepts_valid_strict_and_inclusive_boundaries(self) -> None:
        cases = (
            ("Wake-up time shall be <100ms.", "Wake-up time is 99ms and passed validation."),
            ("Wake-up time shall be ≤100ms.", "Wake-up time is 100ms and passed validation."),
            ("Wake-up time shall be ≤100ms.", "Wake-up time is 0.1s and passed validation."),
            ("Operating voltage shall be ≥12V.", "Operating voltage is 12V and passed validation."),
            ("Operating voltage range shall be 9–16V.", "Operating voltage range is 9–16V and passed validation."),
        )

        for requirement, evidence_excerpt in cases:
            with self.subTest(requirement=requirement, evidence=evidence_excerpt):
                result = self._reconcile_numeric_case(
                    requirement=requirement,
                    evidence_excerpt=evidence_excerpt,
                )
                self.assertEqual(result["ai_decision"], "ACCEPT")
                self.assertEqual(result["validation_status"], "accepted")

    def test_numeric_guard_does_not_match_a_different_parameter(self) -> None:
        result = self._reconcile_numeric_case(
            requirement="Wake-up time shall be ≤100ms.",
            evidence_excerpt="Shutdown time is 80ms and passed validation.",
        )

        self.assertEqual(result["ai_decision"], "NEED_CLARIFICATION")
        self.assertIn("numeric_evidence_unsafe_correspondence", result["fallback_reason"])

    def test_numeric_guard_rejects_negated_evidence_and_candidate_constraints(self) -> None:
        evidence_result = self._reconcile_numeric_case(
            requirement="Wake-up time shall be ≤100ms.",
            evidence_excerpt="Wake-up time is not 80ms.",
        )
        candidate_result = self._reconcile_numeric_case(
            requirement="Wake-up time shall be ≤100ms.",
            candidate_requirement="Wake-up time shall not be ≤100ms.",
            evidence_excerpt="Wake-up time is 80ms and passed validation.",
        )

        self.assertEqual(evidence_result["ai_decision"], "NEED_CLARIFICATION")
        self.assertIn("numeric_evidence_unsafe_correspondence", evidence_result["fallback_reason"])
        self.assertEqual(candidate_result["ai_decision"], "NEED_CLARIFICATION")
        self.assertIn("numeric_requirement_mismatch", candidate_result["fallback_reason"])

    def test_numeric_guard_fails_closed_for_unsupported_contract_shapes(self) -> None:
        cases = (
            ("Wake-up time shall be ≤2min.", "Wake-up time shall be ≤200min.", "Wake-up time is 80ms."),
            ("Wake-up time is 2min.", "Wake-up time is 200min.", "Wake-up time is 150min."),
            ("Operating voltage shall be -12..12V.", "Operating voltage shall be 12V.", "Operating voltage is 12V."),
            ("Operating voltage is -12V.", "Operating voltage is 12V.", "Operating voltage is 12V."),
            ("Operating voltage is −12V.", "Operating voltage is +12V.", "Operating voltage is +12V."),
            ("Operating voltage shall be 12VDC.", "Operating voltage shall be 12VDC.", "Operating voltage requirement passed validation."),
            ("Operating voltage shall be 12 V DC.", "Operating voltage shall be 12 V DC.", "Operating voltage requirement passed validation."),
            ("Operating voltage shall be 12±0.5V.", "Operating voltage shall be 24±0.5V.", "Operating voltage is 12V."),
            ("Operating voltage is 12±.5V.", "Operating voltage is 24±.5V.", "Operating voltage is 5V."),
            ("Operating voltage is 12±,5V.", "Operating voltage is 24±,5V.", "Operating voltage is 5V."),
            ("Operating voltage is 12 (±.5) V.", "Operating voltage is 12 (±.5) V.", "Operating voltage requirement passed validation."),
            ("Operating voltage is 12（±0.5）V.", "Operating voltage is 12（±0.5）V.", "Operating voltage requirement passed validation."),
            ("Operating voltage is 12[±0.5]V.", "Operating voltage is 12[±0.5]V.", "Operating voltage requirement passed validation."),
            ("Operating voltage is 12{±0.5}V.", "Operating voltage is 12{±0.5}V.", "Operating voltage requirement passed validation."),
            ("Operating voltage is 12V±0.5.", "Operating voltage is 12V±0.5.", "Operating voltage is 12V."),
            ("Operating voltage is 12 V (+0.5/-0.2).", "Operating voltage is 12 V (+0.5/-0.2).", "Operating voltage is 12V."),
            ("Operating voltage is 12V +0.5/-0.2.", "Operating voltage is 12V +0.5/-0.2.", "Operating voltage is 12V."),
            ("Operating voltage is 12 ±0.5 [V].", "Operating voltage is 12 ±0.5 [V].", "Operating voltage is 12V."),
            ("Wake-up time is 1e-3s.", "Wake-up time is 3s.", "Wake-up time is 1ms."),
            ("Wake-up time is 1×10^3ms.", "Wake-up time is 2×10^3ms.", "Wake-up time is 2×10^3ms."),
            ("Wake-up time is 1×10³ms.", "Wake-up time is 1×10³ms.", "Wake-up time requirement passed validation."),
            ("Wake-up time is 1 000ms.", "Wake-up time is 2 000ms.", "Wake-up time is 2 000ms."),
            ("Wake-up time is 1'000ms.", "Wake-up time is 1'000ms.", "Wake-up time requirement passed validation."),
            ("Operating voltage range is 9…16V.", "Operating voltage range is 9…16V.", "Operating voltage is 16V."),
            ("Operating voltage range is 9..16V.", "Operating voltage range is 9..16V.", "Operating voltage is 0.16V."),
            ("Operating voltage range is 9...16V.", "Operating voltage range is 9...16V.", "Operating voltage is 0.16V."),
            ("Operating voltage range is [9, 16] V.", "Operating voltage range is [9, 16] V.", "Operating voltage requirement passed validation."),
            ("Operating voltage is 12 [V].", "Operating voltage is 12 [V].", "Operating voltage requirement passed validation."),
            ("Operating V=12.", "Operating V=12.", "Operating voltage requirement passed validation."),
            ("Operating Voltage [V]: 12.", "Operating Voltage [V]: 12.", "Operating voltage requirement passed validation."),
            ("Operating voltage is 12/24V.", "Operating voltage is 24V.", "Operating voltage is 24V."),
            ("Output torque shall be 10Nm.", "Output torque shall be 10Nm.", "Output torque requirement passed validation."),
            ("Output torque shall be 10 Nm.", "Output torque shall be 10 Nm.", "Output torque requirement passed validation."),
            ("Cable length shall be 5 mm.", "Cable length shall be 5 mm.", "Cable length requirement passed validation."),
            ("Cable length shall be 5 millimetres.", "Cable length shall be 5 millimetres.", "Cable length requirement passed validation."),
            ("Motor speed shall be 3000 rpm.", "Motor speed shall be 3000 rpm.", "Motor speed requirement passed validation."),
            ("Pressure shall be 10 kilopascals.", "Pressure shall be 10 kilopascals.", "Pressure requirement passed validation."),
            ("Resistance shall be 10 Ω.", "Resistance shall be 10 Ω.", "Resistance requirement passed validation."),
            ("电缆长度应为10毫米。", "电缆长度应为10毫米。", "电缆长度通过验证。"),
            ("Wake-up time shall be 10^3ms.", "Wake-up time shall be 10^3ms.", "Wake-up time is 3ms."),
            ("Wake-up time shall be 10**3ms.", "Wake-up time shall be 10**3ms.", "Wake-up time is 3ms."),
            ("Wake-up time shall be ≤½s.", "Wake-up time shall be ≤½s.", "Shutdown time is 0.4s."),
            ("Wake-up time shall be ≤①s.", "Wake-up time shall be ≤①s.", "Shutdown time is 0.4s."),
            ("Wake-up time shall be at most one second.", "Wake-up time shall be at most one second.", "Shutdown time passed."),
            ("Wake-up time shall be one hundred milliseconds.", "Wake-up time shall be one hundred milliseconds.", "Shutdown time passed."),
            ("Resistance shall be ten Ω.", "Resistance shall be ten Ω.", "Pressure test passed validation."),
            ("Operating voltage shall be a dozen volts.", "Operating voltage shall be a dozen volts.", "Pressure test passed validation."),
            ("Operating voltage shall be half a volt.", "Operating voltage shall be half a volt.", "Pressure test passed validation."),
            ("唤醒时间不超过一百毫秒。", "唤醒时间不超过一百毫秒。", "关机时间通过验证。"),
            ("工作电压应为半伏。", "工作电压应为半伏。", "压力测试通过。"),
            ("工作电压应为拾贰伏。", "工作电压应为拾贰伏。", "压力测试通过。"),
            ("1.5 millimetres tolerance.", "1.5 millimetres tolerance.", "Requirement passed."),
            ("1.5 Ω resistance.", "1.5 Ω resistance.", "Requirement passed."),
            ("Requirement 5 millimetres.", "Requirement 5 millimetres.", "Requirement passed."),
            ("Operating voltage shall be 12 V ⎓.", "Operating voltage shall be 12 V ⎓.", "Operating voltage is 12V."),
            ("Rail 5-Ω resistance shall be supported.", "Rail 5-Ω resistance shall be supported.", "Pressure test passed."),
            ("Rail 5 (Ω) resistance shall be supported.", "Rail 5 (Ω) resistance shall be supported.", "Pressure test passed."),
            ("Operating voltage shall be ≥0V.", "Operating voltage shall be ≥0V.", "Operating voltage is ➖12V."),
            ("Operating voltage shall be ≥0V.", "Operating voltage shall be ≥0V.", "Operating voltage is ➖️12V."),
            ("Operating voltage shall be ≥0V.", "Operating voltage shall be ≥0V.", "Operating voltage is ˗12V."),
            ("Operating voltage shall be 12 V (⎓).", "Operating voltage shall be 12 V (⎓).", "Operating voltage is 12 V (∿)."),
            ("Operating voltage shall be 12 V [=].", "Operating voltage shall be 12 V [=].", "Operating voltage is 12 V [~]."),
            ("Operating voltage shall be 12 V_RMS.", "Operating voltage shall be 12 V_RMS.", "Operating voltage is 12V."),
            ("Operating voltage shall be １２Ｖ.", "Operating voltage shall be １２Ｖ.", "Operating voltage requirement passed validation."),
        )

        for requirement, candidate_requirement, evidence_excerpt in cases:
            with self.subTest(requirement=requirement):
                result = self._reconcile_numeric_case(
                    requirement=requirement,
                    candidate_requirement=candidate_requirement,
                    evidence_excerpt=evidence_excerpt,
                )
                self.assertEqual(result["ai_decision"], "NEED_CLARIFICATION")
                self.assertIn("numeric_constraint_unsupported", result["fallback_reason"])

    def test_numeric_guard_unions_raw_and_normalized_server_constraints(self) -> None:
        result = self._reconcile_numeric_evidence_set(
            requirement="Wake-up time shall be ≤100ms.",
            normalized_requirement=(
                "Wake-up time shall be ≤100ms and input voltage range shall be 9–16V."
            ),
            candidate_requirement="Wake-up time shall be ≤100ms.",
            evidence_excerpts=["Wake-up time is 80ms and passed validation."],
            cited_indexes=(0,),
        )

        self.assertEqual(result["ai_decision"], "NEED_CLARIFICATION")
        self.assertIn("numeric_requirement_mismatch", result["fallback_reason"])

    def test_numeric_guard_checks_retrieved_contradiction_omitted_by_model(self) -> None:
        result = self._reconcile_numeric_evidence_set(
            requirement="Wake-up time shall be ≤100ms.",
            evidence_excerpts=[
                "Wake-up time is 80ms and passed validation.",
                "Wake-up time is 120ms in the latest canonical run.",
            ],
            cited_indexes=(0,),
        )

        self.assertEqual(result["ai_decision"], "NEED_CLARIFICATION")
        self.assertIn("numeric_evidence_contradiction", result["fallback_reason"])

    def test_numeric_guard_requires_cited_evidence_to_support_serving_decision(self) -> None:
        accepted_hitchhike = self._reconcile_numeric_evidence_set(
            requirement="Wake-up time shall be ≤100ms.",
            evidence_excerpts=[
                "Shutdown time is 80ms and passed validation.",
                "Wake-up time is 80ms and passed validation.",
            ],
            cited_indexes=(0,),
        )
        rejected_hitchhike = self._reconcile_numeric_evidence_set(
            requirement="Wake-up time shall be ≤100ms.",
            evidence_excerpts=[
                "Shutdown time is 250ms and failed validation.",
                "Wake-up time is 120ms and failed validation.",
            ],
            cited_indexes=(0,),
            decision="REJECT",
        )
        hard_override_hitchhike = self._reconcile_numeric_evidence_set(
            requirement="Wake-up time shall be ≤100ms.",
            evidence_excerpts=[
                "Shutdown time is 250ms and failed validation.",
                "Wake-up time is 120ms and failed validation.",
            ],
            cited_indexes=(0,),
            decision="ACCEPT",
        )

        for result in (accepted_hitchhike, rejected_hitchhike, hard_override_hitchhike):
            self.assertEqual(result["ai_decision"], "NEED_CLARIFICATION")
            self.assertIn("numeric_evidence_unsafe_correspondence", result["fallback_reason"])

    def test_numeric_guard_supports_multiple_same_dimension_parameters(self) -> None:
        result = self._reconcile_numeric_evidence_set(
            requirement=(
                "Wake-up time shall be ≤100ms and shutdown time shall be ≤200ms."
            ),
            evidence_excerpts=[
                "Wake-up time is 80ms and passed validation.",
                "Shutdown time is 180ms and passed validation.",
            ],
            cited_indexes=(0, 1),
        )

        self.assertEqual(result["ai_decision"], "ACCEPT")
        self.assertEqual(result["validation_status"], "accepted")

    def test_numeric_guard_does_not_treat_an_empty_anchor_as_a_wildcard(self) -> None:
        result = self._reconcile_numeric_case(
            requirement="Operating voltage shall be ≥12V.",
            evidence_excerpt="Charging voltage is 13V and passed validation.",
        )

        self.assertEqual(result["ai_decision"], "NEED_CLARIFICATION")
        self.assertIn("numeric_evidence_unsafe_correspondence", result["fallback_reason"])

    def test_numeric_guard_keeps_shared_parameter_qualifiers_separate(self) -> None:
        requirement = (
            "Front wake-up time shall be ≤100ms and rear wake-up time shall be ≤200ms."
        )
        missing_rear = self._reconcile_numeric_evidence_set(
            requirement=requirement,
            evidence_excerpts=["Front wake-up time is 80ms and passed validation."],
            cited_indexes=(0,),
        )
        both_present = self._reconcile_numeric_evidence_set(
            requirement=requirement,
            evidence_excerpts=[
                "Front wake-up time is 80ms and passed validation.",
                "Rear wake-up time is 180ms and passed validation.",
            ],
            cited_indexes=(0, 1),
        )

        self.assertEqual(missing_rear["ai_decision"], "NEED_CLARIFICATION")
        self.assertIn("numeric_evidence_unsafe_correspondence", missing_rear["fallback_reason"])
        self.assertEqual(both_present["ai_decision"], "ACCEPT")
        self.assertEqual(both_present["validation_status"], "accepted")

    def test_numeric_guard_rejects_narrower_scope_evidence_for_generic_requirement(self) -> None:
        cases = (
            ("Wake-up time shall be ≤100ms.", "Front wake-up time is 80ms and passed validation."),
            ("Operating voltage shall be ≥12V.", "Cold-start operating voltage is 13V and passed validation."),
            ("Input voltage shall be ≥12V.", "Left-channel input voltage is 13V and passed validation."),
            ("Wake-up time shall be ≤100ms.", "ECU-A wake-up time is 80ms and passed validation."),
            ("Operating voltage maximum shall be ≤12V.", "Operating voltage minimum is 10V and passed validation."),
            ("Operating voltage max. shall be ≤12V.", "Charging voltage min. is 10V and passed validation."),
            ("Operating voltage max. SHALL be ≤12V.", "Charging voltage min. IS 10V and passed validation."),
            ("Wake-up time shall be ≤100ms.", "Ch. A wake-up time is 80ms and passed validation."),
        )

        for requirement, evidence_excerpt in cases:
            with self.subTest(requirement=requirement):
                result = self._reconcile_numeric_case(
                    requirement=requirement,
                    evidence_excerpt=evidence_excerpt,
                )
                self.assertEqual(result["ai_decision"], "NEED_CLARIFICATION")
                self.assertIn("numeric_evidence_unsafe_correspondence", result["fallback_reason"])

    def test_numeric_guard_keeps_numbered_and_chinese_qualifiers_separate(self) -> None:
        cases = (
            (
                "Channel 1 wake-up time shall be ≤100ms and Channel 2 wake-up time shall be ≤200ms.",
                [
                    "Channel 1 wake-up time is 80ms and passed validation.",
                    "Channel 2 wake-up time is 180ms and passed validation.",
                ],
            ),
            (
                "前部唤醒时间应≤100ms，后部唤醒时间应≤200ms。",
                ["前部唤醒时间为80ms，通过验证。", "后部唤醒时间为180ms，通过验证。"],
            ),
            (
                "ECU1 wake-up time shall be ≤100ms and ECU2 wake-up time shall be ≤200ms.",
                ["ECU1 wake-up time is 80ms.", "ECU2 wake-up time is 180ms."],
            ),
            (
                "CAN1 wake-up time shall be ≤100ms and CAN2 wake-up time shall be ≤200ms.",
                ["CAN1 wake-up time is 80ms.", "CAN2 wake-up time is 180ms."],
            ),
            (
                "Channel[1] wake-up shall be ≤100ms and Channel[2] wake-up shall be ≤200ms.",
                ["Channel[1] wake-up is 80ms.", "Channel[2] wake-up is 180ms."],
            ),
            (
                "Ch.1 wake-up shall be ≤100ms and Ch.2 wake-up shall be ≤200ms.",
                ["Ch.1 wake-up is 80ms.", "Ch.2 wake-up is 180ms."],
            ),
            (
                "前轴唤醒应≤100ms，后轴唤醒应≤200ms。",
                ["前轴唤醒为80ms。", "后轴唤醒为180ms。"],
            ),
            (
                "Channel Ⅰ wake-up shall be ≤100ms and Channel Ⅱ wake-up shall be ≤200ms.",
                ["Channel Ⅰ wake-up is 80ms.", "Channel Ⅱ wake-up is 180ms."],
            ),
            (
                "第1通道唤醒应≤100ms，第2通道唤醒应≤200ms。",
                ["第1通道唤醒为80ms。", "第2通道唤醒为180ms。"],
            ),
            (
                "通道(一)唤醒应≤100ms，通道(二)唤醒应≤200ms。",
                ["通道(一)唤醒为80ms。", "通道(二)唤醒为180ms。"],
            ),
            (
                "前唤醒应≤100ms，后唤醒应≤200ms。",
                ["前唤醒为80ms。", "后唤醒为180ms。"],
            ),
            (
                "Bus1 wake-up shall be ≤100ms and Bus2 wake-up shall be ≤200ms.",
                ["Bus1 wake-up is 80ms.", "Bus2 wake-up is 180ms."],
            ),
        )

        for requirement, evidence_excerpts in cases:
            with self.subTest(requirement=requirement):
                missing_second = self._reconcile_numeric_evidence_set(
                    requirement=requirement,
                    evidence_excerpts=evidence_excerpts[:1],
                    cited_indexes=(0,),
                )
                both_present = self._reconcile_numeric_evidence_set(
                    requirement=requirement,
                    evidence_excerpts=evidence_excerpts,
                    cited_indexes=(0, 1),
                )
                self.assertEqual(missing_second["ai_decision"], "NEED_CLARIFICATION")
                self.assertIn("numeric_evidence_unsafe_correspondence", missing_second["fallback_reason"])
                self.assertEqual(both_present["ai_decision"], "ACCEPT")

    def test_numeric_guard_includes_postfix_scope_in_parameter_anchor(self) -> None:
        cases = (
            (
                "Wake-up time shall be ≤100ms for Channel 1.",
                "Wake-up time is 80ms for Channel 2.",
            ),
            (
                "Wake-up time shall be ≤100ms in cold mode.",
                "Wake-up time is 80ms in warm mode.",
            ),
            ("唤醒时间应≤100ms（前部）。", "唤醒时间为80ms（后部）。"),
        )

        for requirement, evidence_excerpt in cases:
            with self.subTest(requirement=requirement):
                result = self._reconcile_numeric_case(
                    requirement=requirement,
                    evidence_excerpt=evidence_excerpt,
                )
                self.assertEqual(result["ai_decision"], "NEED_CLARIFICATION")
                self.assertIn("numeric_evidence_unsafe_correspondence", result["fallback_reason"])

    def test_numeric_guard_normalizes_equivalent_identifiers_and_result_modifiers(self) -> None:
        cases = (
            ("Channel 01 wake-up shall be ≤100ms.", "Channel 1 wake-up is 80ms."),
            ("通道一唤醒时间应≤100ms。", "通道1唤醒时间为80ms。"),
            ("Channel Ⅰ wake-up shall be ≤100ms.", "Channel 1 wake-up is 80ms."),
            ("ECU Ⅱ wake-up shall be ≤200ms.", "ECU2 wake-up is 180ms."),
            (
                "Wake-up time shall be ≤100ms.",
                "Actual recorded wake-up time is 80ms in the latest canonical run.",
            ),
        )

        for requirement, evidence_excerpt in cases:
            with self.subTest(requirement=requirement):
                result = self._reconcile_numeric_case(
                    requirement=requirement,
                    evidence_excerpt=evidence_excerpt,
                )
                self.assertEqual(result["ai_decision"], "ACCEPT")
                self.assertEqual(result["validation_status"], "accepted")

    def test_numeric_guard_rejects_partially_consumed_evidence(self) -> None:
        cases = (
            ("Operating voltage shall be ≥0V.", "Operating voltage is -12V."),
            ("Operating voltage shall be ≥0V.", "Operating voltage is －12V."),
            ("Wake-up time shall be ≤100ms.", "Wake-up time is 1\u202f080ms."),
            ("Wake-up time shall be ≤100ms.", "Wake-up time is 1'080ms."),
            ("Operating voltage shall be 12V.", "Operating voltage is 12 V.A.C."),
            ("Operating voltage shall be 12V.", "Operating voltage is 12 V-AC."),
            ("Operating voltage shall be ≥0V.", "Operating voltage is ‐12V."),
            ("Operating voltage shall be ≥0V.", "Operating voltage is ‑12V."),
            ("Operating voltage shall be ≥0V.", "Operating voltage is ‒12V."),
            ("Operating voltage shall be ≥0V.", "Operating voltage is –12V."),
            ("Operating voltage shall be ≥0V.", "Operating voltage is ⁻12V."),
            ("Wake-up time shall be ≤100ms.", "Wake-up time is 1\u200a080ms."),
            ("Wake-up time shall be ≤100ms.", "Wake-up time is ١٬٠٨٠ms."),
            ("Operating voltage shall be 12V.", "Operating voltage is 12 V ⎓."),
            ("Operating voltage shall be 12V.", "Operating voltage is 12 V_RMS."),
        )

        for requirement, evidence_excerpt in cases:
            with self.subTest(evidence=evidence_excerpt):
                result = self._reconcile_numeric_case(
                    requirement=requirement,
                    evidence_excerpt=evidence_excerpt,
                )
                self.assertEqual(result["ai_decision"], "NEED_CLARIFICATION")
                self.assertIn("numeric_constraint_unsupported", result["fallback_reason"])

    def test_numeric_guard_applies_correspondence_to_reject_and_hard_override(self) -> None:
        irrelevant_cases = (
            (
                "Operating voltage shall be ≥12V.",
                "Charging voltage is 5V and failed validation.",
            ),
            (
                "Wake-up time shall be ≤100ms.",
                "Shutdown time is 250ms and failed validation.",
            ),
        )

        for requirement, evidence_excerpt in irrelevant_cases:
            with self.subTest(requirement=requirement, decision="REJECT"):
                direct_reject = self._reconcile_numeric_case(
                    requirement=requirement,
                    evidence_excerpt=evidence_excerpt,
                    decision="REJECT",
                )
                hard_override = self._reconcile_numeric_case(
                    requirement=requirement,
                    evidence_excerpt=evidence_excerpt,
                    decision="ACCEPT",
                )
                self.assertEqual(direct_reject["ai_decision"], "NEED_CLARIFICATION")
                self.assertEqual(hard_override["ai_decision"], "NEED_CLARIFICATION")
                self.assertIn("numeric_evidence_unsafe_correspondence", direct_reject["fallback_reason"])
                self.assertIn("numeric_evidence_unsafe_correspondence", hard_override["fallback_reason"])

        relevant_reject = self._reconcile_numeric_case(
            requirement="Operating voltage shall be ≥12V.",
            evidence_excerpt="Operating voltage is 5V and failed validation.",
            decision="REJECT",
        )
        self.assertEqual(relevant_reject["ai_decision"], "REJECT")
        self.assertEqual(relevant_reject["validation_status"], "accepted")

    def test_numeric_guard_separates_english_sentence_anchors(self) -> None:
        cases = (
            (
                "Wake-up time shall be ≤100ms. Shutdown time shall be ≤200ms.",
                "Wake-up time is 80ms. Shutdown validation passed.",
                ["Wake-up time is 80ms.", "Shutdown time is 180ms."],
            ),
            (
                "Wake-up time shall be ≤1 sec. Shutdown time shall be ≤2 sec.",
                "Wake-up time is 0.8 sec. Shutdown validation passed.",
                ["Wake-up time is 0.8 sec.", "Shutdown time is 1.8 sec."],
            ),
            (
                "Wake-up time shall be ≤100ms max. Shutdown time shall be ≤200ms.",
                "Wake-up time is 80ms max. Shutdown validation passed.",
                ["Wake-up time is 80ms max.", "Shutdown time is 180ms."],
            ),
            (
                "Wake-up time shall be ≤0.1 sec. shutdown time shall be ≤0.2 sec.",
                "Wake-up time is 0.08 sec. shutdown validation passed.",
                ["Wake-up time is 0.08 sec.", "shutdown time is 0.18 sec."],
            ),
            (
                "Wake-up time shall be ≤100ms max. shutdown time shall be ≤200ms max.",
                "Wake-up time is 80ms max. shutdown validation passed.",
                ["Wake-up time is 80ms max.", "shutdown time is 180ms max."],
            ),
            (
                "Wake-up time shall be ≤100ms! Shutdown time shall be ≤200ms!",
                "Wake-up time is 80ms! Shutdown validation passed!",
                ["Wake-up time is 80ms!", "Shutdown time is 180ms!"],
            ),
        )

        for requirement, incomplete_evidence, complete_evidence in cases:
            with self.subTest(requirement=requirement):
                incomplete = self._reconcile_numeric_evidence_set(
                    requirement=requirement,
                    evidence_excerpts=[incomplete_evidence],
                    cited_indexes=(0,),
                )
                complete = self._reconcile_numeric_evidence_set(
                    requirement=requirement,
                    evidence_excerpts=complete_evidence,
                    cited_indexes=(0, 1),
                )
                self.assertEqual(incomplete["ai_decision"], "NEED_CLARIFICATION")
                self.assertIn("numeric_evidence_unsafe_correspondence", incomplete["fallback_reason"])
                self.assertEqual(complete["ai_decision"], "ACCEPT")

    def test_numeric_guard_keeps_reference_identifiers_in_scope(self) -> None:
        cases = (
            ("ISO 26262", "ISO 21434", "ISO 26262"),
            ("ISO 26262:2018", "ISO 26262:2020", "ISO 26262-2018"),
            ("ISO 26262-6", "ISO 26262-7", "ISO 26262:6"),
            ("Version 2", "Version 3", "Version 2"),
            ("Version:02", "Version 3", "v2"),
            ("MY2025", "MY2024", "Model year 2025"),
            ("Section 3", "Section 4", "Sec. 3"),
            ("5.1", "6.2", "5.1"),
        )

        for requirement_scope, wrong_scope, equivalent_scope in cases:
            requirement = f"{requirement_scope} wake-up time shall be ≤100ms."
            wrong = self._reconcile_numeric_case(
                requirement=requirement,
                evidence_excerpt=f"{wrong_scope} wake-up time is 80ms.",
            )
            equivalent = self._reconcile_numeric_case(
                requirement=requirement,
                evidence_excerpt=f"{equivalent_scope} wake-up time is 80ms.",
            )

            with self.subTest(requirement_scope=requirement_scope):
                self.assertEqual(wrong["ai_decision"], "NEED_CLARIFICATION")
                self.assertIn("numeric_evidence_unsafe_correspondence", wrong["fallback_reason"])
                self.assertEqual(equivalent["ai_decision"], "ACCEPT")

    def test_numeric_guard_allows_reference_sentence_before_numeric_requirement(self) -> None:
        result = self._reconcile_numeric_case(
            requirement="See Sec. 5. Wake-up time shall be ≤100ms.",
            evidence_excerpt="Wake-up time is 80ms and passed validation.",
        )

        self.assertEqual(result["ai_decision"], "ACCEPT")
        self.assertEqual(result["validation_status"], "accepted")

    def test_numeric_guard_fails_closed_for_unparsed_word_quantities_on_all_decisions(self) -> None:
        cases = (
            ("Wake-up time shall be ten ms.", "Pressure test passed validation.", "ACCEPT"),
            ("Wake-up time shall be ten ms.", "Pressure test passed validation.", "DEVIATION_ACCEPT"),
            ("Wake-up time shall be ten ms.", "Pressure test failed validation.", "REJECT"),
            ("Wake-up time shall be ten ms.", "Pressure test failed validation.", "ACCEPT"),
            ("Resistance shall be one third volt.", "Pressure test passed validation.", "ACCEPT"),
            ("Resistance shall be half of a volt.", "Pressure test passed validation.", "ACCEPT"),
            ("Resistance shall be a single volt.", "Pressure test passed validation.", "ACCEPT"),
            ("Wake-up shall be ten to the third milliseconds.", "Pressure passed.", "ACCEPT"),
            ("工作电压应为半个伏。", "压力测试通过。", "ACCEPT"),
            ("电阻应为十欧。", "压力测试通过。", "ACCEPT"),
            ("工作电压应为廿伏。", "压力测试通过。", "ACCEPT"),
            ("工作电压应为卅伏。", "压力测试通过。", "ACCEPT"),
            ("压力应为十牛顿。", "温度测试通过。", "ACCEPT"),
            ("A volt shall be supported.", "Pressure test passed.", "ACCEPT"),
            ("An amp shall be supported.", "Pressure test passed.", "ACCEPT"),
            ("A millisecond shall be supported.", "Pressure test passed.", "ACCEPT"),
            ("Scores of volts shall be supported.", "Pressure test passed.", "ACCEPT"),
            ("A pair of volts shall be supported.", "Pressure test passed.", "ACCEPT"),
            ("Twice per cycle shall be supported.", "Pressure test passed.", "ACCEPT"),
            ("几伏应被支持。", "压力测试通过。", "ACCEPT"),
            ("十几伏应被支持。", "压力测试通过。", "ACCEPT"),
            ("一打伏应被支持。", "压力测试通过。", "ACCEPT"),
            ("Wake-up shall occur once.", "Pressure test passed.", "ACCEPT"),
            ("Wake-up shall occur twice.", "Pressure test passed.", "ACCEPT"),
            ("Wake-up shall occur fourfold.", "Pressure test passed.", "ACCEPT"),
            ("检查应执行一次。", "压力测试通过。", "ACCEPT"),
            ("检查应执行两次。", "压力测试通过。", "ACCEPT"),
            ("检查应重复三遍。", "压力测试通过。", "ACCEPT"),
        )

        for requirement, evidence, decision in cases:
            with self.subTest(requirement=requirement, decision=decision):
                result = self._reconcile_numeric_case(
                    requirement=requirement,
                    evidence_excerpt=evidence,
                    decision=decision,
                )
                self.assertEqual(result["ai_decision"], "NEED_CLARIFICATION")
                self.assertIn("numeric_constraint_unsupported", result["fallback_reason"])

    def test_numeric_guard_compares_pure_identifier_scopes_and_normalizes_equivalents(self) -> None:
        cases = (
            ("Channel1 shall apply.", "Channel2 shall apply.", "Channel 1 shall apply."),
            ("Bus1 shall apply.", "Bus2 shall apply.", "Bus 1 shall apply."),
            ("5.1 shall apply.", "6.2 shall apply.", "5.1 shall apply."),
            ("ISO 26262 shall apply.", "ISO 21434 shall apply.", "ISO 26262 shall apply."),
            ("Version:02 shall apply.", "Version 3 shall apply.", "v2 shall apply."),
            ("Channel one shall apply.", "Channel two shall apply.", "Channel 1 shall apply."),
            ("通道(壹)应适用。", "通道(贰)适用。", "通道1适用。"),
            ("通道(十一)应适用。", "通道(十二)适用。", "通道11适用。"),
            ("2025/7/1 shall apply.", "2025/7/2 shall apply.", "2025-07-01 shall apply."),
            ("Section IV shall apply.", "Section V shall apply.", "Section 4 shall apply."),
            ("Version II shall apply.", "Version III shall apply.", "Version 2 shall apply."),
            ("Chapter X shall apply.", "Chapter IX shall apply.", "Chapter 10 shall apply."),
            ("Table 1 shall apply.", "Table 2 shall apply.", "Table 1 shall apply."),
            ("Figure 1 shall apply.", "Figure 2 shall apply.", "Fig. 1 shall apply."),
            ("Page 1 shall apply.", "Page 2 shall apply.", "Page 1 shall apply."),
            ("Table IV shall apply.", "Table V shall apply.", "Table 4 shall apply."),
            ("Section A shall apply.", "Section B shall apply.", "Sec. A shall apply."),
            ("Channel eleventh shall apply.", "Channel twelfth shall apply.", "Channel 11 shall apply."),
            ("The first channel shall apply.", "The second channel shall apply.", "Channel 1 shall apply."),
            ("Channel zeroth shall apply.", "Channel first shall apply.", "Channel 0 shall apply."),
            ("Section eleventh shall apply.", "Section twelfth shall apply.", "Section 11 shall apply."),
            ("第十一节应适用。", "第十二节应适用。", "第11节应适用。"),
            ("第壹章应适用。", "第贰章应适用。", "第1章应适用。"),
            ("表一应适用。", "表二应适用。", "表1应适用。"),
            ("图四应适用。", "图五应适用。", "图4应适用。"),
            ("版本二应适用。", "版本三应适用。", "版本2应适用。"),
        )

        for requirement, wrong_evidence, equivalent_evidence in cases:
            wrong = self._reconcile_numeric_case(
                requirement=requirement,
                evidence_excerpt=wrong_evidence,
            )
            equivalent = self._reconcile_numeric_case(
                requirement=requirement,
                evidence_excerpt=equivalent_evidence,
            )
            with self.subTest(requirement=requirement):
                self.assertEqual(wrong["ai_decision"], "NEED_CLARIFICATION")
                self.assertIn("numeric_evidence_unsafe_correspondence", wrong["fallback_reason"])
                self.assertEqual(equivalent["ai_decision"], "ACCEPT")

    def test_numeric_guard_keeps_chinese_reference_scope_on_numeric_constraint(self) -> None:
        wrong = self._reconcile_numeric_case(
            requirement="第十一节唤醒时间应≤100ms。",
            evidence_excerpt="第十二节唤醒时间为80ms。",
        )
        equivalent = self._reconcile_numeric_case(
            requirement="第十一节唤醒时间应≤100ms。",
            evidence_excerpt="第11节唤醒时间为80ms。",
        )

        self.assertEqual(wrong["ai_decision"], "NEED_CLARIFICATION")
        self.assertIn("numeric_evidence_unsafe_correspondence", wrong["fallback_reason"])
        self.assertEqual(equivalent["ai_decision"], "ACCEPT")

    def test_numeric_guard_rejects_wrapped_signs_and_delayed_symbol_qualifiers(self) -> None:
        cases = (
            ("Operating voltage shall be ≥0V.", "Operating voltage is (➖)12V."),
            ("Operating voltage shall be ≥0V.", "Operating voltage is 【➖】12V."),
            ("Operating voltage shall be ≥0V.", "Operating voltage is ⁃12V."),
            ("Operating voltage shall be ≥0V.", "工作电压为负12V。"),
            ("Operating voltage shall be ≥0V.", "Operating voltage is 12V-."),
            ("Operating voltage shall be ≥0V.", "Operating voltage is (12V)-."),
            ("Operating voltage shall be ≥0V.", "Operating voltage is 12V –."),
            (
                "Operating voltage shall be 12 V nominal (⎓).",
                "Operating voltage is 12 V nominal (∿).",
            ),
        )

        for requirement, evidence in cases:
            with self.subTest(evidence=evidence):
                result = self._reconcile_numeric_case(
                    requirement=requirement,
                    evidence_excerpt=evidence,
                )
                self.assertEqual(result["ai_decision"], "NEED_CLARIFICATION")
                self.assertIn("numeric_constraint_unsupported", result["fallback_reason"])

    def test_numeric_guard_does_not_let_ambiguous_rail_measurements_hitchhike(self) -> None:
        cases = (
            "1.5 newtons shall be supported.",
            "Rail 5 newtons shall be supported.",
            "Rail 5 dynes shall be supported.",
            "Rail 5 ergs shall be supported.",
            "Rail 5 gauss shall be supported.",
            "Rail five dynes shall be supported.",
            "Rail 5 “dynes” shall be supported.",
            "Rail 5/dynes shall be supported.",
            "4.4 “dynes” shall be supported.",
            "通道5达因应被支持。",
            "通道五达因应被支持。",
            "第5通道达因应被支持。",
            "端口5高斯应被支持。",
            "Rail 5 N·m shall be supported.",
            "Rail 5 {Ω} shall be supported.",
            "Rail 5-[Ω] shall be supported.",
            "Rail 5:Ω shall be supported.",
        )

        for requirement in cases:
            with self.subTest(requirement=requirement):
                result = self._reconcile_numeric_case(
                    requirement=requirement,
                    evidence_excerpt="Pressure test passed validation.",
                )
                self.assertEqual(result["ai_decision"], "NEED_CLARIFICATION")
                self.assertIn("numeric_constraint_unsupported", result["fallback_reason"])

    def test_numeric_guard_abstains_for_unrecognized_adjacent_identifiers(self) -> None:
        cases = (
            (
                "Channel(1) wake-up shall be ≤100ms and Channel(2) wake-up shall be ≤200ms.",
                "Channel(1) wake-up is 80ms.",
            ),
            (
                "ECU Ⅰ wake-up shall be ≤100ms and ECU Ⅱ wake-up shall be ≤200ms.",
                "ECU Ⅰ wake-up is 80ms.",
            ),
            (
                "CAN FD1 wake-up shall be ≤100ms and CAN FD2 wake-up shall be ≤200ms.",
                "CAN FD1 wake-up is 80ms.",
            ),
        )

        for requirement, evidence_excerpt in cases:
            with self.subTest(requirement=requirement):
                result = self._reconcile_numeric_case(
                    requirement=requirement,
                    evidence_excerpt=evidence_excerpt,
                )
                self.assertEqual(result["ai_decision"], "NEED_CLARIFICATION")

    def test_reconcile_rejects_model_authored_requirement_rewrite(self) -> None:
        result = self._reconcile_numeric_case(
            requirement="System shall support secure diagnostics.",
            candidate_requirement="System shall disable secure diagnostics.",
            evidence_excerpt="System supports secure diagnostics and passed validation.",
        )

        self.assertEqual(result["ai_decision"], "NEED_CLARIFICATION")
        self.assertEqual(result["normalized_requirement"], "System shall support secure diagnostics.")
        self.assertIn("requirement_projection_out_of_scope", result["fallback_reason"])

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
        retrieved = [_with_evidence_contract(item) for item in retrieved]

        result = reconcile_assessment_output(
            context=context,
            candidate=candidate,
            retrieved_evidences=retrieved,
            fallback_reason="",
        )

        self.assertEqual(result["ai_decision"], "NEED_CLARIFICATION")
        self.assertEqual(result["validation_status"], "fallback")
        self.assertIn("numeric_evidence_unsafe_correspondence", result["fallback_reason"])

    def test_reconcile_assessment_output_uses_server_owned_relevance_for_decision_gate(self) -> None:
        context = {
            "requirement_text": "The component shall support wake-up time less than 100ms.",
            "normalized_requirement": "The component shall support wake-up time less than 100ms.",
            "requirement_type": "FUNCTIONAL",
        }
        canonical_excerpt = "The component supports wake-up time of 80ms and passed validation."
        candidate = {
            "normalized_requirement": context["normalized_requirement"],
            "proposed_decision": "ACCEPT",
            "confidence": 0.96,
            "reason": "The cited evidence demonstrates compliance.",
            "risk_level": "LOW",
            "clarification_question": "",
            "evidences": [
                {
                    "source_doc_id": "kb-score-1",
                    "source_doc_name": "wake-up-spec.txt",
                    "source_location": "Chunk 1",
                    "excerpt": canonical_excerpt,
                    "relevance": 0.99,
                    "source_doc_version": "v1",
                }
            ],
        }
        retrieved = [
            {
                "source_doc_id": "kb-score-1",
                "source_doc_name": "wake-up-spec.txt",
                "source_location": "Chunk 1",
                "excerpt": canonical_excerpt,
                "relevance": 0.01,
                "source_doc_version": "v1",
            }
        ]
        retrieved = [_with_evidence_contract(item) for item in retrieved]

        result = reconcile_assessment_output(
            context=context,
            candidate=candidate,
            retrieved_evidences=retrieved,
            fallback_reason="",
        )

        self.assertEqual(result["ai_decision"], "NEED_CLARIFICATION")
        self.assertEqual(result["evidences"][0]["relevance"], 0.01)
        self.assertIn("decision_requires_stronger_evidence", result["fallback_reason"])

    def test_reconcile_assessment_output_rehydrates_selected_evidence_from_server_candidate(self) -> None:
        context = {
            "requirement_text": "The component shall support wake-up time less than 100ms.",
            "normalized_requirement": "The component shall support wake-up time less than 100ms.",
            "requirement_type": "FUNCTIONAL",
        }
        canonical_excerpt = "The component supports wake-up time of 80ms and passed validation."
        candidate = {
            "normalized_requirement": context["normalized_requirement"],
            "proposed_decision": "ACCEPT",
            "confidence": 0.88,
            "reason": "The cited evidence demonstrates compliance.",
            "risk_level": "LOW",
            "clarification_question": "",
            "evidences": [
                {
                    "source_doc_id": "kb-provenance-1",
                    "source_doc_name": "forged-name.txt",
                    "source_location": "Chunk 7",
                    "excerpt": f"{canonical_excerpt} Forged text must not survive reconciliation.",
                    "relevance": 0.99,
                    "source_doc_version": "forged-version",
                }
            ],
        }
        retrieved = [
            {
                "source_doc_id": "kb-provenance-1",
                "source_doc_name": "canonical-spec.txt",
                "source_location": "Chunk 7",
                "excerpt": canonical_excerpt,
                "relevance": 0.72,
                "source_doc_version": "2026.03",
            }
        ]
        retrieved = [_with_evidence_contract(item) for item in retrieved]

        result = reconcile_assessment_output(
            context=context,
            candidate=candidate,
            retrieved_evidences=retrieved,
            fallback_reason="",
        )

        self.assertEqual(result["ai_decision"], "ACCEPT")
        self.assertEqual(result["evidences"][0]["source_doc_id"], "kb-provenance-1")
        self.assertEqual(result["evidences"][0]["source_doc_name"], "canonical-spec.txt")
        self.assertEqual(result["evidences"][0]["excerpt"], canonical_excerpt)
        self.assertEqual(
            result["evidences"][0]["retrieval_evidence_id"],
            retrieved[0]["evidence_id"],
        )
        self.assertEqual(
            result["evidences"][0]["content_hash"],
            retrieved[0]["content_hash"],
        )
        self.assertIn("legacy_evidence_selector", result["fallback_reason"])

    def test_reconcile_assessment_output_prefers_server_evidence_id(self) -> None:
        context = {
            "requirement_text": "The component shall support wake-up time less than 100ms.",
            "normalized_requirement": "The component shall support wake-up time less than 100ms.",
            "requirement_type": "FUNCTIONAL",
        }
        canonical = _with_evidence_contract(
            {
                "source_doc_id": "kb-id-1",
                "source_doc_name": "canonical.txt",
                "source_location": "Chunk 1",
                "excerpt": "The component supports wake-up time of 80ms and passed validation.",
                "relevance": 0.72,
                "source_doc_version": "v1",
            },
            evidence_id="kb-id-1_paragraph_1",
        )
        candidate = {
            "normalized_requirement": context["normalized_requirement"],
            "proposed_decision": "ACCEPT",
            "confidence": 0.88,
            "reason": "The selected evidence demonstrates compliance.",
            "risk_level": "LOW",
            "clarification_question": "",
            "evidences": [
                {
                    "evidence_id": "kb-id-1_paragraph_1",
                    "content_hash": "0" * 64,
                    "evidence_contract_version": "forged-contract",
                    "source_doc_id": "forged-doc",
                    "source_doc_name": "forged.txt",
                    "source_location": "Forged location",
                    "excerpt": "Forged evidence text.",
                    "relevance": 0.99,
                    "source_doc_version": "forged-version",
                }
            ],
        }

        prompt_payload = json.loads(build_llm_messages(context, [canonical])[1]["content"])
        self.assertEqual(
            prompt_payload["candidate_evidences"][0]["evidence_id"],
            "kb-id-1_paragraph_1",
        )

        result = reconcile_assessment_output(
            context=context,
            candidate=candidate,
            retrieved_evidences=[canonical],
            fallback_reason="",
        )

        self.assertEqual(result["ai_decision"], "ACCEPT")
        self.assertEqual(result["evidences"][0]["source_doc_id"], "kb-id-1")
        self.assertEqual(result["evidences"][0]["source_doc_name"], "canonical.txt")
        self.assertEqual(
            result["evidences"][0]["retrieval_evidence_id"],
            "kb-id-1_paragraph_1",
        )
        self.assertEqual(
            result["evidences"][0]["content_hash"],
            canonical["content_hash"],
        )
        self.assertEqual(result["fallback_reason"], "")

    def test_reconcile_accepts_complete_compliance_contract(self) -> None:
        context = {
            "requirement_text": "系统应支持安全诊断。",
            "normalized_requirement": "系统应支持安全诊断。",
            "requirement_type": "FUNCTIONAL",
        }
        evidence = _with_evidence_contract(
            {
                "source_doc_id": "kb-contract-1",
                "source_doc_name": "validation.txt",
                "source_location": "Chunk 1",
                "excerpt": "安全诊断功能已通过验证并满足项目要求。",
                "relevance": 0.82,
                "source_doc_version": "v1",
                "doc_type": "TEST_REPORT",
                "status": "ACTIVE",
                "content_kind": "paragraph",
            },
            evidence_id="kb-contract-1_paragraph_1",
        )
        candidate = _assessment_candidate(
            evidence,
            decision="ACCEPT",
            reason="证据直接证明安全诊断功能已通过验证。",
            confidence=0.91,
            risk_level="LOW",
        )
        candidate.update(
            {
                "normalized_requirement": context["normalized_requirement"],
                "compliance_status": "COMPLIANT",
                "evidence_findings": [
                    {
                        "retrieval_evidence_id": evidence["evidence_id"],
                        "finding": "SUPPORTS",
                        "supported_claims": ["支持安全诊断"],
                        "unsupported_claims": [],
                        "rationale": "证据明确记录验证通过。",
                    }
                ],
                "sub_judgments": [
                    {
                        "sub_judgment_id": "sj-1",
                        "source_ranges": ["atomic_source_text"],
                        "statement": context["normalized_requirement"],
                        "compliance_status": "COMPLIANT",
                        "reason": "验证记录直接覆盖该约束。",
                        "retrieval_evidence_ids": [evidence["evidence_id"]],
                        "structured_information_gap": {},
                    }
                ],
                "structured_information_gap": {},
            }
        )

        result = reconcile_assessment_output(
            context=context,
            candidate=candidate,
            retrieved_evidences=[evidence],
            fallback_reason="",
        )

        self.assertEqual(result["compliance_status"], "COMPLIANT")
        self.assertEqual(result["ai_decision"], "ACCEPT")
        self.assertEqual(
            result["evidence_findings"][0]["retrieval_evidence_id"],
            evidence["evidence_id"],
        )
        self.assertEqual(result["sub_judgments"][0]["sub_judgment_id"], "sj-1")

    def test_llm_guarded_reconciliation_rejects_normative_text_as_proof(self) -> None:
        statement = "系统应支持安全诊断。"
        evidence = _with_evidence_contract(
            {
                "source_doc_id": "kb-normative-only",
                "source_doc_name": "requirement.txt",
                "source_location": "Clause 1",
                "excerpt": "系统必须满足安全诊断要求。",
                "relevance": 0.92,
                "source_doc_version": "v1",
                "authority": "CUSTOMER_APPROVED",
                "claim_role": "NORMATIVE_REQUIREMENT",
            },
            evidence_id="kb-normative-only_clause_1",
        )
        candidate = _llm_assessment_candidate(
            evidence,
            statement=statement,
            decision="ACCEPT",
            reason="文本与需求高度相关。",
            confidence=0.9,
            risk_level="LOW",
        )

        result = reconcile_assessment_output(
            context={
                "requirement_text": statement,
                "normalized_requirement": statement,
                "requirement_type": "FUNCTIONAL",
            },
            candidate=candidate,
            retrieved_evidences=[evidence],
            fallback_reason="",
            reconciliation_policy="llm_guarded",
        )

        self.assertEqual(result["ai_decision"], "NEED_CLARIFICATION")
        self.assertIn(
            "decision_requires_outcome_proof",
            result["fallback_reason"],
        )

    def test_llm_contract_requires_grounding_for_each_decisive_sub_judgment(self) -> None:
        statement = "系统应支持安全诊断。"
        evidence = _with_evidence_contract(
            {
                "source_doc_id": "kb-sub-grounding",
                "source_doc_name": "validation.txt",
                "source_location": "Result 1",
                "excerpt": "验证结果显示安全诊断已通过。",
                "relevance": 0.9,
            },
            evidence_id="kb-sub-grounding_result_1",
        )
        candidate = _llm_assessment_candidate(
            evidence,
            statement=statement,
            decision="ACCEPT",
            reason="验证结果覆盖该约束。",
            confidence=0.9,
            risk_level="LOW",
        )
        candidate["sub_judgments"][0]["retrieval_evidence_ids"] = []

        result = reconcile_assessment_output(
            context={
                "requirement_text": statement,
                "normalized_requirement": statement,
                "requirement_type": "FUNCTIONAL",
            },
            candidate=candidate,
            retrieved_evidences=[evidence],
            fallback_reason="",
            reconciliation_policy="llm_guarded",
        )

        self.assertEqual(result["ai_decision"], "NEED_CLARIFICATION")
        self.assertIn(
            "sub_judgment_evidence_missing",
            result["fallback_reason"],
        )

    def test_llm_guarded_reconciliation_rejects_legacy_incomplete_output(self) -> None:
        context = {
            "requirement_text": "系统应支持安全诊断。",
            "normalized_requirement": "系统应支持安全诊断。",
            "requirement_type": "FUNCTIONAL",
        }
        evidence = _with_evidence_contract(
            {
                "source_doc_id": "kb-contract-legacy",
                "source_doc_name": "validation.txt",
                "source_location": "Chunk 1",
                "excerpt": "安全诊断功能已通过验证并满足项目要求。",
                "relevance": 0.82,
                "source_doc_version": "v1",
            },
            evidence_id="kb-contract-legacy_paragraph_1",
        )
        candidate = _assessment_candidate(
            evidence,
            decision="ACCEPT",
            reason="旧输出只给出最终标签和证据。",
            confidence=0.9,
            risk_level="LOW",
        )

        result = reconcile_assessment_output(
            context=context,
            candidate=candidate,
            retrieved_evidences=[evidence],
            fallback_reason="",
            reconciliation_policy="llm_guarded",
        )

        self.assertEqual(result["ai_decision"], "NEED_CLARIFICATION")
        self.assertEqual(
            result["compliance_status"],
            "INSUFFICIENT_INFORMATION",
        )
        self.assertIn("sub_judgments_missing", result["fallback_reason"])

    def test_reconcile_fails_closed_when_compliance_and_legacy_projection_conflict(self) -> None:
        context = {
            "requirement_text": "系统应支持安全诊断。",
            "normalized_requirement": "系统应支持安全诊断。",
            "requirement_type": "FUNCTIONAL",
        }
        candidate = {
            "normalized_requirement": context["normalized_requirement"],
            "proposed_decision": "ACCEPT",
            "compliance_status": "NON_COMPLIANT",
            "confidence": 0.8,
            "reason": "模型输出互相冲突。",
            "risk_level": "HIGH",
            "clarification_question": "",
            "evidences": [],
        }

        result = reconcile_assessment_output(
            context=context,
            candidate=candidate,
            retrieved_evidences=[],
            fallback_reason="",
        )

        self.assertEqual(
            result["compliance_status"],
            "INSUFFICIENT_INFORMATION",
        )
        self.assertEqual(result["ai_decision"], "NEED_CLARIFICATION")
        self.assertIn("compliance_decision_conflict", result["fallback_reason"])
        self.assertTrue(
            result["structured_information_gap"]["missing_items"]
        )

    def test_reconcile_rejects_structured_reference_outside_selected_evidence(self) -> None:
        context = {
            "requirement_text": "系统应支持安全诊断。",
            "normalized_requirement": "系统应支持安全诊断。",
            "requirement_type": "FUNCTIONAL",
        }
        evidence = _with_evidence_contract(
            {
                "source_doc_id": "kb-contract-2",
                "source_doc_name": "validation.txt",
                "source_location": "Chunk 1",
                "excerpt": "安全诊断功能已通过验证并满足项目要求。",
                "relevance": 0.82,
                "source_doc_version": "v1",
            },
            evidence_id="kb-contract-2_paragraph_1",
        )
        candidate = _assessment_candidate(
            evidence,
            decision="ACCEPT",
            reason="证据直接证明该约束。",
            confidence=0.9,
            risk_level="LOW",
        )
        candidate.update(
            {
                "normalized_requirement": context["normalized_requirement"],
                "compliance_status": "COMPLIANT",
                "evidence_findings": [
                    {
                        "retrieval_evidence_id": "forged-evidence-id",
                        "finding": "SUPPORTS",
                        "rationale": "伪造引用。",
                    }
                ],
                "sub_judgments": [
                    {
                        "sub_judgment_id": "sj-1",
                        "statement": context["normalized_requirement"],
                        "compliance_status": "COMPLIANT",
                        "reason": "伪造引用。",
                        "retrieval_evidence_ids": ["forged-evidence-id"],
                    }
                ],
            }
        )

        result = reconcile_assessment_output(
            context=context,
            candidate=candidate,
            retrieved_evidences=[evidence],
            fallback_reason="",
        )

        self.assertEqual(result["ai_decision"], "NEED_CLARIFICATION")
        self.assertIn(
            "structured_evidence_reference_out_of_scope",
            result["fallback_reason"],
        )

    def test_reconcile_assessment_output_rejects_conflicting_server_evidence_identity(self) -> None:
        context = {
            "requirement_text": "The component shall support wake-up time less than 100ms.",
            "normalized_requirement": "The component shall support wake-up time less than 100ms.",
            "requirement_type": "FUNCTIONAL",
        }
        evidence_id = "kb-conflict_paragraph_1"
        retrieved = [
            _with_evidence_contract(
                {
                    "source_doc_id": "kb-conflict",
                    "source_doc_name": "spec.txt",
                    "source_location": "Chunk 1",
                    "excerpt": "Wake-up time is 80ms and validation passed.",
                    "relevance": 0.72,
                    "source_doc_version": "v1",
                },
                evidence_id=evidence_id,
            ),
            _with_evidence_contract(
                {
                    "source_doc_id": "kb-conflict",
                    "source_doc_name": "spec.txt",
                    "source_location": "Chunk 1",
                    "excerpt": "Wake-up time is 180ms and validation failed.",
                    "relevance": 0.73,
                    "source_doc_version": "v1",
                },
                evidence_id=evidence_id,
            ),
        ]
        candidate = {
            "normalized_requirement": context["normalized_requirement"],
            "proposed_decision": "ACCEPT",
            "confidence": 0.88,
            "reason": "Accepted.",
            "risk_level": "LOW",
            "clarification_question": "",
            "evidences": [
                {
                    "evidence_id": evidence_id,
                    "source_doc_id": "kb-conflict",
                    "source_doc_name": "spec.txt",
                    "source_location": "Chunk 1",
                    "excerpt": "Wake-up time evidence.",
                    "relevance": 0.72,
                    "source_doc_version": "v1",
                }
            ],
        }

        result = reconcile_assessment_output(
            context=context,
            candidate=candidate,
            retrieved_evidences=retrieved,
            fallback_reason="",
        )

        self.assertEqual(result["ai_decision"], "NEED_CLARIFICATION")
        self.assertIn("evidence_identity_conflict", result["fallback_reason"])

    def test_reconcile_assessment_output_rejects_forged_evidence_location(self) -> None:
        context = {
            "requirement_text": "The component shall support wake-up time less than 100ms.",
            "normalized_requirement": "The component shall support wake-up time less than 100ms.",
            "requirement_type": "FUNCTIONAL",
        }
        canonical_excerpt = "The component supports wake-up time of 80ms and passed validation."
        candidate = {
            "normalized_requirement": context["normalized_requirement"],
            "proposed_decision": "ACCEPT",
            "confidence": 0.88,
            "reason": "The cited evidence demonstrates compliance.",
            "risk_level": "LOW",
            "clarification_question": "",
            "evidences": [
                {
                    "source_doc_id": "kb-location-1",
                    "source_doc_name": "forged-name.txt",
                    "source_location": "Forged location",
                    "excerpt": "Forged excerpt",
                    "relevance": 0.99,
                    "source_doc_version": "forged-version",
                }
            ],
        }
        retrieved = [
            {
                "source_doc_id": "kb-location-1",
                "source_doc_name": "canonical-spec.txt",
                "source_location": "Chunk 2",
                "excerpt": canonical_excerpt,
                "relevance": 0.72,
                "source_doc_version": "2026.03",
            }
        ]
        retrieved = [_with_evidence_contract(item) for item in retrieved]

        result = reconcile_assessment_output(
            context=context,
            candidate=candidate,
            retrieved_evidences=retrieved,
            fallback_reason="",
        )

        self.assertEqual(result["ai_decision"], "NEED_CLARIFICATION")
        self.assertIn("evidence_reference_out_of_scope", result["fallback_reason"])
        self.assertEqual(result["evidences"][0]["source_location"], "Chunk 2")
        self.assertNotIn("Forged", json.dumps(result["evidences"], ensure_ascii=False))

    def test_reconcile_assessment_output_rejects_legacy_server_index_evidence(self) -> None:
        context = {
            "requirement_text": "The component shall support wake-up time less than 100ms.",
            "normalized_requirement": "The component shall support wake-up time less than 100ms.",
            "requirement_type": "FUNCTIONAL",
        }
        legacy_evidence = {
            "source_doc_id": "kb-legacy",
            "source_doc_name": "legacy.txt",
            "source_location": "Chunk 1",
            "excerpt": "The component supports wake-up time of 80ms and passed validation.",
            "relevance": 0.72,
            "source_doc_version": "v0",
        }
        candidate = {
            "normalized_requirement": context["normalized_requirement"],
            "proposed_decision": "ACCEPT",
            "confidence": 0.88,
            "reason": "Accepted.",
            "risk_level": "LOW",
            "clarification_question": "",
            "evidences": [legacy_evidence],
        }

        result = reconcile_assessment_output(
            context=context,
            candidate=candidate,
            retrieved_evidences=[legacy_evidence],
            fallback_reason="",
        )

        self.assertEqual(result["ai_decision"], "NEED_CLARIFICATION")
        self.assertEqual(result["evidences"], [])
        self.assertIn("evidence_index_contract_invalid", result["fallback_reason"])

    def test_evidence_content_hash_uses_canonical_utf8_bytes(self) -> None:
        decomposed = "Cafe\u0301 \t\r\nLine 2  \r\n"
        canonical = "Café\nLine 2"

        self.assertEqual(
            compute_evidence_content_hash(decomposed),
            compute_evidence_content_hash(canonical),
        )
        self.assertNotEqual(
            compute_evidence_content_hash(canonical),
            compute_evidence_content_hash("Café\nLine 3"),
        )

    def test_build_knowledge_chunks_produces_stable_identity_and_content_hash(self) -> None:
        knowledge_doc = {
            "id": "kb-stable",
            "file_name": "stable.txt",
            "doc_version": "v1",
            "doc_type": "REFERENCE",
            "status": "ACTIVE",
            "effective_from": "",
            "effective_to": "",
            "extracted_text": (
                "Wake-up latency requirement.\n"
                "The component supports 80ms wake-up and passed validation.\n"
            ),
        }

        first = build_knowledge_chunks("project-1", knowledge_doc)
        second = build_knowledge_chunks("project-1", dict(knowledge_doc))
        changed = build_knowledge_chunks(
            "project-1",
            {
                **knowledge_doc,
                "extracted_text": knowledge_doc["extracted_text"].replace("80ms", "90ms"),
            },
        )

        self.assertTrue(first)
        self.assertEqual(
            [(item["evidence_id"], item["content_hash"]) for item in first],
            [(item["evidence_id"], item["content_hash"]) for item in second],
        )
        self.assertEqual(
            [item["evidence_id"] for item in first],
            [item["evidence_id"] for item in changed],
        )
        self.assertNotEqual(
            [item["content_hash"] for item in first],
            [item["content_hash"] for item in changed],
        )
        for item in first:
            self.assertEqual(item["evidence_id"], item["chunk_id"])
            self.assertEqual(item["evidence_contract_version"], EVIDENCE_INDEX_CONTRACT_VERSION)
            self.assertEqual(item["content_hash"], compute_evidence_content_hash(item["content"]))
            self.assertEqual(item["meta"]["evidence_id"], item["evidence_id"])
            self.assertEqual(item["meta"]["content_hash"], item["content_hash"])

        contract_fields = {"evidence_id", "content_hash", "evidence_contract_version"}
        self.assertTrue(contract_fields.issubset(_azure_metadata_fields()))
        self.assertTrue(contract_fields.issubset(_dashvector_metadata_fields()))
        for flattened in (
            _flatten_azure_chunk_meta(first[0]["meta"]),
            _flatten_qdrant_chunk_meta(first[0]["meta"], content=first[0]["content"]),
        ):
            self.assertEqual(flattened["evidence_id"], first[0]["evidence_id"])
            self.assertEqual(flattened["content_hash"], first[0]["content_hash"])
            self.assertEqual(flattened["evidence_contract_version"], EVIDENCE_INDEX_CONTRACT_VERSION)

    def test_run_ai_copilot_assessment_azure_llm_primary_does_not_apply_keyword_hard_rule_override(self) -> None:
        llm_primary_config = dataclasses.replace(
            self.config,
            assessment_mode="llm_primary",
            retrieval_backend="local",
            azure_search_endpoint="https://search.example.search.windows.net",
            azure_search_index_name="reqpilot-knowledge",
            azure_openai_endpoint="https://openai.example.openai.azure.com",
            azure_openai_chat_deployment="gpt-5.4",
            azure_openai_embedding_deployment="text-embedding-3-large",
            azure_openai_embedding_dimensions=1536,
        )
        row = {
            "id": "req-azure-hard-rule-1",
            "req_code": "REQ-AZURE-HARD-RULE-1",
            "display_text": "Supplier shall provide EMC validation evidence.",
            "requirement_type": "REGULATION",
            "parent_section_label": "5.1 Compliance",
        }
        retrieved_evidences = [
            _with_evidence_contract(
                {
                "source_doc_id": "kb-1",
                "source_doc_name": "emc-report.txt",
                "source_location": "Chunk 1",
                "excerpt": "Supplier submitted the EMC validation evidence package on 2026-05-01.",
                "relevance": 0.71,
                "source_doc_version": "v1.0",
                },
                evidence_id="kb-1_chunk_1",
            )
        ]
        llm_assessment = _llm_assessment_candidate(
            retrieved_evidences[0],
            statement="Supplier shall provide EMC validation evidence.",
            decision="ACCEPT",
            confidence=0.84,
            reason="The cited evidence directly states that the EMC validation evidence package was submitted.",
            risk_level="LOW",
        )

        with patch(
            "app.services.ai_copilot.AzureOpenAIScreeningEngine.screen",
            return_value={"analysis_label": "REQUIREMENT", "reason": "该条目表达了明确可验证义务。"},
        ), patch(
            "app.services.ai_copilot.HeuristicEvidenceRetriever.retrieve",
            return_value=retrieved_evidences,
        ), patch(
            "app.services.ai_copilot.AzureOpenAIAssessmentEngine.assess",
            return_value=llm_assessment,
        ):
            result = run_ai_copilot_assessment(row, [{"id": "kb-1"}], config=llm_primary_config)

        assessment = result["assessment"]
        self.assertEqual(assessment["ai_decision"], "ACCEPT")
        self.assertEqual(result["runs"][-1]["mode"], "llm_primary")
        self.assertEqual(result["runs"][-1]["validation_status"], "accepted")
        self.assertEqual(result["runs"][-1]["fallback_reason"], "")

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
        self.assertEqual(sync["evidence_contract_version"], EVIDENCE_INDEX_CONTRACT_VERSION)
        self.assertEqual(sync["evidence_index_resync_action"], "")
        payload_path = Path(sync["payload_path"])
        self.assertTrue(payload_path.exists())
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["evidence_contract_version"], EVIDENCE_INDEX_CONTRACT_VERSION)
        self.assertTrue(all(item.get("evidence_id") for item in payload["chunks"]))
        self.assertTrue(all(item.get("content_hash") for item in payload["chunks"]))
        content_kinds = {item["content_kind"] for item in payload["chunks"]}
        self.assertIn("table_slice", content_kinds)
        table_content_hashes = {
            item["content_hash"]
            for item in payload["chunks"]
            if item["content_kind"] == "table_slice"
        }
        paragraph_content_hashes = {
            item["content_hash"]
            for item in payload["chunks"]
            if item["content_kind"] == "paragraph"
        }
        self.assertTrue(
            table_content_hashes.isdisjoint(paragraph_content_hashes)
        )

    def test_sync_knowledge_document_index_does_not_upsert_ineligible_document(self) -> None:
        remote_config = dataclasses.replace(
            self.config,
            retrieval_backend="azure_sdk",
            azure_search_endpoint="https://search.example.search.windows.net",
            azure_openai_endpoint="https://openai.example.openai.azure.com",
            azure_openai_embedding_deployment="text-embedding-3-large",
        )
        knowledge_doc = {
            "id": "kb-draft-sync",
            "file_name": "draft.txt",
            "doc_version": "v1",
            "doc_type": "REFERENCE",
            "status": "DRAFT",
            "effective_from": "2000-01-01",
            "effective_to": "",
            "extracted_text": "This draft must never be indexed as usable evidence.",
        }
        fake_indexer = Mock()

        with patch("app.services.ai_copilot.INDEX_PAYLOAD_DIR", self.index_dir), patch(
            "app.services.ai_copilot._select_indexer",
            return_value=fake_indexer,
        ):
            sync = sync_knowledge_document_index("project-1", knowledge_doc, config=remote_config)

        self.assertEqual(sync["status"], "ERROR")
        self.assertEqual(sync["chunk_count"], 0)
        self.assertIn("status_not_active", sync["error"])
        fake_indexer.replace_chunks.assert_not_called()

    def test_sync_knowledge_document_index_fails_closed_for_zero_chunks(self) -> None:
        knowledge_doc = {
            "id": "kb-empty-sync",
            "file_name": "empty.txt",
            "doc_version": "v1",
            "doc_type": "REFERENCE",
            "status": "ACTIVE",
            "effective_from": "",
            "effective_to": "",
            "extracted_text": "   \n",
        }

        with patch("app.services.ai_copilot.INDEX_PAYLOAD_DIR", self.index_dir):
            sync = sync_knowledge_document_index(
                "project-1",
                knowledge_doc,
                config=self.config,
            )

        self.assertEqual(sync["status"], "ERROR")
        self.assertEqual(sync["chunk_count"], 0)
        self.assertIn("knowledge_document_has_no_chunks", sync["error"])
        self.assertEqual(sync["targets"][self.config.profile_id]["status"], "ERROR")

    def test_adjacent_context_keeps_separate_canonical_evidence_identity(self) -> None:
        knowledge_doc = {
            "id": "kb-adjacent",
            "project_id": "project-1",
            "file_name": "long-spec.txt",
            "doc_version": "v1",
            "doc_type": "REFERENCE",
            "status": "ACTIVE",
            "effective_from": "",
            "effective_to": "",
            "extracted_text": "\n".join(
                f"Requirement evidence line {index} contains a distinct verified statement."
                for index in range(1, 11)
            ),
        }
        chunks = [
            item
            for item in build_knowledge_chunks("project-1", knowledge_doc)
            if item["content_kind"] == "paragraph"
        ]
        self.assertGreaterEqual(len(chunks), 2)
        first = chunks[0]
        first_meta = first["meta"]
        retrieved = {
            "chunk_id": first["chunk_id"],
            "evidence_id": first["evidence_id"],
            "content_hash": first["content_hash"],
            "evidence_contract_version": first["evidence_contract_version"],
            "evidence_contract_verified": True,
            "source_doc_id": first_meta["source_doc_id"],
            "source_doc_name": first_meta["source_doc_name"],
            "source_doc_version": first_meta["source_doc_version"],
            "source_location": first_meta["source_location"],
            "excerpt": first["content"],
            "relevance": 0.8,
            "content_kind": first["content_kind"],
            "doc_type": first_meta["doc_type"],
            "status": first_meta["status"],
            "effective_from": first_meta["effective_from"],
            "effective_to": first_meta["effective_to"],
        }
        embedded_doc = {
            **knowledge_doc,
            "_index_payload": {"chunks": chunks},
        }

        extended = _extend_evidence_with_adjacent_chunks(
            [retrieved],
            knowledge_docs=[embedded_doc],
        )

        self.assertEqual(extended[0]["excerpt"], first["content"])
        self.assertEqual(len(extended), 2)
        self.assertEqual(extended[1]["evidence_id"], chunks[1]["evidence_id"])
        self.assertEqual(extended[1]["content_hash"], chunks[1]["content_hash"])
        self.assertNotEqual(
            extended[0]["evidence_id"],
            extended[1]["evidence_id"],
        )
        self.assertEqual(extended[1]["score_source"], "adjacent_context")
        self.assertEqual(extended[1]["relevance"], 0.0)
        self.assertEqual(
            extended[1]["retrieval_parent_evidence_id"],
            extended[0]["evidence_id"],
        )
        prompt = json.loads(
            build_llm_messages(
                {
                    "requirement_id": "req-adjacent",
                    "normalized_requirement": "验证相邻上下文。",
                    "requirement_text": "验证相邻上下文。",
                },
                extended,
            )[1]["content"]
        )
        self.assertEqual(len(prompt["candidate_evidences"]), 1)
        self.assertEqual(len(prompt["adjacent_evidence_context"]), 1)
        self.assertIsNone(
            prompt["adjacent_evidence_context"][0]["relevance"]
        )

    def test_failed_remote_index_sync_does_not_mark_profile_as_synced(self) -> None:
        remote_config = dataclasses.replace(
            self.config,
            retrieval_backend="azure_sdk",
            azure_auth_mode="rbac",
            azure_search_endpoint="https://search.example.search.windows.net",
            azure_openai_endpoint="https://openai.example.openai.azure.com",
            azure_openai_embedding_deployment="text-embedding-3-large",
        )
        knowledge_doc = {
            "id": "kb-failed-sync",
            "project_id": "project-1",
            "file_name": "failed-sync.txt",
            "doc_version": "v1",
            "doc_type": "REFERENCE",
            "status": "ACTIVE",
            "effective_from": "",
            "effective_to": "",
            "extracted_text": "Wake-up latency is 80ms and validation passed.",
        }
        fake_indexer = Mock()
        fake_indexer.replace_chunks.side_effect = RuntimeError("provider rejected upsert")

        with patch("app.services.ai_copilot.INDEX_PAYLOAD_DIR", self.index_dir), patch(
            "app.services.ai_copilot._select_indexer",
            return_value=fake_indexer,
        ):
            sync = sync_knowledge_document_index("project-1", knowledge_doc, config=remote_config)
            payload = json.loads(Path(sync["payload_path"]).read_text(encoding="utf-8"))
            context: dict[str, object] = {}
            gate_ready = _remote_evidence_index_contract_ready(
                [knowledge_doc],
                profile_id=remote_config.profile_id,
                context=context,
            )

        self.assertEqual(sync["status"], "ERROR")
        fake_indexer.replace_chunks.assert_called_once()
        self.assertNotIn(remote_config.profile_id, payload["profile_ids"])
        self.assertFalse(gate_ready)
        self.assertEqual(context["retrieval_migration_action"], EVIDENCE_INDEX_RESYNC_ACTION)

    def test_remote_index_sync_fails_when_provider_writes_fewer_chunks(self) -> None:
        remote_config = dataclasses.replace(
            self.config,
            retrieval_backend="azure_sdk",
            azure_auth_mode="rbac",
            azure_search_endpoint="https://search.example.search.windows.net",
            azure_openai_endpoint="https://openai.example.openai.azure.com",
            azure_openai_embedding_deployment="text-embedding-3-large",
        )
        knowledge_doc = {
            "id": "kb-partial-sync",
            "project_id": "project-1",
            "file_name": "partial-sync.txt",
            "doc_version": "v1",
            "doc_type": "REFERENCE",
            "status": "ACTIVE",
            "extracted_text": "Validation result confirms wake-up passed.",
        }
        fake_indexer = Mock()
        fake_indexer.replace_chunks.return_value = 0

        with patch(
            "app.services.ai_copilot.INDEX_PAYLOAD_DIR",
            self.index_dir,
        ), patch(
            "app.services.ai_copilot._select_indexer",
            return_value=fake_indexer,
        ):
            sync = sync_knowledge_document_index(
                "project-1",
                knowledge_doc,
                config=remote_config,
            )

        self.assertEqual(sync["status"], "ERROR")
        self.assertEqual(sync["chunk_count"], 0)
        self.assertGreater(sync["planned_chunk_count"], 0)
        self.assertIn(
            "knowledge_index_write_count_mismatch:0/",
            sync["error"],
        )

    def test_remote_evidence_index_gate_requires_and_accepts_resync_payload(self) -> None:
        knowledge_doc = {
            "id": "kb-resync-gate",
            "project_id": "project-1",
            "file_name": "resync.txt",
            "doc_version": "v1",
            "doc_type": "REFERENCE",
            "status": "ACTIVE",
            "effective_from": "",
            "effective_to": "",
            "extracted_text": "Wake-up latency is 80ms and validation passed.",
        }
        missing_context: dict[str, object] = {}

        with patch("app.services.ai_copilot.INDEX_PAYLOAD_DIR", self.index_dir):
            self.assertFalse(
                _remote_evidence_index_contract_ready(
                    [knowledge_doc],
                    profile_id=self.config.profile_id,
                    context=missing_context,
                )
            )
            sync_knowledge_document_index("project-1", knowledge_doc, config=self.config)
            self.assertTrue(
                _remote_evidence_index_contract_ready(
                    [knowledge_doc],
                    profile_id=self.config.profile_id,
                    context={},
                )
            )
            stale_context: dict[str, object] = {}
            self.assertFalse(
                _remote_evidence_index_contract_ready(
                    [
                        {
                            **knowledge_doc,
                            "extracted_text": "Wake-up latency is 180ms and validation failed.",
                        }
                    ],
                    profile_id=self.config.profile_id,
                    context=stale_context,
                )
            )

        self.assertEqual(missing_context["retrieval_migration_action"], EVIDENCE_INDEX_RESYNC_ACTION)
        self.assertEqual(stale_context["retrieval_migration_action"], EVIDENCE_INDEX_RESYNC_ACTION)

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

    def test_azure_search_sdk_indexer_deletes_existing_chunks_for_source_doc_before_upload(self) -> None:
        config = dataclasses.replace(
            self.config,
            retrieval_backend="azure_sdk",
            azure_search_endpoint="https://search.example.search.windows.net",
            azure_search_index_name="reqpilot-knowledge",
            azure_openai_endpoint="https://openai.example.openai.azure.com",
            azure_openai_embedding_deployment="text-embedding-3-large",
            azure_openai_embedding_dimensions=3,
        )
        chunk_id = "kb-2_paragraph_1"
        content = "Supplier submitted the EMC validation evidence package."
        content_hash = compute_evidence_content_hash(content)
        chunks = [
            {
                "chunk_id": chunk_id,
                "evidence_id": chunk_id,
                "content": content,
                "content_hash": content_hash,
                "evidence_contract_version": EVIDENCE_INDEX_CONTRACT_VERSION,
                "meta": {
                    "source_doc_id": "kb-2",
                    "source_doc_name": "knowledge.txt",
                    "source_location": "Chunk 1",
                    "evidence_id": chunk_id,
                    "content_hash": content_hash,
                    "evidence_contract_version": EVIDENCE_INDEX_CONTRACT_VERSION,
                },
            }
        ]
        search_client = Mock()
        search_client.search.side_effect = [
            [{"chunk_id": "kb-2_old"}, {"chunk_id": "kb-2_paragraph_1"}],
            [],
            [{"chunk_id": chunk_id}],
        ]
        search_client.delete_documents.return_value = [
            SimpleNamespace(succeeded=True),
            SimpleNamespace(succeeded=True),
        ]
        search_client.upload_documents.return_value = [SimpleNamespace(succeeded=True)]
        documents_module = ModuleType("azure.search.documents")
        documents_module.SearchClient = Mock(return_value=search_client)
        indexes_module = ModuleType("azure.search.documents.indexes")
        indexes_module.SearchIndexClient = Mock(return_value=Mock())

        with patch.dict(
            sys.modules,
            {
                "azure": ModuleType("azure"),
                "azure.search": ModuleType("azure.search"),
                "azure.search.documents": documents_module,
                "azure.search.documents.indexes": indexes_module,
            },
        ), patch(
            "app.services.ai_copilot._ensure_azure_search_index"
        ), patch(
            "app.services.ai_copilot._azure_search_credential",
            return_value=sentinel.credential,
        ), patch(
            "app.services.ai_copilot._embed_texts",
            return_value=[[0.1, 0.2, 0.3]],
        ):
            uploaded = AzureSearchSDKIndexer(config).upsert_chunks(chunks)

        self.assertEqual(uploaded, 1)
        self.assertEqual(search_client.search.call_count, 3)
        for observed_call in search_client.search.call_args_list:
            self.assertEqual(
                observed_call,
                call(
                    search_text="*",
                    filter="source_doc_id eq 'kb-2'",
                    select=["chunk_id"],
                ),
            )
        search_client.delete_documents.assert_called_once_with(
            documents=[{"chunk_id": "kb-2_old"}, {"chunk_id": "kb-2_paragraph_1"}],
        )
        search_client.upload_documents.assert_called_once()

    def test_azure_haystack_compatibility_profile_uses_sdk_evidence_contract(
        self,
    ) -> None:
        chunks = [{"evidence_id": "controlled-chunk"}]
        context = {"requirement_id": "REQ-1"}
        knowledge_docs = [{"id": "knowledge-1"}]

        with patch(
            "app.services.ai_copilot.AzureSearchSDKIndexer.replace_chunks",
            return_value=1,
        ) as replace_chunks:
            written = AzureHaystackIndexer(self.config).replace_chunks(chunks)
        with patch(
            "app.services.ai_copilot.AzureSearchSDKEvidenceRetriever.retrieve",
            return_value=[{"evidence_id": "controlled-chunk"}],
        ) as retrieve:
            evidences = AzureHaystackEvidenceRetriever(self.config).retrieve(
                context,
                knowledge_docs,
                3,
            )

        self.assertEqual(written, 1)
        self.assertEqual(evidences, [{"evidence_id": "controlled-chunk"}])
        replace_chunks.assert_called_once_with(chunks)
        retrieve.assert_called_once_with(context, knowledge_docs, 3)

    def test_azure_source_document_delete_consumes_all_results_in_bounded_batches(
        self,
    ) -> None:
        old_ids = [f"kb-large_old_{index}" for index in range(1001)]
        search_client = Mock()
        search_client.search.side_effect = [
            [{"chunk_id": chunk_id} for chunk_id in old_ids],
            [],
        ]
        search_client.delete_documents.side_effect = [
            [SimpleNamespace(succeeded=True) for _ in range(1000)],
            [SimpleNamespace(succeeded=True)],
        ]

        _delete_existing_azure_chunks_for_source_docs(
            search_client,
            [{"meta": {"source_doc_id": "kb-large"}}],
        )

        self.assertEqual(search_client.delete_documents.call_count, 2)
        self.assertEqual(
            len(
                search_client.delete_documents.call_args_list[0].kwargs[
                    "documents"
                ]
            ),
            1000,
        )
        self.assertEqual(
            len(
                search_client.delete_documents.call_args_list[1].kwargs[
                    "documents"
                ]
            ),
            1,
        )
        for search_call in search_client.search.call_args_list:
            self.assertNotIn("top", search_call.kwargs)

    def test_dashvector_indexer_replaces_source_document_and_verifies_ids(
        self,
    ) -> None:
        class FakeResponse:
            def __init__(self, output: object) -> None:
                self.output = output
                self.message = ""

            def __bool__(self) -> bool:
                return True

        class FakeDoc:
            def __init__(
                self,
                *,
                id: str,
                vector: list[float],
                fields: dict,
            ) -> None:
                self.id = id
                self.vector = vector
                self.fields = fields

        knowledge_doc = {
            "id": "kb-dashvector-replace",
            "project_id": "project-1",
            "file_name": "dashvector.txt",
            "doc_version": "v1",
            "doc_type": "REFERENCE",
            "status": "ACTIVE",
            "extracted_text": "Validation result confirms the controlled output passed.",
        }
        chunks = build_knowledge_chunks("project-1", knowledge_doc)
        expected_ids = {str(chunk["evidence_id"]) for chunk in chunks}
        collection = Mock()
        collection.query.side_effect = [
            FakeResponse([SimpleNamespace(id="stale-chunk")]),
            FakeResponse([]),
        ]
        collection.delete.return_value = FakeResponse([])
        collection.upsert.return_value = FakeResponse(
            [SimpleNamespace(id=chunk_id) for chunk_id in expected_ids]
        )
        collection.fetch.return_value = FakeResponse(
            {chunk_id: SimpleNamespace(id=chunk_id) for chunk_id in expected_ids}
        )
        dashvector_module = ModuleType("dashvector")
        dashvector_module.Doc = FakeDoc
        config = dataclasses.replace(
            self.config,
            retrieval_backend="dashvector",
            dashvector_collection_name="reqpilot-knowledge",
        )

        with patch.dict(sys.modules, {"dashvector": dashvector_module}), patch(
            "app.services.ai_copilot._dashvector_collection",
            return_value=collection,
        ), patch(
            "app.services.ai_copilot._embed_texts",
            return_value=[[0.1, 0.2, 0.3] for _ in chunks],
        ):
            written = DashVectorIndexer(config).replace_chunks(chunks)

        self.assertEqual(written, len(chunks))
        collection.delete.assert_called_once_with(
            ids=["stale-chunk"],
            partition=None,
        )
        collection.fetch.assert_called_once()
        self.assertEqual(
            set(collection.fetch.call_args.kwargs["ids"]),
            expected_ids,
        )

    def test_qdrant_indexer_replaces_source_document_and_verifies_ids(
        self,
    ) -> None:
        knowledge_doc = {
            "id": "kb-qdrant-replace",
            "project_id": "project-1",
            "file_name": "qdrant.txt",
            "doc_version": "v1",
            "doc_type": "REFERENCE",
            "status": "ACTIVE",
            "extracted_text": "Validation result confirms the controlled output passed.",
        }
        chunks = build_knowledge_chunks("project-1", knowledge_doc)
        expected_ids = {str(chunk["evidence_id"]) for chunk in chunks}
        client = Mock()
        client.count.return_value = SimpleNamespace(count=0)
        client.scroll.return_value = (
            [
                SimpleNamespace(
                    payload={"evidence_id": evidence_id},
                )
                for evidence_id in expected_ids
            ],
            None,
        )
        config = dataclasses.replace(
            self.config,
            retrieval_backend="qdrant",
            qdrant_url="http://127.0.0.1:6333",
            qdrant_collection_name="reqpilot-knowledge",
        )

        with patch(
            "app.services.ai_copilot._qdrant_client",
            return_value=client,
        ), patch(
            "app.services.ai_copilot._ensure_qdrant_collection",
        ), patch(
            "app.services.ai_copilot._embed_texts",
            return_value=[[0.1, 0.2, 0.3] for _ in chunks],
        ):
            written = QdrantIndexer(config).replace_chunks(chunks)

        self.assertEqual(written, len(chunks))
        client.delete.assert_called_once()
        client.count.assert_called_once()
        client.upsert.assert_called_once()
        client.scroll.assert_called_once()

    def test_azure_decision_threshold_uses_azure_search_score_scale(self) -> None:
        self.assertEqual(_decision_evidence_threshold_for_backend("azure_sdk"), 0.03)
        self.assertEqual(_decision_evidence_threshold_for_backend("azure_haystack"), 0.03)

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
            chat_model="qwen3.7-plus",
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
            chat_model="qwen3.7-plus",
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
        content = "Latency target is 150ms."
        evidence = _azure_search_item_to_evidence(
            {
                "evidence_id": "kb-1_paragraph_1",
                "content_hash": compute_evidence_content_hash(content),
                "evidence_contract_version": EVIDENCE_INDEX_CONTRACT_VERSION,
                "source_doc_id": "kb-1",
                "source_doc_name": "knowledge.txt",
                "source_doc_version": "2026.03",
                "source_location": "Chunk 1",
                "content": content,
                "@search.reranker_score": 2.0,
            }
        )

        self.assertEqual(evidence["source_doc_id"], "kb-1")
        self.assertEqual(evidence["relevance"], 0.5)
        self.assertEqual(evidence["evidence_id"], "kb-1_paragraph_1")
        self.assertTrue(evidence["evidence_contract_verified"])

    def test_azure_search_item_to_evidence_exposes_chunk_identity_and_score_source(self) -> None:
        evidence = _azure_search_item_to_evidence(
            {
                "chunk_id": "kb-1_paragraph_1",
                "source_doc_id": "kb-1",
                "source_doc_name": "knowledge.txt",
                "source_doc_version": "2026.03",
                "source_location": "Chunk 1",
                "content": "Latency target is 150ms.",
                "@search.reranker_score": 2.0,
            }
        )

        self.assertIn("chunk_id", evidence)
        self.assertIn("score_source", evidence)
        self.assertEqual(evidence["chunk_id"], "kb-1_paragraph_1")
        self.assertEqual(evidence["score_source"], "azure_reranker")
        self.assertEqual(evidence["relevance"], 0.5)

    def test_build_llm_messages_includes_evidence_slot_and_chunk_id(self) -> None:
        context = {
            "requirement_id": "req-1",
            "normalized_requirement": "Supplier shall provide EMC validation evidence.",
            "requirement_text": "Supplier shall provide EMC validation evidence.",
        }
        messages = build_llm_messages(
            context,
            [
                {
                    "chunk_id": "kb-1_paragraph_1",
                    "source_doc_id": "kb-1",
                    "source_doc_name": "knowledge.txt",
                    "source_doc_version": "2026.03",
                    "source_location": "Chunk 1",
                    "excerpt": "Supplier submitted the EMC validation evidence package.",
                    "relevance": 0.71,
                    "score_source": "azure_reranker",
                }
            ],
            retrieval_backend="azure_sdk",
        )

        payload = json.loads(messages[1]["content"])
        evidence = payload["candidate_evidences"][0]
        self.assertEqual(evidence["slot"], 1)
        self.assertIn("evidence_slot", evidence)
        self.assertIn("chunk_id", evidence)
        self.assertIn("score_source", evidence)
        self.assertEqual(evidence["evidence_slot"], 1)
        self.assertEqual(evidence["chunk_id"], "kb-1_paragraph_1")
        self.assertEqual(evidence["score_source"], "azure_reranker")
        self.assertEqual(payload["decision_rules"]["no_effective_evidence_below"], 0.03)
        self.assertEqual(payload["decision_rules"]["decision_requires_top_evidence_at_least"], 0.03)

    def test_validate_evidence_references_allows_chunk_id_match_when_excerpt_is_rewritten(self) -> None:
        self.assertIn("chunk_id", AssessmentEvidenceSchema.model_fields)
        cited = [
            AssessmentEvidenceSchema(
                source_doc_id="kb-1",
                source_doc_name="knowledge.txt",
                source_location="Chunk 1",
                excerpt="EMC evidence package was submitted by the supplier.",
                relevance=0.71,
                source_doc_version="2026.03",
                chunk_id="kb-1_paragraph_1",
            )
        ]
        retrieved = [
            _with_evidence_contract(
                {
                "chunk_id": "kb-1_paragraph_1",
                "source_doc_id": "kb-1",
                "source_doc_name": "knowledge.txt",
                "source_location": "Chunk 1",
                "excerpt": "Supplier submitted the EMC validation evidence package on 2026-05-01.",
                "relevance": 0.71,
                "source_doc_version": "2026.03",
                },
                evidence_id="kb-1_paragraph_1",
            )
        ]

        self.assertEqual(_validate_evidence_references(cited, retrieved), "")

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
        retrieved = [_with_evidence_contract(item) for item in retrieved]

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
        evidences = [_with_evidence_contract(item) for item in evidences]

        result = fallback_need_clarification(context=context, evidences=evidences, fallback_reason="invalid_output")

        self.assertEqual(result["ai_decision"], "NEED_CLARIFICATION")
        self.assertEqual(result["evidences"][0]["source_doc_id"], "kb-3")
        self.assertEqual(result["fallback_reason"], "invalid_output")

    def test_run_ai_copilot_assessment_llm_shadow_serves_heuristic_when_decisions_conflict(self) -> None:
        llm_shadow_config = self._azure_mode_config("llm_shadow")
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
        retrieved_evidences = [_with_evidence_contract(item) for item in retrieved_evidences]

        heuristic_screen = Mock(
            return_value={"analysis_label": "REQUIREMENT", "reason": "规则识别到明确约束，应继续分析。"}
        )
        heuristic_assess = Mock(
            return_value=_assessment_candidate(
                retrieved_evidences[0],
                decision="ACCEPT",
                reason="规则证据满足该约束。",
                confidence=0.84,
                risk_level="LOW",
            )
        )
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
            return_value=_assessment_candidate(
                retrieved_evidences[0],
                decision="REJECT",
                reason="LLM 与规则结论冲突。",
                confidence=0.99,
                risk_level="HIGH",
            ),
        ):
            result = run_ai_copilot_assessment(row, [{"id": "kb-1"}], config=llm_shadow_config)

        assessment = result["assessment"]
        self.assertEqual(assessment["assessment_mode"], "heuristic")
        self.assertEqual(assessment["ai_decision"], "ACCEPT")
        self.assertEqual(assessment["ai_reason"], "规则证据满足该约束。")
        self.assertEqual(assessment["ai_confidence"], 0.84)
        self.assertEqual(assessment["model_name"], "rules-rag-prototype")
        self.assertIn("最终结论采用启发式规则", assessment["analysis_basis"])
        self.assertNotIn("LLM", assessment["analysis_basis"])
        self.assertIn("检索后端 local", assessment["analysis_basis"])
        self.assertEqual(result["evidences"][0]["source_doc_id"], retrieved_evidences[0]["source_doc_id"])
        self.assertEqual(result["evidences"][0]["excerpt"], retrieved_evidences[0]["excerpt"])
        self.assertEqual(
            [run["mode"] for run in result["runs"]],
            ["screening_heuristic", "screening_llm_shadow", "heuristic", "llm_shadow"],
        )
        llm_run = result["runs"][-1]
        self.assertEqual(json.loads(llm_run["output_payload_json"])["proposed_decision"], "REJECT")
        heuristic_screen.assert_called_once()
        heuristic_assess.assert_called_once()

        trace_payload = json.loads(Path(result["trace_path"]).read_text(encoding="utf-8"))
        self.assertEqual(trace_payload["final_result"]["ai_decision"], "ACCEPT")
        self.assertEqual(trace_payload["final_result"]["ai_reason"], "规则证据满足该约束。")
        self.assertEqual([run["mode"] for run in trace_payload["runs"]], [run["mode"] for run in result["runs"]])

    def test_run_ai_copilot_assessment_llm_shadow_serves_heuristic_info_when_llm_says_requirement(self) -> None:
        llm_shadow_config = self._azure_mode_config("llm_shadow")
        row = {
            "id": "req-shadow-info-1",
            "req_code": "REQ-SHADOW-INFO-1",
            "display_text": "temperature Tmin / Tmax lowest and highest ambient values",
            "requirement_type": "PERFORMANCE",
            "parent_section_label": "3.3 Temperature",
        }

        heuristic_screen = Mock(
            return_value={"analysis_label": "INFO", "reason": "规则判定该条目只是温度术语说明。"}
        )
        with patch(
            "app.services.ai_copilot.HeuristicRequirementScreeningEngine.screen",
            heuristic_screen,
        ), patch(
            "app.services.ai_copilot.AzureOpenAIScreeningEngine.screen",
            return_value={"analysis_label": "REQUIREMENT", "reason": "LLM 将该说明误判为需求。"},
        ), patch(
            "app.services.ai_copilot.HeuristicEvidenceRetriever.retrieve",
            side_effect=AssertionError("INFO serving result must skip retrieval"),
        ):
            result = run_ai_copilot_assessment(row, [{"id": "kb-1"}], config=llm_shadow_config)

        assessment = result["assessment"]
        self.assertEqual(assessment["analysis_label"], "INFO")
        self.assertEqual(assessment["ai_decision"], "")
        self.assertIn("规则判定该条目只是温度术语说明", assessment["analysis_basis"])
        self.assertNotIn("LLM 将该说明误判为需求", assessment["analysis_basis"])
        self.assertEqual(
            [run["mode"] for run in result["runs"]],
            ["screening_heuristic", "screening_llm_shadow"],
        )
        self.assertEqual(
            json.loads(result["runs"][1]["output_payload_json"])["analysis_label"],
            "REQUIREMENT",
        )
        heuristic_screen.assert_called_once()

    def test_run_ai_copilot_assessment_llm_shadow_serves_heuristic_requirement_when_llm_says_info(self) -> None:
        llm_shadow_config = self._azure_mode_config("llm_shadow")
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
        retrieved_evidence = _with_evidence_contract(
            {
                "source_doc_id": "kb-1",
                "source_doc_name": "temperature-spec.txt",
                "source_location": "Chunk 3",
                "excerpt": "The component supports continuous operation from Tmin to Tmax and meets the requirement.",
                "relevance": 0.82,
                "source_doc_version": "v3.0",
            }
        )
        heuristic_candidate = _assessment_candidate(
            retrieved_evidence,
            decision="ACCEPT",
            reason="规则证据覆盖连续运行温度边界。",
            confidence=0.87,
            risk_level="LOW",
        )

        heuristic_screen = Mock(
            return_value={"analysis_label": "REQUIREMENT", "reason": "规则识别到连续运行约束。"}
        )
        with patch(
            "app.services.ai_copilot.HeuristicRequirementScreeningEngine.screen",
            heuristic_screen,
        ), patch(
            "app.services.ai_copilot.AzureOpenAIScreeningEngine.screen",
            return_value={"analysis_label": "INFO", "reason": "该条目仅在定义 Tmin/Tmax 的含义，不构成待判定义务。"},
        ), patch(
            "app.services.ai_copilot.HeuristicEvidenceRetriever.retrieve",
            return_value=[retrieved_evidence],
        ), patch(
            "app.services.ai_copilot.HeuristicAssessmentEngine.assess",
            return_value=heuristic_candidate,
        ), patch(
            "app.services.ai_copilot.AzureOpenAIAssessmentEngine.assess",
            return_value=_assessment_candidate(
                retrieved_evidence,
                decision="NEED_CLARIFICATION",
                reason="Shadow screening disagreement remains observational.",
                confidence=0.7,
                risk_level="MEDIUM",
            ),
        ):
            result = run_ai_copilot_assessment(row, [{"id": "kb-1"}], config=llm_shadow_config)

        assessment = result["assessment"]
        self.assertEqual(assessment["analysis_label"], "REQUIREMENT")
        self.assertEqual(assessment["assessment_mode"], "heuristic")
        self.assertEqual(assessment["ai_decision"], "ACCEPT")
        self.assertEqual(assessment["ai_reason"], "规则证据覆盖连续运行温度边界。")
        self.assertIn("规则识别到连续运行约束", assessment["analysis_basis"])
        self.assertNotIn("该条目仅在定义 Tmin/Tmax", assessment["analysis_basis"])
        self.assertEqual(
            [run["mode"] for run in result["runs"]],
            ["screening_heuristic", "screening_llm_shadow", "heuristic", "llm_shadow"],
        )
        self.assertEqual(json.loads(result["runs"][1]["output_payload_json"])["analysis_label"], "INFO")
        heuristic_screen.assert_called_once()

    def test_run_ai_copilot_assessment_llm_shadow_records_screening_error_without_failing_serving_result(self) -> None:
        llm_shadow_config = self._azure_mode_config("llm_shadow")
        row = {
            "id": "req-shadow-failed-screening-1",
            "req_code": "REQ-SHADOW-FAILED-SCREENING-1",
            "display_text": "Supplier shall provide EMC validation evidence.",
            "requirement_type": "FUNCTIONAL",
            "parent_section_label": "5.1 Compliance",
        }
        retrieved_evidence = _with_evidence_contract(
            {
                "source_doc_id": "kb-1",
                "source_doc_name": "emc-validation.txt",
                "source_location": "Chunk 1",
                "excerpt": "The supplier provides EMC validation evidence and meets the project requirement.",
                "relevance": 0.8,
                "source_doc_version": "v1.0",
            }
        )
        heuristic_candidate = _assessment_candidate(
            retrieved_evidence,
            decision="ACCEPT",
            reason="规则证据覆盖 EMC 验证义务。",
            confidence=0.85,
            risk_level="LOW",
        )

        with patch(
            "app.services.ai_copilot.HeuristicRequirementScreeningEngine.screen",
            return_value={"analysis_label": "REQUIREMENT", "reason": "规则识别到明确的 EMC 证据义务。"},
        ), patch(
            "app.services.ai_copilot.AzureOpenAIScreeningEngine.screen",
            side_effect=TimeoutError("Request timed out."),
        ), patch(
            "app.services.ai_copilot.HeuristicEvidenceRetriever.retrieve",
            return_value=[retrieved_evidence],
        ), patch(
            "app.services.ai_copilot.HeuristicAssessmentEngine.assess",
            return_value=heuristic_candidate,
        ), patch(
            "app.services.ai_copilot.AzureOpenAIAssessmentEngine.assess",
            return_value=_llm_assessment_candidate(
                retrieved_evidence,
                statement="Supplier shall provide EMC validation evidence.",
                decision="ACCEPT",
                reason="Shadow output is observation only.",
                confidence=0.95,
                risk_level="LOW",
            ),
        ):
            result = run_ai_copilot_assessment(row, [{"id": "kb-1"}], config=llm_shadow_config)

        assessment = result["assessment"]
        self.assertEqual(assessment["assessment_mode"], "heuristic")
        self.assertEqual(assessment["analysis_label"], "REQUIREMENT")
        self.assertEqual(assessment["ai_decision"], "ACCEPT")
        self.assertEqual(assessment["ai_reason"], "规则证据覆盖 EMC 验证义务。")
        self.assertIn("规则识别到明确的 EMC 证据义务", assessment["analysis_basis"])
        self.assertNotIn("LLM screening failed", assessment["analysis_basis"])
        self.assertEqual(
            [run["mode"] for run in result["runs"]],
            ["screening_heuristic", "screening_llm_shadow", "heuristic", "llm_shadow"],
        )
        self.assertEqual(result["runs"][1]["validation_status"], "failed")
        self.assertEqual(result["runs"][1]["fallback_reason"], "TimeoutError: Request timed out.")
        self.assertEqual(result["runs"][3]["validation_status"], "accepted")

    def test_run_ai_copilot_assessment_llm_shadow_records_assessment_error_without_failing_serving_result(self) -> None:
        llm_shadow_config = self._azure_mode_config("llm_shadow")
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
        retrieved_evidences = [_with_evidence_contract(item) for item in retrieved_evidences]
        heuristic_candidate = _assessment_candidate(
            retrieved_evidences[0],
            decision="ACCEPT",
            reason="规则证据证明唤醒时间满足要求。",
            confidence=0.81,
            risk_level="LOW",
        )
        heuristic_candidate["normalized_requirement"] = row["display_text"]

        with patch(
            "app.services.ai_copilot.HeuristicRequirementScreeningEngine.screen",
            return_value={"analysis_label": "REQUIREMENT", "reason": "规则识别到明确的唤醒时间约束。"},
        ), patch(
            "app.services.ai_copilot.HeuristicAssessmentEngine.assess",
            return_value=heuristic_candidate,
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
        self.assertEqual(assessment["assessment_mode"], "heuristic")
        self.assertEqual(assessment["analysis_label"], "REQUIREMENT")
        self.assertEqual(assessment["ai_decision"], "ACCEPT")
        self.assertEqual(assessment["ai_reason"], "规则证据证明唤醒时间满足要求。")
        self.assertEqual(assessment["ai_confidence"], 0.81)
        self.assertEqual(result["evidences"][0]["source_doc_id"], retrieved_evidences[0]["source_doc_id"])
        self.assertEqual(result["evidences"][0]["excerpt"], retrieved_evidences[0]["excerpt"])
        self.assertNotIn("LLM requirement analysis failed", assessment["analysis_basis"])
        self.assertEqual(
            [run["mode"] for run in result["runs"]],
            ["screening_heuristic", "screening_llm_shadow", "heuristic", "llm_shadow"],
        )
        llm_run = result["runs"][-1]
        self.assertEqual(llm_run["validation_status"], "failed")
        self.assertEqual(llm_run["fallback_reason"], "ConnectionError: Connection error.")

        trace_payload = json.loads(Path(result["trace_path"]).read_text(encoding="utf-8"))
        self.assertEqual(trace_payload["final_result"]["ai_decision"], "ACCEPT")
        self.assertEqual(trace_payload["runs"][-1]["validation_status"], "failed")

    def test_run_ai_copilot_assessment_llm_shadow_records_invalid_outputs_without_serving_them(self) -> None:
        llm_shadow_config = self._azure_mode_config("llm_shadow")
        row = {
            "id": "req-shadow-invalid-1",
            "req_code": "REQ-SHADOW-INVALID-1",
            "display_text": "System shall support secure diagnostics.",
            "requirement_type": "FUNCTIONAL",
        }
        retrieved_evidence = _with_evidence_contract(
            {
                "source_doc_id": "kb-1",
                "source_doc_name": "diagnostics-spec.txt",
                "source_location": "Chunk 2",
                "excerpt": "The platform supports secure diagnostics and meets the access-control requirement.",
                "relevance": 0.78,
                "source_doc_version": "v2.0",
            }
        )
        heuristic_candidate = _assessment_candidate(
            retrieved_evidence,
            decision="ACCEPT",
            reason="规则证据覆盖安全诊断要求。",
            confidence=0.86,
            risk_level="LOW",
        )

        with patch(
            "app.services.ai_copilot.HeuristicRequirementScreeningEngine.screen",
            return_value={"analysis_label": "REQUIREMENT", "reason": "规则识别到明确的诊断义务。"},
        ), patch(
            "app.services.ai_copilot.AzureOpenAIScreeningEngine.screen",
            return_value={"analysis_label": "MAYBE", "reason": ""},
        ), patch(
            "app.services.ai_copilot.HeuristicEvidenceRetriever.retrieve",
            return_value=[retrieved_evidence],
        ), patch(
            "app.services.ai_copilot.HeuristicAssessmentEngine.assess",
            return_value=heuristic_candidate,
        ), patch(
            "app.services.ai_copilot.AzureOpenAIAssessmentEngine.assess",
            return_value={"proposed_decision": "REJECT"},
        ):
            result = run_ai_copilot_assessment(row, [{"id": "kb-1"}], config=llm_shadow_config)

        assessment = result["assessment"]
        self.assertEqual(assessment["assessment_mode"], "heuristic")
        self.assertEqual(assessment["ai_decision"], "ACCEPT")
        self.assertEqual(assessment["ai_reason"], "规则证据覆盖安全诊断要求。")
        self.assertEqual(assessment["ai_confidence"], 0.86)
        self.assertEqual(
            [run["validation_status"] for run in result["runs"]],
            ["accepted", "fallback", "accepted", "fallback"],
        )
        self.assertIn("screening_schema_failed", result["runs"][1]["fallback_reason"])
        self.assertIn("reason_missing", result["runs"][3]["fallback_reason"])
        self.assertNotIn("schema", assessment["analysis_basis"].lower())

    def test_run_ai_copilot_assessment_llm_primary_fails_closed_when_screening_errors(self) -> None:
        llm_primary_config = dataclasses.replace(
            self.config,
            assessment_mode="llm_primary",
            retrieval_backend="azure_sdk",
            azure_search_endpoint="https://search.example.search.windows.net",
            azure_search_index_name="reqpilot-knowledge",
            azure_openai_endpoint="https://openai.example.openai.azure.com",
            azure_openai_chat_deployment="gpt-5.4",
            azure_openai_embedding_deployment="text-embedding-3-large",
            azure_openai_embedding_dimensions=1536,
        )
        row = {
            "id": "req-primary-failed-screening-1",
            "req_code": "REQ-PRIMARY-FAILED-SCREENING-1",
            "display_text": "Supplier shall provide cybersecurity validation evidence.",
            "requirement_type": "REGULATION",
            "parent_section_label": "5.1 Cybersecurity",
        }
        connection_error = ConnectionError("Connection error.")
        connection_error.__cause__ = ssl.SSLError("UNEXPECTED_EOF_WHILE_READING")

        with patch(
            "app.services.ai_copilot.HeuristicRequirementScreeningEngine.screen",
            side_effect=AssertionError("heuristic screening should not run in Azure llm_primary mode"),
        ), patch(
            "app.services.ai_copilot.AzureOpenAIScreeningEngine.screen",
            side_effect=connection_error,
        ), patch(
            "app.services.ai_copilot.HeuristicEvidenceRetriever.retrieve",
            side_effect=AssertionError("retrieval should not run after screening failure"),
        ), patch(
            "app.services.ai_copilot.AzureOpenAIAssessmentEngine.assess",
            side_effect=AssertionError("assessment should not run after screening failure"),
        ):
            result = run_ai_copilot_assessment(row, [{"id": "kb-1"}], config=llm_primary_config)

        assessment = result["assessment"]
        self.assertEqual(assessment["assessment_mode"], "screening_failed")
        self.assertEqual(assessment["analysis_label"], "REQUIREMENT")
        self.assertEqual(assessment["ai_decision"], "")
        self.assertEqual(result["evidences"], [])
        self.assertIn("LLM screening failed; analysis stopped.", assessment["analysis_basis"])
        self.assertEqual(len(result["runs"]), 1)
        self.assertEqual(result["runs"][0]["mode"], "screening_llm_primary")
        self.assertEqual(result["runs"][0]["validation_status"], "failed")
        self.assertIn("SSLError: ('UNEXPECTED_EOF_WHILE_READING',)", result["runs"][0]["fallback_reason"])

    def test_run_ai_copilot_assessment_llm_primary_fails_closed_when_assessment_errors(self) -> None:
        llm_primary_config = dataclasses.replace(
            self.config,
            assessment_mode="llm_primary",
            retrieval_backend="local",
            azure_search_endpoint="https://search.example.search.windows.net",
            azure_search_index_name="reqpilot-knowledge",
            azure_openai_endpoint="https://openai.example.openai.azure.com",
            azure_openai_chat_deployment="gpt-5.4",
            azure_openai_embedding_deployment="text-embedding-3-large",
            azure_openai_embedding_dimensions=1536,
        )
        row = {
            "id": "req-primary-failed-analysis-1",
            "req_code": "REQ-PRIMARY-FAILED-ANALYSIS-1",
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
            side_effect=AssertionError("heuristic screening should not run in Azure llm_primary mode"),
        ), patch(
            "app.services.ai_copilot.HeuristicAssessmentEngine.assess",
            side_effect=AssertionError("heuristic assessment should not run in Azure llm_primary mode"),
        ), patch(
            "app.services.ai_copilot.AzureOpenAIScreeningEngine.screen",
            return_value={"analysis_label": "REQUIREMENT", "reason": "该条目表达了明确可验证义务。"},
        ), patch(
            "app.services.ai_copilot.HeuristicEvidenceRetriever.retrieve",
            return_value=retrieved_evidences,
        ), patch(
            "app.services.ai_copilot.AzureOpenAIAssessmentEngine.assess",
            side_effect=ConnectionError("Azure OpenAI assessment failed."),
        ):
            result = run_ai_copilot_assessment(row, [{"id": "kb-1"}], config=llm_primary_config)

        assessment = result["assessment"]
        self.assertEqual(assessment["assessment_mode"], "analysis_failed")
        self.assertEqual(assessment["analysis_label"], "REQUIREMENT")
        self.assertEqual(assessment["ai_decision"], "")
        self.assertEqual(result["evidences"], [])
        self.assertIn("LLM requirement analysis failed; analysis stopped.", assessment["analysis_basis"])
        self.assertEqual({run["mode"] for run in result["runs"]}, {"screening_llm_primary", "llm_primary"})
        llm_run = next(run for run in result["runs"] if run["mode"] == "llm_primary")
        self.assertEqual(llm_run["validation_status"], "failed")

    def test_run_ai_copilot_assessment_azure_retrieval_misconfig_does_not_use_heuristic_retrieval(self) -> None:
        llm_shadow_config = dataclasses.replace(
            self.config,
            assessment_mode="llm_shadow",
            retrieval_backend="azure_sdk",
            azure_search_endpoint="",
            azure_search_index_name="reqpilot-knowledge",
            azure_openai_endpoint="https://openai.example.openai.azure.com",
            azure_openai_chat_deployment="gpt-5.4",
            azure_openai_embedding_deployment="",
            azure_openai_embedding_dimensions=1536,
        )
        row = {
            "id": "req-azure-retrieval-misconfig-1",
            "req_code": "REQ-AZURE-RETRIEVAL-MISCONFIG-1",
            "display_text": "Supplier shall provide EMC validation evidence.",
            "requirement_type": "REGULATION",
            "parent_section_label": "5.1 Compliance",
        }

        with patch(
            "app.services.ai_copilot.AzureOpenAIScreeningEngine.screen",
            return_value={"analysis_label": "REQUIREMENT", "reason": "该条目表达了明确可验证义务。"},
        ), patch(
            "app.services.ai_copilot.HeuristicEvidenceRetriever.retrieve",
            side_effect=AssertionError("heuristic retrieval should not run for an Azure retrieval backend"),
        ), patch(
            "app.services.ai_copilot.AzureOpenAIAssessmentEngine.assess",
            side_effect=AssertionError("assessment should not run when Azure retrieval is not configured"),
        ):
            result = run_ai_copilot_assessment(row, [{"id": "kb-1"}], config=llm_shadow_config)

        assessment = result["assessment"]
        self.assertEqual(assessment["assessment_mode"], "analysis_failed")
        self.assertEqual(assessment["analysis_label"], "REQUIREMENT")
        self.assertEqual(assessment["ai_decision"], "")
        self.assertEqual(result["evidences"], [])
        self.assertIn("Azure retrieval runtime is not configured", assessment["analysis_basis"])
        self.assertEqual(
            {run["mode"] for run in result["runs"]},
            {"screening_heuristic", "screening_llm_shadow"},
        )

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
        retrieved_evidences = [_with_evidence_contract(item) for item in retrieved_evidences]

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

    def test_run_ai_copilot_assessment_llm_primary_still_serves_llm_result(self) -> None:
        llm_primary_config = self._azure_mode_config("llm_primary")
        row = {
            "id": "req-primary-1",
            "req_code": "REQ-PRIMARY-1",
            "display_text": "System shall support secure diagnostics.",
            "requirement_type": "FUNCTIONAL",
        }
        retrieved_evidence = _with_evidence_contract(
            {
                "source_doc_id": "kb-1",
                "source_doc_name": "diagnostics-spec.txt",
                "source_location": "Chunk 2",
                "excerpt": "The platform supports secure diagnostics and meets the access-control requirement.",
                "relevance": 0.78,
                "source_doc_version": "v2.0",
            }
        )
        llm_candidate = _llm_assessment_candidate(
            retrieved_evidence,
            statement="System shall support secure diagnostics.",
            decision="ACCEPT",
            reason="LLM primary accepted the direct evidence.",
            confidence=0.92,
            risk_level="LOW",
        )

        with patch(
            "app.services.ai_copilot.HeuristicRequirementScreeningEngine.screen",
            side_effect=AssertionError("llm_primary should keep its screening path"),
        ), patch(
            "app.services.ai_copilot.AzureOpenAIScreeningEngine.screen",
            return_value={"analysis_label": "REQUIREMENT", "reason": "LLM primary screening result."},
        ), patch(
            "app.services.ai_copilot.HeuristicEvidenceRetriever.retrieve",
            return_value=[retrieved_evidence],
        ), patch(
            "app.services.ai_copilot.HeuristicAssessmentEngine.assess",
            side_effect=AssertionError("heuristic fallback should not run after a successful primary result"),
        ), patch(
            "app.services.ai_copilot.AzureOpenAIAssessmentEngine.assess",
            return_value=llm_candidate,
        ):
            result = run_ai_copilot_assessment(row, [{"id": "kb-1"}], config=llm_primary_config)

        assessment = result["assessment"]
        self.assertEqual(assessment["assessment_mode"], "llm_primary")
        self.assertEqual(assessment["ai_decision"], "ACCEPT")
        self.assertEqual(assessment["ai_reason"], "LLM primary accepted the direct evidence.")
        self.assertEqual(assessment["ai_confidence"], 0.92)
        self.assertEqual(assessment["model_name"], "gpt-5.4")
        self.assertEqual(
            [run["mode"] for run in result["runs"]],
            ["screening_llm_primary", "llm_primary"],
        )

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
                "REQPILOT_ALIYUN_CHAT_MODEL": "qwen3.7-plus",
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
                "REQPILOT_ALIYUN_CHAT_MODEL": "qwen3.7-plus",
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
        self.assertEqual(config.screening_model, "qwen3.7-flash")

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
            chat_model="qwen3.7-plus",
            screening_model="qwen3.6-flash",
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
            chat_model="qwen3.7-plus",
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
        fake_client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )
        aliyun_config = dataclasses.replace(
            self.config,
            provider="aliyun",
            profile_id="aliyun",
            chat_model="qwen3.7-plus",
            dashscope_api_key="aliyun-key",
            dashscope_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        )

        with patch(
            "app.services.ai_copilot._aliyun_openai_client", return_value=fake_client
        ):
            with self.assertRaises(TimeoutError):
                _dashscope_json_completion(
                    aliyun_config,
                    [{"role": "user", "content": "return json"}],
                )

        self.assertEqual(create.call_count, 1)

    def test_runtime_aliyun_probe_calls_each_model_ocr_and_embedding_exactly_once(
        self,
    ) -> None:
        chat_create = Mock(
            return_value=SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="OK"))]
            )
        )
        embedding_create = Mock(
            return_value=SimpleNamespace(
                data=[SimpleNamespace(index=0, embedding=[0.1, 0.2, 0.3])]
            )
        )
        fake_client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=chat_create)),
            embeddings=SimpleNamespace(create=embedding_create),
        )
        config = dataclasses.replace(
            self.config,
            provider="aliyun",
            profile_id="aliyun",
            chat_model="qwen3.8-max",
            screening_model="qwen3.7-flash",
            embedding_model="text-embedding-v4",
            visual_text_model="qwen3.5-ocr",
            dashscope_api_key="secret-key-never-returned",
            dashscope_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        )

        ocr_provider = Mock()
        ocr_provider.recognize_page.return_value = object()
        with (
            patch(
                "app.services.ai_copilot._aliyun_openai_client",
                return_value=fake_client,
            ),
            patch.object(
                ai_copilot_module,
                "BailianVisualTextProvider",
                create=True,
                return_value=ocr_provider,
            ) as ocr_provider_type,
            patch.object(
                ai_copilot_module,
                "get_runtime_ai_config_value",
                return_value="late-runtime-model-must-not-be-read",
            ),
        ):
            result = probe_runtime_aliyun_models(config)

        self.assertEqual(chat_create.call_count, 2)
        self.assertEqual(embedding_create.call_count, 1)
        ocr_provider_type.assert_called_once()
        self.assertEqual(
            ocr_provider_type.call_args.kwargs["model"], "qwen3.5-ocr"
        )
        self.assertEqual(ocr_provider_type.call_args.kwargs["max_retries"], 0)
        self.assertLessEqual(
            ocr_provider_type.call_args.kwargs["timeout_seconds"], 15.0
        )
        self.assertEqual(ocr_provider.recognize_page.call_count, 1)
        self.assertEqual(
            [item.kwargs["model"] for item in chat_create.call_args_list],
            ["qwen3.7-flash", "qwen3.8-max"],
        )
        self.assertEqual(result["screening"]["state"], "READY")
        self.assertEqual(result["assessment"]["state"], "READY")
        self.assertEqual(result["ocr"]["state"], "READY")
        self.assertEqual(result["ocr"]["model"], "qwen3.5-ocr")
        self.assertEqual(result["ocr"]["request_count"], 1)
        self.assertEqual(result["ocr"]["message"], "Qwen OCR 文字定位探测成功")
        self.assertEqual(result["embedding"]["dimensions"], 3)
        serialized = json.dumps(result)
        self.assertNotIn("secret-key-never-returned", serialized)
        self.assertNotIn("synthetic connection", serialized)

    def test_runtime_aliyun_ocr_probe_failure_is_content_free(self) -> None:
        config = dataclasses.replace(
            self.config,
            provider="aliyun",
            profile_id="aliyun",
            dashscope_api_key="secret-key-never-returned",
            dashscope_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        )
        ocr_provider = Mock()
        ocr_provider.recognize_page.side_effect = VisualTextProviderRejected(
            VisualTextProviderRejectionCode.PROVIDER_FAILED,
            "secret-provider-body https://dashscope.aliyuncs.com/private",
        )
        with patch.object(
            ai_copilot_module,
            "BailianVisualTextProvider",
            create=True,
            return_value=ocr_provider,
        ):
            result = ai_copilot_module._probe_runtime_aliyun_ocr(config)

        self.assertEqual(result["state"], "UNREACHABLE")
        self.assertEqual(result["role"], "ocr")
        self.assertEqual(result["request_count"], 1)
        serialized = json.dumps(result)
        self.assertNotIn("secret-provider-body", serialized)
        self.assertNotIn("secret-key-never-returned", serialized)
        self.assertNotIn("dashscope.aliyuncs.com", serialized)

    def test_runtime_aliyun_probe_maps_quota_error_without_echoing_provider_body(
        self,
    ) -> None:
        class FakeQuotaError(Exception):
            status_code = 400
            code = "AllocationQuota.FreeTierOnly"

        fake_client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=Mock(
                        side_effect=FakeQuotaError(
                            "AllocationQuota.FreeTierOnly secret-provider-detail"
                        )
                    )
                )
            ),
            embeddings=SimpleNamespace(
                create=Mock(
                    side_effect=FakeQuotaError(
                        "AllocationQuota.FreeTierOnly secret-provider-detail"
                    )
                )
            ),
        )
        config = dataclasses.replace(
            self.config,
            provider="aliyun",
            profile_id="aliyun",
            chat_model="qwen3.8-max",
            screening_model="qwen3.7-flash",
            embedding_model="text-embedding-v4",
            dashscope_api_key="secret-key-never-returned",
            dashscope_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        )

        ocr_provider = Mock()
        ocr_provider.recognize_page.side_effect = VisualTextProviderRejected(
            VisualTextProviderRejectionCode.QUOTA_EXHAUSTED,
            "secret-provider-detail",
        )
        with (
            patch(
                "app.services.ai_copilot._aliyun_openai_client",
                return_value=fake_client,
            ),
            patch.object(
                ai_copilot_module,
                "BailianVisualTextProvider",
                return_value=ocr_provider,
            ),
        ):
            result = probe_runtime_aliyun_models(config)

        for check in result.values():
            self.assertEqual(check["state"], "DEPENDENCY_UNAVAILABLE")
            self.assertEqual(check["reason_code"], "aliyun_quota_unavailable")
        serialized = json.dumps(result)
        self.assertNotIn("secret-provider-detail", serialized)
        self.assertNotIn("secret-key-never-returned", serialized)

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
        retrieved_evidences = [_with_evidence_contract(item) for item in retrieved_evidences]
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

        self.assertEqual(result["assessment"]["assessment_mode"], "heuristic")
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
                "REQPILOT_ALIYUN_CHAT_MODEL": "qwen3.7-plus",
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
                        "chat_model": "qwen3.8-max",
                        "screening_model": "qwen3.7-flash",
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
        self.assertEqual(config.chat_model, "qwen3.8-max")
        self.assertEqual(config.screening_model, "qwen3.7-flash")
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
                    "chat_model": "qwen3.7-plus",
                    "embedding_model": "text-embedding-v4",
                    "qdrant_url": "http://localhost:6333",
                    "qdrant_collection": "reqpilot-knowledge-aliyun-local",
                },
            }
        )

        payload = build_runtime_ai_provider_setup()

        self.assertEqual(payload["default_profile_id"], "aliyun")
        self.assertTrue(payload["aliyun"]["api_key_configured"])
        self.assertEqual(payload["aliyun"]["chat_model"], "qwen3.7-plus")
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
            chat_model="qwen3.7-plus",
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
            chat_model="qwen3.7-plus",
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
            chat_model="qwen3.7-plus",
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
        fake_indexer.replace_chunks.side_effect = lambda chunks: len(chunks)

        with patch("app.services.ai_copilot.INDEX_PAYLOAD_DIR", self.index_dir), patch(
            "app.services.ai_copilot._select_indexer",
            return_value=fake_indexer,
        ):
            sync = sync_knowledge_document_index("project-1", knowledge_doc, config=config)

        self.assertEqual(sync["status"], "READY")
        self.assertEqual(sync["targets"]["aliyun"]["retrieval_backend"], "qdrant")
        self.assertEqual(sync["targets"]["aliyun"]["status"], "READY")
        fake_indexer.replace_chunks.assert_called_once()

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

    def test_ensure_azure_index_contract_runs_create_or_update_path(self) -> None:
        fake_azure = ModuleType("azure")
        fake_azure.__path__ = []
        fake_search = ModuleType("azure.search")
        fake_search.__path__ = []
        fake_documents = ModuleType("azure.search.documents")
        fake_documents.__path__ = []
        fake_indexes = ModuleType("azure.search.documents.indexes")
        search_index_client = Mock(return_value=sentinel.index_client)
        fake_indexes.SearchIndexClient = search_index_client
        fake_azure.search = fake_search
        fake_search.documents = fake_documents
        fake_documents.indexes = fake_indexes

        with patch.dict(
            "sys.modules",
            {
                "azure": fake_azure,
                "azure.search": fake_search,
                "azure.search.documents": fake_documents,
                "azure.search.documents.indexes": fake_indexes,
            },
            clear=False,
        ), patch(
            "app.services.ai_copilot._azure_search_credential",
            return_value=sentinel.credential,
        ), patch(
            "app.services.ai_copilot._ensure_azure_search_index",
        ) as ensure_index:
            _ensure_azure_index_contract(self.config)

        search_index_client.assert_called_once_with(
            endpoint=self.config.azure_search_endpoint,
            credential=sentinel.credential,
        )
        ensure_index.assert_called_once_with(sentinel.index_client, self.config)

    def test_azure_index_schema_contains_assurance_and_native_locator_fields(
        self,
    ) -> None:
        index_client = Mock()

        _ensure_azure_search_index(index_client, self.config)

        search_index = index_client.create_or_update_index.call_args.args[0]
        field_names = {str(field.name) for field in search_index.fields}
        self.assertTrue(
            {
                "authority",
                "claim_role",
                "applicability",
                "native_locator",
                "native_format",
                "native_page",
                "native_table_index",
                "native_body_element_index",
                "native_workbook",
                "native_sheet",
                "native_range",
            }.issubset(field_names)
        )

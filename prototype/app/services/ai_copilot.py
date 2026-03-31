from __future__ import annotations

import json
import os
import re
import uuid
from datetime import date
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from app.services.assessor import (
    COMPLIANCE_HINTS,
    MANDATORY_HINTS,
    MODEL_NAME as HEURISTIC_MODEL_NAME,
    NEGATIVE_PATTERNS,
    PROMPT_VERSION as HEURISTIC_PROMPT_VERSION,
    assess_requirement,
)
from app.services.rag import score_candidate_evidences


VALID_DECISIONS = ("ACCEPT", "REJECT", "DEVIATION_ACCEPT", "NEED_CLARIFICATION")
VALID_RISK_LEVELS = ("LOW", "MEDIUM", "HIGH")
VALID_ANALYSIS_LABELS = ("REQUIREMENT", "INFO")
DEFAULT_PROMPT_VERSION = "v2.0-azure-rag-copilot"
DEFAULT_SCREENING_PROMPT_VERSION = "v2.1-requirement-screening"
DEFAULT_HEURISTIC_MODEL = HEURISTIC_MODEL_NAME
DEFAULT_TOP_K = 5
DEFAULT_AZURE_OPENAI_API_VERSION = "2024-10-21"
DEFAULT_AZURE_OPENAI_SCOPE = "https://cognitiveservices.azure.com/.default"
DEFAULT_AZURE_SEARCH_VECTOR_PROFILE = "reqpilot-vector-profile"
DEFAULT_AZURE_SEARCH_VECTOR_ALGORITHM = "reqpilot-hnsw"
DEFAULT_AZURE_SEARCH_SEMANTIC_CONFIGURATION = "reqpilot-semantic"
MANUAL_OVERRIDE_MODEL_NAME = "manual-override"
MANUAL_OVERRIDE_PROMPT_VERSION = "v1.0-manual-analysis-label-override"

APP_DIR = Path(__file__).resolve().parent
PROTOTYPE_DIR = APP_DIR.parent.parent
STORAGE_DIR = PROTOTYPE_DIR / "storage"
TRACE_DIR = STORAGE_DIR / "ai_traces"
INDEX_PAYLOAD_DIR = STORAGE_DIR / "knowledge_index"


class AssessmentEvidenceSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_doc_id: str
    source_doc_name: str
    source_location: str
    excerpt: str
    relevance: float
    source_doc_version: str


class ScreeningOutputSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    analysis_label: Literal["REQUIREMENT", "INFO"]
    reason: str


class AssessmentOutputSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    normalized_requirement: str
    proposed_decision: Literal["ACCEPT", "REJECT", "DEVIATION_ACCEPT", "NEED_CLARIFICATION"]
    confidence: float
    reason: str
    risk_level: Literal["LOW", "MEDIUM", "HIGH"]
    clarification_question: str
    evidences: list[AssessmentEvidenceSchema]


@dataclass(frozen=True)
class AICopilotConfig:
    assessment_mode: str
    retrieval_backend: str
    top_k: int
    trace_dir: Path
    azure_auth_mode: str
    azure_allow_interactive_login: bool
    azure_search_endpoint: str
    azure_search_api_key: str
    azure_search_index_name: str
    azure_openai_endpoint: str
    azure_openai_api_key: str
    azure_openai_api_version: str
    azure_openai_chat_deployment: str
    azure_openai_embedding_deployment: str
    azure_openai_embedding_dimensions: int
    azure_search_semantic_configuration: str
    llm_timeout_seconds: float

    @property
    def llm_enabled(self) -> bool:
        return self.assessment_mode in {"llm_shadow", "llm_primary"}

    @property
    def uses_api_key_for_openai(self) -> bool:
        return self.azure_auth_mode != "rbac" and bool(self.azure_openai_api_key)

    @property
    def uses_api_key_for_search(self) -> bool:
        return self.azure_auth_mode != "rbac" and bool(self.azure_search_api_key)

    @property
    def uses_rbac_for_openai(self) -> bool:
        return self.azure_auth_mode == "rbac" or (self.azure_auth_mode == "auto" and not self.azure_openai_api_key)

    @property
    def uses_rbac_for_search(self) -> bool:
        return self.azure_auth_mode == "rbac" or (self.azure_auth_mode == "auto" and not self.azure_search_api_key)

    @property
    def azure_openai_chat_ready(self) -> bool:
        return bool(
            self.azure_openai_endpoint
            and self.azure_openai_chat_deployment
            and (self.uses_api_key_for_openai or self.uses_rbac_for_openai)
        )

    @property
    def azure_openai_embedding_ready(self) -> bool:
        return bool(
            self.azure_openai_endpoint
            and self.azure_openai_embedding_deployment
            and (self.uses_api_key_for_openai or self.uses_rbac_for_openai)
        )

    @property
    def azure_search_ready(self) -> bool:
        return bool(
            self.azure_search_endpoint
            and self.azure_search_index_name
            and (self.uses_api_key_for_search or self.uses_rbac_for_search)
        )

    @property
    def azure_ready(self) -> bool:
        return self.azure_openai_chat_ready and self.azure_openai_embedding_ready and self.azure_search_ready


class EvidenceRetriever(Protocol):
    backend_name: str

    def retrieve(self, context: dict[str, Any], knowledge_docs: list[dict[str, Any]], top_k: int) -> list[dict[str, Any]]:
        ...


class AssessmentEngine(Protocol):
    model_name: str
    prompt_version: str

    def assess(self, context: dict[str, Any], evidences: list[dict[str, Any]]) -> dict[str, Any]:
        ...


class ScreeningEngine(Protocol):
    model_name: str
    prompt_version: str

    def screen(self, context: dict[str, Any]) -> dict[str, Any]:
        ...


def load_ai_copilot_config() -> AICopilotConfig:
    requested_mode = str(os.getenv("REQPILOT_AI_ASSESSMENT_MODE") or "heuristic").strip().lower()
    if requested_mode not in {"heuristic", "llm_shadow", "llm_primary"}:
        requested_mode = "heuristic"

    azure_auth_mode = str(os.getenv("REQPILOT_AZURE_AUTH_MODE") or "auto").strip().lower()
    if azure_auth_mode not in {"auto", "api_key", "rbac"}:
        azure_auth_mode = "auto"
    allow_interactive_login = str(os.getenv("REQPILOT_AZURE_ALLOW_INTERACTIVE_LOGIN") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

    azure_search_api_key = str(os.getenv("AZURE_AI_SEARCH_API_KEY") or "").strip()
    azure_openai_api_key = str(os.getenv("AZURE_OPENAI_API_KEY") or "").strip()
    requested_backend = str(os.getenv("REQPILOT_RETRIEVAL_BACKEND") or "").strip().lower()
    if not requested_backend:
        if requested_mode == "heuristic":
            requested_backend = "local"
        elif azure_auth_mode == "rbac" or (azure_auth_mode == "auto" and not azure_search_api_key):
            requested_backend = "azure_sdk"
        else:
            requested_backend = "azure_haystack"
    if requested_backend not in {"local", "azure_haystack", "azure_sdk"}:
        requested_backend = "local"

    try:
        top_k = max(1, int(os.getenv("REQPILOT_AI_TOP_K") or DEFAULT_TOP_K))
    except ValueError:
        top_k = DEFAULT_TOP_K

    try:
        embedding_dimensions = max(1, int(os.getenv("AZURE_OPENAI_EMBEDDING_DIMENSIONS") or 1536))
    except ValueError:
        embedding_dimensions = 1536

    try:
        llm_timeout_seconds = max(5.0, float(os.getenv("REQPILOT_AI_TIMEOUT_SECONDS") or 45.0))
    except ValueError:
        llm_timeout_seconds = 45.0

    trace_dir = Path(str(os.getenv("REQPILOT_AI_TRACE_DIR") or TRACE_DIR))
    return AICopilotConfig(
        assessment_mode=requested_mode,
        retrieval_backend=requested_backend,
        top_k=top_k,
        trace_dir=trace_dir,
        azure_auth_mode=azure_auth_mode,
        azure_allow_interactive_login=allow_interactive_login,
        azure_search_endpoint=str(os.getenv("AZURE_AI_SEARCH_ENDPOINT") or "").strip(),
        azure_search_api_key=azure_search_api_key,
        azure_search_index_name=str(os.getenv("AZURE_AI_SEARCH_INDEX_NAME") or "reqpilot-knowledge").strip()
        or "reqpilot-knowledge",
        azure_openai_endpoint=str(os.getenv("AZURE_OPENAI_ENDPOINT") or "").strip(),
        azure_openai_api_key=azure_openai_api_key,
        azure_openai_api_version=str(os.getenv("AZURE_OPENAI_API_VERSION") or DEFAULT_AZURE_OPENAI_API_VERSION).strip()
        or DEFAULT_AZURE_OPENAI_API_VERSION,
        azure_openai_chat_deployment=str(os.getenv("AZURE_OPENAI_CHAT_DEPLOYMENT") or "").strip(),
        azure_openai_embedding_deployment=str(os.getenv("AZURE_OPENAI_EMBEDDING_DEPLOYMENT") or "").strip(),
        azure_openai_embedding_dimensions=embedding_dimensions,
        azure_search_semantic_configuration=str(os.getenv("AZURE_AI_SEARCH_SEMANTIC_CONFIGURATION") or "").strip(),
        llm_timeout_seconds=llm_timeout_seconds,
    )


def run_ai_copilot_assessment(
    requirement_row: dict[str, Any],
    knowledge_docs: list[dict[str, Any]],
    *,
    config: AICopilotConfig | None = None,
    forced_analysis_label: str | None = None,
    forced_analysis_basis: str = "",
) -> dict[str, Any]:
    config = config or load_ai_copilot_config()
    context = build_assessment_context(requirement_row)
    collector = AssessmentRunCollector(config=config, context=context, retrieval_backend="screening_only")
    screening = _run_requirement_screening(
        context=context,
        config=config,
        collector=collector,
        forced_analysis_label=forced_analysis_label,
        forced_analysis_basis=forced_analysis_basis,
    )
    if screening["analysis_label"] == "INFO":
        return _finalize_assessment_result(
            build_info_assessment_result(context=context, screening=screening),
            [],
            collector,
            final_mode="screening_only",
            final_model_name=screening["model_name"],
            final_prompt_version=screening["prompt_version"],
        )
    if not knowledge_docs:
        return _finalize_assessment_result(
            build_screening_only_requirement_result(context=context, screening=screening),
            [],
            collector,
            final_mode="screening_only",
            final_model_name=screening["model_name"],
            final_prompt_version=screening["prompt_version"],
        )

    retriever = _select_retriever(config)
    collector.retrieval_backend = retriever.backend_name
    evidences = retriever.retrieve(context, knowledge_docs, top_k=config.top_k)
    heuristics = HeuristicAssessmentEngine()

    if config.assessment_mode == "heuristic":
        heuristic_result = heuristics.assess(context, evidences)
        final_result = reconcile_assessment_output(
            context=context,
            candidate=heuristic_result,
            retrieved_evidences=evidences,
            fallback_reason="",
        )
        final_result["analysis_basis"] = screening["reason"]
        collector.record_result(
            mode="heuristic",
            model_name=heuristics.model_name,
            prompt_version=heuristics.prompt_version,
            input_payload={"context": context, "evidences": evidences},
            output_payload=heuristic_result,
            validation_status=final_result["validation_status"],
            fallback_reason=final_result["fallback_reason"],
            retrieval_backend=retriever.backend_name,
        )
        return _finalize_assessment_result(
            final_result,
            evidences,
            collector,
            final_mode="heuristic",
            final_model_name=heuristics.model_name,
            final_prompt_version=heuristics.prompt_version,
        )

    llm_engine = AzureOpenAIAssessmentEngine(config)
    if config.assessment_mode == "llm_shadow":
        heuristic_result = heuristics.assess(context, evidences)
        final_result = reconcile_assessment_output(
            context=context,
            candidate=heuristic_result,
            retrieved_evidences=evidences,
            fallback_reason="",
        )
        final_result["analysis_basis"] = screening["reason"]
        collector.record_result(
            mode="heuristic",
            model_name=heuristics.model_name,
            prompt_version=heuristics.prompt_version,
            input_payload={"context": context, "evidences": evidences},
            output_payload=heuristic_result,
            validation_status=final_result["validation_status"],
            fallback_reason=final_result["fallback_reason"],
            retrieval_backend=retriever.backend_name,
        )
        try:
            llm_result = llm_engine.assess(context, evidences)
            llm_final = reconcile_assessment_output(
                context=context,
                candidate=llm_result,
                retrieved_evidences=evidences,
                fallback_reason="",
            )
            llm_final["analysis_basis"] = screening["reason"]
            collector.record_result(
                mode="llm_shadow",
                model_name=llm_engine.model_name,
                prompt_version=llm_engine.prompt_version,
                input_payload={"context": context, "evidences": evidences},
                output_payload=llm_result,
                validation_status=llm_final["validation_status"],
                fallback_reason=llm_final["fallback_reason"],
                retrieval_backend=retriever.backend_name,
            )
        except Exception as exc:  # pragma: no cover - network/provider dependent
            collector.record_failure(
                mode="llm_shadow",
                model_name=llm_engine.model_name,
                prompt_version=llm_engine.prompt_version,
                input_payload={"context": context, "evidences": evidences},
                fallback_reason=str(exc),
                retrieval_backend=retriever.backend_name,
            )
        return _finalize_assessment_result(
            final_result,
            evidences,
            collector,
            final_mode="heuristic",
            final_model_name=heuristics.model_name,
            final_prompt_version=heuristics.prompt_version,
        )

    try:
        llm_result = llm_engine.assess(context, evidences)
        final_result = reconcile_assessment_output(
            context=context,
            candidate=llm_result,
            retrieved_evidences=evidences,
            fallback_reason="",
        )
        final_result["analysis_basis"] = screening["reason"]
        collector.record_result(
            mode="llm_primary",
            model_name=llm_engine.model_name,
            prompt_version=llm_engine.prompt_version,
            input_payload={"context": context, "evidences": evidences},
            output_payload=llm_result,
            validation_status=final_result["validation_status"],
            fallback_reason=final_result["fallback_reason"],
            retrieval_backend=retriever.backend_name,
        )
    except Exception as exc:  # pragma: no cover - network/provider dependent
        heuristic_result = heuristics.assess(context, evidences)
        final_result = reconcile_assessment_output(
            context=context,
            candidate=heuristic_result,
            retrieved_evidences=evidences,
            fallback_reason=f"llm_primary_failed: {exc}",
        )
        final_result["analysis_basis"] = screening["reason"]
        collector.record_failure(
            mode="llm_primary",
            model_name=llm_engine.model_name,
            prompt_version=llm_engine.prompt_version,
            input_payload={"context": context, "evidences": evidences},
            fallback_reason=str(exc),
            retrieval_backend=retriever.backend_name,
        )
        collector.record_result(
            mode="heuristic_fallback",
            model_name=heuristics.model_name,
            prompt_version=heuristics.prompt_version,
            input_payload={"context": context, "evidences": evidences},
            output_payload=heuristic_result,
            validation_status=final_result["validation_status"],
            fallback_reason=final_result["fallback_reason"],
            retrieval_backend=retriever.backend_name,
        )

    final_mode = "llm_primary"
    final_model_name = llm_engine.model_name
    final_prompt_version = llm_engine.prompt_version
    if collector.runs and collector.runs[-1]["mode"] == "heuristic_fallback":
        final_mode = "heuristic_fallback"
        final_model_name = heuristics.model_name
        final_prompt_version = heuristics.prompt_version

    return _finalize_assessment_result(
        final_result,
        evidences,
        collector,
        final_mode=final_mode,
        final_model_name=final_model_name,
        final_prompt_version=final_prompt_version,
    )


def sync_knowledge_document_index(
    project_id: str,
    knowledge_doc: dict[str, Any],
    *,
    config: AICopilotConfig | None = None,
) -> dict[str, Any]:
    config = config or load_ai_copilot_config()
    chunks = build_knowledge_chunks(project_id, knowledge_doc)
    payload_path = write_knowledge_index_payload(knowledge_doc, chunks, config=config)
    status = "READY"
    error = ""
    synced_at = _now_iso_utc()

    if config.azure_search_ready and config.azure_openai_embedding_ready:
        try:
            _select_azure_indexer(config).upsert_chunks(chunks)
        except Exception as exc:  # pragma: no cover - network/provider dependent
            status = "ERROR"
            error = str(exc)
    return {
        "status": status,
        "error": error,
        "synced_at": synced_at,
        "chunk_count": len(chunks),
        "payload_path": str(payload_path),
    }


def build_assessment_context(requirement_row: dict[str, Any]) -> dict[str, Any]:
    requirement_text = str(
        requirement_row.get("raw_text")
        or requirement_row.get("display_text")
        or requirement_row.get("requirement_text")
        or ""
    ).strip()
    normalized_requirement = normalize_requirement_text(requirement_text)
    section_label = str(
        requirement_row.get("parent_section_label")
        or requirement_row.get("parent_section_title")
        or requirement_row.get("parent_section_code")
        or ""
    ).strip()
    content_summary = summarize_content_blocks(requirement_row.get("content_blocks"))
    table_context = summarize_table_refs(requirement_row.get("table_refs"))
    query_parts = [normalized_requirement]
    if section_label:
        query_parts.append(f"章节上下文：{section_label}")
    if content_summary:
        query_parts.append(f"文本上下文：{content_summary}")
    if table_context:
        query_parts.append(f"表格上下文：{table_context}")

    return {
        "requirement_id": str(requirement_row.get("id") or ""),
        "req_code": str(requirement_row.get("req_code") or ""),
        "entry_kind": str(requirement_row.get("entry_kind") or "REQUIREMENT").strip().upper() or "REQUIREMENT",
        "requirement_type": str(requirement_row.get("requirement_type") or "FUNCTIONAL").strip() or "FUNCTIONAL",
        "requirement_text": requirement_text,
        "normalized_requirement": normalized_requirement,
        "parent_section_code": str(requirement_row.get("parent_section_code") or "").strip(),
        "parent_section_title": str(requirement_row.get("parent_section_title") or "").strip(),
        "parent_section_label": section_label,
        "heading_text": str(requirement_row.get("heading_text") or "").strip(),
        "content_blocks": list(requirement_row.get("content_blocks") or []),
        "table_refs": list(requirement_row.get("table_refs") or []),
        "content_summary": content_summary,
        "table_context": table_context,
        "retrieval_query": "\n".join(part for part in query_parts if part),
    }


def summarize_content_blocks(value: object) -> str:
    if not isinstance(value, list):
        return ""
    paragraphs: list[str] = []
    for block in value:
        if not isinstance(block, dict):
            continue
        if str(block.get("kind") or "").strip().lower() != "paragraph":
            continue
        text = normalize_requirement_text(str(block.get("text") or ""))
        if text:
            paragraphs.append(text)
    combined = " ".join(paragraphs[:3]).strip()
    return combined[:700]


def summarize_table_refs(value: object) -> str:
    if not isinstance(value, list):
        return ""
    projections: list[str] = []
    for ref in value:
        if not isinstance(ref, dict):
            continue
        projection = normalize_requirement_text(str(ref.get("slice_ai_projection") or ""))
        title = normalize_requirement_text(str(ref.get("table_title") or ref.get("title") or ""))
        parts = [part for part in (title, projection) if part]
        if parts:
            projections.append(" | ".join(parts))
    combined = " ".join(projections[:2]).strip()
    return combined[:900]


def normalize_requirement_text(text: str) -> str:
    compact = str(text or "").strip()
    compact = re.sub(r"[ \t\r\f\v]+", " ", compact)
    compact = re.sub(r"\s*\n\s*", " ", compact)
    compact = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", compact)
    return compact.strip()


class HeuristicRequirementScreeningEngine:
    model_name = DEFAULT_HEURISTIC_MODEL
    prompt_version = "v1.0-screening-heuristic"

    def screen(self, context: dict[str, Any]) -> dict[str, Any]:
        requirement_text = normalize_requirement_text(str(context.get("requirement_text") or ""))
        lowered = requirement_text.lower()
        requirement_type = str(context.get("requirement_type") or "FUNCTIONAL").strip().upper()
        token_count = len(re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]", requirement_text))
        has_mandatory = any(token in lowered for token in MANDATORY_HINTS) or any(
            token in requirement_text for token in ("应", "必须", "不得", "应当", "需")
        )
        has_requirement_verb = any(
            token in lowered
            for token in ("support", "meet", "comply", "perform", "provide", "allow", "limit", "withstand")
        ) or any(token in requirement_text for token in ("满足", "支持", "符合", "执行", "提供", "允许", "承受", "限制"))

        if not requirement_text:
            return {"analysis_label": "INFO", "reason": "条目内容为空或仅包含占位信息。"}
        if re.search(r"^(note|remark|说明|备注|背景|概述|范围|scope)\b", lowered, re.I):
            return {"analysis_label": "INFO", "reason": "条目更像背景说明，不构成需要判定的需求。"}
        if re.search(r"(for information only|information only|仅供参考|仅供信息|说明性输入)", requirement_text, re.I):
            return {"analysis_label": "INFO", "reason": "条目被明确标记为说明/参考信息，不进入需求判定。"}
        if re.search(r"(输入条件|边界条件|适用范围|工况范围|参考信息|见表|见图|如下所示|refer to|see table|see figure)", requirement_text, re.I):
            return {"analysis_label": "INFO", "reason": "条目主要提供输入条件或引用说明，不是可直接判定的需求。"}
        if requirement_text.endswith((":", "：")):
            return {"analysis_label": "INFO", "reason": "条目表现为引导性标题或说明前缀，不是可直接判定的需求。"}
        if requirement_type != "REGULATION" and not has_mandatory and not has_requirement_verb and token_count <= 22:
            return {"analysis_label": "INFO", "reason": "条目缺少明确约束或动作要求，更像信息标签/输入描述。"}
        return {"analysis_label": "REQUIREMENT", "reason": "条目包含可判定的约束或行为要求，应进入需求分析。"}


class HeuristicEvidenceRetriever:
    backend_name = "local"

    def retrieve(self, context: dict[str, Any], knowledge_docs: list[dict[str, Any]], top_k: int) -> list[dict[str, Any]]:
        query = str(context.get("retrieval_query") or context.get("normalized_requirement") or "").strip()
        candidates: list[dict[str, Any]] = []
        for doc in knowledge_docs:
            project_id = str(doc.get("project_id") or "").strip()
            doc_boost = _knowledge_doc_relevance_boost(doc)
            for chunk in build_knowledge_chunks(project_id, doc):
                meta = dict(chunk.get("meta") or {})
                content_kind = str(meta.get("content_kind") or "").strip().lower()
                relevance_boost = doc_boost * _content_kind_relevance_boost(content_kind, context=context, doc=doc)
                candidates.append(
                    {
                        "source_doc_id": str(meta.get("source_doc_id") or doc.get("id") or ""),
                        "source_doc_name": str(meta.get("source_doc_name") or doc.get("file_name") or ""),
                        "source_doc_version": str(meta.get("source_doc_version") or doc.get("doc_version") or ""),
                        "source_location": str(meta.get("source_location") or chunk.get("chunk_id") or ""),
                        "excerpt": str(chunk.get("content") or "")[:380],
                        "content": str(chunk.get("content") or ""),
                        "content_kind": content_kind,
                        "relevance_boost": relevance_boost,
                    }
                )
        return score_candidate_evidences(query, candidates, top_k=top_k)


class AzureHaystackEvidenceRetriever:
    backend_name = "azure_haystack"

    def __init__(self, config: AICopilotConfig) -> None:
        self.config = config

    def retrieve(self, context: dict[str, Any], knowledge_docs: list[dict[str, Any]], top_k: int) -> list[dict[str, Any]]:
        if not self.config.azure_search_ready or not self.config.azure_openai_embedding_ready:
            raise RuntimeError("Azure AI retrieval is not configured.")

        from haystack.utils.auth import Secret
        from haystack_integrations.components.retrievers.azure_ai_search import AzureAISearchHybridRetriever
        from haystack_integrations.document_stores.azure_ai_search import AzureAISearchDocumentStore

        query = str(context.get("retrieval_query") or context.get("normalized_requirement") or "").strip()
        filters = build_azure_filter(
            project_id=_single_value(knowledge_docs, "project_id"),
            knowledge_docs=knowledge_docs,
        )
        query_embedding = _embed_text(query, config=self.config)
        document_store = AzureAISearchDocumentStore(
            index_name=self.config.azure_search_index_name,
            azure_endpoint=Secret.from_token(self.config.azure_search_endpoint),
            api_key=Secret.from_token(self.config.azure_search_api_key)
            if self.config.azure_search_api_key
            else Secret.from_env_var("AZURE_AI_SEARCH_API_KEY", strict=False),
            embedding_dimension=self.config.azure_openai_embedding_dimensions,
            metadata_fields=_azure_metadata_fields(),
            include_search_metadata=True,
        )
        kwargs: dict[str, Any] = {}
        if self.config.azure_search_semantic_configuration:
            kwargs["semantic_configuration_name"] = self.config.azure_search_semantic_configuration
        retriever = AzureAISearchHybridRetriever(document_store=document_store, top_k=top_k, **kwargs)
        result = retriever.run(
            query=query,
            query_embedding=query_embedding,
            filters=filters,
            top_k=top_k,
        )
        return [
            _document_to_evidence(doc)
            for doc in result.get("documents") or []
            if _document_to_evidence(doc).get("source_doc_id")
        ]


class AzureSearchSDKEvidenceRetriever:
    backend_name = "azure_sdk"

    def __init__(self, config: AICopilotConfig) -> None:
        self.config = config

    def retrieve(self, context: dict[str, Any], knowledge_docs: list[dict[str, Any]], top_k: int) -> list[dict[str, Any]]:
        if not self.config.azure_search_ready or not self.config.azure_openai_embedding_ready:
            raise RuntimeError("Azure AI retrieval is not configured.")

        from azure.search.documents import SearchClient
        from azure.search.documents.models import VectorizedQuery

        query = str(context.get("retrieval_query") or context.get("normalized_requirement") or "").strip()
        query_embedding = _embed_text(query, config=self.config)
        filter_payload = build_azure_filter(
            project_id=_single_value(knowledge_docs, "project_id"),
            knowledge_docs=knowledge_docs,
        )
        filter_text = _azure_filter_to_odata(filter_payload)
        client = SearchClient(
            endpoint=self.config.azure_search_endpoint,
            index_name=self.config.azure_search_index_name,
            credential=_azure_search_credential(self.config),
        )
        search_kwargs: dict[str, Any] = {
            "top": top_k,
            "select": [
                "chunk_id",
                "content",
                "project_id",
                "source_doc_id",
                "source_doc_name",
                "source_doc_version",
                "doc_type",
                "status",
                "effective_from",
                "effective_to",
                "section_code",
                "section_title",
                "source_location",
                "content_kind",
                "table_id",
                "slice_ai_projection",
            ],
            "vector_queries": [
                VectorizedQuery(
                    vector=query_embedding,
                    k_nearest_neighbors=top_k,
                    fields="embedding",
                )
            ],
        }
        if filter_text:
            search_kwargs["filter"] = filter_text
        if self.config.azure_search_semantic_configuration:
            search_kwargs["query_type"] = "semantic"
            search_kwargs["semantic_configuration_name"] = self.config.azure_search_semantic_configuration
            search_kwargs["semantic_query"] = query
        results = client.search(search_text=query, **search_kwargs)
        evidences: list[dict[str, Any]] = []
        for item in results:
            evidences.append(_azure_search_item_to_evidence(item))
        return [item for item in evidences if item.get("source_doc_id")]


class HeuristicAssessmentEngine:
    model_name = DEFAULT_HEURISTIC_MODEL
    prompt_version = HEURISTIC_PROMPT_VERSION

    def assess(self, context: dict[str, Any], evidences: list[dict[str, Any]]) -> dict[str, Any]:
        heuristic = assess_requirement(
            requirement_text=str(context.get("requirement_text") or ""),
            requirement_type=str(context.get("requirement_type") or "FUNCTIONAL"),
            evidences=evidences,
        )
        decision = str(heuristic.get("ai_decision") or "NEED_CLARIFICATION").strip().upper() or "NEED_CLARIFICATION"
        clarification_question = ""
        if decision == "NEED_CLARIFICATION":
            clarification_question = default_clarification_question(context, evidences)
        return {
            "normalized_requirement": str(context.get("normalized_requirement") or ""),
            "proposed_decision": decision,
            "confidence": float(heuristic.get("ai_confidence") or 0.0),
            "reason": str(heuristic.get("ai_reason") or "未能形成可靠判定。").strip(),
            "risk_level": str(heuristic.get("risk_level") or "HIGH").strip().upper() or "HIGH",
            "clarification_question": clarification_question,
            "evidences": [
                {
                    "source_doc_id": str(item.get("source_doc_id") or ""),
                    "source_doc_name": str(item.get("source_doc_name") or ""),
                    "source_location": str(item.get("source_location") or ""),
                    "excerpt": str(item.get("excerpt") or ""),
                    "relevance": float(item.get("relevance") or 0.0),
                    "source_doc_version": str(item.get("source_doc_version") or ""),
                }
                for item in evidences[:3]
            ],
        }


class AzureOpenAIAssessmentEngine:
    prompt_version = DEFAULT_PROMPT_VERSION

    def __init__(self, config: AICopilotConfig) -> None:
        self.config = config
        self.model_name = config.azure_openai_chat_deployment or "azure-openai"

    def assess(self, context: dict[str, Any], evidences: list[dict[str, Any]]) -> dict[str, Any]:
        if not self.config.azure_openai_chat_ready:
            raise RuntimeError("Azure OpenAI is not configured.")
        if not evidences:
            raise RuntimeError("No evidence candidates available for LLM assessment.")
        client = _azure_openai_client(self.config)
        completion = client.beta.chat.completions.parse(
            model=self.config.azure_openai_chat_deployment,
            messages=build_llm_messages(context, evidences),
            response_format=AssessmentOutputSchema,
            temperature=0.0,
        )
        message = completion.choices[0].message
        if getattr(message, "parsed", None) is None:
            refusal = str(getattr(message, "refusal", "") or "").strip()
            raise RuntimeError(refusal or "Azure OpenAI returned no structured assessment payload.")
        return message.parsed.model_dump()


class AzureOpenAIScreeningEngine:
    prompt_version = DEFAULT_SCREENING_PROMPT_VERSION

    def __init__(self, config: AICopilotConfig) -> None:
        self.config = config
        self.model_name = config.azure_openai_chat_deployment or "azure-openai"

    def screen(self, context: dict[str, Any]) -> dict[str, Any]:
        if not self.config.azure_openai_chat_ready:
            raise RuntimeError("Azure OpenAI is not configured.")
        client = _azure_openai_client(self.config)
        completion = client.beta.chat.completions.parse(
            model=self.config.azure_openai_chat_deployment,
            messages=build_screening_messages(context),
            response_format=ScreeningOutputSchema,
            temperature=0.0,
        )
        message = completion.choices[0].message
        if getattr(message, "parsed", None) is None:
            refusal = str(getattr(message, "refusal", "") or "").strip()
            raise RuntimeError(refusal or "Azure OpenAI returned no structured screening payload.")
        return message.parsed.model_dump()


def _run_requirement_screening(
    *,
    context: dict[str, Any],
    config: AICopilotConfig,
    collector: "AssessmentRunCollector",
    forced_analysis_label: str | None = None,
    forced_analysis_basis: str = "",
) -> dict[str, Any]:
    heuristic_engine = HeuristicRequirementScreeningEngine()
    llm_engine = AzureOpenAIScreeningEngine(config)

    forced_label = str(forced_analysis_label or "").strip().upper()
    if forced_label in VALID_ANALYSIS_LABELS:
        reason = str(forced_analysis_basis or "").strip()
        if not reason:
            if forced_label == "INFO":
                reason = "人工覆盖：该条目仅作为信息输入保留。"
            else:
                reason = "人工覆盖：该条目应作为真实需求继续分析。"
        final_result = {
            "analysis_label": forced_label,
            "reason": reason,
            "validation_status": "accepted",
            "fallback_reason": "",
        }
        collector.record_result(
            mode="screening_manual_override",
            model_name=MANUAL_OVERRIDE_MODEL_NAME,
            prompt_version=MANUAL_OVERRIDE_PROMPT_VERSION,
            input_payload={"context": context, "forced_analysis_label": forced_label},
            output_payload={"analysis_label": forced_label, "reason": reason},
            validation_status=final_result["validation_status"],
            fallback_reason=final_result["fallback_reason"],
        )
        return {
            **final_result,
            "final_mode": "manual_override",
            "model_name": MANUAL_OVERRIDE_MODEL_NAME,
            "prompt_version": MANUAL_OVERRIDE_PROMPT_VERSION,
        }

    if config.assessment_mode == "heuristic":
        heuristic_result = heuristic_engine.screen(context)
        final_result = reconcile_screening_output(candidate=heuristic_result, fallback_reason="")
        collector.record_result(
            mode="screening_heuristic",
            model_name=heuristic_engine.model_name,
            prompt_version=heuristic_engine.prompt_version,
            input_payload={"context": context},
            output_payload=heuristic_result,
            validation_status=final_result["validation_status"],
            fallback_reason=final_result["fallback_reason"],
        )
        return {
            **final_result,
            "final_mode": "heuristic",
            "model_name": heuristic_engine.model_name,
            "prompt_version": heuristic_engine.prompt_version,
        }

    if config.assessment_mode == "llm_shadow":
        heuristic_result = heuristic_engine.screen(context)
        final_result = reconcile_screening_output(candidate=heuristic_result, fallback_reason="")
        collector.record_result(
            mode="screening_heuristic",
            model_name=heuristic_engine.model_name,
            prompt_version=heuristic_engine.prompt_version,
            input_payload={"context": context},
            output_payload=heuristic_result,
            validation_status=final_result["validation_status"],
            fallback_reason=final_result["fallback_reason"],
        )
        try:
            llm_result = llm_engine.screen(context)
            llm_final = reconcile_screening_output(candidate=llm_result, fallback_reason="")
            collector.record_result(
                mode="screening_llm_shadow",
                model_name=llm_engine.model_name,
                prompt_version=llm_engine.prompt_version,
                input_payload={"context": context},
                output_payload=llm_result,
                validation_status=llm_final["validation_status"],
                fallback_reason=llm_final["fallback_reason"],
            )
        except Exception as exc:  # pragma: no cover - network/provider dependent
            collector.record_failure(
                mode="screening_llm_shadow",
                model_name=llm_engine.model_name,
                prompt_version=llm_engine.prompt_version,
                input_payload={"context": context},
                fallback_reason=str(exc),
            )
        return {
            **final_result,
            "final_mode": "heuristic",
            "model_name": heuristic_engine.model_name,
            "prompt_version": heuristic_engine.prompt_version,
        }

    try:
        llm_result = llm_engine.screen(context)
        final_result = reconcile_screening_output(candidate=llm_result, fallback_reason="")
        collector.record_result(
            mode="screening_llm_primary",
            model_name=llm_engine.model_name,
            prompt_version=llm_engine.prompt_version,
            input_payload={"context": context},
            output_payload=llm_result,
            validation_status=final_result["validation_status"],
            fallback_reason=final_result["fallback_reason"],
        )
        return {
            **final_result,
            "final_mode": "llm_primary",
            "model_name": llm_engine.model_name,
            "prompt_version": llm_engine.prompt_version,
        }
    except Exception as exc:  # pragma: no cover - network/provider dependent
        collector.record_failure(
            mode="screening_llm_primary",
            model_name=llm_engine.model_name,
            prompt_version=llm_engine.prompt_version,
            input_payload={"context": context},
            fallback_reason=str(exc),
        )
        heuristic_result = heuristic_engine.screen(context)
        final_result = reconcile_screening_output(
            candidate=heuristic_result,
            fallback_reason=f"llm_primary_screening_failed: {exc}",
        )
        collector.record_result(
            mode="screening_heuristic_fallback",
            model_name=heuristic_engine.model_name,
            prompt_version=heuristic_engine.prompt_version,
            input_payload={"context": context},
            output_payload=heuristic_result,
            validation_status=final_result["validation_status"],
            fallback_reason=final_result["fallback_reason"],
        )
        return {
            **final_result,
            "final_mode": "heuristic_fallback",
            "model_name": heuristic_engine.model_name,
            "prompt_version": heuristic_engine.prompt_version,
        }


def reconcile_screening_output(*, candidate: dict[str, Any], fallback_reason: str) -> dict[str, Any]:
    try:
        parsed = ScreeningOutputSchema.model_validate(candidate)
    except Exception as exc:
        return fallback_requirement_screening(
            fallback_reason=f"{fallback_reason}; screening_schema_failed: {exc}".strip("; ")
        )

    if parsed.analysis_label not in VALID_ANALYSIS_LABELS:
        return fallback_requirement_screening(
            fallback_reason=f"{fallback_reason}; invalid_analysis_label".strip("; ")
        )
    if not parsed.reason.strip():
        return fallback_requirement_screening(
            fallback_reason=f"{fallback_reason}; screening_reason_missing".strip("; ")
        )
    return {
        "analysis_label": parsed.analysis_label,
        "reason": parsed.reason.strip(),
        "validation_status": "accepted",
        "fallback_reason": fallback_reason.strip(),
    }


def fallback_requirement_screening(*, fallback_reason: str) -> dict[str, Any]:
    return {
        "analysis_label": "REQUIREMENT",
        "reason": "条目未被识别为说明性信息，继续进入需求判定。",
        "validation_status": "fallback",
        "fallback_reason": fallback_reason.strip() or "screening_fallback",
    }


def reconcile_assessment_output(
    *,
    context: dict[str, Any],
    candidate: dict[str, Any],
    retrieved_evidences: list[dict[str, Any]],
    fallback_reason: str,
) -> dict[str, Any]:
    normalized_requirement = str(candidate.get("normalized_requirement") or context.get("normalized_requirement") or "").strip()
    if not normalized_requirement:
        normalized_requirement = normalize_requirement_text(str(context.get("requirement_text") or ""))

    try:
        parsed = AssessmentOutputSchema.model_validate(candidate)
    except Exception as exc:
        return fallback_need_clarification(
            context=context,
            evidences=retrieved_evidences,
            fallback_reason=f"{fallback_reason}; schema_validation_failed: {exc}".strip("; "),
        )

    validation_error = _validate_candidate_ranges(parsed)
    if validation_error:
        return fallback_need_clarification(
            context=context,
            evidences=retrieved_evidences,
            fallback_reason=f"{fallback_reason}; {validation_error}".strip("; "),
        )

    evidence_validation = _validate_evidence_references(parsed.evidences, retrieved_evidences)
    if evidence_validation:
        return fallback_need_clarification(
            context=context,
            evidences=retrieved_evidences,
            fallback_reason=f"{fallback_reason}; {evidence_validation}".strip("; "),
        )

    if parsed.proposed_decision != "NEED_CLARIFICATION" and not parsed.evidences:
        return fallback_need_clarification(
            context=context,
            evidences=retrieved_evidences,
            fallback_reason=f"{fallback_reason}; missing_evidence".strip("; "),
        )

    if _evidences_conflict(parsed.evidences) and parsed.proposed_decision != "NEED_CLARIFICATION":
        return fallback_need_clarification(
            context=context,
            evidences=retrieved_evidences,
            fallback_reason=f"{fallback_reason}; conflicting_evidences".strip("; "),
        )

    hard_rule_result = _apply_hard_assessment_rules(
        context=context,
        candidate=parsed,
        retrieved_evidences=retrieved_evidences,
        fallback_reason=fallback_reason,
    )
    if hard_rule_result is not None:
        return hard_rule_result

    result = {
        "analysis_label": "REQUIREMENT",
        "analysis_basis": "",
        "ai_decision": parsed.proposed_decision,
        "ai_reason": parsed.reason.strip(),
        "ai_confidence": round(max(0.0, min(1.0, float(parsed.confidence))), 4),
        "risk_level": parsed.risk_level,
        "clarification_question": parsed.clarification_question.strip(),
        "normalized_requirement": normalized_requirement,
        "evidences": [item.model_dump() for item in parsed.evidences[:3]],
        "validation_status": "accepted",
        "fallback_reason": fallback_reason.strip(),
    }
    if result["ai_decision"] == "NEED_CLARIFICATION" and not result["clarification_question"]:
        result["clarification_question"] = default_clarification_question(context, retrieved_evidences)
    return result


def fallback_need_clarification(
    *,
    context: dict[str, Any],
    evidences: list[dict[str, Any]],
    fallback_reason: str,
) -> dict[str, Any]:
    top_evidences = [
        {
            "source_doc_id": str(item.get("source_doc_id") or ""),
            "source_doc_name": str(item.get("source_doc_name") or ""),
            "source_location": str(item.get("source_location") or ""),
            "excerpt": str(item.get("excerpt") or ""),
            "relevance": round(float(item.get("relevance") or 0.0), 4),
            "source_doc_version": str(item.get("source_doc_version") or ""),
        }
        for item in evidences[:3]
    ]
    return {
        "analysis_label": "REQUIREMENT",
        "analysis_basis": "",
        "ai_decision": "NEED_CLARIFICATION",
        "ai_reason": "缺少可直接采用的有效证据，当前只能保留为待澄清。",
        "ai_confidence": 0.22,
        "risk_level": "HIGH",
        "clarification_question": default_clarification_question(context, evidences),
        "normalized_requirement": str(context.get("normalized_requirement") or ""),
        "evidences": top_evidences,
        "validation_status": "fallback",
        "fallback_reason": fallback_reason.strip() or "fallback",
    }


def default_clarification_question(context: dict[str, Any], evidences: list[dict[str, Any]]) -> str:
    requirement_type = str(context.get("requirement_type") or "FUNCTIONAL").strip().upper()
    if requirement_type == "REGULATION":
        return "请补充直接覆盖该法规条目的合规证明、适用边界或发布版本说明。"
    if evidences:
        return "请确认上述证据是否覆盖当前需求的适用工况、版本边界和平台范围。"
    return "请补充可直接覆盖该需求的内部规范、测试结果或历史项目证据。"


class AssessmentRunCollector:
    def __init__(self, *, config: AICopilotConfig, context: dict[str, Any], retrieval_backend: str) -> None:
        self.config = config
        self.context = context
        self.retrieval_backend = retrieval_backend
        self.runs: list[dict[str, Any]] = []

    def record_result(
        self,
        *,
        mode: str,
        model_name: str,
        prompt_version: str,
        input_payload: dict[str, Any],
        output_payload: dict[str, Any],
        validation_status: str,
        fallback_reason: str,
        retrieval_backend: str | None = None,
    ) -> None:
        self.runs.append(
            {
                "mode": mode,
                "retrieval_backend": retrieval_backend or self.retrieval_backend,
                "model_name": model_name,
                "prompt_version": prompt_version,
                "input_payload_json": json.dumps(input_payload, ensure_ascii=False),
                "output_payload_json": json.dumps(output_payload, ensure_ascii=False),
                "validation_status": validation_status,
                "fallback_reason": fallback_reason,
            }
        )

    def record_failure(
        self,
        *,
        mode: str,
        model_name: str,
        prompt_version: str,
        input_payload: dict[str, Any],
        fallback_reason: str,
        retrieval_backend: str | None = None,
    ) -> None:
        self.runs.append(
            {
                "mode": mode,
                "retrieval_backend": retrieval_backend or self.retrieval_backend,
                "model_name": model_name,
                "prompt_version": prompt_version,
                "input_payload_json": json.dumps(input_payload, ensure_ascii=False),
                "output_payload_json": "",
                "validation_status": "failed",
                "fallback_reason": fallback_reason,
            }
        )


def build_knowledge_chunks(project_id: str, knowledge_doc: dict[str, Any]) -> list[dict[str, Any]]:
    extracted_text = str(knowledge_doc.get("extracted_text") or "")
    lines = [normalize_requirement_text(line) for line in extracted_text.splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return []

    sections = _extract_sections(lines)
    chunks: list[dict[str, Any]] = []
    for index, chunk in enumerate(_chunk_lines(lines), start=1):
        section = _section_for_chunk(chunk, sections)
        chunks.append(
            _build_chunk_record(
                project_id=project_id,
                knowledge_doc=knowledge_doc,
                chunk_id=f"{knowledge_doc['id']}:paragraph:{index}",
                content="\n".join(chunk).strip(),
                content_kind="paragraph",
                source_location=f"Chunk {index}",
                section=section,
            )
        )

    for index, table_lines in enumerate(_extract_markdown_tables(lines), start=1):
        section = _section_for_chunk(table_lines, sections)
        chunks.append(
            _build_chunk_record(
                project_id=project_id,
                knowledge_doc=knowledge_doc,
                chunk_id=f"{knowledge_doc['id']}:table:{index}",
                content="\n".join(table_lines).strip(),
                content_kind="table_slice",
                source_location=f"Table {index}",
                section=section,
                slice_ai_projection=" ".join(table_lines).strip(),
            )
        )

    for index, section in enumerate(sections, start=1):
        summary = section.get("summary") or ""
        if not summary:
            continue
        chunks.append(
            _build_chunk_record(
                project_id=project_id,
                knowledge_doc=knowledge_doc,
                chunk_id=f"{knowledge_doc['id']}:section:{index}",
                content=summary,
                content_kind="section_summary",
                source_location=str(section.get("source_location") or f"Section {index}"),
                section=section,
            )
        )
    return chunks


def write_knowledge_index_payload(
    knowledge_doc: dict[str, Any],
    chunks: list[dict[str, Any]],
    *,
    config: AICopilotConfig,
) -> Path:
    INDEX_PAYLOAD_DIR.mkdir(parents=True, exist_ok=True)
    payload_path = INDEX_PAYLOAD_DIR / f"{knowledge_doc['id']}.json"
    payload = {
        "document_id": knowledge_doc["id"],
        "file_name": knowledge_doc.get("file_name"),
        "generated_at": _now_iso_utc(),
        "retrieval_backend": config.retrieval_backend,
        "chunks": chunks,
    }
    payload_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload_path


class AzureHaystackIndexer:
    def __init__(self, config: AICopilotConfig) -> None:
        self.config = config

    def upsert_chunks(self, chunks: list[dict[str, Any]]) -> int:
        if not chunks:
            return 0

        from haystack import Document
        from haystack.document_stores.types import DuplicatePolicy
        from haystack.utils.auth import Secret
        from haystack_integrations.document_stores.azure_ai_search import AzureAISearchDocumentStore

        document_store = AzureAISearchDocumentStore(
            index_name=self.config.azure_search_index_name,
            azure_endpoint=Secret.from_token(self.config.azure_search_endpoint),
            api_key=Secret.from_token(self.config.azure_search_api_key) if self.config.azure_search_api_key else Secret.from_env_var("AZURE_AI_SEARCH_API_KEY", strict=False),
            embedding_dimension=self.config.azure_openai_embedding_dimensions,
            metadata_fields=_azure_metadata_fields(),
            include_search_metadata=True,
        )
        documents = []
        for chunk in chunks:
            embedding = _embed_text(str(chunk.get("content") or ""), config=self.config)
            documents.append(
                Document(
                    id=str(chunk.get("chunk_id") or uuid.uuid4().hex),
                    content=str(chunk.get("content") or ""),
                    embedding=embedding,
                    meta=dict(chunk.get("meta") or {}),
                )
            )
        return document_store.write_documents(documents, policy=DuplicatePolicy.OVERWRITE)


class AzureSearchSDKIndexer:
    def __init__(self, config: AICopilotConfig) -> None:
        self.config = config

    def upsert_chunks(self, chunks: list[dict[str, Any]]) -> int:
        if not chunks:
            return 0

        from azure.search.documents import SearchClient
        from azure.search.documents.indexes import SearchIndexClient

        index_client = SearchIndexClient(
            endpoint=self.config.azure_search_endpoint,
            credential=_azure_search_credential(self.config),
        )
        _ensure_azure_search_index(index_client, self.config)
        search_client = SearchClient(
            endpoint=self.config.azure_search_endpoint,
            index_name=self.config.azure_search_index_name,
            credential=_azure_search_credential(self.config),
        )
        documents = []
        for chunk in chunks:
            documents.append(
                {
                    "chunk_id": str(chunk.get("chunk_id") or uuid.uuid4().hex),
                    "content": str(chunk.get("content") or ""),
                    "embedding": _embed_text(str(chunk.get("content") or ""), config=self.config),
                    **_flatten_azure_chunk_meta(dict(chunk.get("meta") or {})),
                }
            )
        results = search_client.upload_documents(documents)
        return sum(1 for item in results if bool(getattr(item, "succeeded", False)))


def build_llm_messages(context: dict[str, Any], evidences: list[dict[str, Any]]) -> list[dict[str, str]]:
    system_prompt = (
        "你是汽车行业合规评审助手。你的任务是基于给定需求和候选证据，自动输出结构化判定。"
        "你只能引用候选证据，不得编造新文档。若证据不足、冲突或不够直接，必须输出 NEED_CLARIFICATION。"
        "risk_level 只能是 LOW、MEDIUM、HIGH。"
        "clarification_question 必须始终提供；如果无需澄清请返回空字符串。"
    )
    evidence_blocks = []
    for index, item in enumerate(evidences[:5], start=1):
        evidence_blocks.append(
            {
                "slot": index,
                "source_doc_id": str(item.get("source_doc_id") or ""),
                "source_doc_name": str(item.get("source_doc_name") or ""),
                "source_doc_version": str(item.get("source_doc_version") or ""),
                "source_location": str(item.get("source_location") or ""),
                "excerpt": str(item.get("excerpt") or ""),
                "relevance": round(float(item.get("relevance") or 0.0), 4),
            }
        )
    user_payload = {
        "requirement_id": context.get("requirement_id"),
        "req_code": context.get("req_code"),
        "requirement_type": context.get("requirement_type"),
        "normalized_requirement": context.get("normalized_requirement"),
        "raw_requirement_text": context.get("requirement_text"),
        "parent_section_code": context.get("parent_section_code"),
        "parent_section_title": context.get("parent_section_title"),
        "content_summary": context.get("content_summary"),
        "table_context": context.get("table_context"),
        "candidate_evidences": evidence_blocks,
        "decision_rules": {
            "allowed_decisions": list(VALID_DECISIONS),
            "allowed_risk_levels": list(VALID_RISK_LEVELS),
            "must_not_accept_without_evidence": True,
            "must_not_invent_sources": True,
        },
    }
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False, indent=2)},
    ]


def build_screening_messages(context: dict[str, Any]) -> list[dict[str, str]]:
    system_prompt = (
        "你是汽车需求审阅助手。"
        "请判断当前条目是否属于需要进入合规判定的真实需求。"
        "如果条目只是信息输入、背景说明、标题、上下文标签、适用范围说明或引用提示，请返回 INFO。"
        "只有在条目表达了明确约束、行为要求、性能边界、法规符合性或可验证义务时，才返回 REQUIREMENT。"
        "reason 只写一句简洁依据。"
    )
    user_payload = {
        "requirement_id": context.get("requirement_id"),
        "req_code": context.get("req_code"),
        "entry_kind": context.get("entry_kind"),
        "requirement_type": context.get("requirement_type"),
        "raw_requirement_text": context.get("requirement_text"),
        "normalized_requirement": context.get("normalized_requirement"),
        "parent_section_code": context.get("parent_section_code"),
        "parent_section_title": context.get("parent_section_title"),
        "content_summary": context.get("content_summary"),
        "table_context": context.get("table_context"),
        "allowed_labels": list(VALID_ANALYSIS_LABELS),
    }
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False, indent=2)},
    ]


def build_azure_filter(*, project_id: str, knowledge_docs: list[dict[str, Any]]) -> dict[str, Any] | None:
    conditions: list[dict[str, Any]] = []
    if project_id:
        conditions.append({"field": "project_id", "operator": "==", "value": project_id})
    doc_ids = [str(doc.get("id") or "").strip() for doc in knowledge_docs if str(doc.get("id") or "").strip()]
    if doc_ids:
        conditions.append({"field": "source_doc_id", "operator": "in", "value": doc_ids})
    statuses = sorted(
        {
            str(doc.get("status") or "").strip()
            for doc in knowledge_docs
            if str(doc.get("status") or "").strip()
        }
    )
    if statuses:
        conditions.append({"field": "status", "operator": "in", "value": statuses})
    if not conditions:
        return None
    if len(conditions) == 1:
        return conditions[0]
    return {"operator": "AND", "conditions": conditions}


def _single_value(rows: list[dict[str, Any]], key: str) -> str:
    values = [str(item.get(key) or "").strip() for item in rows if str(item.get(key) or "").strip()]
    return values[0] if values else ""


def _finalize_assessment_result(
    final_result: dict[str, Any],
    evidences: list[dict[str, Any]],
    collector: AssessmentRunCollector,
    *,
    final_mode: str,
    final_model_name: str,
    final_prompt_version: str,
) -> dict[str, Any]:
    TRACE_DIR.mkdir(parents=True, exist_ok=True)
    trace_id = str(uuid.uuid4())
    trace_path = collector.config.trace_dir / f"{trace_id}.json"
    collector.config.trace_dir.mkdir(parents=True, exist_ok=True)
    trace_payload = {
        "context": collector.context,
        "retrieval_backend": collector.retrieval_backend,
        "retrieved_evidences": evidences,
        "runs": collector.runs,
        "final_result": final_result,
        "generated_at": _now_iso_utc(),
    }
    trace_path.write_text(json.dumps(trace_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "assessment": {
            "analysis_label": final_result["analysis_label"],
            "analysis_basis": final_result["analysis_basis"],
            "ai_decision": final_result["ai_decision"],
            "ai_reason": final_result["ai_reason"],
            "ai_confidence": final_result["ai_confidence"],
            "risk_level": final_result["risk_level"],
            "clarification_question": final_result["clarification_question"],
            "normalized_requirement": final_result["normalized_requirement"],
            "assessment_mode": final_mode,
            "model_name": final_model_name,
            "prompt_version": final_prompt_version,
        },
        "evidences": final_result["evidences"],
        "runs": collector.runs,
        "trace_path": str(trace_path),
    }


def build_info_assessment_result(*, context: dict[str, Any], screening: dict[str, Any]) -> dict[str, Any]:
    return {
        "analysis_label": "INFO",
        "analysis_basis": screening["reason"],
        "ai_decision": "",
        "ai_reason": "",
        "ai_confidence": 0.0,
        "risk_level": "",
        "clarification_question": "",
        "normalized_requirement": "",
        "evidences": [],
        "validation_status": screening["validation_status"],
        "fallback_reason": screening["fallback_reason"],
    }


def build_screening_only_requirement_result(*, context: dict[str, Any], screening: dict[str, Any]) -> dict[str, Any]:
    return {
        "analysis_label": "REQUIREMENT",
        "analysis_basis": screening["reason"],
        "ai_decision": "",
        "ai_reason": "",
        "ai_confidence": 0.0,
        "risk_level": "",
        "clarification_question": "",
        "normalized_requirement": str(context.get("normalized_requirement") or ""),
        "evidences": [],
        "validation_status": screening["validation_status"],
        "fallback_reason": screening["fallback_reason"],
    }


def _validate_candidate_ranges(candidate: AssessmentOutputSchema) -> str:
    if not 0.0 <= float(candidate.confidence) <= 1.0:
        return "confidence_out_of_range"
    for evidence in candidate.evidences:
        if not 0.0 <= float(evidence.relevance) <= 1.0:
            return "evidence_relevance_out_of_range"
    if not candidate.reason.strip():
        return "reason_missing"
    return ""


def _validate_evidence_references(
    cited: list[AssessmentEvidenceSchema],
    retrieved: list[dict[str, Any]],
) -> str:
    allowed = {
        (
            str(item.get("source_doc_id") or "").strip(),
            str(item.get("source_location") or "").strip(),
            normalize_requirement_text(str(item.get("excerpt") or "")),
        )
        for item in retrieved
    }
    for evidence in cited:
        key = (
            evidence.source_doc_id.strip(),
            evidence.source_location.strip(),
            normalize_requirement_text(evidence.excerpt),
        )
        if key not in allowed:
            return "evidence_reference_out_of_scope"
    return ""


def _evidences_conflict(evidences: list[AssessmentEvidenceSchema]) -> bool:
    has_negative = False
    has_positive = False
    for evidence in evidences:
        excerpt = str(evidence.excerpt or "")
        if _text_has_negative_signal(excerpt):
            has_negative = True
            continue
        if _text_has_positive_signal(excerpt):
            has_positive = True
    return has_negative and has_positive


def _apply_hard_assessment_rules(
    *,
    context: dict[str, Any],
    candidate: AssessmentOutputSchema,
    retrieved_evidences: list[dict[str, Any]],
    fallback_reason: str,
) -> dict[str, Any] | None:
    requirement_type = str(context.get("requirement_type") or "FUNCTIONAL").strip().upper()
    cited_evidences = [item.model_dump() for item in candidate.evidences[:3]]
    top_relevance = max((float(item.get("relevance") or 0.0) for item in cited_evidences), default=0.0)
    has_negative = any(_text_has_negative_signal(str(item.get("excerpt") or "")) for item in cited_evidences)
    has_positive = any(_text_has_positive_signal(str(item.get("excerpt") or "")) for item in cited_evidences)
    has_compliance = any(_text_has_compliance_signal(str(item.get("excerpt") or "")) for item in cited_evidences)
    decision = str(candidate.proposed_decision or "NEED_CLARIFICATION").strip().upper()
    requirement_text = str(context.get("requirement_text") or "")
    has_mandatory = _text_has_mandatory_signal(requirement_text)

    if decision == "REJECT" and not has_negative:
        return fallback_need_clarification(
            context=context,
            evidences=retrieved_evidences,
            fallback_reason=f"{fallback_reason}; reject_without_negative_signal".strip("; "),
        )

    if decision in {"ACCEPT", "DEVIATION_ACCEPT"} and top_relevance < 0.42:
        return fallback_need_clarification(
            context=context,
            evidences=retrieved_evidences,
            fallback_reason=f"{fallback_reason}; positive_decision_low_evidence".strip("; "),
        )

    if decision == "ACCEPT" and has_mandatory and top_relevance < 0.55:
        return fallback_need_clarification(
            context=context,
            evidences=retrieved_evidences,
            fallback_reason=f"{fallback_reason}; mandatory_requirement_low_evidence".strip("; "),
        )

    if requirement_type == "REGULATION" and decision in {"ACCEPT", "DEVIATION_ACCEPT"} and not has_compliance:
        return fallback_need_clarification(
            context=context,
            evidences=retrieved_evidences,
            fallback_reason=f"{fallback_reason}; regulation_without_compliance_signal".strip("; "),
        )

    if decision in {"ACCEPT", "DEVIATION_ACCEPT"} and has_negative and not has_positive and top_relevance >= 0.58:
        return {
            "analysis_label": "REQUIREMENT",
            "analysis_basis": "",
            "ai_decision": "REJECT",
            "ai_reason": "候选证据存在明确不满足或禁配信号，按硬规则收口为 REJECT。",
            "ai_confidence": max(0.55, round(min(0.98, top_relevance), 4)),
            "risk_level": "HIGH",
            "clarification_question": "",
            "normalized_requirement": str(candidate.normalized_requirement or context.get("normalized_requirement") or ""),
            "evidences": cited_evidences,
            "validation_status": "hard_rule_override",
            "fallback_reason": f"{fallback_reason}; hard_rule_negative_reject".strip("; "),
        }
    return None


def _document_to_evidence(document: Any) -> dict[str, Any]:
    meta = getattr(document, "meta", {}) or {}
    score = getattr(document, "score", None)
    return {
        "source_doc_id": str(meta.get("source_doc_id") or ""),
        "source_doc_name": str(meta.get("source_doc_name") or ""),
        "source_location": str(meta.get("source_location") or ""),
        "excerpt": str(getattr(document, "content", "") or "")[:380],
        "relevance": round(float(score if score is not None else meta.get("relevance") or 0.0), 4),
        "source_doc_version": str(meta.get("source_doc_version") or ""),
    }


def _select_retriever(config: AICopilotConfig) -> EvidenceRetriever:
    if config.retrieval_backend == "azure_sdk" and config.azure_search_ready and config.azure_openai_embedding_ready:
        return AzureSearchSDKEvidenceRetriever(config)
    if config.retrieval_backend == "azure_haystack" and config.azure_search_ready and config.azure_openai_embedding_ready:
        if config.uses_rbac_for_search:
            return AzureSearchSDKEvidenceRetriever(config)
        return AzureHaystackEvidenceRetriever(config)
    return HeuristicEvidenceRetriever()


def _select_azure_indexer(config: AICopilotConfig) -> AzureHaystackIndexer | AzureSearchSDKIndexer:
    if config.retrieval_backend == "azure_sdk" or config.uses_rbac_for_search:
        return AzureSearchSDKIndexer(config)
    return AzureHaystackIndexer(config)


def _azure_metadata_fields() -> dict[str, type]:
    return {
        "project_id": str,
        "chunk_id": str,
        "source_doc_id": str,
        "source_doc_name": str,
        "source_doc_version": str,
        "doc_type": str,
        "status": str,
        "effective_from": str,
        "effective_to": str,
        "section_code": str,
        "section_title": str,
        "source_location": str,
        "content_kind": str,
        "table_id": str,
        "slice_ai_projection": str,
    }


@lru_cache(maxsize=2)
def _default_azure_credential(allow_interactive_login: bool):
    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential(exclude_interactive_browser_credential=not allow_interactive_login)


@lru_cache(maxsize=2)
def _azure_openai_token_provider(allow_interactive_login: bool):
    from azure.identity import get_bearer_token_provider

    return get_bearer_token_provider(
        _default_azure_credential(allow_interactive_login),
        DEFAULT_AZURE_OPENAI_SCOPE,
    )


def _normalize_azure_openai_endpoint(endpoint: str) -> str:
    base = str(endpoint or "").rstrip("/")
    for suffix in ("/openai/v1", "/openai/v1/"):
        if base.endswith(suffix):
            return base[: -len(suffix)].rstrip("/")
    return base


def _azure_openai_client(config: AICopilotConfig):
    from openai import AzureOpenAI

    kwargs: dict[str, Any] = {
        "azure_endpoint": _normalize_azure_openai_endpoint(config.azure_openai_endpoint),
        "api_version": config.azure_openai_api_version,
        "timeout": config.llm_timeout_seconds,
    }
    if config.uses_api_key_for_openai:
        kwargs["api_key"] = config.azure_openai_api_key
    else:
        kwargs["azure_ad_token_provider"] = _azure_openai_token_provider(config.azure_allow_interactive_login)
    return AzureOpenAI(**kwargs)


def _azure_search_credential(config: AICopilotConfig):
    from azure.core.credentials import AzureKeyCredential

    if config.uses_api_key_for_search:
        return AzureKeyCredential(config.azure_search_api_key)
    return _default_azure_credential(config.azure_allow_interactive_login)


def _azure_search_item_to_evidence(item: Any) -> dict[str, Any]:
    payload = dict(item)
    score = payload.get("@search.reranker_score")
    if score is None:
        score = payload.get("@search.score")
    return {
        "source_doc_id": str(payload.get("source_doc_id") or ""),
        "source_doc_name": str(payload.get("source_doc_name") or ""),
        "source_location": str(payload.get("source_location") or ""),
        "excerpt": str(payload.get("content") or "")[:380],
        "relevance": round(float(score or 0.0), 4),
        "source_doc_version": str(payload.get("source_doc_version") or ""),
    }


def _odata_quote(value: object) -> str:
    escaped = str(value or "").replace("'", "''")
    return f"'{escaped}'"


def _azure_search_in(field: str, values: list[str], separator: str = "|") -> str:
    joined = separator.join(str(item).replace("'", "''") for item in values)
    return f"search.in({field}, '{joined}', '{separator}')"


def _azure_filter_to_odata(value: dict[str, Any] | None) -> str | None:
    if not value:
        return None
    operator = str(value.get("operator") or "").strip().upper()
    if operator == "AND":
        parts = [_azure_filter_to_odata(item) for item in value.get("conditions") or []]
        parts = [part for part in parts if part]
        return " and ".join(f"({part})" for part in parts) if parts else None

    field = str(value.get("field") or "").strip()
    op = str(value.get("operator") or "").strip()
    raw = value.get("value")
    if not field:
        return None
    if op == "==" and raw is not None:
        return f"{field} eq {_odata_quote(raw)}"
    if op == "in" and isinstance(raw, list):
        items = [str(item).strip() for item in raw if str(item).strip()]
        if not items:
            return None
        return _azure_search_in(field, items)
    return None


def _flatten_azure_chunk_meta(meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "project_id": str(meta.get("project_id") or ""),
        "source_doc_id": str(meta.get("source_doc_id") or ""),
        "source_doc_name": str(meta.get("source_doc_name") or ""),
        "source_doc_version": str(meta.get("source_doc_version") or ""),
        "doc_type": str(meta.get("doc_type") or ""),
        "status": str(meta.get("status") or ""),
        "effective_from": str(meta.get("effective_from") or ""),
        "effective_to": str(meta.get("effective_to") or ""),
        "section_code": str(meta.get("section_code") or ""),
        "section_title": str(meta.get("section_title") or ""),
        "source_location": str(meta.get("source_location") or ""),
        "content_kind": str(meta.get("content_kind") or ""),
        "table_id": str(meta.get("table_id") or ""),
        "slice_ai_projection": str(meta.get("slice_ai_projection") or ""),
    }


def _knowledge_doc_relevance_boost(knowledge_doc: dict[str, Any]) -> float:
    status = str(knowledge_doc.get("status") or "ACTIVE").strip().upper()
    boost = {
        "ACTIVE": 1.0,
        "DRAFT": 0.9,
        "SUPERSEDED": 0.45,
        "ARCHIVED": 0.2,
    }.get(status, 0.9)
    today = date.today().isoformat()
    effective_from = str(knowledge_doc.get("effective_from") or "").strip()
    effective_to = str(knowledge_doc.get("effective_to") or "").strip()
    if effective_from and today < effective_from:
        boost *= 0.75
    if effective_to and today > effective_to:
        boost *= 0.7
    return max(0.1, min(1.2, boost))


def _content_kind_relevance_boost(content_kind: str, *, context: dict[str, Any], doc: dict[str, Any]) -> float:
    boost = {
        "paragraph": 1.0,
        "table_slice": 1.03,
        "section_summary": 0.88,
    }.get(str(content_kind or "").strip().lower(), 1.0)
    if str(context.get("table_context") or "").strip() and str(content_kind or "").strip().lower() == "table_slice":
        boost *= 1.08
    requirement_type = str(context.get("requirement_type") or "").strip().upper()
    doc_type = str(doc.get("doc_type") or "").strip().upper()
    if requirement_type == "REGULATION" and doc_type in {"REGULATION", "STANDARD"}:
        boost *= 1.08
    return max(0.2, min(1.25, boost))


def _text_has_negative_signal(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(pattern.search(lowered) for pattern in NEGATIVE_PATTERNS)


def _text_has_positive_signal(text: str) -> bool:
    lowered = str(text or "").lower()
    if _text_has_negative_signal(lowered):
        return False
    return any(
        hint in lowered
        for hint in (*COMPLIANCE_HINTS, "pass", "passed", "approved", "validated", "合规", "通过", "批准")
    )


def _text_has_compliance_signal(text: str) -> bool:
    lowered = str(text or "").lower()
    if _text_has_negative_signal(lowered):
        return False
    return any(hint in lowered for hint in (*COMPLIANCE_HINTS, "pass", "passed", "approved", "合规", "通过", "批准"))


def _text_has_mandatory_signal(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(token in lowered for token in MANDATORY_HINTS)


def _ensure_azure_search_index(index_client: Any, config: AICopilotConfig) -> None:
    from azure.search.documents.indexes.models import (
        HnswAlgorithmConfiguration,
        SearchField,
        SearchFieldDataType,
        SearchIndex,
        SearchableField,
        SemanticConfiguration,
        SemanticField,
        SemanticPrioritizedFields,
        SemanticSearch,
        SimpleField,
        VectorSearch,
        VectorSearchProfile,
    )

    semantic_name = config.azure_search_semantic_configuration or DEFAULT_AZURE_SEARCH_SEMANTIC_CONFIGURATION
    fields = [
        SimpleField(name="chunk_id", type=SearchFieldDataType.String, key=True, filterable=True),
        SearchableField(name="content", type=SearchFieldDataType.String),
        SearchField(
            name="embedding",
            type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
            searchable=True,
            vector_search_dimensions=config.azure_openai_embedding_dimensions,
            vector_search_profile_name=DEFAULT_AZURE_SEARCH_VECTOR_PROFILE,
        ),
        SimpleField(name="project_id", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="source_doc_id", type=SearchFieldDataType.String, filterable=True),
        SearchableField(name="source_doc_name", type=SearchFieldDataType.String),
        SimpleField(name="source_doc_version", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="doc_type", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="status", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="effective_from", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="effective_to", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="section_code", type=SearchFieldDataType.String, filterable=True),
        SearchableField(name="section_title", type=SearchFieldDataType.String),
        SimpleField(name="source_location", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="content_kind", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="table_id", type=SearchFieldDataType.String, filterable=True),
        SearchableField(name="slice_ai_projection", type=SearchFieldDataType.String),
    ]
    vector_search = VectorSearch(
        profiles=[
            VectorSearchProfile(
                name=DEFAULT_AZURE_SEARCH_VECTOR_PROFILE,
                algorithm_configuration_name=DEFAULT_AZURE_SEARCH_VECTOR_ALGORITHM,
            )
        ],
        algorithms=[HnswAlgorithmConfiguration(name=DEFAULT_AZURE_SEARCH_VECTOR_ALGORITHM)],
    )
    semantic_search = SemanticSearch(
        default_configuration_name=semantic_name,
        configurations=[
            SemanticConfiguration(
                name=semantic_name,
                prioritized_fields=SemanticPrioritizedFields(
                    content_fields=[SemanticField(field_name="content")],
                    keywords_fields=[
                        SemanticField(field_name="source_doc_name"),
                        SemanticField(field_name="section_title"),
                        SemanticField(field_name="slice_ai_projection"),
                    ],
                ),
            )
        ],
    )
    index_client.create_or_update_index(
        SearchIndex(
            name=config.azure_search_index_name,
            fields=fields,
            vector_search=vector_search,
            semantic_search=semantic_search,
        )
    )


def _embed_text(text: str, *, config: AICopilotConfig) -> list[float]:
    if not config.azure_openai_embedding_ready:
        raise RuntimeError("Azure OpenAI embeddings are not configured.")

    client = _azure_openai_client(config)
    payload: dict[str, Any] = {
        "model": config.azure_openai_embedding_deployment,
        "input": text,
    }
    if config.azure_openai_embedding_dimensions > 0:
        payload["dimensions"] = config.azure_openai_embedding_dimensions
    response = client.embeddings.create(**payload)
    return list(response.data[0].embedding)


def _chunk_lines(lines: list[str], *, lines_per_chunk: int = 6, overlap: int = 2) -> list[list[str]]:
    if not lines:
        return []
    chunks: list[list[str]] = []
    step = max(1, lines_per_chunk - overlap)
    start = 0
    while start < len(lines):
        block = lines[start : start + lines_per_chunk]
        if not block:
            break
        if len(" ".join(block)) >= 35:
            chunks.append(block)
        start += step
    return chunks


def _extract_markdown_tables(lines: list[str]) -> list[list[str]]:
    tables: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if line.startswith("|") and line.count("|") >= 2:
            current.append(line)
            continue
        if len(current) >= 2:
            tables.append(current[:])
        current = []
    if len(current) >= 2:
        tables.append(current)
    return tables


def _extract_sections(lines: list[str]) -> list[dict[str, str]]:
    sections: list[dict[str, str]] = []
    pattern = re.compile(r"^(?:([A-Z]:(?:\d+(?:\.\d+)*)?)|(\d+(?:\.\d+){0,5}))\s+(.{1,120})$")
    for index, line in enumerate(lines, start=1):
        match = pattern.match(line)
        if not match:
            continue
        code = (match.group(1) or match.group(2) or "").strip()
        title = (match.group(3) or "").strip()
        summary = " ".join(lines[index : index + 3]).strip()
        sections.append(
            {
                "section_code": code,
                "section_title": title,
                "summary": summary[:500],
                "source_location": f"Line {index}",
            }
        )
    return sections


def _section_for_chunk(chunk: list[str], sections: list[dict[str, str]]) -> dict[str, str]:
    joined = " ".join(chunk)
    for section in sections:
        code = str(section.get("section_code") or "").strip()
        title = str(section.get("section_title") or "").strip()
        if code and code in joined:
            return section
        if title and title in joined:
            return section
    return {}


def _build_chunk_record(
    *,
    project_id: str,
    knowledge_doc: dict[str, Any],
    chunk_id: str,
    content: str,
    content_kind: str,
    source_location: str,
    section: dict[str, str],
    slice_ai_projection: str = "",
) -> dict[str, Any]:
    meta = {
        "project_id": project_id,
        "source_doc_id": str(knowledge_doc.get("id") or ""),
        "source_doc_name": str(knowledge_doc.get("file_name") or ""),
        "source_doc_version": str(knowledge_doc.get("doc_version") or ""),
        "doc_type": str(knowledge_doc.get("doc_type") or "REFERENCE"),
        "status": str(knowledge_doc.get("status") or "ACTIVE"),
        "effective_from": str(knowledge_doc.get("effective_from") or ""),
        "effective_to": str(knowledge_doc.get("effective_to") or ""),
        "section_code": str(section.get("section_code") or ""),
        "section_title": str(section.get("section_title") or ""),
        "source_location": source_location,
        "content_kind": content_kind,
        "table_id": "",
        "slice_ai_projection": slice_ai_projection,
    }
    return {
        "chunk_id": chunk_id,
        "content": content,
        "content_kind": content_kind,
        "meta": meta,
    }


def _now_iso_utc() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()

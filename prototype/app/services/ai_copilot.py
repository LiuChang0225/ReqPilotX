from __future__ import annotations

import json
import math
import os
import re
import uuid
from datetime import date
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from app.runtime_paths import resolve_storage_dir
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
PREFERRED_FINAL_VALIDATION_STATUSES = {"accepted", "hard_rule_override"}
DEFAULT_PROMPT_VERSION = "v2.0-azure-rag-copilot"
DEFAULT_SCREENING_PROMPT_VERSION = "v2.1-requirement-screening"
DEFAULT_HEURISTIC_MODEL = HEURISTIC_MODEL_NAME
DEFAULT_TOP_K = 5
DEFAULT_AZURE_PROFILE_ID = "azure"
DEFAULT_ALIYUN_PROFILE_ID = "aliyun"
DEFAULT_AZURE_OPENAI_API_VERSION = "2024-10-21"
DEFAULT_AZURE_OPENAI_SCOPE = "https://cognitiveservices.azure.com/.default"
DEFAULT_AZURE_SEARCH_VECTOR_PROFILE = "reqpilot-vector-profile"
DEFAULT_AZURE_SEARCH_VECTOR_ALGORITHM = "reqpilot-hnsw"
DEFAULT_AZURE_SEARCH_SEMANTIC_CONFIGURATION = "reqpilot-semantic"
DEFAULT_DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_DASHSCOPE_CHAT_MODEL = "qwen3.5-plus"
DEFAULT_DASHSCOPE_EMBEDDING_MODEL = "tongyi-embedding-vision-flash-2026-03-06"
DEFAULT_DASHVECTOR_COLLECTION_NAME = "reqpilot-knowledge"
DEFAULT_DASHVECTOR_PROTOCOL = "http"
DEFAULT_QDRANT_COLLECTION_NAME = DEFAULT_DASHVECTOR_COLLECTION_NAME
DISABLED_RETRIEVAL_BACKEND = "disabled"
ALIYUN_RETRIEVAL_DISABLED_REASON = "ALIYUN_RETRIEVAL_BACKEND_DISABLED"
ALIYUN_RETRIEVAL_DISABLED_MESSAGE = (
    "Aliyun retrieval backends qdrant, dashvector, and local have been intentionally removed pending a supported remote retrieval implementation."
)
MANUAL_OVERRIDE_MODEL_NAME = "manual-override"
MANUAL_OVERRIDE_PROMPT_VERSION = "v1.0-manual-analysis-label-override"

GENERIC_TABLE_FACET_LABELS = {
    "table",
    "item",
    "items",
    "parameter",
    "parameters",
    "project",
    "requirement",
    "requirements",
    "项目",
    "内容",
    "说明",
    "要求",
}
GENERIC_DESCRIPTOR_TERMS = (
    "试验参数",
    "test parameters",
    "技术要求",
    "technical requirement",
    "试验方法",
    "test method",
    "test methods",
    "试验目的",
    "test aim",
)
NUMERIC_CONSTRAINT_PATTERN = re.compile(
    r"(?:[<>≤≥±]?\s*-?\d+(?:\.\d+)?\s*(?:°C|ms|s|min|h|V|A|mΩ|Ω|kbps|Mbps|%))",
    re.I,
)

APP_DIR = Path(__file__).resolve().parent
PROTOTYPE_DIR = APP_DIR.parent.parent
STORAGE_DIR = resolve_storage_dir(PROTOTYPE_DIR)
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
    profile_id: str = DEFAULT_AZURE_PROFILE_ID
    provider: str = "azure"
    profile_label: str = "Azure"
    chat_model: str = ""
    embedding_model: str = ""
    dashscope_api_key: str = ""
    dashscope_base_url: str = DEFAULT_DASHSCOPE_BASE_URL
    dashscope_embedding_dimensions: int = 0
    dashvector_api_key: str = ""
    dashvector_endpoint: str = ""
    dashvector_collection_name: str = DEFAULT_DASHVECTOR_COLLECTION_NAME
    dashvector_partition_name: str = ""
    dashvector_protocol: str = DEFAULT_DASHVECTOR_PROTOCOL
    qdrant_url: str = ""
    qdrant_api_key: str = ""
    qdrant_collection_name: str = DEFAULT_QDRANT_COLLECTION_NAME

    @property
    def llm_enabled(self) -> bool:
        return self.assessment_mode in {"llm_shadow", "llm_primary"}

    @property
    def display_name(self) -> str:
        return self.profile_label or self.profile_id.title()

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

    @property
    def dashscope_chat_ready(self) -> bool:
        return bool(self.dashscope_api_key and self.dashscope_base_url and self.chat_model)

    @property
    def dashscope_embedding_ready(self) -> bool:
        return bool(self.dashscope_api_key and self.dashscope_base_url and self.embedding_model)

    @property
    def dashvector_ready(self) -> bool:
        return bool(self.dashvector_api_key and self.dashvector_endpoint and self.dashvector_collection_name)

    @property
    def qdrant_ready(self) -> bool:
        return bool(self.qdrant_url and self.qdrant_collection_name)

    @property
    def llm_runtime_ready(self) -> bool:
        if not self.llm_enabled:
            return True
        if self.provider == DEFAULT_ALIYUN_PROFILE_ID:
            return self.dashscope_chat_ready
        return self.azure_openai_chat_ready

    @property
    def retrieval_runtime_ready(self) -> bool:
        if self.provider == DEFAULT_ALIYUN_PROFILE_ID:
            return False
        if self.retrieval_backend == "local":
            return True
        return self.azure_openai_embedding_ready and self.azure_search_ready

    @property
    def provider_ready(self) -> bool:
        return self.llm_runtime_ready and self.retrieval_runtime_ready

    @property
    def auth_mode(self) -> str:
        if self.provider == DEFAULT_ALIYUN_PROFILE_ID:
            return "api_key" if self.dashscope_api_key else "missing_api_key"
        return self.azure_auth_mode


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


def _parse_assessment_mode(value: str, *, default: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"heuristic", "llm_shadow", "llm_primary"}:
        return normalized
    return default


def _parse_positive_int(value: str, *, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(value or default))
    except (TypeError, ValueError):
        return default


def _parse_positive_float(value: str, *, default: float, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(value or default))
    except (TypeError, ValueError):
        return default


def _normalize_dashvector_endpoint(endpoint: str) -> str:
    normalized = str(endpoint or "").strip().rstrip("/")
    if normalized.startswith("https://"):
        return normalized[len("https://") :]
    if normalized.startswith("http://"):
        return normalized[len("http://") :]
    return normalized


def _normalize_qdrant_url(url: str) -> str:
    return str(url or "").strip().rstrip("/")


def _resolve_default_profile_id(profiles: dict[str, AICopilotConfig]) -> str:
    requested = str(os.getenv("REQPILOT_AI_DEFAULT_PROFILE") or "").strip().lower()
    ordered = [requested, DEFAULT_AZURE_PROFILE_ID, DEFAULT_ALIYUN_PROFILE_ID]
    seen: set[str] = set()
    for candidate in ordered:
        if not candidate or candidate in seen or candidate not in profiles:
            continue
        seen.add(candidate)
        if profiles[candidate].provider_ready:
            return candidate
    for candidate in ordered:
        if candidate and candidate in profiles:
            return candidate
    return next(iter(profiles))


def load_ai_profiles() -> dict[str, AICopilotConfig]:
    try:
        top_k = _parse_positive_int(str(os.getenv("REQPILOT_AI_TOP_K") or DEFAULT_TOP_K), default=DEFAULT_TOP_K)
        azure_embedding_dimensions = _parse_positive_int(
            str(os.getenv("AZURE_OPENAI_EMBEDDING_DIMENSIONS") or 1536),
            default=1536,
        )
        aliyun_embedding_dimensions = _parse_positive_int(
            str(os.getenv("REQPILOT_ALIYUN_EMBEDDING_DIMENSIONS") or 0),
            default=0,
            minimum=0,
        )
        llm_timeout_seconds = _parse_positive_float(
            str(os.getenv("REQPILOT_AI_TIMEOUT_SECONDS") or 45.0),
            default=45.0,
            minimum=5.0,
        )
    except ValueError:
        top_k = DEFAULT_TOP_K
        azure_embedding_dimensions = 1536
        aliyun_embedding_dimensions = 0
        llm_timeout_seconds = 45.0

    trace_dir = Path(str(os.getenv("REQPILOT_AI_TRACE_DIR") or TRACE_DIR))

    azure_auth_mode = str(os.getenv("REQPILOT_AZURE_AUTH_MODE") or "auto").strip().lower()
    if azure_auth_mode == "managed_identity":
        azure_auth_mode = "rbac"
    if azure_auth_mode not in {"auto", "api_key", "rbac"}:
        azure_auth_mode = "auto"
    allow_interactive_login = str(os.getenv("REQPILOT_AZURE_ALLOW_INTERACTIVE_LOGIN") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    azure_search_endpoint = str(os.getenv("AZURE_AI_SEARCH_ENDPOINT") or "").strip()
    azure_search_api_key = str(os.getenv("AZURE_AI_SEARCH_API_KEY") or "").strip()
    azure_search_index_name = str(os.getenv("AZURE_AI_SEARCH_INDEX_NAME") or DEFAULT_DASHVECTOR_COLLECTION_NAME).strip() or DEFAULT_DASHVECTOR_COLLECTION_NAME
    azure_openai_endpoint = str(os.getenv("AZURE_OPENAI_ENDPOINT") or "").strip()
    azure_openai_api_key = str(os.getenv("AZURE_OPENAI_API_KEY") or "").strip()
    azure_openai_chat_deployment = str(os.getenv("AZURE_OPENAI_CHAT_DEPLOYMENT") or "").strip()
    azure_openai_embedding_deployment = str(os.getenv("AZURE_OPENAI_EMBEDDING_DEPLOYMENT") or "").strip()
    requested_mode_raw = str(os.getenv("REQPILOT_AI_ASSESSMENT_MODE") or "").strip().lower()
    if requested_mode_raw:
        azure_mode = _parse_assessment_mode(requested_mode_raw, default="heuristic")
    else:
        azure_runtime_configured = bool(
            azure_search_endpoint
            and azure_search_index_name
            and azure_openai_endpoint
            and azure_openai_chat_deployment
            and azure_openai_embedding_deployment
        )
        azure_mode = "llm_shadow" if azure_runtime_configured else "heuristic"
    azure_backend = str(os.getenv("REQPILOT_RETRIEVAL_BACKEND") or "").strip().lower()
    if not azure_backend:
        if azure_mode == "heuristic":
            azure_backend = "local"
        elif azure_auth_mode == "rbac" or (azure_auth_mode == "auto" and not azure_search_api_key):
            azure_backend = "azure_sdk"
        else:
            azure_backend = "azure_haystack"
    if azure_backend not in {"local", "azure_haystack", "azure_sdk"}:
        azure_backend = "local"

    dashscope_api_key = str(
        os.getenv("REQPILOT_ALIYUN_API_KEY")
        or os.getenv("REQPILOT_DASHSCOPE_API_KEY")
        or os.getenv("DASHSCOPE_API_KEY")
        or ""
    ).strip()
    dashscope_base_url = str(
        os.getenv("REQPILOT_DASHSCOPE_BASE_URL")
        or os.getenv("DASHSCOPE_BASE_URL")
        or DEFAULT_DASHSCOPE_BASE_URL
    ).strip() or DEFAULT_DASHSCOPE_BASE_URL
    aliyun_mode = _parse_assessment_mode(
        str(os.getenv("REQPILOT_ALIYUN_ASSESSMENT_MODE") or "llm_shadow"),
        default="llm_shadow",
    )
    aliyun_chat_model = str(os.getenv("REQPILOT_ALIYUN_CHAT_MODEL") or DEFAULT_DASHSCOPE_CHAT_MODEL).strip() or DEFAULT_DASHSCOPE_CHAT_MODEL
    aliyun_embedding_model = str(os.getenv("REQPILOT_ALIYUN_EMBEDDING_MODEL") or DEFAULT_DASHSCOPE_EMBEDDING_MODEL).strip() or DEFAULT_DASHSCOPE_EMBEDDING_MODEL
    aliyun_backend = DISABLED_RETRIEVAL_BACKEND

    profiles = {
        DEFAULT_AZURE_PROFILE_ID: AICopilotConfig(
            profile_id=DEFAULT_AZURE_PROFILE_ID,
            provider=DEFAULT_AZURE_PROFILE_ID,
            profile_label="Azure",
            assessment_mode=azure_mode,
            retrieval_backend=azure_backend,
            top_k=top_k,
            trace_dir=trace_dir,
            azure_auth_mode=azure_auth_mode,
            azure_allow_interactive_login=allow_interactive_login,
            azure_search_endpoint=azure_search_endpoint,
            azure_search_api_key=azure_search_api_key,
            azure_search_index_name=azure_search_index_name,
            azure_openai_endpoint=azure_openai_endpoint,
            azure_openai_api_key=azure_openai_api_key,
            azure_openai_api_version=str(os.getenv("AZURE_OPENAI_API_VERSION") or DEFAULT_AZURE_OPENAI_API_VERSION).strip()
            or DEFAULT_AZURE_OPENAI_API_VERSION,
            azure_openai_chat_deployment=azure_openai_chat_deployment,
            azure_openai_embedding_deployment=azure_openai_embedding_deployment,
            azure_openai_embedding_dimensions=azure_embedding_dimensions,
            azure_search_semantic_configuration=str(os.getenv("AZURE_AI_SEARCH_SEMANTIC_CONFIGURATION") or "").strip(),
            llm_timeout_seconds=llm_timeout_seconds,
            chat_model=azure_openai_chat_deployment,
            embedding_model=azure_openai_embedding_deployment,
        ),
        DEFAULT_ALIYUN_PROFILE_ID: AICopilotConfig(
            profile_id=DEFAULT_ALIYUN_PROFILE_ID,
            provider=DEFAULT_ALIYUN_PROFILE_ID,
            profile_label="Aliyun",
            assessment_mode=aliyun_mode,
            retrieval_backend=aliyun_backend,
            top_k=top_k,
            trace_dir=trace_dir,
            azure_auth_mode="",
            azure_allow_interactive_login=False,
            azure_search_endpoint="",
            azure_search_api_key="",
            azure_search_index_name="",
            azure_openai_endpoint="",
            azure_openai_api_key="",
            azure_openai_api_version=DEFAULT_AZURE_OPENAI_API_VERSION,
            azure_openai_chat_deployment="",
            azure_openai_embedding_deployment="",
            azure_openai_embedding_dimensions=0,
            azure_search_semantic_configuration="",
            llm_timeout_seconds=llm_timeout_seconds,
            chat_model=aliyun_chat_model,
            embedding_model=aliyun_embedding_model,
            dashscope_api_key=dashscope_api_key,
            dashscope_base_url=dashscope_base_url,
            dashscope_embedding_dimensions=aliyun_embedding_dimensions,
        ),
    }
    return profiles


def list_ai_copilot_profiles() -> list[AICopilotConfig]:
    profiles = load_ai_profiles()
    ordered_ids = [
        profile_id
        for profile_id in (_resolve_default_profile_id(profiles), DEFAULT_AZURE_PROFILE_ID, DEFAULT_ALIYUN_PROFILE_ID)
        if profile_id in profiles
    ]
    seen: set[str] = set()
    ordered: list[AICopilotConfig] = []
    for profile_id in ordered_ids:
        if profile_id in seen:
            continue
        ordered.append(profiles[profile_id])
        seen.add(profile_id)
    for profile_id, config in profiles.items():
        if profile_id in seen:
            continue
        ordered.append(config)
    return ordered


def load_ai_copilot_config(profile_id: str | None = None) -> AICopilotConfig:
    profiles = load_ai_profiles()
    normalized_profile_id = str(profile_id or "").strip().lower()
    if normalized_profile_id in profiles:
        return profiles[normalized_profile_id]
    return profiles[_resolve_default_profile_id(profiles)]


def serialize_ai_profile(config: AICopilotConfig) -> dict[str, Any]:
    return {
        "id": config.profile_id,
        "provider": config.provider,
        "label": config.display_name,
        "assessment_mode": config.assessment_mode,
        "retrieval_backend": config.retrieval_backend,
        "chat_model": config.chat_model,
        "embedding_model": config.embedding_model,
        "auth_mode": config.auth_mode,
        "ready": config.provider_ready,
        "llm_ready": config.llm_runtime_ready,
        "retrieval_ready": config.retrieval_runtime_ready,
    }


def get_ai_profile_missing_config(config: AICopilotConfig) -> list[str]:
    missing: list[str] = []
    if config.provider == DEFAULT_ALIYUN_PROFILE_ID:
        if config.llm_enabled and not config.dashscope_api_key:
            missing.append("DASHSCOPE_API_KEY")
        if config.llm_enabled and not config.dashscope_base_url:
            missing.append("REQPILOT_DASHSCOPE_BASE_URL")
        if config.llm_enabled and not config.chat_model:
            missing.append("REQPILOT_ALIYUN_CHAT_MODEL")
        if config.retrieval_backend == DISABLED_RETRIEVAL_BACKEND:
            missing.append(ALIYUN_RETRIEVAL_DISABLED_REASON)
        return missing

    if config.llm_enabled and not config.azure_openai_endpoint:
        missing.append("AZURE_OPENAI_ENDPOINT")
    if config.llm_enabled and not config.azure_openai_chat_deployment:
        missing.append("AZURE_OPENAI_CHAT_DEPLOYMENT")
    if config.retrieval_backend in {"azure_sdk", "azure_haystack"} and not config.azure_openai_embedding_deployment:
        missing.append("AZURE_OPENAI_EMBEDDING_DEPLOYMENT")
    if config.retrieval_backend in {"azure_sdk", "azure_haystack"} and not config.azure_search_endpoint:
        missing.append("AZURE_AI_SEARCH_ENDPOINT")
    if config.retrieval_backend in {"azure_sdk", "azure_haystack"} and not config.azure_search_index_name:
        missing.append("AZURE_AI_SEARCH_INDEX_NAME")
    if config.azure_auth_mode == "api_key":
        if (config.llm_enabled or config.retrieval_backend in {"azure_sdk", "azure_haystack"}) and not config.azure_openai_api_key:
            missing.append("AZURE_OPENAI_API_KEY")
        if config.retrieval_backend == "azure_haystack" and not config.azure_search_api_key:
            missing.append("AZURE_AI_SEARCH_API_KEY")
    return missing


def ensure_ai_profile_runnable(config: AICopilotConfig) -> None:
    missing = get_ai_profile_missing_config(config)
    if missing:
        missing_text = ", ".join(missing)
        raise RuntimeError(f"{config.display_name} profile is not fully configured: {missing_text}")


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
        final_result["analysis_basis"] = _build_requirement_analysis_basis(
            screening=screening,
            retrieval_backend=retriever.backend_name,
            evidences=evidences,
            selected_mode="heuristic",
        )
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

    llm_engine = _select_assessment_engine(config)
    if config.assessment_mode == "llm_shadow":
        heuristic_result = heuristics.assess(context, evidences)
        heuristic_final = reconcile_assessment_output(
            context=context,
            candidate=heuristic_result,
            retrieved_evidences=evidences,
            fallback_reason="",
        )
        collector.record_result(
            mode="heuristic",
            model_name=heuristics.model_name,
            prompt_version=heuristics.prompt_version,
            input_payload={"context": context, "evidences": evidences},
            output_payload=heuristic_result,
            validation_status=heuristic_final["validation_status"],
            fallback_reason=heuristic_final["fallback_reason"],
            retrieval_backend=retriever.backend_name,
        )
        selected_result = heuristic_final
        selected_mode = "heuristic"
        selected_model_name = heuristics.model_name
        selected_prompt_version = heuristics.prompt_version
        llm_final: dict[str, Any] | None = None
        try:
            llm_result = llm_engine.assess(context, evidences)
            llm_final = reconcile_assessment_output(
                context=context,
                candidate=llm_result,
                retrieved_evidences=evidences,
                fallback_reason="",
            )
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
            if _prefer_shadow_final_result(llm_final):
                selected_result = llm_final
                selected_mode = "llm_shadow"
                selected_model_name = llm_engine.model_name
                selected_prompt_version = llm_engine.prompt_version
        except Exception as exc:  # pragma: no cover - network/provider dependent
            collector.record_failure(
                mode="llm_shadow",
                model_name=llm_engine.model_name,
                prompt_version=llm_engine.prompt_version,
                input_payload={"context": context, "evidences": evidences},
                fallback_reason=str(exc),
                retrieval_backend=retriever.backend_name,
            )
        selected_result["analysis_basis"] = _build_requirement_analysis_basis(
            screening=screening,
            retrieval_backend=retriever.backend_name,
            evidences=evidences,
            selected_mode=selected_mode,
            heuristic_final=heuristic_final,
            llm_final=llm_final,
        )
        return _finalize_assessment_result(
            selected_result,
            evidences,
            collector,
            final_mode=selected_mode,
            final_model_name=selected_model_name,
            final_prompt_version=selected_prompt_version,
        )

    try:
        llm_result = llm_engine.assess(context, evidences)
        final_result = reconcile_assessment_output(
            context=context,
            candidate=llm_result,
            retrieved_evidences=evidences,
            fallback_reason="",
        )
        final_result["analysis_basis"] = _build_requirement_analysis_basis(
            screening=screening,
            retrieval_backend=retriever.backend_name,
            evidences=evidences,
            selected_mode="llm_primary",
        )
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
        final_result["analysis_basis"] = _build_requirement_analysis_basis(
            screening=screening,
            retrieval_backend=retriever.backend_name,
            evidences=evidences,
            selected_mode="heuristic_fallback",
        )
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
    profile_ids: list[str] | None = None,
) -> dict[str, Any]:
    chunks = build_knowledge_chunks(project_id, knowledge_doc)
    selected_profiles: list[AICopilotConfig]
    if config is not None:
        selected_profiles = [config]
    else:
        all_profiles = load_ai_profiles()
        requested_ids = [
            str(item or "").strip().lower()
            for item in (profile_ids or list(all_profiles.keys()))
            if str(item or "").strip()
        ]
        selected_profiles = [all_profiles[item] for item in requested_ids if item in all_profiles]
        if not selected_profiles:
            selected_profiles = list_ai_copilot_profiles()
    payload_path = write_knowledge_index_payload(
        knowledge_doc,
        chunks,
        profile_ids=[item.profile_id for item in selected_profiles],
    )
    synced_at = _now_iso_utc()

    targets: dict[str, dict[str, Any]] = {}
    for selected in selected_profiles:
        status = "READY"
        error = ""
        if not chunks:
            status = "READY"
        elif selected.provider == DEFAULT_ALIYUN_PROFILE_ID:
            status = "ERROR"
            error = ALIYUN_RETRIEVAL_DISABLED_MESSAGE
        elif selected.retrieval_backend == "local":
            status = "READY"
        elif not selected.retrieval_runtime_ready:
            status = "ERROR"
            error = f"{selected.display_name} retrieval runtime is not configured."
        else:
            try:
                _select_indexer(selected).upsert_chunks(chunks)
            except Exception as exc:  # pragma: no cover - network/provider dependent
                status = "ERROR"
                error = str(exc)
        targets[selected.profile_id] = {
            "profile_id": selected.profile_id,
            "provider": selected.provider,
            "profile_label": selected.display_name,
            "retrieval_backend": selected.retrieval_backend,
            "status": status,
            "error": error,
            "synced_at": synced_at,
            "chunk_count": len(chunks),
            "payload_path": str(payload_path),
        }
    target_states = list(targets.values())
    ready_count = sum(1 for item in target_states if str(item.get("status") or "") == "READY")
    overall_status = "READY" if ready_count == len(target_states) else ("PARTIAL" if ready_count else "ERROR")
    overall_error = "; ".join(
        str(item.get("error") or "").strip()
        for item in target_states
        if str(item.get("error") or "").strip()
    )
    return {
        "status": overall_status,
        "error": overall_error,
        "synced_at": synced_at,
        "chunk_count": len(chunks),
        "payload_path": str(payload_path),
        "targets": targets,
        **(target_states[0] if len(target_states) == 1 else {}),
    }


def build_assessment_context(requirement_row: dict[str, Any]) -> dict[str, Any]:
    requirement_text = str(
        requirement_row.get("raw_text")
        or requirement_row.get("display_text")
        or requirement_row.get("requirement_text")
        or ""
    ).strip()
    normalization = build_requirement_normalization_payload(
        requirement_row,
        requirement_text=requirement_text,
    )
    normalized_requirement = str(normalization.get("summary") or "")
    section_label = str(
        requirement_row.get("parent_section_label")
        or requirement_row.get("parent_section_title")
        or requirement_row.get("parent_section_code")
        or ""
    ).strip()
    content_summary = summarize_content_blocks(requirement_row.get("content_blocks"))
    table_context = summarize_table_refs(requirement_row.get("table_refs"))
    retrieval_requirement = str(normalization.get("retrieval_text") or normalized_requirement).strip()
    normalization_facet_text = str(normalization.get("facet_text") or "").strip()
    query_parts: list[str] = []
    if retrieval_requirement:
        query_parts.append(retrieval_requirement)
    if normalization_facet_text:
        query_parts.append(f"结构化要点：{normalization_facet_text}")
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
        "retrieval_requirement": retrieval_requirement,
        "normalization_facets": list(normalization.get("facets") or []),
        "normalization_facet_text": normalization_facet_text,
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


def build_requirement_normalization_payload(
    requirement_row: dict[str, Any],
    *,
    requirement_text: str | None = None,
) -> dict[str, Any]:
    raw_text = str(
        requirement_text
        or requirement_row.get("raw_text")
        or requirement_row.get("display_text")
        or requirement_row.get("requirement_text")
        or ""
    ).strip()
    compact_text = normalize_requirement_text(raw_text)
    core_text = _strip_requirement_heading(
        compact_text,
        section_code=str(requirement_row.get("parent_section_code") or "").strip(),
        heading_candidates=[
            requirement_row.get("heading_text"),
            requirement_row.get("parent_section_title"),
            requirement_row.get("parent_section_label"),
        ],
    )
    table_facets = _extract_table_facets(requirement_row.get("table_refs"))
    text_facets = _extract_text_facets(core_text, requirement_row=requirement_row)
    facets = table_facets or text_facets
    summary = _build_normalized_requirement_summary(
        core_text,
        requirement_row=requirement_row,
        table_facets=table_facets,
        text_facets=text_facets,
    )
    if not summary:
        summary = core_text or compact_text
    summary = summary[:500].strip()
    facet_text = "；".join(
        f"{str(item.get('label') or '').strip()}={str(item.get('value') or '').strip()}"
        for item in facets
        if str(item.get("label") or "").strip() and str(item.get("value") or "").strip()
    )
    retrieval_parts: list[str] = []
    for candidate in (summary, core_text, facet_text):
        _append_unique_text(retrieval_parts, candidate)
    retrieval_text = " ".join(retrieval_parts).strip()[:1800]
    return {
        "summary": summary,
        "facets": facets[:8],
        "facet_text": facet_text[:1200],
        "retrieval_text": retrieval_text,
        "raw_text": compact_text,
        "core_text": core_text,
    }


def build_requirement_normalization_detail(
    requirement_row: dict[str, Any],
    *,
    persisted_summary: str = "",
) -> dict[str, Any]:
    payload = build_requirement_normalization_payload(requirement_row)
    computed_summary = str(payload.get("summary") or "").strip()
    persisted_candidate = normalize_requirement_text(persisted_summary)
    raw_text = normalize_requirement_text(str(payload.get("raw_text") or ""))
    core_text = normalize_requirement_text(str(payload.get("core_text") or ""))
    if not persisted_candidate:
        summary = computed_summary
    elif _texts_equivalent(persisted_candidate, computed_summary):
        summary = persisted_candidate
    elif any(_texts_equivalent(persisted_candidate, candidate) for candidate in (raw_text, core_text) if candidate):
        summary = computed_summary
    else:
        summary = persisted_candidate
    return {
        "summary": summary,
        "facets": list(payload.get("facets") or []),
        "raw_text": str(payload.get("raw_text") or "").strip(),
        "retrieval_text": str(payload.get("retrieval_text") or "").strip(),
    }


def _append_unique_text(parts: list[str], value: str) -> None:
    token = normalize_requirement_text(value)
    if not token:
        return
    if any(_texts_equivalent(existing, token) for existing in parts):
        return
    parts.append(token)


def _texts_equivalent(left: str, right: str) -> bool:
    return normalize_requirement_text(left).lower() == normalize_requirement_text(right).lower()


def _strip_requirement_heading(text: str, *, section_code: str, heading_candidates: list[object]) -> str:
    remaining = normalize_requirement_text(text)
    if section_code and remaining.startswith(section_code):
        remaining = remaining[len(section_code) :].strip(" ：:;；,.，")
    for heading in heading_candidates:
        candidate = normalize_requirement_text(str(heading or ""))
        if not candidate:
            continue
        if section_code:
            candidate = re.sub(rf"^{re.escape(section_code)}\s*", "", candidate).strip(" ：:;；,.，")
        if not candidate:
            continue
        if remaining.lower().startswith(candidate.lower()):
            remaining = remaining[len(candidate) :].strip(" ：:;；,.，")
    return remaining or normalize_requirement_text(text)


def _extract_table_facets(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    facets: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for ref in value[:2]:
        if not isinstance(ref, dict):
            continue
        rows = ref.get("slice_rows")
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, list) or len(row) < 2:
                continue
            label = normalize_requirement_text(str(row[0] or "")).strip("| ")
            content = normalize_requirement_text(str(row[1] or "")).strip("| ")
            if not label or not content:
                continue
            if _is_generic_table_facet(label, content):
                continue
            signature = (label.lower(), content.lower())
            if signature in seen:
                continue
            seen.add(signature)
            facets.append({"label": label, "value": content})
            if len(facets) >= 8:
                return facets
    return facets


def _is_generic_table_facet(label: str, value: str) -> bool:
    simplified_label = re.sub(r"[^\w\u4e00-\u9fff]+", " ", label).strip().lower()
    simplified_value = re.sub(r"[^\w\u4e00-\u9fff]+", " ", value).strip().lower()
    if simplified_label in GENERIC_TABLE_FACET_LABELS:
        return True
    if simplified_label.startswith("table ") and "parameter" in simplified_value:
        return True
    if simplified_label in {"项目 item", "item", "project item"} and simplified_value.startswith("要求"):
        return True
    if simplified_label.startswith("项目") and simplified_value.startswith("要求"):
        return True
    return False


def _extract_text_facets(text: str, *, requirement_row: dict[str, Any]) -> list[dict[str, str]]:
    facets: list[dict[str, str]] = []
    subject = _extract_subject(text)
    if subject:
        facets.append({"label": "对象", "value": subject})
    parameter_tokens = _extract_parameter_tokens(text)
    if parameter_tokens:
        facets.append({"label": "关键参数", "value": "、".join(parameter_tokens[:6])})
    numeric_constraints = _extract_numeric_constraints(text)
    if numeric_constraints:
        facets.append({"label": "约束/数值", "value": "；".join(numeric_constraints[:6])})
    conditions = _extract_condition_phrases(text)
    if conditions:
        facets.append({"label": "适用条件", "value": "；".join(conditions[:3])})
    descriptor = _extract_descriptor(text, requirement_row)
    if descriptor and not any(_texts_equivalent(descriptor, item["value"]) for item in facets):
        facets.append({"label": "主题", "value": descriptor})
    return facets[:6]


def _extract_subject(text: str) -> str:
    if "系统/组件" in text:
        return "系统/组件"
    candidates = (
        ("零部件", "零部件"),
        ("组件", "组件"),
        ("系统", "系统"),
        ("试样", "试样"),
        ("试件", "试件"),
        ("DUT", "DUT/试样"),
        ("ECU", "ECU"),
        ("module", "模块/module"),
        ("component", "组件/component"),
        ("system", "系统/system"),
        ("software", "软件/software"),
    )
    lowered = text.lower()
    for needle, label in candidates:
        if needle in text or needle.lower() in lowered:
            return label
    return ""


def _extract_parameter_tokens(text: str) -> list[str]:
    matches: list[str] = []
    patterns = (
        (r"\bTmin\b", re.I),
        (r"\bTmax\b", re.I),
        (r"\bUBmin\b", re.I),
        (r"\bUSmin\b", re.I),
        (r"\bUmin\b", re.I),
        (r"\bUmax\b", re.I),
        (r"\bUeff\b", re.I),
        (r"\bt1\b", re.I),
        (r"\bt2\b", re.I),
        (r"\btr\b", re.I),
        (r"\btf\b", re.I),
        (r"\bCAN\b", 0),
        (r"功能状态\s*[A-D]", re.I),
        (r"functional state\s*[A-D]", re.I),
        (r"\bASIL-[A-D]\b", re.I),
        (r"\b\d+(?:\.\d+)?\s*(?:kbps|Mbps)\b", re.I),
    )
    for pattern, flags in patterns:
        for match in re.finditer(pattern, text, flags):
            token = normalize_requirement_text(match.group(0))
            if token and token not in matches:
                matches.append(token)
            if len(matches) >= 8:
                return matches
    return matches


def _extract_numeric_constraints(text: str) -> list[str]:
    matches: list[str] = []
    for match in NUMERIC_CONSTRAINT_PATTERN.finditer(text):
        token = normalize_requirement_text(match.group(0))
        if not token or token in matches:
            continue
        matches.append(token)
        if len(matches) >= 8:
            break
    return matches


def _extract_condition_phrases(text: str) -> list[str]:
    conditions: list[str] = []
    patterns = (
        r"(?:在|于)[^。；;]{4,36}?(?:时|下)",
        r"(?:under|during|at|for)\s+[^.;,]{4,80}(?=$|[.;,])",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.I):
            token = normalize_requirement_text(match.group(0)).strip(" ，,;；")
            if not token or token in conditions:
                continue
            if re.search(r"[A-Za-z]", token) and len(token) > 50:
                continue
            conditions.append(token)
            if len(conditions) >= 4:
                return conditions
    return conditions


def _extract_descriptor(text: str, requirement_row: dict[str, Any]) -> str:
    candidates = [
        normalize_requirement_text(text),
        normalize_requirement_text(str(requirement_row.get("parent_section_title") or "")),
        normalize_requirement_text(str(requirement_row.get("heading_text") or "")),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        match = re.search(r"(.+?)(?:的)?试验参数(?:见表|如下|为)", candidate)
        if match:
            return normalize_requirement_text(match.group(1)).strip(" ：:;；,.，")
        match = re.search(r"(.+?)\s+test parameters", candidate, re.I)
        if match:
            return normalize_requirement_text(match.group(1)).strip(" ：:;；,.，")
    for candidate in candidates[1:]:
        simplified = candidate
        for token in GENERIC_DESCRIPTOR_TERMS:
            simplified = re.sub(re.escape(token), "", simplified, flags=re.I)
        simplified = normalize_requirement_text(simplified).strip(" ：:;；,.，")
        bilingual_prefix = re.match(r"^([\u4e00-\u9fffA-Za-z0-9/、（）()\-]+?)\s+[A-Za-z].*$", simplified)
        if bilingual_prefix and re.search(r"[\u4e00-\u9fff]", bilingual_prefix.group(1)):
            return bilingual_prefix.group(1)
        if simplified:
            return simplified
    return ""


def _extract_functional_states(text: str) -> list[str]:
    states: list[str] = []
    for match in re.finditer(r"(?:功能状态|functional state)\s*([A-D])", text, re.I):
        token = f"功能状态{match.group(1).upper()}"
        if token not in states:
            states.append(token)
    return states


def _split_requirement_sentences(text: str) -> list[str]:
    seeded = re.sub(r"(?<=[。！？.!?;；])\s+", "\n", normalize_requirement_text(text))
    return [normalize_requirement_text(part) for part in seeded.split("\n") if normalize_requirement_text(part)]


def _select_best_requirement_sentence(text: str) -> str:
    best_sentence = ""
    best_score = -10
    for sentence in _split_requirement_sentences(text):
        lowered = sentence.lower()
        score = 0
        if re.search(r"\b(shall|must|should|support|meet|comply|provide|ensure|allow|limit)\b", lowered):
            score += 4
        if re.search(r"(应|必须|不得|需|需要|满足|支持|符合|提供|保证|允许|限制)", sentence):
            score += 4
        if re.search(r"\b(tmin|tmax|ubmin|usmin|umin|umax|ueff|can|asil)\b", lowered):
            score += 3
        if re.search(r"\d", sentence):
            score += 2
        if len(sentence) < 12:
            score -= 3
        if len(sentence) > 220:
            score -= 1
        if score > best_score:
            best_score = score
            best_sentence = sentence
    return best_sentence


def _build_normalized_requirement_summary(
    text: str,
    *,
    requirement_row: dict[str, Any],
    table_facets: list[dict[str, str]],
    text_facets: list[dict[str, str]],
) -> str:
    if table_facets:
        descriptor = _extract_descriptor(text, requirement_row)
        pairs = "；".join(
            f"{item['label']}={item['value']}" for item in table_facets[:6] if item.get("label") and item.get("value")
        )
        if descriptor and pairs:
            return f"{descriptor}：{pairs}。"
        if pairs:
            return f"关键参数：{pairs}。"

    lowered = text.lower()
    subject = _extract_subject(text)
    if "tmin" in lowered and "tmax" in lowered and ("连续" in text or "continu" in lowered):
        return f"{subject or '系统/组件'}应定义并满足可连续运行的环境最低温度Tmin和最高温度Tmax。"
    if re.search(r"最低工作电压|lowest operating voltage|\b(?:ubmin|usmin|umin)\b", text, re.I) and re.search(
        r"持续工作|operate for a long time|full functionality|continuous",
        text,
        re.I,
    ):
        return f"{subject or 'DUT/试样'}应在最低工作电压边界下保持持续工作。"
    if re.search(r"\bCAN\b", text, re.I) and re.search(r"500\s*k?bps", text, re.I) and re.search(r"1\s*m?bps", text, re.I):
        return f"{subject or '系统'}应兼容 CAN 500 kbps 和 1 Mbps 两种接口速率。"
    states = _extract_functional_states(text)
    if states:
        return f"{subject or 'DUT/零部件'}应满足{'、'.join(states)}对应的功能状态要求。"

    best_sentence = _select_best_requirement_sentence(text)
    if best_sentence:
        return best_sentence[:320]
    if text_facets:
        return "；".join(f"{item['label']}={item['value']}" for item in text_facets[:4]) + "。"
    return text[:320]


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


def _context_has_explicit_requirement_signal(context: dict[str, Any]) -> bool:
    candidates = (
        context.get("normalized_requirement"),
        context.get("requirement_text"),
        context.get("content_summary"),
    )
    for raw_candidate in candidates:
        candidate = normalize_requirement_text(str(raw_candidate or ""))
        if not candidate:
            continue
        lowered = candidate.lower()
        if any(token in candidate for token in ("应", "必须", "应当", "不得", "需")):
            return True
        if re.search(r"\b(shall|must|required(?:\s+to)?|needs?\s+to)\b", lowered):
            return True
        if re.search(r"\b(tmin|tmax|ubmin|usmin|umin|umax|ueff)\b", lowered) and re.search(
            r"(连续|持续|运行|工作|full functionality|continuous|operate|operation)",
            candidate,
            re.I,
        ):
            return True
    return False


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


class DashVectorEvidenceRetriever:
    backend_name = "dashvector"

    def __init__(self, config: AICopilotConfig) -> None:
        self.config = config

    def retrieve(self, context: dict[str, Any], knowledge_docs: list[dict[str, Any]], top_k: int) -> list[dict[str, Any]]:
        if not self.config.dashvector_ready or not self.config.dashscope_embedding_ready:
            raise RuntimeError("Aliyun DashVector retrieval is not configured.")

        query = str(context.get("retrieval_query") or context.get("normalized_requirement") or "").strip()
        query_embedding = _embed_text(query, config=self.config)
        collection = _dashvector_collection(self.config, create_if_missing=False)
        query_response = collection.query(
            query_embedding,
            topk=top_k,
            filter=_build_dashvector_filter(
                project_id=_single_value(knowledge_docs, "project_id"),
                knowledge_docs=knowledge_docs,
            ),
            output_fields=list(_dashvector_metadata_fields().keys()),
            partition=self.config.dashvector_partition_name or None,
        )
        if not query_response:
            raise RuntimeError(f"DashVector query failed: {query_response.message}")
        evidences: list[dict[str, Any]] = []
        for item in query_response.output or []:
            evidence = _dashvector_doc_to_evidence(item)
            if evidence.get("source_doc_id"):
                evidences.append(evidence)
        return evidences


class QdrantEvidenceRetriever:
    backend_name = "qdrant"

    def __init__(self, config: AICopilotConfig) -> None:
        self.config = config

    def retrieve(self, context: dict[str, Any], knowledge_docs: list[dict[str, Any]], top_k: int) -> list[dict[str, Any]]:
        if not self.config.qdrant_ready or not self.config.dashscope_embedding_ready:
            raise RuntimeError("Aliyun Qdrant retrieval is not configured.")

        query = str(context.get("retrieval_query") or context.get("normalized_requirement") or "").strip()
        query_embedding = _embed_text(query, config=self.config)
        client = _qdrant_client(
            url=self.config.qdrant_url,
            api_key=self.config.qdrant_api_key,
            timeout=self.config.llm_timeout_seconds,
        )
        _ensure_qdrant_collection(
            client,
            self.config,
            dimension=len(query_embedding),
            create_if_missing=False,
        )
        search_kwargs: dict[str, Any] = {
            "collection_name": self.config.qdrant_collection_name,
            "query_vector": query_embedding,
            "limit": top_k,
            "with_payload": True,
        }
        query_filter = _build_qdrant_filter(
            project_id=_single_value(knowledge_docs, "project_id"),
            knowledge_docs=knowledge_docs,
        )
        if query_filter is not None:
            search_kwargs["query_filter"] = query_filter
        points = client.search(**search_kwargs)
        evidences: list[dict[str, Any]] = []
        for item in points or []:
            evidence = _qdrant_point_to_evidence(item)
            if evidence.get("source_doc_id"):
                evidences.append(evidence)
        return evidences


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


class AliyunDashScopeAssessmentEngine:
    prompt_version = "v2.0-aliyun-rag-copilot"

    def __init__(self, config: AICopilotConfig) -> None:
        self.config = config
        self.model_name = config.chat_model or DEFAULT_DASHSCOPE_CHAT_MODEL

    def assess(self, context: dict[str, Any], evidences: list[dict[str, Any]]) -> dict[str, Any]:
        if not self.config.dashscope_chat_ready:
            raise RuntimeError("Aliyun DashScope chat runtime is not configured.")
        if not evidences:
            raise RuntimeError("No evidence candidates available for LLM assessment.")
        messages = _build_json_compatible_messages(
            build_llm_messages(context, evidences),
            required_keys=[
                "normalized_requirement",
                "proposed_decision",
                "confidence",
                "reason",
                "risk_level",
                "clarification_question",
                "evidences",
            ],
        )
        return _dashscope_json_completion(self.config, messages)


class AliyunDashScopeScreeningEngine:
    prompt_version = "v2.1-requirement-screening-aliyun"

    def __init__(self, config: AICopilotConfig) -> None:
        self.config = config
        self.model_name = config.chat_model or DEFAULT_DASHSCOPE_CHAT_MODEL

    def screen(self, context: dict[str, Any]) -> dict[str, Any]:
        if not self.config.dashscope_chat_ready:
            raise RuntimeError("Aliyun DashScope chat runtime is not configured.")
        messages = _build_json_compatible_messages(
            build_screening_messages(context),
            required_keys=["analysis_label", "reason"],
        )
        return _dashscope_json_completion(self.config, messages)


def _run_requirement_screening(
    *,
    context: dict[str, Any],
    config: AICopilotConfig,
    collector: "AssessmentRunCollector",
    forced_analysis_label: str | None = None,
    forced_analysis_basis: str = "",
) -> dict[str, Any]:
    heuristic_engine = HeuristicRequirementScreeningEngine()
    llm_engine = _select_screening_engine(config)

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
        heuristic_final = reconcile_screening_output(candidate=heuristic_result, fallback_reason="")
        collector.record_result(
            mode="screening_heuristic",
            model_name=heuristic_engine.model_name,
            prompt_version=heuristic_engine.prompt_version,
            input_payload={"context": context},
            output_payload=heuristic_result,
            validation_status=heuristic_final["validation_status"],
            fallback_reason=heuristic_final["fallback_reason"],
        )
        selected_result = heuristic_final
        selected_mode = "heuristic"
        selected_model_name = heuristic_engine.model_name
        selected_prompt_version = heuristic_engine.prompt_version
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
            if _prefer_shadow_screening_result(
                context=context,
                heuristic_final=heuristic_final,
                llm_final=llm_final,
            ):
                selected_result = llm_final
                selected_mode = "llm_shadow"
                selected_model_name = llm_engine.model_name
                selected_prompt_version = llm_engine.prompt_version
        except Exception as exc:  # pragma: no cover - network/provider dependent
            collector.record_failure(
                mode="screening_llm_shadow",
                model_name=llm_engine.model_name,
                prompt_version=llm_engine.prompt_version,
                input_payload={"context": context},
                fallback_reason=str(exc),
            )
        return {
            **selected_result,
            "final_mode": selected_mode,
            "model_name": selected_model_name,
            "prompt_version": selected_prompt_version,
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


def _prefer_shadow_final_result(candidate: dict[str, Any] | None) -> bool:
    if not isinstance(candidate, dict):
        return False
    return str(candidate.get("validation_status") or "").strip() in PREFERRED_FINAL_VALIDATION_STATUSES


def _prefer_shadow_screening_result(
    *,
    context: dict[str, Any],
    heuristic_final: dict[str, Any] | None,
    llm_final: dict[str, Any] | None,
) -> bool:
    if not _prefer_shadow_final_result(llm_final):
        return False
    heuristic_label = str((heuristic_final or {}).get("analysis_label") or "").strip().upper()
    llm_label = str((llm_final or {}).get("analysis_label") or "").strip().upper()
    if (
        heuristic_label == "REQUIREMENT"
        and llm_label == "INFO"
        and _context_has_explicit_requirement_signal(context)
    ):
        return False
    return True


def _format_analysis_mode_label(mode: str) -> str:
    normalized = str(mode or "").strip().lower()
    return {
        "heuristic": "启发式",
        "llm_shadow": "LLM",
        "llm_primary": "LLM 主链",
        "heuristic_fallback": "启发式回退",
        "screening_only": "筛选",
    }.get(normalized, normalized or "分析")


def _result_outcome_label(result: dict[str, Any] | None) -> str:
    if not isinstance(result, dict):
        return ""
    analysis_label = str(result.get("analysis_label") or "").strip().upper()
    if analysis_label == "INFO":
        return "INFO"
    decision = str(result.get("ai_decision") or "").strip().upper()
    return decision or "待分析"


def _build_requirement_analysis_basis(
    *,
    screening: dict[str, Any],
    retrieval_backend: str,
    evidences: list[dict[str, Any]],
    selected_mode: str,
    heuristic_final: dict[str, Any] | None = None,
    llm_final: dict[str, Any] | None = None,
) -> str:
    parts = [
        f"已完成{_format_analysis_mode_label(selected_mode)}分析",
        f"检索后端 {retrieval_backend or 'local'}",
        f"候选证据 {len(evidences)} 条",
    ]
    screening_reason = str(screening.get("reason") or "").strip()
    if screening_reason:
        parts.append(f"筛选依据：{screening_reason}")

    heuristic_outcome = _result_outcome_label(heuristic_final)
    llm_outcome = _result_outcome_label(llm_final)
    if heuristic_outcome and llm_outcome and heuristic_outcome != llm_outcome:
        parts.append(f"启发式={heuristic_outcome}，LLM={llm_outcome}，当前采用{_format_analysis_mode_label(selected_mode)}结论")
    elif selected_mode == "llm_shadow" and llm_outcome:
        parts.append("最终结论采用 LLM 输出")
    elif selected_mode.startswith("heuristic"):
        parts.append("最终结论采用启发式规则")
    return "；".join(parts) + "。"


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
                "profile_id": self.config.profile_id,
                "provider": self.config.provider,
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
                "profile_id": self.config.profile_id,
                "provider": self.config.provider,
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


def _knowledge_chunk_id(document_id: str, content_kind: str, index: int) -> str:
    return f"{document_id}_{content_kind}_{index}"


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
                chunk_id=_knowledge_chunk_id(str(knowledge_doc["id"]), "paragraph", index),
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
                chunk_id=_knowledge_chunk_id(str(knowledge_doc["id"]), "table", index),
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
                chunk_id=_knowledge_chunk_id(str(knowledge_doc["id"]), "section", index),
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
    profile_ids: list[str] | None = None,
) -> Path:
    INDEX_PAYLOAD_DIR.mkdir(parents=True, exist_ok=True)
    payload_path = INDEX_PAYLOAD_DIR / f"{knowledge_doc['id']}.json"
    payload = {
        "document_id": knowledge_doc["id"],
        "file_name": knowledge_doc.get("file_name"),
        "generated_at": _now_iso_utc(),
        "profile_ids": list(profile_ids or []),
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


class DashVectorIndexer:
    def __init__(self, config: AICopilotConfig) -> None:
        self.config = config

    def upsert_chunks(self, chunks: list[dict[str, Any]]) -> int:
        if not chunks:
            return 0

        from dashvector import Doc

        first_embedding = _embed_text(str(chunks[0].get("content") or ""), config=self.config)
        collection = _dashvector_collection(
            self.config,
            create_if_missing=True,
            dimension=len(first_embedding),
        )
        docs = []
        for index, chunk in enumerate(chunks):
            docs.append(
                Doc(
                    id=str(chunk.get("chunk_id") or uuid.uuid4().hex),
                    vector=first_embedding if index == 0 else _embed_text(str(chunk.get("content") or ""), config=self.config),
                    fields=_flatten_dashvector_chunk_meta(
                        dict(chunk.get("meta") or {}),
                        content=str(chunk.get("content") or ""),
                    ),
                )
            )
        response = collection.upsert(docs, partition=self.config.dashvector_partition_name or None)
        if not response:
            raise RuntimeError(f"DashVector upsert failed: {response.message}")
        return len(response.output or [])


class QdrantIndexer:
    def __init__(self, config: AICopilotConfig) -> None:
        self.config = config

    def upsert_chunks(self, chunks: list[dict[str, Any]]) -> int:
        if not chunks:
            return 0

        from qdrant_client.http import models as qdrant_models

        first_embedding = _embed_text(str(chunks[0].get("content") or ""), config=self.config)
        client = _qdrant_client(
            url=self.config.qdrant_url,
            api_key=self.config.qdrant_api_key,
            timeout=self.config.llm_timeout_seconds,
        )
        _ensure_qdrant_collection(
            client,
            self.config,
            dimension=len(first_embedding),
            create_if_missing=True,
        )
        points = []
        for index, chunk in enumerate(chunks):
            points.append(
                qdrant_models.PointStruct(
                    id=str(chunk.get("chunk_id") or uuid.uuid4().hex),
                    vector=first_embedding if index == 0 else _embed_text(str(chunk.get("content") or ""), config=self.config),
                    payload=_flatten_qdrant_chunk_meta(
                        dict(chunk.get("meta") or {}),
                        content=str(chunk.get("content") or ""),
                    ),
                )
            )
        client.upsert(
            collection_name=self.config.qdrant_collection_name,
            points=points,
            wait=True,
        )
        return len(points)


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
    trace_id = str(uuid.uuid4())
    trace_path = collector.config.trace_dir / f"{trace_id}.json"
    collector.config.trace_dir.mkdir(parents=True, exist_ok=True)
    trace_payload = {
        "context": collector.context,
        "profile_id": collector.config.profile_id,
        "provider": collector.config.provider,
        "retrieval_backend": collector.retrieval_backend,
        "retrieved_evidences": evidences,
        "runs": collector.runs,
        "final_result": final_result,
        "generated_at": _now_iso_utc(),
    }
    trace_path.write_text(json.dumps(trace_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "assessment": {
            "profile_id": collector.config.profile_id,
            "provider": collector.config.provider,
            "analysis_label": final_result["analysis_label"],
            "analysis_basis": final_result["analysis_basis"],
            "ai_decision": final_result["ai_decision"],
            "ai_reason": final_result["ai_reason"],
            "ai_confidence": final_result["ai_confidence"],
            "risk_level": final_result["risk_level"],
            "clarification_question": final_result["clarification_question"],
            "normalized_requirement": final_result["normalized_requirement"],
            "assessment_mode": final_mode,
            "retrieval_backend": collector.retrieval_backend,
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
        "analysis_basis": f"筛选判定为信息条目：{screening['reason']}",
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
        "analysis_basis": f"已完成需求筛选，但当前没有知识文档，尚未进入需求分析。筛选依据：{screening['reason']}",
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
    for evidence in cited:
        cited_doc_id = evidence.source_doc_id.strip()
        cited_location = evidence.source_location.strip()
        cited_excerpt = normalize_requirement_text(evidence.excerpt)
        if not cited_doc_id or not cited_location or not cited_excerpt:
            return "evidence_reference_out_of_scope"
        matched = False
        for item in retrieved:
            item_doc_id = str(item.get("source_doc_id") or "").strip()
            item_location = str(item.get("source_location") or "").strip()
            item_excerpt = normalize_requirement_text(str(item.get("excerpt") or ""))
            if item_doc_id != cited_doc_id or item_location != cited_location:
                continue
            if cited_excerpt in item_excerpt or item_excerpt in cited_excerpt:
                matched = True
                break
        if not matched:
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


def _dashvector_doc_to_evidence(document: Any) -> dict[str, Any]:
    fields = getattr(document, "fields", {}) or {}
    score = getattr(document, "score", None)
    return {
        "source_doc_id": str(fields.get("source_doc_id") or ""),
        "source_doc_name": str(fields.get("source_doc_name") or ""),
        "source_location": str(fields.get("source_location") or ""),
        "excerpt": str(fields.get("content") or "")[:380],
        "relevance": round(_normalize_dashvector_relevance_score(score), 4),
        "source_doc_version": str(fields.get("source_doc_version") or ""),
    }


def _qdrant_point_to_evidence(point: Any) -> dict[str, Any]:
    payload = getattr(point, "payload", {}) or {}
    score = getattr(point, "score", None)
    return {
        "source_doc_id": str(payload.get("source_doc_id") or ""),
        "source_doc_name": str(payload.get("source_doc_name") or ""),
        "source_location": str(payload.get("source_location") or ""),
        "excerpt": str(payload.get("content") or "")[:380],
        "relevance": round(_normalize_qdrant_relevance_score(score), 4),
        "source_doc_version": str(payload.get("source_doc_version") or ""),
    }


def _select_retriever(config: AICopilotConfig) -> EvidenceRetriever:
    if config.provider == DEFAULT_ALIYUN_PROFILE_ID:
        raise RuntimeError(ALIYUN_RETRIEVAL_DISABLED_MESSAGE)
    if config.retrieval_backend == "qdrant" and config.qdrant_ready and config.dashscope_embedding_ready:
        return QdrantEvidenceRetriever(config)
    if config.retrieval_backend == "dashvector" and config.dashvector_ready and config.dashscope_embedding_ready:
        return DashVectorEvidenceRetriever(config)
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


def _select_indexer(config: AICopilotConfig) -> AzureHaystackIndexer | AzureSearchSDKIndexer | DashVectorIndexer | QdrantIndexer:
    if config.provider == DEFAULT_ALIYUN_PROFILE_ID:
        raise RuntimeError(ALIYUN_RETRIEVAL_DISABLED_MESSAGE)
    if config.retrieval_backend == "qdrant":
        return QdrantIndexer(config)
    if config.retrieval_backend == "dashvector":
        return DashVectorIndexer(config)
    return _select_azure_indexer(config)


def _select_assessment_engine(config: AICopilotConfig) -> AssessmentEngine:
    if config.provider == DEFAULT_ALIYUN_PROFILE_ID:
        return AliyunDashScopeAssessmentEngine(config)
    return AzureOpenAIAssessmentEngine(config)


def _select_screening_engine(config: AICopilotConfig) -> ScreeningEngine:
    if config.provider == DEFAULT_ALIYUN_PROFILE_ID:
        return AliyunDashScopeScreeningEngine(config)
    return AzureOpenAIScreeningEngine(config)


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


def _dashvector_metadata_fields() -> dict[str, type]:
    return {
        **_azure_metadata_fields(),
        "content": str,
    }


def _flatten_dashvector_chunk_meta(meta: dict[str, Any], *, content: str) -> dict[str, Any]:
    return {
        **_flatten_azure_chunk_meta(meta),
        "content": content,
    }


def _flatten_qdrant_chunk_meta(meta: dict[str, Any], *, content: str) -> dict[str, Any]:
    return {
        **_flatten_azure_chunk_meta(meta),
        "content": content,
    }


@lru_cache(maxsize=2)
def _default_azure_credential(allow_interactive_login: bool):
    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential(
        exclude_interactive_browser_credential=not allow_interactive_login,
        exclude_shared_token_cache_credential=True,
        exclude_visual_studio_code_credential=True,
    )


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


def _aliyun_openai_client(config: AICopilotConfig):
    from openai import OpenAI

    if not config.dashscope_api_key:
        raise RuntimeError("Aliyun DashScope API key is not configured.")
    return OpenAI(
        api_key=config.dashscope_api_key,
        base_url=config.dashscope_base_url,
        timeout=config.llm_timeout_seconds,
    )


def _build_json_compatible_messages(messages: list[dict[str, str]], *, required_keys: list[str]) -> list[dict[str, str]]:
    if not messages:
        return messages
    cloned = [dict(item) for item in messages]
    required_keys_text = ", ".join(required_keys)
    suffix = (
        "\n\n请只返回一个 JSON 对象，不要返回 Markdown 代码块、解释文字或前后缀。"
        f" JSON 中必须包含这些字段：{required_keys_text}。"
    )
    cloned[-1]["content"] = str(cloned[-1].get("content") or "") + suffix
    return cloned


def _extract_json_object(text: str) -> dict[str, Any]:
    payload = str(text or "").strip()
    if payload.startswith("```"):
        payload = re.sub(r"^```(?:json)?", "", payload, flags=re.I).strip()
        payload = re.sub(r"```$", "", payload).strip()
    start = payload.find("{")
    end = payload.rfind("}")
    if start >= 0 and end >= start:
        payload = payload[start : end + 1]
    if not payload:
        raise RuntimeError("Aliyun DashScope returned an empty JSON payload.")
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Aliyun DashScope returned invalid JSON: {exc}") from exc


def _dashscope_json_completion(config: AICopilotConfig, messages: list[dict[str, str]]) -> dict[str, Any]:
    client = _aliyun_openai_client(config)
    payload: dict[str, Any] = {
        "model": config.chat_model,
        "messages": messages,
        "temperature": 0.1,
    }
    try:
        completion = client.chat.completions.create(
            **payload,
            response_format={"type": "json_object"},
        )
    except Exception:
        completion = client.chat.completions.create(**payload)
    message = completion.choices[0].message
    content = getattr(message, "content", "")
    if isinstance(content, list):
        content = "".join(str(item.get("text") or "") for item in content if isinstance(item, dict))
    return _extract_json_object(str(content or ""))


def _normalize_dashvector_relevance_score(score: Any) -> float:
    try:
        numeric_score = float(score or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if numeric_score <= 0.0:
        return 0.0
    return numeric_score / (1.0 + math.fabs(numeric_score))


def _normalize_qdrant_relevance_score(score: Any) -> float:
    try:
        numeric_score = float(score or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if numeric_score <= 0.0:
        return 0.0
    if numeric_score <= 1.0:
        return numeric_score
    return numeric_score / (1.0 + math.fabs(numeric_score))


def _build_dashvector_filter(*, project_id: str, knowledge_docs: list[dict[str, Any]]) -> str | None:
    filters: list[str] = []
    if project_id:
        filters.append(f"project_id = '{str(project_id).replace("'", "''")}'")
    doc_ids = [str(doc.get("id") or "").strip() for doc in knowledge_docs if str(doc.get("id") or "").strip()]
    if doc_ids:
        quoted = ", ".join(f"'{item.replace("'", "''")}'" for item in doc_ids)
        filters.append(f"source_doc_id in ({quoted})")
    statuses = sorted(
        {
            str(doc.get("status") or "").strip()
            for doc in knowledge_docs
            if str(doc.get("status") or "").strip()
        }
    )
    if statuses:
        quoted_statuses = ", ".join(f"'{item.replace("'", "''")}'" for item in statuses)
        filters.append(f"status in ({quoted_statuses})")
    return " and ".join(filters) if filters else None


def _build_qdrant_filter(*, project_id: str, knowledge_docs: list[dict[str, Any]]):
    from qdrant_client.http import models as qdrant_models

    must: list[Any] = []
    if project_id:
        must.append(
            qdrant_models.FieldCondition(
                key="project_id",
                match=qdrant_models.MatchValue(value=project_id),
            )
        )
    doc_ids = [str(doc.get("id") or "").strip() for doc in knowledge_docs if str(doc.get("id") or "").strip()]
    doc_condition = _build_qdrant_multi_value_condition("source_doc_id", doc_ids)
    if doc_condition is not None:
        must.append(doc_condition)
    statuses = sorted(
        {
            str(doc.get("status") or "").strip()
            for doc in knowledge_docs
            if str(doc.get("status") or "").strip()
        }
    )
    status_condition = _build_qdrant_multi_value_condition("status", statuses)
    if status_condition is not None:
        must.append(status_condition)
    if not must:
        return None
    return qdrant_models.Filter(must=must)


def _build_qdrant_multi_value_condition(key: str, values: list[str]):
    from qdrant_client.http import models as qdrant_models

    cleaned = [item for item in values if str(item or "").strip()]
    if not cleaned:
        return None
    if len(cleaned) == 1:
        return qdrant_models.FieldCondition(
            key=key,
            match=qdrant_models.MatchValue(value=cleaned[0]),
        )
    match_any_cls = getattr(qdrant_models, "MatchAny", None)
    if match_any_cls is not None:
        return qdrant_models.FieldCondition(
            key=key,
            match=match_any_cls(any=cleaned),
        )
    return qdrant_models.Filter(
        should=[
            qdrant_models.FieldCondition(
                key=key,
                match=qdrant_models.MatchValue(value=item),
            )
            for item in cleaned
        ]
    )


@lru_cache(maxsize=6)
def _dashvector_client(
    endpoint: str,
    api_key: str,
    protocol: str,
    timeout: float,
):
    from dashvector import Client, DashVectorProtocol

    if protocol == "grpc":
        selected_protocol = DashVectorProtocol.GRPC
    else:
        selected_protocol = DashVectorProtocol.HTTP
    return Client(
        endpoint=endpoint,
        api_key=api_key,
        protocol=selected_protocol,
        timeout=timeout,
    )


@lru_cache(maxsize=6)
def _qdrant_client(
    url: str,
    api_key: str,
    timeout: float,
):
    from qdrant_client import QdrantClient

    kwargs: dict[str, Any] = {
        "url": url,
        "timeout": timeout,
    }
    if api_key:
        kwargs["api_key"] = api_key
    return QdrantClient(**kwargs)


def _qdrant_collection_vector_size(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, dict):
        raw_size = value.get("size")
        if isinstance(raw_size, int) and raw_size > 0:
            return raw_size
        for nested in value.values():
            nested_size = _qdrant_collection_vector_size(nested)
            if nested_size is not None:
                return nested_size
        return None

    raw_size = getattr(value, "size", None)
    if isinstance(raw_size, int) and raw_size > 0:
        return raw_size

    for attr_name in ("vectors", "params", "config"):
        nested = getattr(value, attr_name, None)
        if nested is None:
            continue
        nested_size = _qdrant_collection_vector_size(nested)
        if nested_size is not None:
            return nested_size

    for method_name in ("model_dump", "dict"):
        method = getattr(value, method_name, None)
        if not callable(method):
            continue
        try:
            nested_payload = method()
        except TypeError:
            continue
        nested_size = _qdrant_collection_vector_size(nested_payload)
        if nested_size is not None:
            return nested_size

    return None


def _validate_qdrant_collection_dimension(
    collection_info: Any,
    *,
    collection_name: str,
    dimension: int,
) -> None:
    current_dimension = _qdrant_collection_vector_size(collection_info)
    if current_dimension is None or current_dimension == dimension:
        return
    raise RuntimeError(
        f"Qdrant collection '{collection_name}' vector size {current_dimension} does not match embedding dimension {dimension}. "
        "Use a separate collection or recreate the collection for the selected embedding model."
    )


def _dashvector_collection(
    config: AICopilotConfig,
    *,
    create_if_missing: bool,
    dimension: int = 0,
):
    client = _dashvector_client(
        endpoint=config.dashvector_endpoint,
        api_key=config.dashvector_api_key,
        protocol=config.dashvector_protocol,
        timeout=config.llm_timeout_seconds,
    )
    collection = client.get(config.dashvector_collection_name)
    if collection:
        return collection
    if not create_if_missing:
        raise RuntimeError(f"DashVector collection is unavailable: {collection.message}")
    create_response = client.create(
        config.dashvector_collection_name,
        dimension=dimension,
        fields_schema=_dashvector_metadata_fields(),
        timeout=30,
    )
    if not create_response:
        raise RuntimeError(f"DashVector collection creation failed: {create_response.message}")
    collection = client.get(config.dashvector_collection_name)
    if not collection:
        raise RuntimeError(f"DashVector collection is unavailable after creation: {collection.message}")
    return collection


def _ensure_qdrant_collection(
    client: Any,
    config: AICopilotConfig,
    *,
    dimension: int,
    create_if_missing: bool,
) -> None:
    from qdrant_client.http import models as qdrant_models

    try:
        collection_info = client.get_collection(collection_name=config.qdrant_collection_name)
    except Exception as exc:
        if not create_if_missing:
            raise RuntimeError(
                f"Qdrant collection '{config.qdrant_collection_name}' is unavailable: {exc}"
            ) from exc
    else:
        _validate_qdrant_collection_dimension(
            collection_info,
            collection_name=config.qdrant_collection_name,
            dimension=dimension,
        )
        return
    client.create_collection(
        collection_name=config.qdrant_collection_name,
        vectors_config=qdrant_models.VectorParams(
            size=dimension,
            distance=qdrant_models.Distance.COSINE,
        ),
    )


def _azure_search_credential(config: AICopilotConfig):
    from azure.core.credentials import AzureKeyCredential

    if config.uses_api_key_for_search:
        return AzureKeyCredential(config.azure_search_api_key)
    return _default_azure_credential(config.azure_allow_interactive_login)


def _azure_search_item_to_evidence(item: Any) -> dict[str, Any]:
    payload = dict(item)
    score = payload.get("@search.reranker_score")
    is_reranker_score = score is not None
    if score is None:
        score = payload.get("@search.score")
    return {
        "source_doc_id": str(payload.get("source_doc_id") or ""),
        "source_doc_name": str(payload.get("source_doc_name") or ""),
        "source_location": str(payload.get("source_location") or ""),
        "excerpt": str(payload.get("content") or "")[:380],
        "relevance": round(_normalize_azure_relevance_score(score, is_reranker_score=is_reranker_score), 4),
        "source_doc_version": str(payload.get("source_doc_version") or ""),
    }


def _normalize_azure_relevance_score(score: Any, *, is_reranker_score: bool) -> float:
    try:
        numeric_score = float(score or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if numeric_score <= 0.0:
        return 0.0
    if is_reranker_score:
        return min(numeric_score / 4.0, 1.0)
    return numeric_score / (1.0 + math.fabs(numeric_score))


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
    if config.provider == DEFAULT_ALIYUN_PROFILE_ID:
        if not config.dashscope_embedding_ready:
            raise RuntimeError("Aliyun DashScope embeddings are not configured.")
        client = _aliyun_openai_client(config)
        payload: dict[str, Any] = {
            "model": config.embedding_model,
            "input": text,
        }
        if config.dashscope_embedding_dimensions > 0:
            payload["dimensions"] = config.dashscope_embedding_dimensions
        response = client.embeddings.create(**payload)
        return list(response.data[0].embedding)

    if not config.azure_openai_embedding_ready:
        raise RuntimeError("Azure OpenAI embeddings are not configured.")

    client = _azure_openai_client(config)
    payload = {
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

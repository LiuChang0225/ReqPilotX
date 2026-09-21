from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import tempfile
import threading
import time
import uuid
from datetime import date
from dataclasses import dataclass, replace
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Literal, Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx
from PIL import Image as PILImage
from pydantic import BaseModel, ConfigDict, Field

from reqpilotx_server.adapters.documents.bailian_visual_text_provider import (
    DEFAULT_BAILIAN_OCR_MODEL,
    BailianVisualTextProvider,
)
from reqpilotx_server.domain.source_visual_text import VisualTextPageInput
from reqpilotx_server.ports.visual_text_provider import (
    VisualTextProviderRejected,
    VisualTextProviderRejectionCode,
)
from reqpilotx_server.domain.assessment.numeric_constraints import (
    validate_numeric_decision_support,
    validate_numeric_requirement_projection,
)
from reqpilotx_server.domain.knowledge import (
    ACTIVE_KNOWLEDGE_STATUS,
    EVIDENCE_INDEX_CONTRACT_VERSION,
    EvidenceRef,
    compute_evidence_content_hash,
    evaluate_knowledge_evidence_eligibility,
    has_current_evidence_contract,
)
from reqpilotx_server.runtime_paths import resolve_storage_dir
from reqpilotx_server.services.assessor import (
    COMPLIANCE_HINTS,
    MANDATORY_HINTS,
    MODEL_NAME as HEURISTIC_MODEL_NAME,
    NEGATIVE_PATTERNS,
    PARTIAL_COMPLIANCE_HINTS,
    PROMPT_VERSION as HEURISTIC_PROMPT_VERSION,
    assess_requirement,
)
from reqpilotx_server.services.rag import score_candidate_evidences
from reqpilotx_server.services.visual_text_snapshot_cache import VisualTextProviderTelemetryV1

_logger = logging.getLogger(__name__)


VALID_DECISIONS = ("ACCEPT", "REJECT", "DEVIATION_ACCEPT", "NEED_CLARIFICATION")
VALID_COMPLIANCE_STATUSES = (
    "COMPLIANT",
    "PARTIALLY_COMPLIANT",
    "NON_COMPLIANT",
    "INSUFFICIENT_INFORMATION",
)
DECISION_TO_COMPLIANCE_STATUS = {
    "ACCEPT": "COMPLIANT",
    "DEVIATION_ACCEPT": "PARTIALLY_COMPLIANT",
    "REJECT": "NON_COMPLIANT",
    "NEED_CLARIFICATION": "INSUFFICIENT_INFORMATION",
}
VALID_RISK_LEVELS = ("LOW", "MEDIUM", "HIGH")
VALID_ANALYSIS_LABELS = ("REQUIREMENT", "INFO")
DEFAULT_PROMPT_VERSION = "v3.1-compliance-proof-context"
DEFAULT_SCREENING_PROMPT_VERSION = "v2.1-requirement-screening"
DEFAULT_HEURISTIC_MODEL = HEURISTIC_MODEL_NAME
DEFAULT_TOP_K = 5
MAX_MODEL_PRIMARY_EVIDENCES = 5
MAX_MODEL_ADJACENT_EVIDENCES = 3
MAX_PERSISTED_EVIDENCES = (
    MAX_MODEL_PRIMARY_EVIDENCES + MAX_MODEL_ADJACENT_EVIDENCES
)
REMOTE_INDEX_OPERATION_BATCH_SIZE = 1000
REMOTE_INDEX_VERIFY_ATTEMPTS = 4
REMOTE_INDEX_VERIFY_DELAY_SECONDS = 0.25
DASHVECTOR_MATCH_BATCH_SIZE = 100
DASHVECTOR_MAX_DELETE_BATCHES = 100
DEFAULT_SCREENING_TIMEOUT_SECONDS = 18.0
DEFAULT_ASSESSMENT_TIMEOUT_SECONDS = 45.0
DEFAULT_AZURE_PROFILE_ID = "azure"
DEFAULT_ALIYUN_PROFILE_ID = "aliyun"
DEFAULT_AZURE_OPENAI_API_VERSION = "2024-10-21"
DEFAULT_AZURE_OPENAI_SCOPE = "https://cognitiveservices.azure.com/.default"
DEFAULT_AZURE_SEARCH_SCOPE = "https://search.azure.com/.default"
DEFAULT_AZURE_SEARCH_VECTOR_PROFILE = "reqpilot-vector-profile"
DEFAULT_AZURE_SEARCH_VECTOR_ALGORITHM = "reqpilot-hnsw"
DEFAULT_AZURE_SEARCH_SEMANTIC_CONFIGURATION = "reqpilot-semantic"
DEFAULT_DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_DASHSCOPE_CHAT_MODEL = "qwen3.8-max"
DEFAULT_DASHSCOPE_SCREENING_MODEL = "qwen3.7-flash"
DEFAULT_DASHSCOPE_EMBEDDING_MODEL = "text-embedding-v4"
_DASHSCOPE_PUBLIC_HOSTS = frozenset(
    {"dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com"}
)
_DASHSCOPE_WORKSPACE_HOST_PATTERN = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.maas\.aliyuncs\.com$"
)
DEFAULT_AZURE_OPENAI_MAX_RETRIES = 2
DEFAULT_ALIYUN_OPENAI_MAX_RETRIES = 0
DEFAULT_AZURE_EMBEDDING_BATCH_SIZE = 16
DEFAULT_ALIYUN_EMBEDDING_BATCH_SIZE = 10
MAX_DASHSCOPE_EMBEDDING_BATCH_SIZE = 10
DEFAULT_DASHVECTOR_COLLECTION_NAME = "reqpilot-knowledge"
DEFAULT_DASHVECTOR_PROTOCOL = "http"
DEFAULT_QDRANT_COLLECTION_NAME = DEFAULT_DASHVECTOR_COLLECTION_NAME
DEFAULT_ALIYUN_LOCAL_QDRANT_COLLECTION_NAME = "reqpilot-knowledge-aliyun-local"
DISABLED_RETRIEVAL_BACKEND = "disabled"
ALIYUN_RETRIEVAL_DISABLED_REASON = "ALIYUN_RETRIEVAL_BACKEND_DISABLED"
ALIYUN_QDRANT_UNREACHABLE_REASON = "ALIYUN_QDRANT_UNREACHABLE"
ALIYUN_API_KEY_NON_ASCII_REASON = "REQPILOT_ALIYUN_API_KEY_NON_ASCII"
ALIYUN_QDRANT_API_KEY_NON_ASCII_REASON = "REQPILOT_ALIYUN_QDRANT_API_KEY_NON_ASCII"
ALIYUN_RETRIEVAL_DISABLED_MESSAGE = (
    "Aliyun local Qdrant POC is disabled. Set REQPILOT_ALIYUN_RETRIEVAL_BACKEND=qdrant and configure DashScope plus a local Qdrant service to enable the non-company-network path."
)
MANUAL_OVERRIDE_MODEL_NAME = "manual-override"
MANUAL_OVERRIDE_PROMPT_VERSION = "v1.0-manual-analysis-label-override"
QDRANT_HYBRID_DENSE_TOP_K = 24
QDRANT_HYBRID_SPARSE_TOP_K = 24
QDRANT_HYBRID_FUSED_TOP_K = 12
QDRANT_HYBRID_RERANK_TOP_K = 8
QDRANT_FINAL_EVIDENCE_LIMIT = int(os.environ.get("REQPILOT_MAX_EVIDENCE", "5"))
QDRANT_MIN_EFFECTIVE_EVIDENCE_SCORE = 0.20
QDRANT_MIN_DECISION_EVIDENCE_SCORE = 0.35
# Azure AI Search @search.score is a backend-native score, not the normalized Qdrant score.
AZURE_MIN_EFFECTIVE_EVIDENCE_SCORE = 0.03
AZURE_MIN_DECISION_EVIDENCE_SCORE = 0.03
QDRANT_LOCAL_RERANKER_MODEL = "BAAI/bge-reranker-v2-m3"
NO_ELIGIBLE_EVIDENCE_ID = "__reqpilot_no_eligible_evidence__"
EVIDENCE_INDEX_RESYNC_ACTION = "RESYNC_KNOWLEDGE_INDEX"
EVIDENCE_INDEX_RESYNC_REQUIRED_REASON = (
    "EVIDENCE_INDEX_RESYNC_REQUIRED：知识索引缺少当前 evidence identity/content hash contract，请重新同步知识文档。"
)
DASHVECTOR_INDEX_MIGRATION_ACTION = "CREATE_VERSIONED_DASHVECTOR_COLLECTION_AND_RESYNC"
DECISION_PROOF_CLAIM_ROLES = {
    "VALIDATION_RESULT",
    "TEST_RESULT",
    "CERTIFICATION",
    "COMPLIANCE_RECORD",
    "RELEASED_DESIGN_EVIDENCE",
}

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
DEFAULT_LOCAL_QDRANT_URL = "http://localhost:6333"
QDRANT_POINT_ID_NAMESPACE = uuid.UUID("3f49040f-6402-4100-a18e-368ded2f08c0")

_RUNTIME_AI_ENV_OVERRIDES: dict[str, str] = {}
_RUNTIME_AI_CONFIG_LOCK = threading.RLock()


def _runtime_config_snapshot_from_file() -> dict[str, str]:
    config_path = STORAGE_DIR / "config.json"
    if not config_path.exists():
        return {}
    loaded = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError("runtime config root must be a JSON object")
    return {
        str(key): str(value)
        for key, value in loaded.items()
        if value is not None and str(value) != ""
    }


def _load_runtime_config_from_file() -> None:
    try:
        config_data = _runtime_config_snapshot_from_file()
        # Always load config.json values into overrides so that the persisted
        # user configuration takes precedence over environment defaults set by
        # app_desktop.py.
        _RUNTIME_AI_ENV_OVERRIDES.update(config_data)
    except Exception as e:
        print(f"[ReqPilotX] Error loading config.json: {e}")


_load_runtime_config_from_file()


def _clear_runtime_connection_caches() -> None:
    for name in (
        "_default_azure_credential",
        "_azure_openai_token_provider",
        "_azure_openai_client",
        "_aliyun_openai_client",
        "_dashvector_client",
        "_qdrant_client",
    ):
        cache_clear = getattr(globals().get(name), "cache_clear", None)
        if callable(cache_clear):
            cache_clear()


def reload_runtime_ai_config_from_file() -> int:
    """Atomically replace one idle worker's effective persisted AI snapshot."""

    global _RUNTIME_AI_ENV_OVERRIDES

    try:
        snapshot = _runtime_config_snapshot_from_file()
    except Exception as exc:
        raise RuntimeError("Runtime provider configuration could not be reloaded.") from exc
    raw_revision = snapshot.get(
        "REQPILOT_RUNTIME_CONNECTION_REVISION",
        os.getenv("REQPILOT_RUNTIME_CONNECTION_REVISION") or "0",
    )
    try:
        revision = int(raw_revision)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            "Runtime provider configuration has an invalid connection revision."
        ) from exc
    if revision < 0:
        raise RuntimeError(
            "Runtime provider configuration has an invalid connection revision."
        )
    with _RUNTIME_AI_CONFIG_LOCK:
        # Readers that do not take the mutation lock see either the complete
        # previous mapping or the complete new mapping, never a clear/update
        # intermediate state.
        _RUNTIME_AI_ENV_OVERRIDES = dict(snapshot)
        _clear_runtime_connection_caches()
    return revision


def _save_runtime_config_to_file() -> None:
    config_path = STORAGE_DIR / "config.json"
    temporary_path: Path | None = None
    try:
        STORAGE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(STORAGE_DIR, 0o700)
        existing_config: dict[str, Any] = {}
        if config_path.exists():
            loaded_config = json.loads(config_path.read_text(encoding="utf-8"))
            if not isinstance(loaded_config, dict):
                raise ValueError("runtime config root must be a JSON object")
            existing_config = loaded_config
        for k, v in _RUNTIME_AI_ENV_OVERRIDES.items():
            if v is not None:
                if v == "":
                    existing_config.pop(k, None)
                else:
                    existing_config[k] = v
        if "REQPILOT_ALIYUN_API_KEY" in existing_config:
            existing_config["REQPILOT_DASHSCOPE_API_KEY"] = existing_config["REQPILOT_ALIYUN_API_KEY"]
            existing_config["DASHSCOPE_API_KEY"] = existing_config["REQPILOT_ALIYUN_API_KEY"]
        else:
            existing_config.pop("REQPILOT_DASHSCOPE_API_KEY", None)
            existing_config.pop("DASHSCOPE_API_KEY", None)
        serialized = (
            json.dumps(existing_config, indent=2, ensure_ascii=False) + "\n"
        ).encode("utf-8")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".config.json.",
            suffix=".tmp",
            dir=STORAGE_DIR,
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as handle:
            if hasattr(os, "fchmod"):
                os.fchmod(handle.fileno(), 0o600)
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, config_path)
        temporary_path = None
        _fsync_directory(STORAGE_DIR)
    except Exception as exc:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
        raise RuntimeError(
            "Runtime provider configuration could not be persisted safely."
        ) from exc


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(directory, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        # The file itself has already been flushed and atomically replaced.
        # A filesystem without directory fsync must not turn that successful
        # swap into a false API failure after the old file is gone.
        _logger.warning("Runtime config directory fsync is unavailable.")


class AIAnalysisCancelledError(RuntimeError):
    """Raised when a live AI analysis run is stopped by the user."""


class AssessmentEvidenceSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_slot: int = 0
    chunk_id: str = ""
    source_doc_id: str = ""
    source_doc_name: str = ""
    source_location: str = ""
    excerpt: str = ""
    relevance: float = 0.0
    source_doc_version: str = ""
    evidence_id: str = ""
    content_hash: str = ""
    evidence_contract_version: str = ""


class ScreeningOutputSchema(BaseModel):
    model_config = ConfigDict(extra="ignore")

    analysis_label: Literal["REQUIREMENT", "INFO"]
    reason: str


class AssessmentGapEvidenceSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    retrieval_evidence_id: str = ""
    proves: str = ""


class AssessmentMissingItemSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item: str = ""
    blocking_reason: str = ""
    required_input: str = ""


class AssessmentEvidenceConflictSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    retrieval_evidence_ids: list[str] = Field(default_factory=list)
    description: str = ""


class AssessmentInformationGapSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "assessment-information-gap/v1"
    found_evidence: list[AssessmentGapEvidenceSchema] = Field(default_factory=list)
    missing_items: list[AssessmentMissingItemSchema] = Field(default_factory=list)
    conflicts: list[AssessmentEvidenceConflictSchema] = Field(default_factory=list)
    clarification_questions: list[str] = Field(default_factory=list)


class AssessmentEvidenceFindingSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    retrieval_evidence_id: str = ""
    finding: Literal[
        "SUPPORTS",
        "PARTIALLY_SUPPORTS",
        "CONTRADICTS",
        "CONTEXT_ONLY",
    ] = "CONTEXT_ONLY"
    supported_claims: list[str] = Field(default_factory=list)
    unsupported_claims: list[str] = Field(default_factory=list)
    rationale: str = ""


class AssessmentSubJudgmentSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sub_judgment_id: str = ""
    source_ranges: list[str] = Field(default_factory=list)
    statement: str = ""
    compliance_status: Literal[
        "COMPLIANT",
        "PARTIALLY_COMPLIANT",
        "NON_COMPLIANT",
        "INSUFFICIENT_INFORMATION",
    ] = "INSUFFICIENT_INFORMATION"
    reason: str = ""
    retrieval_evidence_ids: list[str] = Field(default_factory=list)
    structured_information_gap: AssessmentInformationGapSchema = Field(
        default_factory=AssessmentInformationGapSchema
    )


class AssessmentOutputSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    normalized_requirement: str = ""
    proposed_decision: Literal["ACCEPT", "REJECT", "DEVIATION_ACCEPT", "NEED_CLARIFICATION"] = "NEED_CLARIFICATION"
    compliance_status: Literal[
        "COMPLIANT",
        "PARTIALLY_COMPLIANT",
        "NON_COMPLIANT",
        "INSUFFICIENT_INFORMATION",
    ] | None = None
    confidence: float = 0.0
    reason: str = ""
    risk_level: Literal["LOW", "MEDIUM", "HIGH"] = "MEDIUM"
    clarification_question: str = ""
    evidences: list[AssessmentEvidenceSchema] = Field(default_factory=list)
    evidence_findings: list[AssessmentEvidenceFindingSchema] = Field(default_factory=list)
    sub_judgments: list[AssessmentSubJudgmentSchema] = Field(default_factory=list)
    structured_information_gap: AssessmentInformationGapSchema = Field(
        default_factory=AssessmentInformationGapSchema
    )


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
    openai_max_retries: int = DEFAULT_AZURE_OPENAI_MAX_RETRIES
    embedding_batch_size: int = DEFAULT_AZURE_EMBEDDING_BATCH_SIZE
    profile_id: str = DEFAULT_AZURE_PROFILE_ID
    provider: str = "azure"
    profile_label: str = "Azure"
    chat_model: str = ""
    screening_model: str = ""
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
    visual_text_model: str = DEFAULT_BAILIAN_OCR_MODEL

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
            if self.retrieval_backend == "qdrant":
                return self.dashscope_embedding_ready and self.qdrant_ready
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

    def assess(
        self,
        context: dict[str, Any],
        evidences: list[dict[str, Any]],
        *,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        ...


class ScreeningEngine(Protocol):
    model_name: str
    prompt_version: str

    def screen(
        self,
        context: dict[str, Any],
        *,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
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


def _get_config_value(name: str, default: str = "") -> str:
    if name in _RUNTIME_AI_ENV_OVERRIDES:
        return str(_RUNTIME_AI_ENV_OVERRIDES[name] or "").strip()
    return str(os.getenv(name) or default).strip()


def get_runtime_ai_config_value(name: str, default: str = "") -> str:
    """Read one effective runtime setting without exposing the override store."""

    return _get_config_value(name, default)


def get_runtime_connection_revision() -> int:
    value = _get_config_value("REQPILOT_RUNTIME_CONNECTION_REVISION", "0")
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


def _first_nonempty_env(*names: str) -> str:
    for name in names:
        value = _get_config_value(name)
        if value:
            return value
    return ""


def _resolve_default_profile_id(profiles: dict[str, AICopilotConfig]) -> str:
    requested = _get_config_value("REQPILOT_AI_DEFAULT_PROFILE").lower()
    if requested and requested in profiles:
        return requested
    ordered = [DEFAULT_ALIYUN_PROFILE_ID, DEFAULT_AZURE_PROFILE_ID]
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
        top_k = _parse_positive_int(_get_config_value("REQPILOT_AI_TOP_K", str(DEFAULT_TOP_K)), default=DEFAULT_TOP_K)
        azure_embedding_dimensions = _parse_positive_int(
            _get_config_value("AZURE_OPENAI_EMBEDDING_DIMENSIONS", "1536"),
            default=1536,
        )
        azure_openai_max_retries = _parse_positive_int(
            _get_config_value("AZURE_OPENAI_MAX_RETRIES", str(DEFAULT_AZURE_OPENAI_MAX_RETRIES)),
            default=DEFAULT_AZURE_OPENAI_MAX_RETRIES,
            minimum=0,
        )
        azure_embedding_batch_size = _parse_positive_int(
            _get_config_value("AZURE_OPENAI_EMBEDDING_BATCH_SIZE", str(DEFAULT_AZURE_EMBEDDING_BATCH_SIZE)),
            default=DEFAULT_AZURE_EMBEDDING_BATCH_SIZE,
            minimum=1,
        )
        aliyun_embedding_dimensions = _parse_positive_int(
            _get_config_value("REQPILOT_ALIYUN_EMBEDDING_DIMENSIONS", "0"),
            default=0,
            minimum=0,
        )
        aliyun_openai_max_retries = _parse_positive_int(
            _get_config_value("REQPILOT_ALIYUN_MAX_RETRIES", str(DEFAULT_ALIYUN_OPENAI_MAX_RETRIES)),
            default=DEFAULT_ALIYUN_OPENAI_MAX_RETRIES,
            minimum=0,
        )
        aliyun_embedding_batch_size = _parse_positive_int(
            _get_config_value("REQPILOT_ALIYUN_EMBEDDING_BATCH_SIZE", str(DEFAULT_ALIYUN_EMBEDDING_BATCH_SIZE)),
            default=DEFAULT_ALIYUN_EMBEDDING_BATCH_SIZE,
            minimum=1,
        )
        llm_timeout_seconds = _parse_positive_float(
            _get_config_value("REQPILOT_AI_TIMEOUT_SECONDS", "45.0"),
            default=45.0,
            minimum=5.0,
        )
    except ValueError:
        top_k = DEFAULT_TOP_K
        azure_embedding_dimensions = 1536
        azure_openai_max_retries = DEFAULT_AZURE_OPENAI_MAX_RETRIES
        azure_embedding_batch_size = DEFAULT_AZURE_EMBEDDING_BATCH_SIZE
        aliyun_embedding_dimensions = 0
        aliyun_openai_max_retries = DEFAULT_ALIYUN_OPENAI_MAX_RETRIES
        aliyun_embedding_batch_size = DEFAULT_ALIYUN_EMBEDDING_BATCH_SIZE
        llm_timeout_seconds = 45.0
    aliyun_embedding_batch_size = min(max(aliyun_embedding_batch_size, 1), MAX_DASHSCOPE_EMBEDDING_BATCH_SIZE)

    trace_dir = Path(_get_config_value("REQPILOT_AI_TRACE_DIR", str(TRACE_DIR)))

    azure_auth_mode = _get_config_value("REQPILOT_AZURE_AUTH_MODE", "auto").lower()
    if azure_auth_mode == "managed_identity":
        azure_auth_mode = "rbac"
    if azure_auth_mode not in {"auto", "api_key", "rbac"}:
        azure_auth_mode = "auto"
    allow_interactive_login = _get_config_value("REQPILOT_AZURE_ALLOW_INTERACTIVE_LOGIN").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    azure_search_endpoint = _get_config_value("AZURE_AI_SEARCH_ENDPOINT")
    azure_search_api_key = _get_config_value("AZURE_AI_SEARCH_API_KEY")
    azure_search_index_name = _get_config_value("AZURE_AI_SEARCH_INDEX_NAME", DEFAULT_DASHVECTOR_COLLECTION_NAME) or DEFAULT_DASHVECTOR_COLLECTION_NAME
    azure_openai_endpoint = _get_config_value("AZURE_OPENAI_ENDPOINT")
    azure_openai_api_key = _get_config_value("AZURE_OPENAI_API_KEY")
    azure_openai_chat_deployment = _get_config_value("AZURE_OPENAI_CHAT_DEPLOYMENT")
    azure_openai_embedding_deployment = _get_config_value("AZURE_OPENAI_EMBEDDING_DEPLOYMENT")
    requested_mode_raw = _get_config_value("REQPILOT_AI_ASSESSMENT_MODE").lower()
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
    azure_backend = _get_config_value("REQPILOT_RETRIEVAL_BACKEND").lower()
    if not azure_backend:
        if azure_mode == "heuristic":
            azure_backend = "local"
        elif azure_auth_mode == "rbac" or (azure_auth_mode == "auto" and not azure_search_api_key):
            azure_backend = "azure_sdk"
        else:
            azure_backend = "azure_haystack"
    if azure_backend not in {"local", "azure_haystack", "azure_sdk"}:
        azure_backend = "local"

    dashscope_api_key = _first_nonempty_env(
        "REQPILOT_ALIYUN_API_KEY",
        "REQPILOT_DASHSCOPE_API_KEY",
        "DASHSCOPE_API_KEY",
    )
    dashscope_base_url = _first_nonempty_env(
        "REQPILOT_DASHSCOPE_BASE_URL",
        "DASHSCOPE_BASE_URL",
    ) or DEFAULT_DASHSCOPE_BASE_URL
    aliyun_mode = _parse_assessment_mode(
        _get_config_value("REQPILOT_ALIYUN_ASSESSMENT_MODE", "llm_shadow"),
        default="llm_shadow",
    )
    azure_screening_model = _get_config_value("AZURE_OPENAI_SCREENING_DEPLOYMENT") or azure_openai_chat_deployment
    aliyun_chat_model = _get_config_value("REQPILOT_ALIYUN_CHAT_MODEL", DEFAULT_DASHSCOPE_CHAT_MODEL) or DEFAULT_DASHSCOPE_CHAT_MODEL
    aliyun_screening_model = (
        _get_config_value("REQPILOT_ALIYUN_SCREENING_CHAT_MODEL", DEFAULT_DASHSCOPE_SCREENING_MODEL)
        or DEFAULT_DASHSCOPE_SCREENING_MODEL
    )
    aliyun_embedding_model = _get_config_value("REQPILOT_ALIYUN_EMBEDDING_MODEL", DEFAULT_DASHSCOPE_EMBEDDING_MODEL) or DEFAULT_DASHSCOPE_EMBEDDING_MODEL
    aliyun_visual_text_model = (
        _get_config_value(
            "REQPILOT_ALIYUN_VISUAL_TEXT_MODEL",
            DEFAULT_BAILIAN_OCR_MODEL,
        )
        or DEFAULT_BAILIAN_OCR_MODEL
    )
    requested_aliyun_backend = _get_config_value("REQPILOT_ALIYUN_RETRIEVAL_BACKEND").lower()
    aliyun_backend = "qdrant" if requested_aliyun_backend == "qdrant" else DISABLED_RETRIEVAL_BACKEND
    aliyun_qdrant_url = _normalize_qdrant_url(
        _first_nonempty_env(
            "REQPILOT_ALIYUN_QDRANT_URL",
            "REQPILOT_QDRANT_URL",
            "QDRANT_URL",
        )
    )
    aliyun_qdrant_api_key = _first_nonempty_env(
        "REQPILOT_ALIYUN_QDRANT_API_KEY",
        "REQPILOT_QDRANT_API_KEY",
        "QDRANT_API_KEY",
    )
    aliyun_qdrant_collection = str(
        _first_nonempty_env(
            "REQPILOT_ALIYUN_QDRANT_COLLECTION",
            "REQPILOT_QDRANT_COLLECTION",
        )
        or DEFAULT_ALIYUN_LOCAL_QDRANT_COLLECTION_NAME
    ).strip() or DEFAULT_ALIYUN_LOCAL_QDRANT_COLLECTION_NAME

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
            azure_openai_api_version=_get_config_value("AZURE_OPENAI_API_VERSION", DEFAULT_AZURE_OPENAI_API_VERSION)
            or DEFAULT_AZURE_OPENAI_API_VERSION,
            azure_openai_chat_deployment=azure_openai_chat_deployment,
            azure_openai_embedding_deployment=azure_openai_embedding_deployment,
            azure_openai_embedding_dimensions=azure_embedding_dimensions,
            azure_search_semantic_configuration=_get_config_value("AZURE_AI_SEARCH_SEMANTIC_CONFIGURATION"),
            llm_timeout_seconds=llm_timeout_seconds,
            openai_max_retries=azure_openai_max_retries,
            embedding_batch_size=azure_embedding_batch_size,
            chat_model=azure_openai_chat_deployment,
            screening_model=azure_screening_model,
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
            openai_max_retries=aliyun_openai_max_retries,
            embedding_batch_size=aliyun_embedding_batch_size,
            chat_model=aliyun_chat_model,
            screening_model=aliyun_screening_model,
            embedding_model=aliyun_embedding_model,
            dashscope_api_key=dashscope_api_key,
            dashscope_base_url=dashscope_base_url,
            dashscope_embedding_dimensions=aliyun_embedding_dimensions,
            qdrant_url=aliyun_qdrant_url,
            qdrant_api_key=aliyun_qdrant_api_key,
            qdrant_collection_name=aliyun_qdrant_collection,
            visual_text_model=aliyun_visual_text_model,
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
    # When Azure falls back to heuristic mode because no Azure infrastructure is
    # configured, ``provider_ready`` is True (local path needs nothing) but the
    # profile is misleading if reported as "ready" in the UI.  Surface a more
    # honest signal: Azure is only "ready" when its LLM or retrieval services
    # are actually reachable.
    effective_ready = config.provider_ready
    if effective_ready and config.provider == DEFAULT_AZURE_PROFILE_ID and not config.azure_ready:
        effective_ready = False
    return {
        "id": config.profile_id,
        "provider": config.provider,
        "label": config.display_name,
        "assessment_mode": config.assessment_mode,
        "retrieval_backend": config.retrieval_backend,
        "chat_model": config.chat_model,
        "embedding_model": config.embedding_model,
        "auth_mode": config.auth_mode,
        "ready": effective_ready,
        "llm_ready": config.llm_runtime_ready,
        "retrieval_ready": config.retrieval_runtime_ready,
    }


def _set_runtime_config_value(name: str, value: str | None) -> None:
    if value is None:
        return
    text = str(value).strip()
    _RUNTIME_AI_ENV_OVERRIDES[name] = text


def _material_runtime_override_snapshot() -> dict[str, str]:
    return {
        key: value
        for key, value in _RUNTIME_AI_ENV_OVERRIDES.items()
        if key != "REQPILOT_RUNTIME_CONNECTION_REVISION"
    }


def clear_runtime_ai_provider_overrides() -> None:
    _RUNTIME_AI_ENV_OVERRIDES.clear()
    _clear_runtime_connection_caches()


def is_ascii_runtime_secret(value: str) -> bool:
    text = str(value or "")
    return not text or text.isascii()


def validate_dashscope_compatible_base_url(value: str) -> str:
    """Return a canonical, allowlisted DashScope OpenAI-compatible base URL."""

    raw_value = str(value or "").strip()
    try:
        parsed = urlsplit(raw_value)
        port = parsed.port
        hostname = str(parsed.hostname or "").lower()
    except ValueError as exc:
        raise ValueError(
            "Aliyun base URL must use an approved DashScope compatible endpoint."
        ) from exc
    host_allowed = hostname in _DASHSCOPE_PUBLIC_HOSTS or bool(
        _DASHSCOPE_WORKSPACE_HOST_PATTERN.fullmatch(hostname)
    )
    if (
        parsed.scheme.lower() != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or not host_allowed
        or parsed.path.rstrip("/") != "/compatible-mode/v1"
        or parsed.query
        or parsed.fragment
        or "?" in raw_value
        or "#" in raw_value
    ):
        raise ValueError(
            "Aliyun base URL must use an approved DashScope compatible endpoint."
        )
    return urlunsplit(
        ("https", hostname, "/compatible-mode/v1", "", "")
    )


def _dedupe_preserving_order(values: list[str]) -> list[str]:
    return list(dict.fromkeys(str(item) for item in values if str(item)))


def build_runtime_ai_provider_setup() -> dict[str, Any]:
    default_profile = load_ai_copilot_config().profile_id
    azure_config = load_ai_copilot_config(DEFAULT_AZURE_PROFILE_ID)
    aliyun_config = load_ai_copilot_config(DEFAULT_ALIYUN_PROFILE_ID)
    profiles = [
        {
            **serialize_ai_profile(config),
            "default": config.profile_id == default_profile,
            "missing": get_ai_profile_missing_config(config),
        }
        for config in list_ai_copilot_profiles()
    ]
    return {
        "default_profile_id": default_profile,
        "provider_selection_required_before_knowledge_upload": True,
        "profiles": profiles,
        "azure": {
            "provider": DEFAULT_AZURE_PROFILE_ID,
            "auth_mode": azure_config.azure_auth_mode or "rbac",
            "allow_interactive_login": azure_config.azure_allow_interactive_login,
            "ready": azure_config.provider_ready and azure_config.azure_ready,
            "missing": get_ai_profile_missing_config(azure_config),
            "recommended_auth": "azure_cli_rbac",
            "interactive_browser_supported": True,
            "document_intelligence_endpoint": _get_config_value(
                "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT"
            ),
            "document_intelligence_api_key_configured": bool(
                _get_config_value("AZURE_DOCUMENT_INTELLIGENCE_API_KEY")
            ),
            "search_endpoint": _get_config_value("AZURE_AI_SEARCH_ENDPOINT"),
            "search_index_name": _get_config_value("AZURE_AI_SEARCH_INDEX_NAME"),
            "openai_endpoint": _get_config_value("AZURE_OPENAI_ENDPOINT"),
            "chat_deployment": _get_config_value("AZURE_OPENAI_CHAT_DEPLOYMENT"),
            "sol_deployment": _get_config_value(
                "AZURE_OPENAI_SOL_DEPLOYMENT",
                _get_config_value("AZURE_OPENAI_CHAT_DEPLOYMENT"),
            ),
            "terra_deployment": _get_config_value(
                "AZURE_OPENAI_TERRA_DEPLOYMENT",
                _get_config_value("AZURE_OPENAI_CHAT_DEPLOYMENT"),
            ),
            "embedding_deployment": _get_config_value(
                "AZURE_OPENAI_EMBEDDING_DEPLOYMENT"
            ),
            "screening_deployment": _get_config_value(
                "AZURE_OPENAI_SCREENING_DEPLOYMENT"
            ),
            "assessment_mode": azure_config.assessment_mode,
        },
        "aliyun": {
            "provider": DEFAULT_ALIYUN_PROFILE_ID,
            "retrieval_backend": aliyun_config.retrieval_backend,
            "ready": aliyun_config.provider_ready,
            "missing": get_ai_profile_missing_config(aliyun_config),
            "base_url": aliyun_config.dashscope_base_url or DEFAULT_DASHSCOPE_BASE_URL,
            "chat_model": aliyun_config.chat_model or DEFAULT_DASHSCOPE_CHAT_MODEL,
            "screening_model": aliyun_config.screening_model
            or DEFAULT_DASHSCOPE_SCREENING_MODEL,
            "embedding_model": aliyun_config.embedding_model
            or DEFAULT_DASHSCOPE_EMBEDDING_MODEL,
            "qdrant_url": aliyun_config.qdrant_url or DEFAULT_LOCAL_QDRANT_URL,
            "qdrant_collection": aliyun_config.qdrant_collection_name
            or DEFAULT_ALIYUN_LOCAL_QDRANT_COLLECTION_NAME,
            "api_key_configured": bool(aliyun_config.dashscope_api_key),
            "qdrant_api_key_configured": bool(aliyun_config.qdrant_api_key),
        },
        "connection_revision": get_runtime_connection_revision(),
    }


def apply_runtime_ai_provider_setup(payload: dict[str, Any]) -> dict[str, Any]:
    with _RUNTIME_AI_CONFIG_LOCK:
        previous_overrides = dict(_RUNTIME_AI_ENV_OVERRIDES)
        try:
            return _apply_runtime_ai_provider_setup_mutating(payload)
        except Exception:
            _RUNTIME_AI_ENV_OVERRIDES.clear()
            _RUNTIME_AI_ENV_OVERRIDES.update(previous_overrides)
            raise


def _apply_runtime_ai_provider_setup_mutating(
    payload: dict[str, Any],
) -> dict[str, Any]:
    previous_material_snapshot = _material_runtime_override_snapshot()
    default_profile_id = str(payload.get("default_profile_id") or "").strip().lower()
    aliyun_payload = payload.get("aliyun") or {}
    reading_visual_payload = aliyun_payload.get("reading_visual")
    validated_aliyun_base_url: str | None = None
    if default_profile_id == DEFAULT_ALIYUN_PROFILE_ID or aliyun_payload:
        validated_aliyun_base_url = validate_dashscope_compatible_base_url(
            str(
                aliyun_payload.get("base_url")
                or _get_config_value("REQPILOT_DASHSCOPE_BASE_URL")
                or DEFAULT_DASHSCOPE_BASE_URL
            )
        )
    if default_profile_id in {DEFAULT_AZURE_PROFILE_ID, DEFAULT_ALIYUN_PROFILE_ID}:
        _set_runtime_config_value("REQPILOT_AI_DEFAULT_PROFILE", default_profile_id)

    azure_payload = payload.get("azure") or {}
    if default_profile_id == DEFAULT_AZURE_PROFILE_ID or azure_payload:
        _set_runtime_config_value(
            "REQPILOT_AZURE_AUTH_MODE", str(azure_payload.get("auth_mode") or "rbac")
        )
        # Persist Azure deployment infrastructure details
        if str(azure_payload.get("document_intelligence_endpoint") or "").strip():
            _set_runtime_config_value(
                "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT",
                str(azure_payload["document_intelligence_endpoint"]).strip(),
            )
        if str(azure_payload.get("document_intelligence_api_key") or "").strip():
            _set_runtime_config_value(
                "AZURE_DOCUMENT_INTELLIGENCE_API_KEY",
                str(azure_payload["document_intelligence_api_key"]).strip(),
            )
        if str(azure_payload.get("search_endpoint") or "").strip():
            _set_runtime_config_value(
                "AZURE_AI_SEARCH_ENDPOINT",
                str(azure_payload["search_endpoint"]).strip(),
            )
        if str(azure_payload.get("search_index_name") or "").strip():
            _set_runtime_config_value(
                "AZURE_AI_SEARCH_INDEX_NAME",
                str(azure_payload["search_index_name"]).strip(),
            )
        if str(azure_payload.get("openai_endpoint") or "").strip():
            _set_runtime_config_value(
                "AZURE_OPENAI_ENDPOINT", str(azure_payload["openai_endpoint"]).strip()
            )
        if str(azure_payload.get("chat_deployment") or "").strip():
            _set_runtime_config_value(
                "AZURE_OPENAI_CHAT_DEPLOYMENT",
                str(azure_payload["chat_deployment"]).strip(),
            )
        if str(azure_payload.get("sol_deployment") or "").strip():
            _set_runtime_config_value(
                "AZURE_OPENAI_SOL_DEPLOYMENT",
                str(azure_payload["sol_deployment"]).strip(),
            )
        if str(azure_payload.get("terra_deployment") or "").strip():
            _set_runtime_config_value(
                "AZURE_OPENAI_TERRA_DEPLOYMENT",
                str(azure_payload["terra_deployment"]).strip(),
            )
        if str(azure_payload.get("embedding_deployment") or "").strip():
            _set_runtime_config_value(
                "AZURE_OPENAI_EMBEDDING_DEPLOYMENT",
                str(azure_payload["embedding_deployment"]).strip(),
            )
        if str(azure_payload.get("screening_deployment") or "").strip():
            _set_runtime_config_value(
                "AZURE_OPENAI_SCREENING_DEPLOYMENT",
                str(azure_payload["screening_deployment"]).strip(),
            )
        # Auto-detect assessment mode: enable llm_shadow when all required infra vars are present
        azure_search_ep = _get_config_value("AZURE_AI_SEARCH_ENDPOINT")
        azure_openai_ep = _get_config_value("AZURE_OPENAI_ENDPOINT")
        azure_chat_dep = _get_config_value("AZURE_OPENAI_CHAT_DEPLOYMENT")
        azure_embed_dep = _get_config_value("AZURE_OPENAI_EMBEDDING_DEPLOYMENT")
        if azure_search_ep and azure_openai_ep and azure_chat_dep and azure_embed_dep:
            _set_runtime_config_value("REQPILOT_AI_ASSESSMENT_MODE", "llm_shadow")
    if "allow_interactive_login" in azure_payload:
        _set_runtime_config_value(
            "REQPILOT_AZURE_ALLOW_INTERACTIVE_LOGIN",
            "true" if bool(azure_payload.get("allow_interactive_login")) else "",
        )

    if default_profile_id == DEFAULT_ALIYUN_PROFILE_ID or aliyun_payload:
        _set_runtime_config_value(
            "REQPILOT_ALIYUN_RETRIEVAL_BACKEND",
            str(aliyun_payload.get("retrieval_backend") or "qdrant"),
        )
        _set_runtime_config_value("REQPILOT_ALIYUN_ASSESSMENT_MODE", "llm_shadow")
        _set_runtime_config_value(
            "REQPILOT_DASHSCOPE_BASE_URL",
            validated_aliyun_base_url,
        )
        _set_runtime_config_value(
            "REQPILOT_ALIYUN_CHAT_MODEL",
            str(
                aliyun_payload.get("chat_model")
                or _get_config_value("REQPILOT_ALIYUN_CHAT_MODEL")
                or DEFAULT_DASHSCOPE_CHAT_MODEL
            ),
        )
        _set_runtime_config_value(
            "REQPILOT_ALIYUN_SCREENING_CHAT_MODEL",
            str(
                aliyun_payload.get("screening_model")
                or _get_config_value("REQPILOT_ALIYUN_SCREENING_CHAT_MODEL")
                or DEFAULT_DASHSCOPE_SCREENING_MODEL
            ),
        )
        _set_runtime_config_value(
            "REQPILOT_ALIYUN_EMBEDDING_MODEL",
            str(
                aliyun_payload.get("embedding_model")
                or _get_config_value("REQPILOT_ALIYUN_EMBEDDING_MODEL")
                or DEFAULT_DASHSCOPE_EMBEDDING_MODEL
            ),
        )
        _set_runtime_config_value(
            "REQPILOT_ALIYUN_QDRANT_URL",
            str(
                aliyun_payload.get("qdrant_url")
                or _get_config_value("REQPILOT_ALIYUN_QDRANT_URL")
                or DEFAULT_LOCAL_QDRANT_URL
            ),
        )
        _set_runtime_config_value(
            "REQPILOT_ALIYUN_QDRANT_COLLECTION",
            str(
                aliyun_payload.get("qdrant_collection")
                or _get_config_value("REQPILOT_ALIYUN_QDRANT_COLLECTION")
                or DEFAULT_ALIYUN_LOCAL_QDRANT_COLLECTION_NAME
            ),
        )
    if str(aliyun_payload.get("api_key") or "").strip():
        _set_runtime_config_value(
            "REQPILOT_ALIYUN_API_KEY", str(aliyun_payload.get("api_key") or "")
        )
    if str(aliyun_payload.get("qdrant_api_key") or "").strip():
        _set_runtime_config_value(
            "REQPILOT_ALIYUN_QDRANT_API_KEY",
            str(aliyun_payload.get("qdrant_api_key") or ""),
        )
    if reading_visual_payload is not None:
        from reqpilotx_server.application.reading_visual_configuration import (
            reading_visual_persisted_values,
            validate_reading_visual_setup_payload,
        )

        validate_reading_visual_setup_payload(reading_visual_payload)
        for key, value in reading_visual_persisted_values().items():
            _set_runtime_config_value(key, value)
    material_changed = (
        _material_runtime_override_snapshot() != previous_material_snapshot
    )
    if not material_changed:
        return build_runtime_ai_provider_setup()
    _set_runtime_config_value(
        "REQPILOT_RUNTIME_CONNECTION_REVISION",
        str(get_runtime_connection_revision() + 1),
    )
    response = build_runtime_ai_provider_setup()
    _save_runtime_config_to_file()
    return response


def get_ai_profile_missing_config(config: AICopilotConfig) -> list[str]:
    missing: list[str] = []
    if config.provider == DEFAULT_ALIYUN_PROFILE_ID:
        if config.llm_enabled and not config.dashscope_api_key:
            missing.append("DASHSCOPE_API_KEY")
        elif config.llm_enabled and not is_ascii_runtime_secret(
            config.dashscope_api_key
        ):
            missing.append(ALIYUN_API_KEY_NON_ASCII_REASON)
        if config.llm_enabled and not config.dashscope_base_url:
            missing.append("REQPILOT_DASHSCOPE_BASE_URL")
        if config.llm_enabled and not config.chat_model:
            missing.append("REQPILOT_ALIYUN_CHAT_MODEL")
        if config.llm_enabled and not config.screening_model:
            missing.append("REQPILOT_ALIYUN_SCREENING_CHAT_MODEL")
        if config.retrieval_backend == DISABLED_RETRIEVAL_BACKEND:
            missing.append(ALIYUN_RETRIEVAL_DISABLED_REASON)
        elif config.retrieval_backend == "qdrant":
            if not config.dashscope_api_key:
                missing.append("DASHSCOPE_API_KEY")
            elif not is_ascii_runtime_secret(config.dashscope_api_key):
                missing.append(ALIYUN_API_KEY_NON_ASCII_REASON)
            if not config.dashscope_base_url:
                missing.append("REQPILOT_DASHSCOPE_BASE_URL")
            if not config.embedding_model:
                missing.append("REQPILOT_ALIYUN_EMBEDDING_MODEL")
            if not config.qdrant_url:
                missing.append("REQPILOT_ALIYUN_QDRANT_URL")
            if not config.qdrant_collection_name:
                missing.append("REQPILOT_ALIYUN_QDRANT_COLLECTION")
            if config.qdrant_api_key and not is_ascii_runtime_secret(
                config.qdrant_api_key
            ):
                missing.append(ALIYUN_QDRANT_API_KEY_NON_ASCII_REASON)
        else:
            missing.append("REQPILOT_ALIYUN_RETRIEVAL_BACKEND=qdrant")
        return _dedupe_preserving_order(missing)

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
    return _dedupe_preserving_order(missing)


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
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
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
        progress_callback=progress_callback,
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
    if _is_visual_only_requirement(context):
        return _finalize_assessment_result(
            build_visual_only_assessment_result(context=context, screening=screening),
            [],
            collector,
            final_mode="visual_only_routing",
            final_model_name=screening["model_name"],
            final_prompt_version=screening["prompt_version"],
        )
    if screening.get("screening_failed"):
        return _finalize_assessment_result(
            build_screening_failed_assessment_result(context=context, screening=screening),
            [],
            collector,
            final_mode="screening_failed",
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

    try:
        retriever = _select_retriever(config)
    except RuntimeError as exc:
        if config.assessment_mode != "heuristic":
            return _finalize_assessment_result(
                build_analysis_failed_assessment_result(
                    context=context,
                    screening=screening,
                    retrieval_backend=str(config.retrieval_backend or ""),
                    failure_reason=str(exc),
                ),
                [],
                collector,
                final_mode="analysis_failed",
                final_model_name=screening["model_name"],
                final_prompt_version=screening["prompt_version"],
            )
        raise
    _emit_progress_stage_update(
        progress_callback,
        stage="retrieval",
        mode=screening.get("final_mode") or "screening_only",
        model_name=screening.get("model_name") or "",
        provider=config.provider,
        message="筛选完成，正在检索候选证据。",
    )
    collector.retrieval_backend = retriever.backend_name
    evidences = retriever.retrieve(context, knowledge_docs, top_k=config.top_k)
    evidences = _extend_evidence_with_adjacent_chunks(evidences, knowledge_docs=knowledge_docs)
    heuristics = HeuristicAssessmentEngine()

    if config.assessment_mode in {"heuristic", "llm_shadow"}:
        _emit_progress_stage_update(
            progress_callback,
            stage="assessment",
            mode="heuristic",
            model_name=heuristics.model_name,
            provider=config.provider,
            message="候选证据已就绪，正在应用规则引擎生成结论。",
        )
        heuristic_result = heuristics.assess(context, evidences)
        final_result = reconcile_assessment_output(
            context=context,
            candidate=heuristic_result,
            retrieved_evidences=evidences,
            fallback_reason="",
        )
        final_result["analysis_basis"] = _build_requirement_analysis_basis(
            context=context,
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
        if config.assessment_mode == "heuristic":
            return _finalize_assessment_result(
                final_result,
                evidences,
                collector,
                final_mode="heuristic",
                final_model_name=heuristics.model_name,
                final_prompt_version=heuristics.prompt_version,
            )

    llm_engine = _select_assessment_engine(config)
    assessment_progress_callback = _progress_callback_with_metadata(
        progress_callback,
        stage="assessment",
        mode="llm_shadow" if config.assessment_mode == "llm_shadow" else "llm_primary",
        model_name=llm_engine.model_name,
        provider=config.provider,
    )
    _emit_progress_stage_update(
        progress_callback,
        stage="assessment",
        mode="llm_shadow" if config.assessment_mode == "llm_shadow" else "llm_primary",
        model_name=llm_engine.model_name,
        provider=config.provider,
        message="候选证据已就绪，正在生成结构化结论。",
    )
    if config.assessment_mode == "llm_shadow":
        # The heuristic result above is the serving result. Reconciliation here
        # validates shadow telemetry only and must never replace final_result.
        try:
            llm_result = llm_engine.assess(context, evidences, progress_callback=assessment_progress_callback)
            llm_final = reconcile_assessment_output(
                context=context,
                candidate=llm_result,
                retrieved_evidences=evidences,
                fallback_reason="",
                retrieval_backend=retriever.backend_name,
                reconciliation_policy=_reconciliation_policy_for_config(config),
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
        except AIAnalysisCancelledError:
            raise
        except Exception as exc:  # pragma: no cover - network/provider dependent
            failure_reason = _provider_failure_reason(exc)
            collector.record_failure(
                mode="llm_shadow",
                model_name=llm_engine.model_name,
                prompt_version=llm_engine.prompt_version,
                input_payload={"context": context, "evidences": evidences},
                fallback_reason=failure_reason,
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
        llm_result = llm_engine.assess(context, evidences, progress_callback=assessment_progress_callback)
        final_result = reconcile_assessment_output(
            context=context,
            candidate=llm_result,
            retrieved_evidences=evidences,
            fallback_reason="",
            retrieval_backend=retriever.backend_name,
            reconciliation_policy=_reconciliation_policy_for_config(config),
        )
        final_result["analysis_basis"] = _build_requirement_analysis_basis(
            context=context,
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
    except AIAnalysisCancelledError:
        raise
    except Exception as exc:  # pragma: no cover - network/provider dependent
        failure_reason = _provider_failure_reason(exc)
        collector.record_failure(
            mode="llm_primary",
            model_name=llm_engine.model_name,
            prompt_version=llm_engine.prompt_version,
            input_payload={"context": context, "evidences": evidences},
            fallback_reason=failure_reason,
            retrieval_backend=retriever.backend_name,
        )
        return _finalize_assessment_result(
            build_analysis_failed_assessment_result(
                context=context,
                screening=screening,
                retrieval_backend=retriever.backend_name,
                failure_reason=failure_reason,
            ),
            [],
            collector,
            final_mode="analysis_failed",
            final_model_name=llm_engine.model_name,
            final_prompt_version=llm_engine.prompt_version,
        )

    return _finalize_assessment_result(
        final_result,
        evidences,
        collector,
        final_mode="llm_primary",
        final_model_name=llm_engine.model_name,
        final_prompt_version=llm_engine.prompt_version,
    )


def sync_knowledge_document_index(
    project_id: str,
    knowledge_doc: dict[str, Any],
    *,
    config: AICopilotConfig | None = None,
    profile_ids: list[str] | None = None,
    payload_dir: Path | None = None,
) -> dict[str, Any]:
    lifecycle = evaluate_knowledge_evidence_eligibility(knowledge_doc)
    chunks = build_knowledge_chunks(project_id, knowledge_doc) if lifecycle.allowed else []
    indexing_precondition_error = ""
    if not lifecycle.allowed:
        indexing_precondition_error = (
            f"knowledge_document_not_eligible: {lifecycle.reason}"
        )
    elif not chunks:
        indexing_precondition_error = "knowledge_document_has_no_chunks"
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
    # Persist the current canonical payload before touching a remote provider,
    # but do not claim any profile was synced until its upsert succeeds.
    payload_path = write_knowledge_index_payload(
        knowledge_doc,
        chunks,
        profile_ids=[],
        payload_dir=payload_dir,
    )
    synced_at = _now_iso_utc()

    targets: dict[str, dict[str, Any]] = {}
    synced_profile_ids: list[str] = []
    for selected in selected_profiles:
        status = "READY"
        error = ""
        written_chunk_count = 0
        try:
            evidence_index_resource = _evidence_index_resource(selected)
        except ValueError as exc:
            evidence_index_resource = str(selected.dashvector_collection_name or "")
            status = "ERROR"
            error = str(exc)
        if indexing_precondition_error:
            status = "ERROR"
            error = indexing_precondition_error
        elif status == "ERROR":
            pass
        elif selected.provider == DEFAULT_ALIYUN_PROFILE_ID and selected.retrieval_backend == DISABLED_RETRIEVAL_BACKEND:
            status = "ERROR"
            error = ALIYUN_RETRIEVAL_DISABLED_MESSAGE
        elif selected.retrieval_backend == "local":
            status = "READY"
            written_chunk_count = len(chunks)
        elif not selected.retrieval_runtime_ready:
            status = "ERROR"
            error = f"{selected.display_name} retrieval runtime is not configured."
        else:
            try:
                written_chunk_count = int(
                    _select_indexer(selected).replace_chunks(chunks) or 0
                )
                if written_chunk_count != len(chunks):
                    status = "ERROR"
                    error = (
                        "knowledge_index_write_count_mismatch:"
                        f"{written_chunk_count}/{len(chunks)}"
                    )
            except Exception as exc:  # pragma: no cover - network/provider dependent
                status = "ERROR"
                error = str(exc)
        if status == "READY":
            synced_profile_ids.append(selected.profile_id)
        targets[selected.profile_id] = {
            "profile_id": selected.profile_id,
            "provider": selected.provider,
            "profile_label": selected.display_name,
            "retrieval_backend": selected.retrieval_backend,
            "status": status,
            "error": error,
            "synced_at": synced_at,
            "chunk_count": written_chunk_count,
            "planned_chunk_count": len(chunks),
            "payload_path": str(payload_path),
            "evidence_contract_version": EVIDENCE_INDEX_CONTRACT_VERSION,
            "evidence_index_resync_action": "" if status == "READY" else EVIDENCE_INDEX_RESYNC_ACTION,
            "evidence_index_resource": evidence_index_resource,
        }
    payload_path = write_knowledge_index_payload(
        knowledge_doc,
        chunks,
        profile_ids=synced_profile_ids,
        payload_dir=payload_dir,
    )
    target_states = list(targets.values())
    ready_count = sum(1 for item in target_states if str(item.get("status") or "") == "READY")
    overall_status = (
        "ERROR"
        if not target_states
        else "READY"
        if ready_count == len(target_states)
        else "PARTIAL"
        if ready_count
        else "ERROR"
    )
    overall_error = "; ".join(
        str(item.get("error") or "").strip()
        for item in target_states
        if str(item.get("error") or "").strip()
    )
    overall_chunk_count = min(
        (int(item.get("chunk_count") or 0) for item in target_states),
        default=0,
    )
    return {
        "status": overall_status,
        "error": overall_error,
        "synced_at": synced_at,
        "chunk_count": overall_chunk_count,
        "planned_chunk_count": len(chunks),
        "payload_path": str(payload_path),
        "evidence_contract_version": EVIDENCE_INDEX_CONTRACT_VERSION,
        "evidence_index_resync_action": "" if overall_status == "READY" else EVIDENCE_INDEX_RESYNC_ACTION,
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
    source_context = build_requirement_source_context(
        requirement_row,
        atomic_source_text=requirement_text,
        parent_section_code=str(requirement_row.get("parent_section_code") or "").strip(),
        parent_section_title=str(requirement_row.get("parent_section_title") or "").strip(),
    )
    retrieval_requirement = str(normalization.get("retrieval_text") or normalized_requirement).strip()
    normalization_facet_text = str(normalization.get("facet_text") or "").strip()
    dense_query_parts: list[str] = []
    if requirement_text:
        dense_query_parts.append(requirement_text)
    if normalized_requirement and normalized_requirement != requirement_text:
        dense_query_parts.append(normalized_requirement)
    if normalization_facet_text:
        dense_query_parts.append(f"结构化要点：{normalization_facet_text}")
    if section_label:
        dense_query_parts.append(f"章节上下文：{section_label}")
    if content_summary and content_summary != requirement_text:
        dense_query_parts.append(f"内容摘要：{content_summary}")
    if table_context:
        dense_query_parts.append(f"表格上下文：{table_context}")
    dense_retrieval_query = "\n".join(part for part in dense_query_parts if part)
    sparse_retrieval_query = requirement_text

    return {
        "requirement_id": str(requirement_row.get("id") or ""),
        "req_code": str(requirement_row.get("req_code") or ""),
        "entry_kind": str(requirement_row.get("entry_kind") or "REQUIREMENT").strip().upper() or "REQUIREMENT",
        "requirement_type": str(requirement_row.get("requirement_type") or "FUNCTIONAL").strip() or "FUNCTIONAL",
        "requirement_text": requirement_text,
        "atomic_source_text": requirement_text,
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
        "source_context": source_context,
        "context_warnings": list(source_context.get("context_warnings") or []),
        "context_completeness": dict(source_context.get("context_completeness") or {}),
        "context_completeness_status": dict(
            source_context.get("context_completeness_status") or {}
        ),
        "retrieval_query": dense_retrieval_query,
        "dense_retrieval_query": dense_retrieval_query,
        "sparse_retrieval_query": sparse_retrieval_query,
        "retrieval_gate_reason": "",
    }


def build_requirement_source_context(
    requirement_row: dict[str, Any],
    *,
    atomic_source_text: str,
    parent_section_code: str,
    parent_section_title: str,
) -> dict[str, Any]:
    """Build a bounded, explicit context contract for one assessment item.

    Optional hierarchy fields are passed through only when the caller actually
    owns them. Missing hierarchy is surfaced as a warning instead of being
    reconstructed from model assumptions.
    """

    full_parent_clause = _first_context_text(
        requirement_row,
        "full_parent_clause",
        "parent_clause_text",
        "parent_context_statement",
        "parent_requirement_text",
    )
    direct_parent = _first_context_value(
        requirement_row,
        "direct_parent",
        "parent_requirement",
        "parent_entry",
    )
    direct_children = _first_context_value(
        requirement_row,
        "direct_children",
        "child_requirements",
        "children",
    )
    adjacent_context = _first_context_value(
        requirement_row,
        "adjacent_context",
        "adjacent_entries",
        "sibling_context",
        "neighboring_requirements",
    )
    explicit_ancestor_path = _first_context_value(
        requirement_row,
        "ancestor_path",
        "ancestor_sections",
        "section_path",
    )
    explicit_ancestor_items = _bounded_context_items(
        explicit_ancestor_path,
        max_items=12,
        max_chars=5000,
    )
    ancestor_path = list(explicit_ancestor_items)
    declared_ancestor_status = str(
        requirement_row.get("ancestor_path_status") or ""
    ).strip().upper()
    ancestor_path_is_partial = bool(
        ancestor_path and declared_ancestor_status == "PARTIAL"
    )
    if not ancestor_path and (parent_section_code or parent_section_title):
        ancestor_path = [
            {
                "section_code": parent_section_code,
                "section_title": parent_section_title,
                "path_source": "current_parent_section",
            }
        ]
        ancestor_path_is_partial = True

    content_blocks, content_blocks_truncated = _project_content_blocks(
        requirement_row.get("content_blocks")
    )
    table_refs, table_refs_truncated = _project_table_context(
        requirement_row.get("table_refs")
    )
    direct_parent_items = _bounded_context_items(direct_parent, max_items=1, max_chars=3000)
    direct_children_items = _bounded_context_items(direct_children, max_items=8, max_chars=5000)
    adjacent_items = _bounded_context_items(adjacent_context, max_items=8, max_chars=5000)

    warnings: list[str] = []
    if not full_parent_clause:
        warnings.append("full_parent_clause_unavailable")
    if not ancestor_path:
        warnings.append("ancestor_path_unavailable")
    elif ancestor_path_is_partial:
        warnings.append("ancestor_path_partial")
    if not direct_parent_items:
        warnings.append("direct_parent_unavailable")
    if not direct_children_items:
        warnings.append("direct_children_unavailable")
    if not adjacent_items:
        warnings.append("adjacent_context_unavailable")
    if content_blocks_truncated:
        warnings.append("content_blocks_truncated")
    if table_refs_truncated:
        warnings.append("table_context_truncated")

    context_completeness = {
        "atomic_source_text": bool(atomic_source_text),
        "full_parent_clause": bool(full_parent_clause),
        "ancestor_path": bool(explicit_ancestor_items),
        "direct_parent": bool(direct_parent_items),
        "direct_children": bool(direct_children_items),
        "adjacent_context": bool(adjacent_items),
        "content_blocks": bool(content_blocks),
        "table_context": bool(table_refs),
    }
    context_completeness_status = {
        key: ("FULL" if available else "MISSING")
        for key, available in context_completeness.items()
    }
    if ancestor_path_is_partial:
        context_completeness_status["ancestor_path"] = "PARTIAL"

    return {
        "atomic_source_text": atomic_source_text,
        "full_parent_clause": full_parent_clause,
        "parent_clause_source": "explicit" if full_parent_clause else "unavailable",
        "ancestor_path": ancestor_path,
        "direct_parent": direct_parent_items[0] if direct_parent_items else None,
        "direct_children": direct_children_items,
        "adjacent_context": adjacent_items,
        "content_blocks": content_blocks,
        "table_context": table_refs,
        "context_completeness": context_completeness,
        "context_completeness_status": context_completeness_status,
        "context_warnings": warnings,
    }


def _first_context_text(requirement_row: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = requirement_row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:12_000]
    return ""


def _first_context_value(requirement_row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = requirement_row.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _bounded_context_items(value: Any, *, max_items: int, max_chars: int) -> list[Any]:
    if value in (None, "", [], {}):
        return []
    source = value if isinstance(value, list) else [value]
    output: list[Any] = []
    consumed = 0
    for item in source[:max_items]:
        if not isinstance(item, (str, dict)):
            continue
        normalized: Any
        if isinstance(item, str):
            normalized = item.strip()
        else:
            normalized = {
                str(key): nested
                for key, nested in item.items()
                if nested not in (None, "", [], {})
            }
        if not normalized:
            continue
        encoded = json.dumps(normalized, ensure_ascii=False)
        if consumed + len(encoded) > max_chars:
            break
        output.append(normalized)
        consumed += len(encoded)
    return output


def _project_content_blocks(value: Any) -> tuple[list[dict[str, Any]], bool]:
    if not isinstance(value, list):
        return [], False
    allowed_keys = (
        "kind",
        "text",
        "table_id",
        "source_location",
        "page",
        "page_index",
        "section_code",
        "section_title",
    )
    projected: list[dict[str, Any]] = []
    consumed = 0
    truncated = False
    for raw in value:
        if not isinstance(raw, dict):
            continue
        item = {
            key: raw.get(key)
            for key in allowed_keys
            if raw.get(key) not in (None, "", [], {})
        }
        if not item:
            continue
        encoded = json.dumps(item, ensure_ascii=False)
        if len(projected) >= 30 or consumed + len(encoded) > 12_000:
            truncated = True
            break
        projected.append(item)
        consumed += len(encoded)
    return projected, truncated


def _project_table_context(value: Any) -> tuple[list[dict[str, Any]], bool]:
    if not isinstance(value, list):
        return [], False
    allowed_keys = (
        "table_id",
        "table_title",
        "title",
        "source_location",
        "page",
        "page_index",
        "slice_ai_projection",
        "slice_rows",
        "headers",
        "units",
        "merged_cells",
        "notes",
    )
    projected: list[dict[str, Any]] = []
    consumed = 0
    truncated = False
    for raw in value:
        if not isinstance(raw, dict):
            continue
        item = {
            key: raw.get(key)
            for key in allowed_keys
            if raw.get(key) not in (None, "", [], {})
        }
        if not item:
            continue
        encoded = json.dumps(item, ensure_ascii=False)
        if len(projected) >= 12 or consumed + len(encoded) > 16_000:
            truncated = True
            break
        projected.append(item)
        consumed += len(encoded)
    return projected, truncated


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
    original = normalize_requirement_text(text)
    remaining = original
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
    if not remaining:
        return original
    if len(remaining) < 24:
        return original
    if len(original) > 0 and len(remaining) < int(len(original) * 0.7):
        return original
    return remaining


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

def _eligible_knowledge_documents(
    knowledge_docs: list[dict[str, Any]],
    *,
    as_of: date | None = None,
) -> list[dict[str, Any]]:
    return [
        doc
        for doc in knowledge_docs
        if evaluate_knowledge_evidence_eligibility(doc, as_of=as_of).allowed
        and str(doc.get("id") or "").strip()
    ]


def _filter_retrieved_evidence_candidates(
    evidences: list[dict[str, Any]],
    knowledge_docs: list[dict[str, Any]],
    *,
    as_of: date | None = None,
    context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    eligible_docs = {
        str(doc.get("id") or "").strip(): doc
        for doc in _eligible_knowledge_documents(knowledge_docs, as_of=as_of)
    }
    canonical_candidates = _canonical_evidence_candidate_map(list(eligible_docs.values()))
    filtered: list[dict[str, Any]] = []
    contract_rejected = False
    for evidence in evidences:
        if not has_current_evidence_contract(evidence) or not bool(evidence.get("evidence_contract_verified")):
            contract_rejected = True
            continue
        evidence_id = str(evidence.get("evidence_id") or "").strip()
        content_hash = str(evidence.get("content_hash") or "").strip().lower()
        canonical_candidate = canonical_candidates.get(evidence_id)
        if canonical_candidate is None or canonical_candidate["content_hash"] != content_hash:
            contract_rejected = True
            continue
        source_doc_id = str(evidence.get("source_doc_id") or "").strip()
        if source_doc_id != canonical_candidate["source_doc_id"]:
            contract_rejected = True
            continue
        knowledge_doc = eligible_docs.get(source_doc_id)
        if knowledge_doc is None:
            contract_rejected = True
            continue
        candidate_lifecycle = {
            "status": evidence.get("status"),
            "doc_type": evidence.get("doc_type"),
            "effective_from": evidence.get("effective_from"),
            "effective_to": evidence.get("effective_to"),
        }
        if not evaluate_knowledge_evidence_eligibility(candidate_lifecycle, as_of=as_of).allowed:
            contract_rejected = True
            continue
        if not _lifecycle_metadata_matches(candidate_lifecycle, knowledge_doc):
            contract_rejected = True
            continue
        canonicalized = dict(evidence)
        for field in (
            "source_doc_id",
            "source_doc_name",
            "source_doc_version",
            "source_location",
            "excerpt",
            "doc_type",
            "status",
            "effective_from",
            "effective_to",
            "authority",
            "claim_role",
            "applicability",
            "table_id",
            "native_locator",
            "native_format",
            "native_page",
            "native_table_index",
            "native_body_element_index",
            "native_workbook",
            "native_sheet",
            "native_range",
        ):
            canonicalized[field] = canonical_candidate[field]
        if "content_kind" in canonicalized:
            canonicalized["content_kind"] = canonical_candidate["content_kind"]
        filtered.append(canonicalized)
    if contract_rejected and not filtered and context is not None:
        _mark_evidence_index_resync_required(context)
    return filtered


def _mark_evidence_index_resync_required(
    context: dict[str, Any],
    *,
    detail: str = "",
) -> None:
    context["retrieval_gate_reason"] = EVIDENCE_INDEX_RESYNC_REQUIRED_REASON
    context["retrieval_migration_action"] = EVIDENCE_INDEX_RESYNC_ACTION
    if detail:
        context["retrieval_migration_detail"] = detail


def _is_evidence_contract_schema_error(exc: Exception) -> bool:
    detail = str(exc or "").strip().lower()
    contract_field_mentioned = any(
        field in detail
        for field in ("evidence_id", "content_hash", "evidence_contract_version")
    )
    schema_failure_mentioned = any(
        token in detail
        for token in (
            "unknown field",
            "field not found",
            "does not exist",
            "invalid field",
            "invalid expression",
            "could not find",
        )
    )
    return contract_field_mentioned and schema_failure_mentioned


def _canonical_evidence_candidate_map(
    knowledge_docs: list[dict[str, Any]],
) -> dict[str, dict[str, str]]:
    candidates: dict[str, dict[str, str]] = {}
    conflicts: set[str] = set()
    for knowledge_doc in knowledge_docs:
        project_id = str(knowledge_doc.get("project_id") or "").strip()
        for chunk in build_knowledge_chunks(project_id, knowledge_doc):
            evidence_id = str(chunk.get("evidence_id") or "").strip()
            content_hash = str(chunk.get("content_hash") or "").strip().lower()
            if not evidence_id or not content_hash:
                continue
            meta = dict(chunk.get("meta") or {})
            candidate = {
                "evidence_id": evidence_id,
                "content_hash": content_hash,
                "source_doc_id": str(meta.get("source_doc_id") or "").strip(),
                "source_doc_name": str(meta.get("source_doc_name") or "").strip(),
                "source_doc_version": str(meta.get("source_doc_version") or "").strip(),
                "source_location": str(meta.get("source_location") or "").strip(),
                "excerpt": str(chunk.get("content") or "")[:380].strip(),
                "content_kind": str(meta.get("content_kind") or "").strip(),
                "doc_type": str(meta.get("doc_type") or "").strip(),
                "status": str(meta.get("status") or "").strip(),
                "effective_from": str(meta.get("effective_from") or "").strip(),
                "effective_to": str(meta.get("effective_to") or "").strip(),
                "authority": str(meta.get("authority") or "UNKNOWN").strip(),
                "claim_role": str(meta.get("claim_role") or "UNKNOWN").strip(),
                "applicability": str(meta.get("applicability") or "UNKNOWN").strip(),
                "table_id": str(meta.get("table_id") or "").strip(),
                "native_locator": str(meta.get("native_locator") or "").strip(),
                "native_format": str(meta.get("native_format") or "").strip(),
                "native_page": str(meta.get("native_page") or "").strip(),
                "native_table_index": str(
                    meta.get("native_table_index") or ""
                ).strip(),
                "native_body_element_index": str(
                    meta.get("native_body_element_index") or ""
                ).strip(),
                "native_workbook": str(
                    meta.get("native_workbook") or ""
                ).strip(),
                "native_sheet": str(meta.get("native_sheet") or "").strip(),
                "native_range": str(meta.get("native_range") or "").strip(),
            }
            previous_candidate = candidates.get(evidence_id)
            if previous_candidate is not None and previous_candidate != candidate:
                conflicts.add(evidence_id)
                continue
            candidates[evidence_id] = candidate
    for evidence_id in conflicts:
        candidates.pop(evidence_id, None)
    return candidates


def _canonical_evidence_contract_map(knowledge_docs: list[dict[str, Any]]) -> dict[str, str]:
    return {
        evidence_id: candidate["content_hash"]
        for evidence_id, candidate in _canonical_evidence_candidate_map(knowledge_docs).items()
    }


def _remote_evidence_index_contract_ready(
    knowledge_docs: list[dict[str, Any]],
    *,
    profile_id: str,
    context: dict[str, Any],
) -> bool:
    for knowledge_doc in knowledge_docs:
        document_id = str(knowledge_doc.get("id") or "").strip()
        payload = _knowledge_index_payload(knowledge_doc)
        if payload is None:
            _mark_evidence_index_resync_required(context, detail=f"Missing current index payload for {document_id}.")
            return False
        if str(payload.get("evidence_contract_version") or "").strip() != EVIDENCE_INDEX_CONTRACT_VERSION:
            _mark_evidence_index_resync_required(context, detail=f"Stale index contract for {document_id}.")
            return False
        payload_profiles = {str(item or "").strip().lower() for item in payload.get("profile_ids") or []}
        if str(profile_id or "").strip().lower() not in payload_profiles:
            _mark_evidence_index_resync_required(context, detail=f"Index payload for {document_id} is not synced to {profile_id}.")
            return False
        payload_chunks = payload.get("chunks")
        if not isinstance(payload_chunks, list):
            _mark_evidence_index_resync_required(context, detail=f"Index payload chunks are invalid for {document_id}.")
            return False
        payload_contracts: dict[str, str] = {}
        try:
            for chunk in payload_chunks:
                if not isinstance(chunk, dict):
                    raise ValueError("Invalid chunk payload.")
                evidence_id = _require_chunk_evidence_contract(chunk)
                content_hash = str(chunk.get("content_hash") or "").strip().lower()
                if evidence_id in payload_contracts and payload_contracts[evidence_id] != content_hash:
                    raise ValueError("Conflicting evidence identity in index payload.")
                payload_contracts[evidence_id] = content_hash
        except ValueError as exc:
            _mark_evidence_index_resync_required(context, detail=f"{document_id}: {exc}")
            return False
        if payload_contracts != _canonical_evidence_contract_map([knowledge_doc]):
            _mark_evidence_index_resync_required(context, detail=f"Index payload is stale for {document_id}.")
            return False
    return True


def _lifecycle_metadata_matches(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return (
        str(left.get("status") or "").strip().upper()
        == str(right.get("status") or "").strip().upper()
        and str(left.get("doc_type") or "").strip().upper()
        == str(right.get("doc_type") or "").strip().upper()
        and str(left.get("effective_from") or "").strip()
        == str(right.get("effective_from") or "").strip()
        and str(left.get("effective_to") or "").strip()
        == str(right.get("effective_to") or "").strip()
    )


class HeuristicEvidenceRetriever:
    backend_name = "local"

    def retrieve(self, context: dict[str, Any], knowledge_docs: list[dict[str, Any]], top_k: int) -> list[dict[str, Any]]:
        query = str(context.get("retrieval_query") or context.get("normalized_requirement") or "").strip()
        candidates: list[dict[str, Any]] = []
        eligible_docs = _eligible_knowledge_documents(knowledge_docs)
        for doc in eligible_docs:
            project_id = str(doc.get("project_id") or "").strip()
            for chunk in build_knowledge_chunks(project_id, doc):
                meta = dict(chunk.get("meta") or {})
                content_kind = str(meta.get("content_kind") or "").strip().lower()
                relevance_boost = _content_kind_relevance_boost(content_kind, context=context, doc=doc)
                candidates.append(
                    {
                        "source_doc_id": str(meta.get("source_doc_id") or doc.get("id") or ""),
                        "source_doc_name": str(meta.get("source_doc_name") or doc.get("file_name") or ""),
                        "source_doc_version": str(meta.get("source_doc_version") or doc.get("doc_version") or ""),
                        "source_location": str(meta.get("source_location") or chunk.get("chunk_id") or ""),
                        "excerpt": str(chunk.get("content") or "")[:380],
                        "content": str(chunk.get("content") or ""),
                        "content_kind": content_kind,
                        "doc_type": str(meta.get("doc_type") or ""),
                        "status": str(meta.get("status") or ""),
                        "effective_from": str(meta.get("effective_from") or ""),
                        "effective_to": str(meta.get("effective_to") or ""),
                        **_evidence_contract_fields(meta, content=str(chunk.get("content") or "")),
                        **_evidence_lifecycle_fields(meta),
                        "relevance_boost": relevance_boost,
                    }
                )
        evidences = score_candidate_evidences(query, candidates, top_k=top_k)
        return _filter_retrieved_evidence_candidates(evidences, eligible_docs, context=context)


class AzureHaystackEvidenceRetriever:
    backend_name = "azure_haystack"

    def __init__(self, config: AICopilotConfig) -> None:
        self.config = config

    def retrieve(self, context: dict[str, Any], knowledge_docs: list[dict[str, Any]], top_k: int) -> list[dict[str, Any]]:
        # The public profile name is retained for compatibility, but current
        # evidence-v2 indexes use the explicit Azure SDK schema/key contract.
        # Routing both Azure profiles through the same adapter prevents the
        # Haystack-required `id` key from diverging from the canonical
        # `chunk_id` key used by synchronization and cleanup.
        return AzureSearchSDKEvidenceRetriever(self.config).retrieve(
            context,
            knowledge_docs,
            top_k,
        )


class AzureSearchSDKEvidenceRetriever:
    backend_name = "azure_sdk"

    def __init__(self, config: AICopilotConfig) -> None:
        self.config = config

    def retrieve(self, context: dict[str, Any], knowledge_docs: list[dict[str, Any]], top_k: int) -> list[dict[str, Any]]:
        if not self.config.azure_search_ready or not self.config.azure_openai_embedding_ready:
            raise RuntimeError("Azure AI retrieval is not configured.")

        eligible_docs = _eligible_knowledge_documents(knowledge_docs)
        if not eligible_docs:
            return []
        if not _remote_evidence_index_contract_ready(
            eligible_docs,
            profile_id=self.config.profile_id,
            context=context,
        ):
            return []

        from azure.search.documents import SearchClient
        from azure.search.documents.models import VectorizedQuery

        query = str(context.get("retrieval_query") or context.get("normalized_requirement") or "").strip()
        query_embedding = _embed_text(query, config=self.config)
        filter_payload = build_azure_filter(
            project_id=_single_value(eligible_docs, "project_id"),
            knowledge_docs=eligible_docs,
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
                "evidence_id",
                "content_hash",
                "evidence_contract_version",
                "content",
                "project_id",
                "source_doc_id",
                "source_doc_name",
                "source_doc_version",
                "doc_type",
                "status",
                "effective_from",
                "effective_to",
                "authority",
                "claim_role",
                "applicability",
                "section_code",
                "section_title",
                "source_location",
                "content_kind",
                "table_id",
                "native_locator",
                "native_format",
                "native_page",
                "native_table_index",
                "native_body_element_index",
                "native_workbook",
                "native_sheet",
                "native_range",
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
        evidences: list[dict[str, Any]] = []
        try:
            results = client.search(search_text=query, **search_kwargs)
            for item in results:
                evidences.append(_azure_search_item_to_evidence(item))
        except Exception as exc:
            if _is_evidence_contract_schema_error(exc):
                _mark_evidence_index_resync_required(context, detail=str(exc))
                return []
            raise
        return _filter_retrieved_evidence_candidates(
            [item for item in evidences if item.get("source_doc_id")],
            eligible_docs,
            context=context,
        )


class DashVectorEvidenceRetriever:
    backend_name = "dashvector"

    def __init__(self, config: AICopilotConfig) -> None:
        self.config = config

    def retrieve(self, context: dict[str, Any], knowledge_docs: list[dict[str, Any]], top_k: int) -> list[dict[str, Any]]:
        if not self.config.dashvector_ready or not self.config.dashscope_embedding_ready:
            raise RuntimeError("Aliyun DashVector retrieval is not configured.")

        eligible_docs = _eligible_knowledge_documents(knowledge_docs)
        if not eligible_docs:
            return []
        if not _remote_evidence_index_contract_ready(
            eligible_docs,
            profile_id=self.config.profile_id,
            context=context,
        ):
            return []

        query = str(context.get("retrieval_query") or context.get("normalized_requirement") or "").strip()
        query_embedding = _embed_text(query, config=self.config)
        try:
            contract_config = _dashvector_contract_config(self.config)
            collection = _dashvector_collection(contract_config, create_if_missing=False)
        except (RuntimeError, ValueError) as exc:
            target_collection = str(self.config.dashvector_collection_name or "")
            try:
                target_collection = _dashvector_contract_collection_name(target_collection)
            except ValueError:
                pass
            context["retrieval_gate_reason"] = (
                f"DASHVECTOR_EVIDENCE_INDEX_MIGRATION_REQUIRED：当前 evidence contract 使用独立集合 "
                f"{target_collection}，请修正集合配置并重新同步知识文档。"
            )
            context["retrieval_migration_action"] = DASHVECTOR_INDEX_MIGRATION_ACTION
            context["retrieval_migration_detail"] = str(exc)
            return []
        query_response = collection.query(
            query_embedding,
            topk=top_k,
            filter=_build_dashvector_filter(
                project_id=_single_value(eligible_docs, "project_id"),
                knowledge_docs=eligible_docs,
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
        return _filter_retrieved_evidence_candidates(evidences, eligible_docs, context=context)


class QdrantEvidenceRetriever:
    backend_name = "qdrant"

    def __init__(self, config: AICopilotConfig) -> None:
        self.config = config

    def retrieve(self, context: dict[str, Any], knowledge_docs: list[dict[str, Any]], top_k: int) -> list[dict[str, Any]]:
        if not self.config.qdrant_ready or not self.config.dashscope_embedding_ready:
            raise RuntimeError("Aliyun Qdrant retrieval is not configured.")
        eligible_docs = _eligible_knowledge_documents(knowledge_docs)
        dense_query = str(
            context.get("dense_retrieval_query")
            or context.get("retrieval_query")
            or context.get("requirement_text")
            or context.get("normalized_requirement")
            or ""
        ).strip()
        sparse_query = str(context.get("sparse_retrieval_query") or context.get("requirement_text") or dense_query).strip()
        if not dense_query and not sparse_query:
            context["retrieval_gate_reason"] = "检索查询为空，当前无法生成有效证据。"
            return []
        if not eligible_docs:
            context["retrieval_gate_reason"] = "当前没有处于有效生命周期内的知识文档。"
            return []
        if self._missing_authoritative_regulation_docs(context=context, knowledge_docs=eligible_docs):
            context["retrieval_gate_reason"] = "缺少法规类知识文档，当前无法为该法规条目提供直接证据。"
            return []
        if not _remote_evidence_index_contract_ready(
            eligible_docs,
            profile_id=self.config.profile_id,
            context=context,
        ):
            return []

        allow_noise = self._query_targets_noise_chunks(dense_query or sparse_query)
        query_embedding = _embed_text(dense_query or sparse_query, config=self.config)
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
            "limit": max(QDRANT_HYBRID_DENSE_TOP_K, int(top_k or 0) or 0),
            "with_payload": True,
        }
        query_filter = _build_qdrant_filter(
            project_id=_single_value(eligible_docs, "project_id"),
            knowledge_docs=eligible_docs,
        )
        if query_filter is not None:
            search_kwargs["query_filter"] = query_filter
        points = client.search(**search_kwargs)
        dense_documents = self._dense_documents(points or [], allow_noise=allow_noise)
        sparse_documents = self._sparse_documents(eligible_docs, query=sparse_query, allow_noise=allow_noise)
        fused_documents = self._fuse_documents(dense_documents=dense_documents, sparse_documents=sparse_documents)
        reranked_documents = self._rerank_documents(query=dense_query or sparse_query, documents=fused_documents)
        final_documents = self._select_final_documents(reranked_documents, allow_noise=allow_noise)
        if not final_documents and not str(context.get("retrieval_gate_reason") or "").strip():
            context["retrieval_gate_reason"] = "未检索到可直接采用的有效证据。"
        evidences = [_document_to_evidence(document) for document in final_documents]
        return _filter_retrieved_evidence_candidates(evidences, eligible_docs, context=context)

    def _dense_documents(self, points: list[Any], *, allow_noise: bool) -> list[Any]:
        from haystack import Document

        documents: list[Any] = []
        for rank, point in enumerate(points, start=1):
            payload = dict(getattr(point, "payload", {}) or {})
            meta = _normalize_chunk_meta(
                payload,
                content=str(payload.get("content") or ""),
            )
            if not allow_noise and _chunk_meta_is_noise_like(meta):
                continue
            documents.append(
                Document(
                    id=str(payload.get("chunk_id") or getattr(point, "id", "") or uuid.uuid4().hex),
                    content=str(payload.get("content") or ""),
                    score=round(_normalize_qdrant_relevance_score(getattr(point, "score", None)), 4),
                    meta={
                        **meta,
                        "retrieval_source": "dense",
                        "retrieval_rank": rank,
                    },
                )
            )
        return documents

    def _sparse_documents(self, knowledge_docs: list[dict[str, Any]], *, query: str, allow_noise: bool) -> list[Any]:
        from haystack import Document
        from haystack.components.retrievers.in_memory import InMemoryBM25Retriever
        from haystack.document_stores.in_memory import InMemoryDocumentStore

        chunk_documents: list[Any] = []
        for knowledge_doc in knowledge_docs:
            for chunk in _load_knowledge_chunk_records(knowledge_doc):
                content = str(chunk.get("content") or "")
                meta = _normalize_chunk_meta(
                    dict(chunk.get("meta") or {}),
                    content=content,
                )
                if not allow_noise and _chunk_meta_is_noise_like(meta):
                    continue
                chunk_documents.append(
                    Document(
                        id=str(chunk.get("evidence_id") or chunk.get("chunk_id") or ""),
                        content=content,
                        meta=meta,
                    )
                )
        if not chunk_documents:
            return []
        document_store = InMemoryDocumentStore()
        document_store.write_documents(chunk_documents)
        retriever = InMemoryBM25Retriever(document_store=document_store)
        result = retriever.run(query=query, top_k=QDRANT_HYBRID_SPARSE_TOP_K)
        documents: list[Any] = []
        for rank, document in enumerate(result.get("documents") or [], start=1):
            documents.append(
                Document(
                    id=str(getattr(document, "id", "") or uuid.uuid4().hex),
                    content=str(getattr(document, "content", "") or ""),
                    score=round(_normalize_bm25_relevance_score(getattr(document, "score", None)), 4),
                    meta={
                        **_normalize_chunk_meta(getattr(document, "meta", {}) or {}, content=str(getattr(document, "content", "") or "")),
                        "retrieval_source": "sparse",
                        "retrieval_rank": rank,
                    },
                )
            )
        return documents

    def _fuse_documents(self, *, dense_documents: list[Any], sparse_documents: list[Any]) -> list[Any]:
        if not dense_documents and not sparse_documents:
            return []
        from haystack.components.joiners import DocumentJoiner

        joiner = DocumentJoiner(join_mode="reciprocal_rank_fusion", top_k=QDRANT_HYBRID_FUSED_TOP_K)
        result = joiner.run(documents=[dense_documents, sparse_documents], top_k=QDRANT_HYBRID_FUSED_TOP_K)
        return list(result.get("documents") or [])

    def _rerank_documents(self, *, query: str, documents: list[Any]) -> list[Any]:
        if not documents:
            return []
        ranker = _get_qdrant_local_reranker()
        if ranker is None:
            return sorted(
                documents,
                key=lambda item: float(getattr(item, "score", 0.0) or 0.0),
                reverse=True,
            )[:QDRANT_HYBRID_RERANK_TOP_K]
        result = ranker.run(query=query, documents=documents, top_k=QDRANT_HYBRID_RERANK_TOP_K, scale_score=True)
        return list(result.get("documents") or [])

    def _select_final_documents(self, documents: list[Any], *, allow_noise: bool) -> list[Any]:
        if not documents:
            return []
        top_score = float(getattr(documents[0], "score", 0.0) or 0.0)
        if top_score < QDRANT_MIN_EFFECTIVE_EVIDENCE_SCORE:
            return []

        # Score margin guard: if top-1 and top-2 are very close but from
        # different documents, mark retrieval as low-confidence
        if len(documents) >= 2:
            second_score = float(getattr(documents[1], "score", 0.0) or 0.0)
            top_meta = _normalize_chunk_meta(getattr(documents[0], "meta", {}) or {}, content="")
            second_meta = _normalize_chunk_meta(getattr(documents[1], "meta", {}) or {}, content="")
            top_doc = str(top_meta.get("source_doc_id") or "").strip()
            second_doc = str(second_meta.get("source_doc_id") or "").strip()
            if top_doc and second_doc and top_doc != second_doc:
                margin = abs(top_score - second_score)
                if margin < 0.005:
                    _logger.info(
                        "Low-confidence retrieval: top-1 (score=%.4f, doc=%s) vs top-2 (score=%.4f, doc=%s) margin=%.5f",
                        top_score, top_doc, second_score, second_doc, margin,
                    )

        supporting_docs_by_source: dict[str, list[Any]] = {}
        for document in documents:
            meta = _normalize_chunk_meta(getattr(document, "meta", {}) or {}, content=str(getattr(document, "content", "") or ""))
            source_doc_id = str(meta.get("source_doc_id") or "").strip()
            if not source_doc_id:
                continue
            if str(meta.get("content_kind") or "").strip().lower() in {"paragraph", "table_slice"}:
                supporting_docs_by_source.setdefault(source_doc_id, []).append(document)

        final_documents: list[Any] = []
        for document in documents:
            meta = _normalize_chunk_meta(getattr(document, "meta", {}) or {}, content=str(getattr(document, "content", "") or ""))
            if not allow_noise and _chunk_meta_is_noise_like(meta):
                continue
            content_kind = str(meta.get("content_kind") or "").strip().lower()
            source_doc_id = str(meta.get("source_doc_id") or "").strip()
            if content_kind == "section_summary" and not supporting_docs_by_source.get(source_doc_id):
                continue
            final_documents.append(document)
            if len(final_documents) >= QDRANT_FINAL_EVIDENCE_LIMIT:
                break
        return final_documents

    def _missing_authoritative_regulation_docs(
        self,
        *,
        context: dict[str, Any],
        knowledge_docs: list[dict[str, Any]],
    ) -> bool:
        requirement_type = str(context.get("requirement_type") or "").strip().upper()
        if requirement_type != "REGULATION":
            return False
        for knowledge_doc in knowledge_docs:
            if str(knowledge_doc.get("doc_type") or "").strip().upper() in {"REGULATION", "STANDARD"}:
                return False
        return True

    def _query_targets_noise_chunks(self, query: str) -> bool:
        normalized = normalize_requirement_text(query).lower()
        noise_tokens = (
            "cover",
            "封面",
            "glossary",
            "术语",
            "revision history",
            "版本历史",
            "签字",
            "signature",
            "disclaimer",
            "免责声明",
        )
        return any(token in normalized for token in noise_tokens)


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
                    "evidence_id": str(item.get("evidence_id") or ""),
                    "content_hash": str(item.get("content_hash") or ""),
                    "evidence_contract_version": str(item.get("evidence_contract_version") or ""),
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


def _screening_runtime_config(config: AICopilotConfig) -> AICopilotConfig:
    return replace(
        config,
        llm_timeout_seconds=min(float(config.llm_timeout_seconds or DEFAULT_SCREENING_TIMEOUT_SECONDS), DEFAULT_SCREENING_TIMEOUT_SECONDS),
        openai_max_retries=1,
    )


def _assessment_runtime_config(config: AICopilotConfig) -> AICopilotConfig:
    return replace(
        config,
        llm_timeout_seconds=min(float(config.llm_timeout_seconds or DEFAULT_ASSESSMENT_TIMEOUT_SECONDS), DEFAULT_ASSESSMENT_TIMEOUT_SECONDS),
        openai_max_retries=0,
    )


class AzureOpenAIAssessmentEngine:
    prompt_version = DEFAULT_PROMPT_VERSION

    def __init__(self, config: AICopilotConfig) -> None:
        self.config = config
        self.model_name = config.azure_openai_chat_deployment or "azure-openai"

    def assess(
        self,
        context: dict[str, Any],
        evidences: list[dict[str, Any]],
        *,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        effective_config = _assessment_runtime_config(self.config)
        if not effective_config.azure_openai_chat_ready:
            raise RuntimeError("Azure OpenAI is not configured.")
        return _azure_json_completion(
            effective_config,
            build_llm_messages(context, evidences, retrieval_backend=effective_config.retrieval_backend),
            model_name=effective_config.azure_openai_chat_deployment,
            response_format=AssessmentOutputSchema,
            progress_callback=progress_callback,
        )


class AzureOpenAIScreeningEngine:
    prompt_version = DEFAULT_SCREENING_PROMPT_VERSION

    def __init__(self, config: AICopilotConfig) -> None:
        self.config = config
        self.model_name = config.screening_model or config.azure_openai_chat_deployment or "azure-openai"

    def screen(
        self,
        context: dict[str, Any],
        *,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        effective_config = _screening_runtime_config(self.config)
        if not effective_config.azure_openai_chat_ready:
            raise RuntimeError("Azure OpenAI is not configured.")
        return _azure_json_completion(
            effective_config,
            build_screening_messages(context),
            model_name=self.model_name,
            response_format=ScreeningOutputSchema,
            progress_callback=progress_callback,
        )


class AliyunDashScopeAssessmentEngine:
    prompt_version = DEFAULT_PROMPT_VERSION

    def __init__(self, config: AICopilotConfig) -> None:
        self.config = config
        self.model_name = config.chat_model or DEFAULT_DASHSCOPE_CHAT_MODEL

    def assess(
        self,
        context: dict[str, Any],
        evidences: list[dict[str, Any]],
        *,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        effective_config = _assessment_runtime_config(self.config)
        if not effective_config.dashscope_chat_ready:
            raise RuntimeError("Aliyun DashScope chat runtime is not configured.")
        messages = _build_json_compatible_messages(
            build_llm_messages(context, evidences, retrieval_backend=effective_config.retrieval_backend),
            required_keys=[
                "normalized_requirement",
                "proposed_decision",
                "compliance_status",
                "confidence",
                "reason",
                "risk_level",
                "clarification_question",
                "evidences",
                "evidence_findings",
                "sub_judgments",
                "structured_information_gap",
            ],
        )
        return _dashscope_json_completion(
            effective_config,
            messages,
            progress_callback=progress_callback,
        )


class AliyunDashScopeScreeningEngine:
    prompt_version = "v2.1-requirement-screening-aliyun"

    def __init__(self, config: AICopilotConfig) -> None:
        self.config = config
        self.model_name = config.screening_model or config.chat_model or DEFAULT_DASHSCOPE_CHAT_MODEL

    def screen(
        self,
        context: dict[str, Any],
        *,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        effective_config = _screening_runtime_config(self.config)
        if not effective_config.dashscope_chat_ready:
            raise RuntimeError("Aliyun DashScope chat runtime is not configured.")
        messages = _build_json_compatible_messages(
            build_screening_messages(context),
            required_keys=["analysis_label", "reason"],
        )
        return _dashscope_json_completion(
            effective_config,
            messages,
            model_name=self.model_name,
            progress_callback=progress_callback,
        )


def _run_requirement_screening(
    *,
    context: dict[str, Any],
    config: AICopilotConfig,
    collector: "AssessmentRunCollector",
    forced_analysis_label: str | None = None,
    forced_analysis_basis: str = "",
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
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
        _emit_progress_stage_update(
            progress_callback,
            stage="screening",
            mode="screening_manual_override",
            model_name=MANUAL_OVERRIDE_MODEL_NAME,
            provider=config.provider,
            message="人工覆盖已生效，跳过 AI 筛选。",
        )
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

    if config.assessment_mode in {"heuristic", "llm_shadow"}:
        _emit_progress_stage_update(
            progress_callback,
            stage="screening",
            mode="screening_heuristic",
            model_name=heuristic_engine.model_name,
            provider=config.provider,
            message="正在使用规则引擎进行 requirement / INFO 筛选。",
        )
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
        serving_result = {
            **final_result,
            "final_mode": "heuristic",
            "model_name": heuristic_engine.model_name,
            "prompt_version": heuristic_engine.prompt_version,
        }
        if config.assessment_mode == "heuristic":
            return serving_result

    if config.assessment_mode == "llm_shadow":
        # Screening shadow output is observable through runs, but routing stays
        # bound to the heuristic serving_result even when the provider fails.
        try:
            _emit_progress_stage_update(
                progress_callback,
                stage="screening",
                mode="screening_llm_shadow",
                model_name=llm_engine.model_name,
                provider=config.provider,
                message="正在进行 requirement / INFO 筛选。",
            )
            llm_result = llm_engine.screen(
                context,
                progress_callback=_progress_callback_with_metadata(
                    progress_callback,
                    stage="screening",
                    mode="screening_llm_shadow",
                    model_name=llm_engine.model_name,
                    provider=config.provider,
                ),
            )
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
        except AIAnalysisCancelledError:
            raise
        except Exception as exc:  # pragma: no cover - network/provider dependent
            failure_reason = _provider_failure_reason(exc)
            collector.record_failure(
                mode="screening_llm_shadow",
                model_name=llm_engine.model_name,
                prompt_version=llm_engine.prompt_version,
                input_payload={"context": context},
                fallback_reason=failure_reason,
            )
        return serving_result

    try:
        _emit_progress_stage_update(
            progress_callback,
            stage="screening",
            mode="screening_llm_primary",
            model_name=llm_engine.model_name,
            provider=config.provider,
            message="正在进行 requirement / INFO 筛选。",
        )
        llm_result = llm_engine.screen(
            context,
            progress_callback=_progress_callback_with_metadata(
                progress_callback,
                stage="screening",
                mode="screening_llm_primary",
                model_name=llm_engine.model_name,
                provider=config.provider,
            ),
        )
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
    except AIAnalysisCancelledError:
        raise
    except Exception as exc:  # pragma: no cover - network/provider dependent
        failure_reason = _provider_failure_reason(exc)
        collector.record_failure(
            mode="screening_llm_primary",
            model_name=llm_engine.model_name,
            prompt_version=llm_engine.prompt_version,
            input_payload={"context": context},
            fallback_reason=failure_reason,
        )
        return {
            "analysis_label": "REQUIREMENT",
            "reason": "LLM screening failed; analysis stopped",
            "validation_status": "failed",
            "fallback_reason": failure_reason,
            "final_mode": "screening_failed",
            "model_name": llm_engine.model_name,
            "prompt_version": llm_engine.prompt_version,
            "screening_failed": True,
        }


def _provider_failure_reason(exc: Exception) -> str:
    reason = f"{type(exc).__name__}: {exc}".strip()
    cause = exc.__cause__ or exc.__context__
    if cause is not None and cause is not exc:
        reason = f"{reason}; caused by {type(cause).__name__}: {cause}"
    return reason[:1000]


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


def _backfill_evidence_fields(candidate: dict[str, Any], retrieved_evidences: list[dict[str, Any]]) -> None:
    """Fill in missing evidence metadata fields that LLMs commonly omit.

    Handles three LLM output patterns:
    1. Evidence as dicts with missing fields (Qwen 3.5 pattern) — backfill from retrieved
    2. Evidence as plain UUID/chunk_id strings (Qwen 3.7 pattern) — reconstruct from retrieved
    3. Evidence as dicts with slot references — match by slot index
    """
    raw_evidences = candidate.get("evidences")
    if not isinstance(raw_evidences, list) or not retrieved_evidences:
        return

    # Build lookup maps from retrieved evidences
    retrieved_by_slot: dict[int, dict[str, Any]] = {
        i + 1: item for i, item in enumerate(retrieved_evidences) if isinstance(item, dict)
    }
    retrieved_by_chunk_id: dict[str, dict[str, Any]] = {}
    for item in retrieved_evidences:
        if isinstance(item, dict):
            cid = str(item.get("chunk_id") or "").strip()
            if cid:
                retrieved_by_chunk_id[cid] = item

    # Detect if evidence items are strings (UUID/chunk_id references)
    has_string_evidence = any(isinstance(ev, str) for ev in raw_evidences)
    if has_string_evidence:
        # Build chunk_id lookup from retrieved evidences AND knowledge index files
        chunk_id_lookup: dict[str, dict[str, Any]] = {}
        for item in retrieved_evidences:
            if isinstance(item, dict):
                cid = str(item.get("chunk_id") or "").strip()
                if cid:
                    chunk_id_lookup[cid] = item

        # If not enough matches from retrieved, load from knowledge index
        string_ids = [ev.strip() for ev in raw_evidences if isinstance(ev, str)]
        unmatched = [sid for sid in string_ids if sid not in chunk_id_lookup]
        if unmatched:
            try:
                for idx_file in INDEX_PAYLOAD_DIR.glob("*.json"):
                    with open(idx_file, encoding="utf-8") as f:
                        payload = json.load(f)
                    chunks = payload if isinstance(payload, list) else payload.get("chunks", [])
                    doc_id_from_file = idx_file.stem
                    for ci, chunk in enumerate(chunks):
                        if isinstance(chunk, dict):
                            cid = str(chunk.get("chunk_id") or "")
                            if cid and cid not in chunk_id_lookup:
                                chunk_id_lookup[cid] = {
                                    "source_doc_id": doc_id_from_file,
                                    "source_doc_name": str(chunk.get("source_doc_name") or ""),
                                    "source_doc_version": str(chunk.get("source_doc_version") or ""),
                                    "source_location": str(chunk.get("source_location") or f"Chunk {ci+1}"),
                                    "excerpt": str(chunk.get("content") or "")[:800],
                                    "relevance": 0.5,
                                    "chunk_id": cid,
                                }
            except Exception:
                pass

        reconstructed: list[dict[str, Any]] = []
        for idx, ev in enumerate(raw_evidences):
            if isinstance(ev, str):
                ev_str = ev.strip()
                source = chunk_id_lookup.get(ev_str)
                if source is None:
                    for cid, item in chunk_id_lookup.items():
                        if ev_str in cid or cid in ev_str:
                            source = item
                            break
                if source is None:
                    source = retrieved_by_slot.get(idx + 1)
                if source is not None:
                    reconstructed.append({
                        "evidence_slot": idx + 1,
                        "chunk_id": str(source.get("chunk_id") or ev_str),
                        "source_doc_id": str(source.get("source_doc_id") or ""),
                        "source_doc_name": str(source.get("source_doc_name") or ""),
                        "source_doc_version": str(source.get("source_doc_version") or ""),
                        "source_location": str(source.get("source_location") or ""),
                        "excerpt": str(source.get("excerpt") or ""),
                        "relevance": float(source.get("relevance") or 0.0),
                    })
            elif isinstance(ev, dict):
                reconstructed.append(ev)
        if reconstructed:
            candidate["evidences"] = reconstructed
        return

    # Standard backfill for dict evidences with missing fields
    for ev in raw_evidences:
        if not isinstance(ev, dict):
            continue
        slot = int(ev.get("slot") or ev.get("evidence_slot") or 0)
        source = retrieved_by_slot.get(slot)
        if source is None:
            # Try matching by chunk_id
            ev_chunk_id = str(ev.get("chunk_id") or "").strip()
            if ev_chunk_id:
                source = retrieved_by_chunk_id.get(ev_chunk_id)
        if source is None:
            # Fallback: try matching by excerpt substring
            ev_excerpt = normalize_requirement_text(str(ev.get("excerpt") or ""))
            for item in retrieved_evidences:
                item_excerpt = normalize_requirement_text(str(item.get("excerpt") or ""))
                if ev_excerpt and item_excerpt and (ev_excerpt in item_excerpt or item_excerpt in ev_excerpt):
                    source = item
                    break
        if source is None:
            continue
        for field in ("source_doc_id", "source_doc_name", "source_doc_version", "chunk_id"):
            if not ev.get(field):
                ev[field] = str(source.get(field) or "")
        if not ev.get("relevance") and source.get("relevance"):
            try:
                ev["relevance"] = float(source["relevance"])
            except (TypeError, ValueError):
                pass


def reconcile_assessment_output(
    *,
    context: dict[str, Any],
    candidate: dict[str, Any],
    retrieved_evidences: list[dict[str, Any]],
    fallback_reason: str,
    retrieval_backend: str = "",
    reconciliation_policy: Literal["heuristic", "llm_guarded"] = "heuristic",
) -> dict[str, Any]:
    normalized_requirement = str(context.get("normalized_requirement") or "").strip()
    if not normalized_requirement:
        normalized_requirement = normalize_requirement_text(str(context.get("requirement_text") or ""))

    if not isinstance(candidate, dict):
        candidate = {}
    else:
        candidate = dict(candidate)
    if (
        "structured_information_gap" not in candidate
        and isinstance(candidate.get("information_gap"), dict)
    ):
        candidate["structured_information_gap"] = candidate.pop(
            "information_gap"
        )
    candidate_declares_compliance_contract = bool(
        str(candidate.get("compliance_status") or "").strip()
    )

    # Backfill missing evidence fields from retrieved evidences (LLMs may omit required metadata)
    _backfill_evidence_fields(candidate, retrieved_evidences)

    try:
        parsed = AssessmentOutputSchema.model_validate(candidate)
    except Exception as exc:
        _logger.warning(
            "Assessment schema validation failed for req %s: %s | candidate keys: %s",
            context.get("req_code", "?"), exc, list(candidate.keys()) if isinstance(candidate, dict) else type(candidate).__name__,
        )
        return fallback_need_clarification(
            context=context,
            evidences=retrieved_evidences,
            fallback_reason=f"{fallback_reason}; schema_validation_failed: {exc}".strip("; "),
        )

    expected_compliance_status = DECISION_TO_COMPLIANCE_STATUS[parsed.proposed_decision]
    compliance_status = str(
        parsed.compliance_status or expected_compliance_status
    ).strip()
    if compliance_status != expected_compliance_status:
        return fallback_need_clarification(
            context=context,
            evidences=retrieved_evidences,
            fallback_reason=(
                f"{fallback_reason}; compliance_decision_conflict"
            ).strip("; "),
        )

    validation_error = _validate_candidate_ranges(parsed)
    if validation_error:
        return fallback_need_clarification(
            context=context,
            evidences=retrieved_evidences,
            fallback_reason=f"{fallback_reason}; {validation_error}".strip("; "),
        )

    canonical_evidences, evidence_validation, used_legacy_evidence_selector = _resolve_evidence_references(
        parsed.evidences,
        retrieved_evidences,
    )
    if evidence_validation:
        return fallback_need_clarification(
            context=context,
            evidences=retrieved_evidences,
            fallback_reason=f"{fallback_reason}; {evidence_validation}".strip("; "),
        )
    if reconciliation_policy == "llm_guarded":
        primary_visible, adjacent_visible = (
            _select_model_evidence_candidates(retrieved_evidences)
        )
        visible_evidence_ids = {
            str(
                item.get("evidence_id") or item.get("chunk_id") or ""
            ).strip()
            for item in (*primary_visible, *adjacent_visible)
            if str(
                item.get("evidence_id") or item.get("chunk_id") or ""
            ).strip()
        }
        if any(
            item.evidence_id not in visible_evidence_ids
            for item in canonical_evidences
        ):
            return fallback_need_clarification(
                context=context,
                evidences=retrieved_evidences,
                fallback_reason=(
                    f"{fallback_reason}; "
                    "evidence_reference_not_in_model_input"
                ).strip("; "),
            )

    structured_validation = _validate_structured_assessment_contract(
        parsed,
        selected_evidence_ids={item.evidence_id for item in canonical_evidences},
        require_complete=(
            reconciliation_policy == "llm_guarded"
            or candidate_declares_compliance_contract
        ),
    )
    if structured_validation:
        return fallback_need_clarification(
            context=context,
            evidences=retrieved_evidences,
            fallback_reason=f"{fallback_reason}; {structured_validation}".strip("; "),
        )

    reconciled_fallback_reason = fallback_reason.strip()
    if used_legacy_evidence_selector:
        reconciled_fallback_reason = f"{reconciled_fallback_reason}; legacy_evidence_selector".strip("; ")

    if parsed.proposed_decision != "NEED_CLARIFICATION" and not parsed.evidences:
        return fallback_need_clarification(
            context=context,
            evidences=retrieved_evidences,
            fallback_reason=f"{reconciled_fallback_reason}; missing_evidence".strip("; "),
        )

    if _evidences_conflict(canonical_evidences):
        return fallback_need_clarification(
            context=context,
            evidences=retrieved_evidences,
            fallback_reason=f"{reconciled_fallback_reason}; conflicting_evidences".strip("; "),
        )

    if parsed.proposed_decision != "NEED_CLARIFICATION":
        candidate_requirement = str(parsed.normalized_requirement or normalized_requirement).strip()
        numeric_requirement_error = validate_numeric_requirement_projection(
            raw_requirement=str(context.get("requirement_text") or ""),
            normalized_requirement=normalized_requirement,
            candidate_requirement=candidate_requirement,
        )
        if numeric_requirement_error:
            return fallback_need_clarification(
                context=context,
                evidences=retrieved_evidences,
                fallback_reason=f"{reconciled_fallback_reason}; {numeric_requirement_error}".strip("; "),
            )
        if normalize_requirement_text(candidate_requirement) != normalize_requirement_text(normalized_requirement):
            return fallback_need_clarification(
                context=context,
                evidences=retrieved_evidences,
                fallback_reason=f"{reconciled_fallback_reason}; requirement_projection_out_of_scope".strip("; "),
            )

    candidate_serving_decision = str(parsed.proposed_decision or "").strip()
    if candidate_serving_decision in {
        "ACCEPT",
        "REJECT",
        "DEVIATION_ACCEPT",
    }:
        cited_numeric_error = validate_numeric_decision_support(
            decision=candidate_serving_decision,
            raw_requirement=str(context.get("requirement_text") or ""),
            normalized_requirement=str(
                context.get("normalized_requirement") or ""
            ),
            evidence_texts=[item.excerpt for item in canonical_evidences],
        )
        if cited_numeric_error:
            return fallback_need_clarification(
                context=context,
                evidences=retrieved_evidences,
                fallback_reason=(
                    f"{reconciled_fallback_reason}; {cited_numeric_error}"
                ).strip("; "),
            )
        server_evidences = _server_evidence_refs(retrieved_evidences)
        numeric_evidence_error = validate_numeric_decision_support(
            decision=candidate_serving_decision,
            raw_requirement=str(context.get("requirement_text") or ""),
            normalized_requirement=str(
                context.get("normalized_requirement") or ""
            ),
            evidence_texts=[item.excerpt for item in server_evidences],
        )
        if numeric_evidence_error:
            return fallback_need_clarification(
                context=context,
                evidences=retrieved_evidences,
                fallback_reason=(
                    f"{reconciled_fallback_reason}; {numeric_evidence_error}"
                ).strip("; "),
            )

    hard_rule_result: dict[str, Any] | None = None
    if reconciliation_policy == "llm_guarded":
        evidence_strength_result = _apply_evidence_strength_guardrail(
            context=context,
            candidate=parsed,
            canonical_evidences=canonical_evidences,
            retrieved_evidences=retrieved_evidences,
            fallback_reason=reconciled_fallback_reason,
            retrieval_backend=retrieval_backend,
        )
        if evidence_strength_result is not None:
            return evidence_strength_result
    else:
        hard_rule_result = _apply_hard_assessment_rules(
            context=context,
            candidate=parsed,
            canonical_evidences=canonical_evidences,
            retrieved_evidences=retrieved_evidences,
            fallback_reason=reconciled_fallback_reason,
        )
    serving_decision = str(
        (hard_rule_result or {}).get("ai_decision") or parsed.proposed_decision
    ).strip()
    if serving_decision in {"ACCEPT", "REJECT", "DEVIATION_ACCEPT"}:
        cited_numeric_error = validate_numeric_decision_support(
            decision=serving_decision,
            raw_requirement=str(context.get("requirement_text") or ""),
            normalized_requirement=str(context.get("normalized_requirement") or ""),
            evidence_texts=[item.excerpt for item in canonical_evidences],
        )
        if cited_numeric_error:
            return fallback_need_clarification(
                context=context,
                evidences=retrieved_evidences,
                fallback_reason=f"{reconciled_fallback_reason}; {cited_numeric_error}".strip("; "),
            )
        server_evidences = _server_evidence_refs(retrieved_evidences)
        numeric_evidence_error = validate_numeric_decision_support(
            decision=serving_decision,
            raw_requirement=str(context.get("requirement_text") or ""),
            normalized_requirement=str(context.get("normalized_requirement") or ""),
            evidence_texts=[item.excerpt for item in server_evidences],
        )
        if numeric_evidence_error:
            return fallback_need_clarification(
                context=context,
                evidences=retrieved_evidences,
                fallback_reason=f"{reconciled_fallback_reason}; {numeric_evidence_error}".strip("; "),
            )
    if hard_rule_result is not None:
        return _with_assessment_semantics(
            hard_rule_result,
            context=context,
            canonical_evidences=canonical_evidences,
            retrieved_evidences=retrieved_evidences,
        )

    canonical_evidence_mappings = _canonical_evidence_mappings(
        canonical_evidences,
        retrieved_evidences,
        limit=MAX_PERSISTED_EVIDENCES,
    )
    structured_gap = parsed.structured_information_gap.model_dump()
    if (
        compliance_status == "INSUFFICIENT_INFORMATION"
        and not _structured_information_gap_has_content(structured_gap)
    ):
        structured_gap = _build_structured_information_gap(
            context,
            canonical_evidence_mappings,
        )
    result = {
        "analysis_label": "REQUIREMENT",
        "analysis_basis": "",
        "ai_decision": parsed.proposed_decision,
        "compliance_status": compliance_status,
        "ai_reason": parsed.reason.strip(),
        "ai_confidence": round(max(0.0, min(1.0, float(parsed.confidence))), 4),
        "risk_level": parsed.risk_level,
        "clarification_question": parsed.clarification_question.strip(),
        "normalized_requirement": normalized_requirement,
        "evidences": canonical_evidence_mappings,
        "evidence_findings": [
            item.model_dump() for item in parsed.evidence_findings
        ]
        or _build_default_evidence_findings(
            compliance_status,
            canonical_evidence_mappings,
            reason=parsed.reason.strip(),
        ),
        "sub_judgments": [item.model_dump() for item in parsed.sub_judgments]
        or _build_default_sub_judgments(
            context=context,
            compliance_status=compliance_status,
            reason=parsed.reason.strip(),
            evidences=canonical_evidence_mappings,
            structured_information_gap=structured_gap,
        ),
        "structured_information_gap": structured_gap,
        "validation_status": "accepted",
        "fallback_reason": reconciled_fallback_reason,
    }
    if result["ai_decision"] == "NEED_CLARIFICATION" and not result["clarification_question"]:
        result["clarification_question"] = default_clarification_question(context, retrieved_evidences)
    return result


def _validate_structured_assessment_contract(
    candidate: AssessmentOutputSchema,
    *,
    selected_evidence_ids: set[str],
    require_complete: bool,
) -> str:
    if len(selected_evidence_ids) > MAX_PERSISTED_EVIDENCES:
        return "too_many_selected_evidences"
    referenced_ids: list[str] = []
    for finding in candidate.evidence_findings:
        evidence_id = finding.retrieval_evidence_id.strip()
        if not evidence_id or not finding.rationale.strip():
            return "evidence_finding_incomplete"
        referenced_ids.append(evidence_id)
    for judgment in candidate.sub_judgments:
        if (
            not judgment.sub_judgment_id.strip()
            or not judgment.statement.strip()
            or not judgment.reason.strip()
        ):
            return "sub_judgment_incomplete"
        judgment_evidence_ids = [
            item.strip()
            for item in judgment.retrieval_evidence_ids
            if item.strip()
        ]
        if (
            require_complete
            and judgment.compliance_status != "INSUFFICIENT_INFORMATION"
            and not judgment_evidence_ids
        ):
            return "sub_judgment_evidence_missing"
        referenced_ids.extend(judgment_evidence_ids)
        nested_gap = judgment.structured_information_gap
        if (
            require_complete
            and judgment.compliance_status == "INSUFFICIENT_INFORMATION"
            and (
                not nested_gap.missing_items
                or not nested_gap.clarification_questions
            )
        ):
            return "sub_judgment_information_gap_incomplete"
        referenced_ids.extend(
            item.retrieval_evidence_id.strip()
            for item in nested_gap.found_evidence
            if item.retrieval_evidence_id.strip()
        )
        for conflict in nested_gap.conflicts:
            referenced_ids.extend(
                item.strip()
                for item in conflict.retrieval_evidence_ids
                if item.strip()
            )
    gap = candidate.structured_information_gap
    referenced_ids.extend(
        item.retrieval_evidence_id.strip()
        for item in gap.found_evidence
        if item.retrieval_evidence_id.strip()
    )
    for conflict in gap.conflicts:
        referenced_ids.extend(
            item.strip()
            for item in conflict.retrieval_evidence_ids
            if item.strip()
        )
    if any(item not in selected_evidence_ids for item in referenced_ids):
        return "structured_evidence_reference_out_of_scope"

    if not require_complete:
        return ""
    status = str(
        candidate.compliance_status
        or DECISION_TO_COMPLIANCE_STATUS[candidate.proposed_decision]
    )
    if not candidate.sub_judgments:
        return "sub_judgments_missing"
    sub_statuses = {
        item.compliance_status for item in candidate.sub_judgments
    }
    if "NON_COMPLIANT" in sub_statuses:
        aggregated_status = "NON_COMPLIANT"
    elif "INSUFFICIENT_INFORMATION" in sub_statuses:
        aggregated_status = "INSUFFICIENT_INFORMATION"
    elif "PARTIALLY_COMPLIANT" in sub_statuses:
        aggregated_status = "PARTIALLY_COMPLIANT"
    else:
        aggregated_status = "COMPLIANT"
    if status != aggregated_status:
        return "sub_judgment_status_conflict"

    finding_types = {item.finding for item in candidate.evidence_findings}
    if (
        status == "COMPLIANT"
        and (
            "SUPPORTS" not in finding_types
            or "CONTRADICTS" in finding_types
        )
    ):
        return "evidence_finding_status_conflict"
    if status == "NON_COMPLIANT" and "CONTRADICTS" not in finding_types:
        return "evidence_finding_status_conflict"
    if (
        status == "PARTIALLY_COMPLIANT"
        and "PARTIALLY_SUPPORTS" not in finding_types
    ):
        return "evidence_finding_status_conflict"
    if status == "INSUFFICIENT_INFORMATION":
        if not gap.missing_items or not gap.clarification_questions:
            return "structured_information_gap_incomplete"
    elif not candidate.evidence_findings:
        return "evidence_findings_missing"
    return ""


def _canonical_evidence_mappings(
    canonical_evidences: list[EvidenceRef],
    retrieved_evidences: list[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    by_id = {
        str(item.get("evidence_id") or "").strip(): item
        for item in retrieved_evidences
        if str(item.get("evidence_id") or "").strip()
    }
    output: list[dict[str, Any]] = []
    for evidence in canonical_evidences[:limit]:
        server = by_id.get(evidence.evidence_id, {})
        output.append(
            {
                **evidence.to_mapping(),
                "evidence_id": evidence.evidence_id,
                "retrieval_evidence_id": evidence.evidence_id,
                "content_hash": evidence.content_hash,
                "chunk_content_hash": evidence.content_hash,
                "excerpt_hash": compute_evidence_content_hash(evidence.excerpt),
                "evidence_contract_version": evidence.evidence_contract_version,
                "chunk_id": str(server.get("chunk_id") or ""),
                "score_source": str(server.get("score_source") or ""),
                "retrieval_parent_evidence_id": str(
                    server.get("retrieval_parent_evidence_id") or ""
                ),
                "retrieval_parent_relevance": (
                    float(server.get("retrieval_parent_relevance") or 0.0)
                    if str(server.get("score_source") or "")
                    == "adjacent_context"
                    else None
                ),
                "doc_type": str(server.get("doc_type") or ""),
                "status": str(server.get("status") or ""),
                "effective_from": str(server.get("effective_from") or ""),
                "effective_to": str(server.get("effective_to") or ""),
                "content_kind": str(server.get("content_kind") or ""),
                "authority": str(server.get("authority") or "UNKNOWN"),
                "claim_role": str(server.get("claim_role") or "UNKNOWN"),
                "applicability": str(server.get("applicability") or "UNKNOWN"),
                "table_id": str(server.get("table_id") or ""),
                "native_locator": str(server.get("native_locator") or ""),
                "native_format": str(server.get("native_format") or ""),
                "native_page": str(server.get("native_page") or ""),
                "native_table_index": str(
                    server.get("native_table_index") or ""
                ),
                "native_body_element_index": str(
                    server.get("native_body_element_index") or ""
                ),
                "native_workbook": str(server.get("native_workbook") or ""),
                "native_sheet": str(server.get("native_sheet") or ""),
                "native_range": str(server.get("native_range") or ""),
                "section_code": str(server.get("section_code") or ""),
                "section_title": str(server.get("section_title") or ""),
            }
        )
    return output


def _structured_information_gap_has_content(value: dict[str, Any]) -> bool:
    return any(
        value.get(key)
        for key in (
            "found_evidence",
            "missing_items",
            "conflicts",
            "clarification_questions",
        )
    )


def _build_structured_information_gap(
    context: dict[str, Any],
    evidences: list[dict[str, Any]],
) -> dict[str, Any]:
    question = default_clarification_question(context, evidences)
    retrieval_gate_reason = str(context.get("retrieval_gate_reason") or "").strip()
    blocking_reason = (
        retrieval_gate_reason
        or "当前证据无法直接、完整地证明需求中的全部约束。"
    )
    requirement = str(
        context.get("normalized_requirement")
        or context.get("requirement_text")
        or "当前需求"
    ).strip()
    positive_ids = [
        str(
            item.get("retrieval_evidence_id")
            or item.get("evidence_id")
            or ""
        )
        for item in evidences
        if _text_has_positive_signal(str(item.get("excerpt") or ""))
    ]
    negative_ids = [
        str(
            item.get("retrieval_evidence_id")
            or item.get("evidence_id")
            or ""
        )
        for item in evidences
        if _text_has_negative_signal(str(item.get("excerpt") or ""))
    ]
    conflict_ids = list(
        dict.fromkeys(
            item for item in [*positive_ids, *negative_ids] if item
        )
    )
    return {
        "schema_version": "assessment-information-gap/v1",
        "found_evidence": [
            {
                "retrieval_evidence_id": str(
                    item.get("retrieval_evidence_id")
                    or item.get("evidence_id")
                    or ""
                ),
                "proves": "存在与当前需求相关且可追溯的候选内容。",
            }
            for item in evidences
            if str(
                item.get("retrieval_evidence_id")
                or item.get("evidence_id")
                or ""
            ).strip()
        ],
        "missing_items": [
            {
                "item": requirement,
                "blocking_reason": blocking_reason,
                "required_input": question,
            }
        ],
        "conflicts": (
            [
                {
                    "retrieval_evidence_ids": conflict_ids,
                    "description": "候选证据同时出现支持与否定信号，无法安全合并为单一结论。",
                }
            ]
            if positive_ids and negative_ids
            else []
        ),
        "clarification_questions": [question] if question else [],
    }


def _build_default_evidence_findings(
    compliance_status: str,
    evidences: list[dict[str, Any]],
    *,
    reason: str,
) -> list[dict[str, Any]]:
    finding = {
        "COMPLIANT": "SUPPORTS",
        "PARTIALLY_COMPLIANT": "PARTIALLY_SUPPORTS",
        "NON_COMPLIANT": "CONTRADICTS",
        "INSUFFICIENT_INFORMATION": "CONTEXT_ONLY",
    }.get(compliance_status, "CONTEXT_ONLY")
    return [
        {
            "retrieval_evidence_id": str(
                item.get("retrieval_evidence_id")
                or item.get("evidence_id")
                or ""
            ),
            "finding": finding,
            "supported_claims": [],
            "unsupported_claims": [],
            "rationale": reason,
        }
        for item in evidences
        if str(
            item.get("retrieval_evidence_id")
            or item.get("evidence_id")
            or ""
        ).strip()
    ]


def _build_default_sub_judgments(
    *,
    context: dict[str, Any],
    compliance_status: str,
    reason: str,
    evidences: list[dict[str, Any]],
    structured_information_gap: dict[str, Any],
) -> list[dict[str, Any]]:
    statement = str(
        context.get("normalized_requirement")
        or context.get("requirement_text")
        or ""
    ).strip()
    if not statement:
        return []
    return [
        {
            "sub_judgment_id": "overall",
            "source_ranges": ["atomic_source_text"],
            "statement": statement,
            "compliance_status": compliance_status,
            "reason": reason,
            "retrieval_evidence_ids": [
                str(
                    item.get("retrieval_evidence_id")
                    or item.get("evidence_id")
                    or ""
                )
                for item in evidences
                if str(
                    item.get("retrieval_evidence_id")
                    or item.get("evidence_id")
                    or ""
                ).strip()
            ],
            "structured_information_gap": (
                structured_information_gap
                if compliance_status == "INSUFFICIENT_INFORMATION"
                else {}
            ),
        }
    ]


def _with_assessment_semantics(
    result: dict[str, Any],
    *,
    context: dict[str, Any],
    canonical_evidences: list[EvidenceRef],
    retrieved_evidences: list[dict[str, Any]],
) -> dict[str, Any]:
    enriched = dict(result)
    compliance_status = DECISION_TO_COMPLIANCE_STATUS.get(
        str(enriched.get("ai_decision") or "").strip(),
        "",
    )
    evidences = _canonical_evidence_mappings(
        canonical_evidences,
        retrieved_evidences,
        limit=MAX_PERSISTED_EVIDENCES,
    )
    gap = (
        _build_structured_information_gap(context, evidences)
        if compliance_status == "INSUFFICIENT_INFORMATION"
        else {}
    )
    enriched.update(
        {
            "compliance_status": compliance_status,
            "evidences": evidences,
            "evidence_findings": _build_default_evidence_findings(
                compliance_status,
                evidences,
                reason=str(enriched.get("ai_reason") or ""),
            ),
            "sub_judgments": _build_default_sub_judgments(
                context=context,
                compliance_status=compliance_status,
                reason=str(enriched.get("ai_reason") or ""),
                evidences=evidences,
                structured_information_gap=gap,
            ),
            "structured_information_gap": gap,
        }
    )
    return enriched


def fallback_need_clarification(
    *,
    context: dict[str, Any],
    evidences: list[dict[str, Any]],
    fallback_reason: str,
) -> dict[str, Any]:
    canonical_evidences = _server_evidence_refs(evidences)[:3]
    top_evidences = _canonical_evidence_mappings(
        canonical_evidences,
        evidences,
        limit=3,
    )
    retrieval_gate_reason = str(context.get("retrieval_gate_reason") or "").strip()
    ai_reason = "缺少可直接采用的有效证据，当前只能保留为待澄清。"
    if retrieval_gate_reason:
        ai_reason = f"{retrieval_gate_reason}，当前只能保留为待澄清。"
    structured_gap = _build_structured_information_gap(context, top_evidences)
    result = {
        "analysis_label": "REQUIREMENT",
        "analysis_basis": "",
        "ai_decision": "NEED_CLARIFICATION",
        "compliance_status": "INSUFFICIENT_INFORMATION",
        "ai_reason": ai_reason,
        "ai_confidence": 0.22,
        "risk_level": "HIGH",
        "clarification_question": default_clarification_question(context, evidences),
        "normalized_requirement": str(context.get("normalized_requirement") or ""),
        "evidences": top_evidences,
        "evidence_findings": _build_default_evidence_findings(
            "INSUFFICIENT_INFORMATION",
            top_evidences,
            reason=ai_reason,
        ),
        "structured_information_gap": structured_gap,
        "validation_status": "fallback",
        "fallback_reason": fallback_reason.strip() or "fallback",
    }
    result["sub_judgments"] = _build_default_sub_judgments(
        context=context,
        compliance_status="INSUFFICIENT_INFORMATION",
        reason=ai_reason,
        evidences=top_evidences,
        structured_information_gap=structured_gap,
    )
    return result


def default_clarification_question(context: dict[str, Any], evidences: list[dict[str, Any]]) -> str:
    requirement_type = str(context.get("requirement_type") or "FUNCTIONAL").strip().upper()
    if requirement_type == "REGULATION":
        return "请补充直接覆盖该法规条目的合规证明、适用边界或发布版本说明。"
    if evidences:
        return "请确认上述证据是否覆盖当前需求的适用工况、版本边界和平台范围。"
    return "请补充可直接覆盖该需求的内部规范、测试结果或历史项目证据。"


def _format_analysis_mode_label(mode: str) -> str:
    normalized = str(mode or "").strip().lower()
    return {
        "heuristic": "启发式",
        "llm_shadow": "LLM",
        "llm_primary": "LLM 主链",
        "heuristic_fallback": "启发式回退",
        "screening_only": "筛选",
        "screening_failed": "筛选失败",
        "analysis_failed": "分析失败",
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
    context: dict[str, Any],
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
    retrieval_gate_reason = str(context.get("retrieval_gate_reason") or "").strip()
    if retrieval_gate_reason:
        parts.append(retrieval_gate_reason)

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
    tables = _extract_markdown_tables(lines)
    paragraph_lines = _without_markdown_table_blocks(lines)
    chunks: list[dict[str, Any]] = []
    for index, chunk in enumerate(_chunk_lines(paragraph_lines), start=1):
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

    for index, table_lines in enumerate(tables, start=1):
        section = _section_for_chunk(table_lines, sections)
        locator_metadata = _parse_source_locator_metadata(table_lines)
        native_locator = str(locator_metadata.get("native_locator") or "")
        chunks.append(
            _build_chunk_record(
                project_id=project_id,
                knowledge_doc=knowledge_doc,
                chunk_id=_knowledge_chunk_id(str(knowledge_doc["id"]), "table", index),
                content="\n".join(table_lines).strip(),
                content_kind="table_slice",
                source_location=native_locator or f"Table {index}",
                section=section,
                slice_ai_projection=" ".join(table_lines).strip(),
                table_id=str(locator_metadata.get("table_id") or ""),
                native_locator_metadata=locator_metadata,
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
    payload_dir: Path | None = None,
) -> Path:
    target_dir = Path(payload_dir) if payload_dir is not None else INDEX_PAYLOAD_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    payload_path = target_dir / f"{knowledge_doc['id']}.json"
    merged_profile_ids = {str(item or "").strip().lower() for item in profile_ids or [] if str(item or "").strip()}
    if payload_path.exists():
        try:
            existing_payload = json.loads(payload_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing_payload = {}
        if (
            str(existing_payload.get("evidence_contract_version") or "").strip()
            == EVIDENCE_INDEX_CONTRACT_VERSION
            and _chunk_payload_contract_map(existing_payload.get("chunks")) == _chunk_payload_contract_map(chunks)
        ):
            merged_profile_ids.update(
                str(item or "").strip().lower()
                for item in existing_payload.get("profile_ids") or []
                if str(item or "").strip()
            )
    payload = {
        "document_id": knowledge_doc["id"],
        "file_name": knowledge_doc.get("file_name"),
        "generated_at": _now_iso_utc(),
        "evidence_contract_version": EVIDENCE_INDEX_CONTRACT_VERSION,
        "profile_ids": sorted(merged_profile_ids),
        "chunks": chunks,
    }
    payload_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload_path


def _chunk_payload_contract_map(value: Any) -> dict[str, str]:
    if not isinstance(value, list):
        return {}
    contracts: dict[str, str] = {}
    for chunk in value:
        if not isinstance(chunk, dict):
            return {}
        evidence_id = str(chunk.get("evidence_id") or "").strip()
        content_hash = str(chunk.get("content_hash") or "").strip().lower()
        if not evidence_id or not content_hash:
            return {}
        if evidence_id in contracts and contracts[evidence_id] != content_hash:
            return {}
        contracts[evidence_id] = content_hash
    return contracts


def _require_chunk_evidence_contract(chunk: dict[str, Any]) -> str:
    evidence_id = str(chunk.get("evidence_id") or "").strip()
    chunk_id = str(chunk.get("chunk_id") or "").strip()
    content_hash = str(chunk.get("content_hash") or "").strip().lower()
    contract_version = str(chunk.get("evidence_contract_version") or "").strip()
    if not evidence_id or evidence_id != chunk_id:
        raise ValueError("Knowledge chunk has no stable evidence identity.")
    if contract_version != EVIDENCE_INDEX_CONTRACT_VERSION:
        raise ValueError("Knowledge chunk evidence contract version is stale.")
    if content_hash != compute_evidence_content_hash(chunk.get("content")):
        raise ValueError("Knowledge chunk content hash does not match canonical content.")
    meta = dict(chunk.get("meta") or {})
    if (
        str(meta.get("evidence_id") or "").strip() != evidence_id
        or str(meta.get("content_hash") or "").strip().lower() != content_hash
        or str(meta.get("evidence_contract_version") or "").strip() != contract_version
    ):
        raise ValueError("Knowledge chunk metadata does not match its evidence contract.")
    return evidence_id


def _chunk_source_doc_id(chunk: dict[str, Any]) -> str:
    source_doc_id = str((chunk.get("meta") or {}).get("source_doc_id") or "").strip()
    if not source_doc_id:
        raise ValueError("Knowledge chunk has no source document identity.")
    return source_doc_id


def _expected_chunk_ids_by_source_doc(
    chunks: list[dict[str, Any]],
) -> dict[str, set[str]]:
    expected: dict[str, set[str]] = {}
    for chunk in chunks:
        evidence_id = _require_chunk_evidence_contract(chunk)
        source_doc_id = _chunk_source_doc_id(chunk)
        source_ids = expected.setdefault(source_doc_id, set())
        if evidence_id in source_ids:
            raise ValueError("Knowledge chunk evidence identity is duplicated.")
        source_ids.add(evidence_id)
    if not expected:
        raise ValueError("Knowledge index replacement requires at least one source document.")
    return expected


def _source_doc_ids_from_chunks(chunks: list[dict[str, Any]]) -> list[str]:
    source_doc_ids = sorted({_chunk_source_doc_id(chunk) for chunk in chunks})
    if not source_doc_ids:
        raise ValueError("Knowledge index replacement requires a source document.")
    return source_doc_ids


def _batched(values: list[str], size: int) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _azure_search_client(config: AICopilotConfig) -> Any:
    from azure.search.documents import SearchClient

    return SearchClient(
        endpoint=config.azure_search_endpoint,
        index_name=config.azure_search_index_name,
        credential=_azure_search_credential(config),
    )


def _azure_source_doc_chunk_ids(search_client: Any, source_doc_id: str) -> set[str]:
    filter_text = _azure_filter_to_odata(
        {
            "field": "source_doc_id",
            "operator": "==",
            "value": source_doc_id,
        }
    )
    if not filter_text:
        raise RuntimeError("Azure source document filter could not be constructed.")
    results = search_client.search(
        search_text="*",
        filter=filter_text,
        select=["chunk_id"],
    )
    return {
        str(item.get("chunk_id") or "").strip()
        for item in results
        if str(item.get("chunk_id") or "").strip()
    }


def _require_azure_indexing_results(
    results: Any,
    *,
    expected_count: int,
    operation: str,
) -> None:
    try:
        items = list(results)
    except TypeError as exc:
        raise RuntimeError(
            f"Azure {operation} did not return per-document results."
        ) from exc
    succeeded = sum(
        bool(
            item.get("succeeded")
            if isinstance(item, dict)
            else getattr(item, "succeeded", False)
        )
        for item in items
    )
    if len(items) != expected_count or succeeded != expected_count:
        raise RuntimeError(
            f"Azure {operation} result mismatch:"
            f"{succeeded}/{expected_count} succeeded."
        )


def _wait_for_azure_source_doc_ids(
    search_client: Any,
    *,
    source_doc_id: str,
    expected_ids: set[str],
) -> bool:
    for attempt in range(REMOTE_INDEX_VERIFY_ATTEMPTS):
        if _azure_source_doc_chunk_ids(search_client, source_doc_id) == expected_ids:
            return True
        if attempt + 1 < REMOTE_INDEX_VERIFY_ATTEMPTS:
            time.sleep(REMOTE_INDEX_VERIFY_DELAY_SECONDS)
    return False


def _delete_existing_azure_chunks_for_source_docs(
    search_client: Any,
    chunks: list[dict[str, Any]],
) -> None:
    source_doc_ids = _source_doc_ids_from_chunks(chunks)
    for source_doc_id in source_doc_ids:
        existing_ids = sorted(
            _azure_source_doc_chunk_ids(search_client, source_doc_id)
        )
        for chunk_ids in _batched(
            existing_ids,
            REMOTE_INDEX_OPERATION_BATCH_SIZE,
        ):
            results = search_client.delete_documents(
                documents=[
                    {"chunk_id": chunk_id}
                    for chunk_id in chunk_ids
                ]
            )
            _require_azure_indexing_results(
                results,
                expected_count=len(chunk_ids),
                operation="delete",
            )
        if not _wait_for_azure_source_doc_ids(
            search_client,
            source_doc_id=source_doc_id,
            expected_ids=set(),
        ):
            raise RuntimeError(
                "Azure source-document replacement could not verify old chunk deletion."
            )


def _verify_azure_replacement(
    search_client: Any,
    *,
    expected_by_source_doc: dict[str, set[str]],
) -> None:
    for source_doc_id, expected_ids in expected_by_source_doc.items():
        if not _wait_for_azure_source_doc_ids(
            search_client,
            source_doc_id=source_doc_id,
            expected_ids=expected_ids,
        ):
            raise RuntimeError(
                "Azure source-document replacement verification failed:"
                f" expected {len(expected_ids)} current chunks."
            )


class AzureHaystackIndexer:
    def __init__(self, config: AICopilotConfig) -> None:
        self.config = config

    def replace_chunks(self, chunks: list[dict[str, Any]]) -> int:
        return AzureSearchSDKIndexer(self.config).replace_chunks(chunks)

    def upsert_chunks(self, chunks: list[dict[str, Any]]) -> int:
        return self.replace_chunks(chunks)


class AzureSearchSDKIndexer:
    def __init__(self, config: AICopilotConfig) -> None:
        self.config = config

    def replace_chunks(self, chunks: list[dict[str, Any]]) -> int:
        if not chunks:
            return 0

        _ensure_azure_index_contract(self.config)
        expected_by_source_doc = _expected_chunk_ids_by_source_doc(chunks)
        search_client = _azure_search_client(self.config)
        _delete_existing_azure_chunks_for_source_docs(search_client, chunks)
        contents = [str(chunk.get("content") or "") for chunk in chunks]
        embeddings = _embed_texts(contents, config=self.config)
        documents = []
        for chunk, embedding in zip(chunks, embeddings, strict=False):
            evidence_id = _require_chunk_evidence_contract(chunk)
            documents.append(
                {
                    "chunk_id": evidence_id,
                    "content": str(chunk.get("content") or ""),
                    "embedding": embedding,
                    **_flatten_azure_chunk_meta(dict(chunk.get("meta") or {})),
                }
            )
        written = 0
        for document_batch in [
            documents[index : index + REMOTE_INDEX_OPERATION_BATCH_SIZE]
            for index in range(0, len(documents), REMOTE_INDEX_OPERATION_BATCH_SIZE)
        ]:
            results = search_client.upload_documents(document_batch)
            _require_azure_indexing_results(
                results,
                expected_count=len(document_batch),
                operation="upload",
            )
            written += len(document_batch)
        _verify_azure_replacement(
            search_client,
            expected_by_source_doc=expected_by_source_doc,
        )
        return written

    def upsert_chunks(self, chunks: list[dict[str, Any]]) -> int:
        return self.replace_chunks(chunks)


def delete_azure_search_chunks_for_source_docs(
    config: AICopilotConfig,
    *,
    source_doc_ids: list[str],
) -> None:
    if not config.azure_search_ready:
        raise RuntimeError("Azure AI Search is not configured.")

    from azure.search.documents import SearchClient

    chunks = [
        {"meta": {"source_doc_id": source_doc_id}}
        for source_doc_id in source_doc_ids
        if str(source_doc_id or "").strip()
    ]
    if not chunks:
        return
    search_client = SearchClient(
        endpoint=config.azure_search_endpoint,
        index_name=config.azure_search_index_name,
        credential=_azure_search_credential(config),
    )
    _delete_existing_azure_chunks_for_source_docs(search_client, chunks)


class DashVectorIndexer:
    def __init__(self, config: AICopilotConfig) -> None:
        self.config = config

    def replace_chunks(self, chunks: list[dict[str, Any]]) -> int:
        if not chunks:
            return 0

        from dashvector import Doc

        expected_by_source_doc = _expected_chunk_ids_by_source_doc(chunks)
        contents = [str(chunk.get("content") or "") for chunk in chunks]
        embeddings = _embed_texts(contents, config=self.config)
        first_embedding = embeddings[0]
        collection = _dashvector_collection(
            _dashvector_contract_config(self.config),
            create_if_missing=True,
            dimension=len(first_embedding),
        )
        partition = self.config.dashvector_partition_name or None
        for source_doc_id in expected_by_source_doc:
            _delete_dashvector_source_doc_chunks(
                collection,
                source_doc_id=source_doc_id,
                partition=partition,
            )
        docs = []
        for chunk, embedding in zip(chunks, embeddings, strict=False):
            evidence_id = _require_chunk_evidence_contract(chunk)
            docs.append(
                Doc(
                    id=evidence_id,
                    vector=embedding,
                    fields=_flatten_dashvector_chunk_meta(
                        dict(chunk.get("meta") or {}),
                        content=str(chunk.get("content") or ""),
                    ),
                )
            )
        written = 0
        for document_batch in [
            docs[index : index + DASHVECTOR_MATCH_BATCH_SIZE]
            for index in range(0, len(docs), DASHVECTOR_MATCH_BATCH_SIZE)
        ]:
            response = collection.upsert(document_batch, partition=partition)
            if not response:
                raise RuntimeError(f"DashVector upsert failed: {response.message}")
            batch_written = len(response.output or [])
            if batch_written != len(document_batch):
                raise RuntimeError(
                    "DashVector write result mismatch:"
                    f"{batch_written}/{len(document_batch)} succeeded."
                )
            written += batch_written
        if written != len(docs):
            raise RuntimeError(
                "DashVector write result mismatch:"
                f"{written}/{len(docs)} succeeded."
            )
        _verify_dashvector_expected_ids(
            collection,
            expected_ids={
                evidence_id
                for ids in expected_by_source_doc.values()
                for evidence_id in ids
            },
            partition=partition,
        )
        return written

    def upsert_chunks(self, chunks: list[dict[str, Any]]) -> int:
        return self.replace_chunks(chunks)


class QdrantIndexer:
    def __init__(self, config: AICopilotConfig) -> None:
        self.config = config

    def replace_chunks(self, chunks: list[dict[str, Any]]) -> int:
        if not chunks:
            return 0

        from qdrant_client.http import models as qdrant_models

        expected_by_source_doc = _expected_chunk_ids_by_source_doc(chunks)
        contents = [str(chunk.get("content") or "") for chunk in chunks]
        embeddings = _embed_texts(contents, config=self.config)
        first_embedding = embeddings[0]
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
        for source_doc_id in expected_by_source_doc:
            source_filter = _qdrant_source_doc_filter(
                qdrant_models,
                source_doc_id,
            )
            client.delete(
                collection_name=self.config.qdrant_collection_name,
                points_selector=source_filter,
                wait=True,
            )
            if _qdrant_source_doc_count(
                client,
                collection_name=self.config.qdrant_collection_name,
                source_filter=source_filter,
            ) != 0:
                raise RuntimeError(
                    "Qdrant source-document replacement could not verify old chunk deletion."
                )
        points = []
        for chunk, embedding in zip(chunks, embeddings, strict=False):
            evidence_id = _require_chunk_evidence_contract(chunk)
            points.append(
                qdrant_models.PointStruct(
                    id=_qdrant_point_id(evidence_id),
                    vector=embedding,
                    payload=_flatten_qdrant_chunk_meta(
                        dict(chunk.get("meta") or {}),
                        content=str(chunk.get("content") or ""),
                    ),
                )
            )
        client.upsert(collection_name=self.config.qdrant_collection_name, points=points, wait=True)
        for source_doc_id, expected_ids in expected_by_source_doc.items():
            _verify_qdrant_source_doc_ids(
                client,
                collection_name=self.config.qdrant_collection_name,
                source_doc_id=source_doc_id,
                expected_ids=expected_ids,
                qdrant_models=qdrant_models,
            )
        return len(points)

    def upsert_chunks(self, chunks: list[dict[str, Any]]) -> int:
        return self.replace_chunks(chunks)


def _dashvector_source_doc_filter(source_doc_id: str) -> str:
    return f"source_doc_id = '{source_doc_id.replace("'", "''")}'"


def _delete_dashvector_source_doc_chunks(
    collection: Any,
    *,
    source_doc_id: str,
    partition: str | None,
) -> None:
    for _ in range(DASHVECTOR_MAX_DELETE_BATCHES):
        response = collection.query(
            topk=DASHVECTOR_MATCH_BATCH_SIZE,
            filter=_dashvector_source_doc_filter(source_doc_id),
            output_fields=["evidence_id"],
            partition=partition,
        )
        if not response:
            raise RuntimeError(
                f"DashVector replacement query failed: {response.message}"
            )
        existing_ids = [
            str(getattr(item, "id", "") or "").strip()
            for item in response.output or []
            if str(getattr(item, "id", "") or "").strip()
        ]
        if not existing_ids:
            return
        delete_response = collection.delete(
            ids=existing_ids,
            partition=partition,
        )
        if not delete_response:
            raise RuntimeError(
                f"DashVector replacement delete failed: {delete_response.message}"
            )
        time.sleep(REMOTE_INDEX_VERIFY_DELAY_SECONDS)
    raise RuntimeError(
        "DashVector source-document replacement exceeded the bounded delete budget."
    )


def _verify_dashvector_expected_ids(
    collection: Any,
    *,
    expected_ids: set[str],
    partition: str | None,
) -> None:
    found_ids: set[str] = set()
    for attempt in range(REMOTE_INDEX_VERIFY_ATTEMPTS):
        found_ids = set()
        for ids in _batched(sorted(expected_ids), DASHVECTOR_MATCH_BATCH_SIZE):
            response = collection.fetch(ids=ids, partition=partition)
            if not response:
                raise RuntimeError(
                    f"DashVector replacement verification failed: {response.message}"
                )
            output = response.output or {}
            if isinstance(output, dict):
                found_ids.update(
                    str(item).strip()
                    for item in output
                    if str(item).strip()
                )
            else:
                found_ids.update(
                    str(getattr(item, "id", "") or "").strip()
                    for item in output
                    if str(getattr(item, "id", "") or "").strip()
                )
        if found_ids == expected_ids:
            return
        if attempt + 1 < REMOTE_INDEX_VERIFY_ATTEMPTS:
            time.sleep(REMOTE_INDEX_VERIFY_DELAY_SECONDS)
    raise RuntimeError(
        "DashVector source-document replacement verification failed:"
        f" found {len(found_ids)}/{len(expected_ids)} expected chunks."
    )


def _qdrant_source_doc_filter(qdrant_models: Any, source_doc_id: str) -> Any:
    return qdrant_models.Filter(
        must=[
            qdrant_models.FieldCondition(
                key="source_doc_id",
                match=qdrant_models.MatchValue(value=source_doc_id),
            )
        ]
    )


def _qdrant_source_doc_count(
    client: Any,
    *,
    collection_name: str,
    source_filter: Any,
) -> int:
    result = client.count(
        collection_name=collection_name,
        count_filter=source_filter,
        exact=True,
    )
    return int(getattr(result, "count", 0) or 0)


def _verify_qdrant_source_doc_ids(
    client: Any,
    *,
    collection_name: str,
    source_doc_id: str,
    expected_ids: set[str],
    qdrant_models: Any,
) -> None:
    source_filter = _qdrant_source_doc_filter(qdrant_models, source_doc_id)
    found_ids: set[str] = set()
    offset: Any = None
    seen_offsets: set[str] = set()
    while True:
        records, next_offset = client.scroll(
            collection_name=collection_name,
            scroll_filter=source_filter,
            limit=256,
            offset=offset,
            with_payload=["evidence_id"],
            with_vectors=False,
        )
        for record in records:
            payload = getattr(record, "payload", None) or {}
            evidence_id = str(payload.get("evidence_id") or "").strip()
            if evidence_id:
                found_ids.add(evidence_id)
        if next_offset is None:
            break
        offset_key = str(next_offset)
        if offset_key in seen_offsets:
            raise RuntimeError(
                "Qdrant replacement verification returned a repeated scroll offset."
            )
        seen_offsets.add(offset_key)
        offset = next_offset
    if found_ids != expected_ids:
        raise RuntimeError(
            "Qdrant source-document replacement verification failed:"
            f" found {len(found_ids)}/{len(expected_ids)} expected chunks."
        )


def _select_model_evidence_candidates(
    evidences: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    primary = [
        item
        for item in evidences
        if str(item.get("score_source") or "") != "adjacent_context"
    ][:MAX_MODEL_PRIMARY_EVIDENCES]
    adjacent = [
        item
        for item in evidences
        if str(item.get("score_source") or "") == "adjacent_context"
    ][:MAX_MODEL_ADJACENT_EVIDENCES]
    return primary, adjacent


def build_llm_messages(
    context: dict[str, Any],
    evidences: list[dict[str, Any]],
    *,
    retrieval_backend: str = "local",
) -> list[dict[str, str]]:
    system_prompt = (
        "你是汽车行业需求符合性分析助手。你只依据输入中的需求上下文和候选证据判断，"
        "不得使用未提供的常识补全事实，不得根据文件名、文档类型或相似措辞自行推断权威性、"
        "适用性、有效性或产品能力。authority 为 UNKNOWN 时必须按未知处理。"
        "所有分析字段使用中文；证据原文保持原语言。"
        "\n\n输出合同："
        "\n1. compliance_status 是符合性事实，只能为 COMPLIANT、PARTIALLY_COMPLIANT、"
        "NON_COMPLIANT、INSUFFICIENT_INFORMATION；它不是人工业务处置结论。"
        "\n2. proposed_decision 仅为旧接口兼容投影，必须严格对应："
        "COMPLIANT→ACCEPT，PARTIALLY_COMPLIANT→DEVIATION_ACCEPT，"
        "NON_COMPLIANT→REJECT，INSUFFICIENT_INFORMATION→NEED_CLARIFICATION。"
        "\n3. 逐个拆解需求中的独立约束并写入 sub_judgments；总体状态必须与子判断一致。"
        "\n4. 每条被采用证据都要在 evidences 中原样引用 evidence_id，"
        "并在 evidence_findings 中说明它支持、部分支持、反驳或仅提供上下文。"
        "不得改写 evidence_id、content_hash、出处、原文或相关性分数。"
        "\n5. 若证据缺失、冲突、生命周期/适用范围不明、上下文被截断，或不能直接覆盖全部约束，"
        "输出 INSUFFICIENT_INFORMATION，并完整填写 structured_information_gap："
        "已找到什么、缺什么、为何阻断、需要什么输入以及澄清问题。"
        "\n6. normalized_requirement 必须逐字返回输入中的同名字段。"
        "\n7. clarification_question 在无需澄清时为空；信息不足时必须非空。"
        "\n8. 仅有检索相关度不能证明符合、部分符合或不符合；结论必须由证据原文中的可核查事实支撑。"
        "\n9. adjacent_evidence_context 是与已检索 chunk 相邻的未打分上下文；"
        "不得把相邻关系或父 chunk 的分数当成该文本的符合性证明。"
        "\n10. 每个非 INSUFFICIENT_INFORMATION 的 sub_judgment 必须引用至少一条证据；"
        "规范性要求文本、目标、计划或模板不是产品已经符合/不符合的事实记录。"
    )
    primary_evidences, adjacent_evidences = (
        _select_model_evidence_candidates(evidences)
    )

    def _prompt_evidence_block(
        item: dict[str, Any],
        *,
        slot: int,
        is_adjacent_context: bool,
    ) -> dict[str, Any]:
        return {
                "slot": slot,
                "evidence_slot": slot,
                "is_adjacent_context": is_adjacent_context,
                "retrieval_parent_evidence_id": str(
                    item.get("retrieval_parent_evidence_id") or ""
                ),
                "chunk_id": str(item.get("chunk_id") or ""),
                "evidence_id": str(item.get("evidence_id") or ""),
                "content_hash": str(item.get("content_hash") or ""),
                "evidence_contract_version": str(item.get("evidence_contract_version") or ""),
                "source_doc_id": str(item.get("source_doc_id") or ""),
                "source_doc_name": str(item.get("source_doc_name") or ""),
                "source_doc_version": str(item.get("source_doc_version") or ""),
                "source_location": str(item.get("source_location") or ""),
                "doc_type": str(item.get("doc_type") or ""),
                "status": str(item.get("status") or ""),
                "effective_from": str(item.get("effective_from") or ""),
                "effective_to": str(item.get("effective_to") or ""),
                "content_kind": str(item.get("content_kind") or ""),
                "section_code": str(item.get("section_code") or ""),
                "section_title": str(item.get("section_title") or ""),
                "authority": str(item.get("authority") or "UNKNOWN"),
                "claim_role": str(item.get("claim_role") or "UNKNOWN"),
                "applicability": str(item.get("applicability") or "UNKNOWN"),
                "excerpt": str(item.get("excerpt") or ""),
                "relevance": (
                    None
                    if is_adjacent_context
                    else round(float(item.get("relevance") or 0.0), 4)
                ),
                "score_source": str(item.get("score_source") or ""),
            }

    evidence_blocks = [
        _prompt_evidence_block(
            item,
            slot=index,
            is_adjacent_context=False,
        )
        for index, item in enumerate(primary_evidences, start=1)
    ]
    adjacent_evidence_blocks = [
        _prompt_evidence_block(
            item,
            slot=len(evidence_blocks) + index,
            is_adjacent_context=True,
        )
        for index, item in enumerate(adjacent_evidences, start=1)
    ]
    user_payload = {
        "requirement_id": context.get("requirement_id"),
        "req_code": context.get("req_code"),
        "requirement_type": context.get("requirement_type"),
        "normalized_requirement": context.get("normalized_requirement"),
        "raw_requirement_text": context.get("requirement_text"),
        "source_context": context.get("source_context"),
        "context_warnings": context.get("context_warnings"),
        "context_completeness": context.get("context_completeness"),
        "context_completeness_status": context.get(
            "context_completeness_status"
        ),
        "parent_section_code": context.get("parent_section_code"),
        "parent_section_title": context.get("parent_section_title"),
        "content_summary": context.get("content_summary"),
        "table_context": context.get("table_context"),
        "retrieval_gate_reason": context.get("retrieval_gate_reason"),
        "candidate_evidences": evidence_blocks,
        "adjacent_evidence_context": adjacent_evidence_blocks,
        "decision_rules": {
            "allowed_decisions": list(VALID_DECISIONS),
            "allowed_compliance_statuses": list(VALID_COMPLIANCE_STATUSES),
            "decision_to_compliance_status": dict(
                DECISION_TO_COMPLIANCE_STATUS
            ),
            "allowed_risk_levels": list(VALID_RISK_LEVELS),
            "must_not_accept_without_evidence": True,
            "must_not_invent_sources": True,
            "must_analyze_each_independent_constraint": True,
            "must_explain_information_gap_when_insufficient": True,
            "relevance_is_not_compliance_proof": True,
            "no_effective_evidence_below": _effective_evidence_threshold_for_backend(retrieval_backend),
            "decision_requires_top_evidence_at_least": _decision_evidence_threshold_for_backend(retrieval_backend),
        },
    }
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False, indent=2)},
    ]


def build_screening_messages(context: dict[str, Any]) -> list[dict[str, str]]:
    section_title = str(context.get("parent_section_title") or "").strip().lower()
    has_table = bool(context.get("table_context"))
    section_hint = ""
    if any(kw in section_title for kw in ("test", "试验", "测试", "verification", "validation")):
        section_hint = "注意：该条目位于测试/试验章节，通常属于可验证的测试要求，应判定为 REQUIREMENT。"
    elif any(kw in section_title for kw in ("scope", "范围", "适用范围", "applicable")):
        section_hint = "注意：该条目位于适用范围章节，可能仅为范围说明，请仔细判断是否包含具体约束。"
    table_hint = "该条目包含表格数据，表格中的参数值通常是具体的技术要求。" if has_table else ""
    system_prompt = (
        "你是汽车需求审阅助手。"
        "所有输出文字必须使用中文。"
        "请判断当前条目是否属于需要进入合规判定的真实需求。"
        "如果条目只是信息输入、背景说明、标题、上下文标签、适用范围说明或引用提示，请返回 INFO。"
        "只有在条目表达了明确约束、行为要求、性能边界、法规符合性或可验证义务时，才返回 REQUIREMENT。"
        "注意：产品能力声明（如'designed to fulfill'、'is designed to'、'is resistant against'）也属于合规需求，应返回 REQUIREMENT。"
        "reason 只写一句简洁依据。"
    )
    if section_hint:
        system_prompt += f"\n{section_hint}"
    if table_hint:
        system_prompt += f"\n{table_hint}"
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


def build_azure_filter(
    *,
    project_id: str,
    knowledge_docs: list[dict[str, Any]],
    as_of: date | None = None,
) -> dict[str, Any] | None:
    eligible_docs = _eligible_knowledge_documents(knowledge_docs, as_of=as_of)
    conditions: list[dict[str, Any]] = []
    if project_id:
        conditions.append({"field": "project_id", "operator": "==", "value": project_id})
    doc_ids = [str(doc.get("id") or "").strip() for doc in eligible_docs]
    if doc_ids:
        conditions.append({"field": "source_doc_id", "operator": "in", "value": doc_ids})
        conditions.append({"field": "status", "operator": "==", "value": ACTIVE_KNOWLEDGE_STATUS})
        conditions.append(
            {
                "field": "evidence_contract_version",
                "operator": "==",
                "value": EVIDENCE_INDEX_CONTRACT_VERSION,
            }
        )
        doc_types = sorted({str(doc.get("doc_type") or "").strip().upper() for doc in eligible_docs})
        conditions.append({"field": "doc_type", "operator": "in", "value": doc_types})
    else:
        conditions.append(
            {
                "field": "source_doc_id",
                "operator": "==",
                "value": NO_ELIGIBLE_EVIDENCE_ID,
            }
        )
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
    final_result = _ensure_final_assessment_semantics(
        final_result,
        context=collector.context,
    )
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
            "compliance_status": final_result["compliance_status"],
            "ai_reason": final_result["ai_reason"],
            "ai_confidence": final_result["ai_confidence"],
            "risk_level": final_result["risk_level"],
            "clarification_question": final_result["clarification_question"],
            "normalized_requirement": final_result["normalized_requirement"],
            "evidence_findings": final_result["evidence_findings"],
            "sub_judgments": final_result["sub_judgments"],
            "structured_information_gap": final_result[
                "structured_information_gap"
            ],
            "assessment_mode": final_mode,
            "retrieval_backend": collector.retrieval_backend,
            "model_name": final_model_name,
            "prompt_version": final_prompt_version,
        },
        "evidences": final_result["evidences"],
        "runs": collector.runs,
        "trace_path": str(trace_path),
    }


def _ensure_final_assessment_semantics(
    final_result: dict[str, Any],
    *,
    context: dict[str, Any],
) -> dict[str, Any]:
    normalized = dict(final_result)
    decision = str(normalized.get("ai_decision") or "").strip()
    compliance_status = str(normalized.get("compliance_status") or "").strip()
    if not compliance_status and decision:
        compliance_status = DECISION_TO_COMPLIANCE_STATUS.get(decision, "")
    normalized["compliance_status"] = compliance_status
    normalized.setdefault("evidence_findings", [])
    normalized.setdefault("sub_judgments", [])
    normalized.setdefault("structured_information_gap", {})
    evidences = [
        dict(item)
        for item in normalized.get("evidences") or []
        if isinstance(item, dict)
    ]
    if (
        compliance_status == "INSUFFICIENT_INFORMATION"
        and not _structured_information_gap_has_content(
            normalized["structured_information_gap"]
            if isinstance(normalized["structured_information_gap"], dict)
            else {}
        )
    ):
        normalized["structured_information_gap"] = (
            _build_structured_information_gap(context, evidences)
        )
    if not normalized["evidence_findings"] and evidences and compliance_status:
        normalized["evidence_findings"] = _build_default_evidence_findings(
            compliance_status,
            evidences,
            reason=str(normalized.get("ai_reason") or ""),
        )
    if not normalized["sub_judgments"] and compliance_status:
        normalized["sub_judgments"] = _build_default_sub_judgments(
            context=context,
            compliance_status=compliance_status,
            reason=str(normalized.get("ai_reason") or ""),
            evidences=evidences,
            structured_information_gap=normalized["structured_information_gap"],
        )
    return normalized


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


_VISUAL_ONLY_PATTERNS = re.compile(
    r"(图纸|工程图|设计图|装配图|外形图|尺寸图|drawing|blueprint|visual\s*inspection"
    r"|外观|清洁度.*图|cleanliness.*drawing)",
    re.IGNORECASE,
)


def _is_visual_only_requirement(context: dict[str, Any]) -> bool:
    requirement_type = str(context.get("requirement_type") or "").strip().upper()
    if requirement_type in {"VISUAL", "DRAWING"}:
        return True
    section_title = str(context.get("parent_section_title") or "").strip()
    requirement_text = str(context.get("requirement_text") or "").strip()
    combined = f"{section_title} {requirement_text}"
    return bool(_VISUAL_ONLY_PATTERNS.search(combined))


def build_visual_only_assessment_result(*, context: dict[str, Any], screening: dict[str, Any]) -> dict[str, Any]:
    return {
        "analysis_label": "REQUIREMENT",
        "analysis_basis": "视觉/图纸类需求，权威证据为工程图纸，文本 RAG 无法提供可靠判定。",
        "ai_decision": "NEED_CLARIFICATION",
        "ai_reason": "该需求涉及图纸或视觉检查，需要人工评审工程图纸以确认合规性。",
        "ai_confidence": 0.0,
        "risk_level": "MEDIUM",
        "clarification_question": "请提供相关工程图纸以确认该需求的合规性。",
        "normalized_requirement": str(context.get("normalized_requirement") or ""),
        "evidences": [],
        "validation_status": "visual_only_routing",
        "fallback_reason": "visual_only_requirement",
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


def build_screening_failed_assessment_result(*, context: dict[str, Any], screening: dict[str, Any]) -> dict[str, Any]:
    detail = str(screening.get("fallback_reason") or "").strip()
    basis = "LLM screening failed; analysis stopped."
    if detail:
        basis = f"{basis} {detail}"
    return {
        "analysis_label": "REQUIREMENT",
        "analysis_basis": basis,
        "ai_decision": "",
        "ai_reason": "",
        "ai_confidence": 0.0,
        "risk_level": "",
        "clarification_question": "",
        "normalized_requirement": str(context.get("normalized_requirement") or ""),
        "evidences": [],
        "validation_status": "failed",
        "fallback_reason": detail,
    }


def build_analysis_failed_assessment_result(
    *,
    context: dict[str, Any],
    screening: dict[str, Any],
    retrieval_backend: str,
    failure_reason: str,
) -> dict[str, Any]:
    detail = str(failure_reason or "").strip()
    basis = "LLM requirement analysis failed; analysis stopped."
    if detail:
        basis = f"{basis} {detail}"
    screening_reason = str(screening.get("reason") or "").strip()
    if screening_reason:
        basis = f"{basis} 筛选依据：{screening_reason}。"
    if retrieval_backend:
        basis = f"{basis} 检索后端 {retrieval_backend}。"
    return {
        "analysis_label": "REQUIREMENT",
        "analysis_basis": basis,
        "ai_decision": "",
        "ai_reason": "",
        "ai_confidence": 0.0,
        "risk_level": "",
        "clarification_question": "",
        "normalized_requirement": str(context.get("normalized_requirement") or ""),
        "evidences": [],
        "validation_status": "failed",
        "fallback_reason": detail,
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
    _, validation_error, _ = _resolve_evidence_references(cited, retrieved)
    return validation_error


def _resolve_evidence_references(
    cited: list[AssessmentEvidenceSchema],
    retrieved: list[dict[str, Any]],
) -> tuple[list[EvidenceRef], str, bool]:
    server_candidates = _server_evidence_refs(retrieved)
    if cited and retrieved and not server_candidates:
        return [], "evidence_index_contract_invalid", False
    selected: list[EvidenceRef] = []
    selected_values: set[EvidenceRef] = set()
    used_legacy_selector = False
    for evidence in cited:
        cited_evidence_id = evidence.evidence_id.strip()
        if cited_evidence_id:
            identity_matches = [item for item in server_candidates if item.evidence_id == cited_evidence_id]
            if not identity_matches:
                return [], "evidence_reference_out_of_scope", False
            if len(identity_matches) > 1:
                return [], "evidence_identity_conflict", False
            matched = identity_matches[0]
            if matched not in selected_values:
                selected.append(matched)
                selected_values.add(matched)
            continue

        used_legacy_selector = True
        cited_doc_id = evidence.source_doc_id.strip()
        cited_location = evidence.source_location.strip()
        cited_excerpt = normalize_requirement_text(evidence.excerpt)
        cited_chunk_id = str(evidence.chunk_id or "").strip()
        cited_slot = int(evidence.evidence_slot or 0)
        matches: list[EvidenceRef] = []
        if cited_chunk_id:
            for raw_item in retrieved:
                if str(raw_item.get("chunk_id") or "").strip() != cited_chunk_id:
                    continue
                candidate = EvidenceRef.from_server_candidate(raw_item)
                if candidate in server_candidates:
                    matches.append(candidate)
        elif cited_slot and 1 <= cited_slot <= len(retrieved):
            candidate = EvidenceRef.from_server_candidate(retrieved[cited_slot - 1])
            if candidate in server_candidates:
                matches.append(candidate)

        if not matches and (not cited_doc_id or not cited_location or not cited_excerpt):
            return [], "evidence_reference_out_of_scope", True
        exact_matches: list[EvidenceRef] = []
        compatible_matches: list[EvidenceRef] = []
        if not matches:
            for item in server_candidates:
                if item.source_doc_id != cited_doc_id or item.source_location != cited_location:
                    continue
                item_excerpt = normalize_requirement_text(item.excerpt)
                if cited_excerpt == item_excerpt:
                    exact_matches.append(item)
                    continue
                if cited_excerpt in item_excerpt or item_excerpt in cited_excerpt:
                    compatible_matches.append(item)
            matches = exact_matches or compatible_matches
        if not matches:
            return [], "evidence_reference_out_of_scope", True
        if len(matches) > 1:
            return [], "evidence_reference_ambiguous", True
        matched = matches[0]
        if matched not in selected_values:
            selected.append(matched)
            selected_values.add(matched)
    return selected, "", used_legacy_selector


def _server_evidence_refs(retrieved: list[dict[str, Any]]) -> list[EvidenceRef]:
    candidates: list[EvidenceRef] = []
    seen: set[EvidenceRef] = set()
    for item in retrieved:
        candidate = EvidenceRef.from_server_candidate(item)
        if not candidate.source_doc_id or not candidate.source_location or not candidate.excerpt:
            continue
        if not candidate.has_current_contract or not bool(item.get("evidence_contract_verified")):
            continue
        if candidate in seen:
            continue
        candidates.append(candidate)
        seen.add(candidate)
    return candidates


def _evidences_conflict(evidences: list[EvidenceRef]) -> bool:
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


def _reconciliation_policy_for_config(config: AICopilotConfig) -> Literal["heuristic", "llm_guarded"]:
    if config.assessment_mode in {"llm_shadow", "llm_primary"}:
        return "llm_guarded"
    return "heuristic"


def _effective_evidence_threshold_for_backend(retrieval_backend: str) -> float:
    backend = str(retrieval_backend or "").strip().lower()
    if backend in {"azure_sdk", "azure_haystack"}:
        return AZURE_MIN_EFFECTIVE_EVIDENCE_SCORE
    return QDRANT_MIN_EFFECTIVE_EVIDENCE_SCORE


def _decision_evidence_threshold_for_backend(retrieval_backend: str) -> float:
    backend = str(retrieval_backend or "").strip().lower()
    if backend in {"azure_sdk", "azure_haystack"}:
        return AZURE_MIN_DECISION_EVIDENCE_SCORE
    return QDRANT_MIN_DECISION_EVIDENCE_SCORE


def _apply_evidence_strength_guardrail(
    *,
    context: dict[str, Any],
    candidate: AssessmentOutputSchema,
    canonical_evidences: list[EvidenceRef],
    retrieved_evidences: list[dict[str, Any]],
    fallback_reason: str,
    retrieval_backend: str,
) -> dict[str, Any] | None:
    decision = str(candidate.proposed_decision or "NEED_CLARIFICATION").strip().upper()
    if decision == "NEED_CLARIFICATION":
        return None
    selected_ids = {
        item.evidence_id
        for item in canonical_evidences[:MAX_PERSISTED_EVIDENCES]
    }
    cited_evidences = [
        item
        for item in retrieved_evidences
        if str(item.get("evidence_id") or item.get("chunk_id") or "").strip()
        in selected_ids
    ]
    top_relevance = max(
        (
            float(
                item.get("retrieval_parent_relevance")
                if str(item.get("score_source") or "") == "adjacent_context"
                else item.get("relevance")
                or 0.0
            )
            for item in cited_evidences
        ),
        default=0.0,
    )
    if top_relevance < _decision_evidence_threshold_for_backend(retrieval_backend):
        return fallback_need_clarification(
            context=context,
            evidences=retrieved_evidences,
            fallback_reason=f"{fallback_reason}; decision_requires_stronger_evidence".strip("; "),
        )
    proof_evidences = [
        item for item in cited_evidences if _evidence_has_decision_proof(item)
    ]
    if not proof_evidences:
        return fallback_need_clarification(
            context=context,
            evidences=retrieved_evidences,
            fallback_reason=(
                f"{fallback_reason}; decision_requires_outcome_proof"
            ).strip("; "),
        )
    if decision == "ACCEPT" and not any(
        _evidence_has_positive_outcome(item)
        for item in proof_evidences
    ):
        return fallback_need_clarification(
            context=context,
            evidences=retrieved_evidences,
            fallback_reason=f"{fallback_reason}; accept_without_positive_proof".strip("; "),
        )
    if decision == "REJECT" and not any(
        _text_has_negative_signal(str(item.get("excerpt") or ""))
        for item in proof_evidences
    ):
        return fallback_need_clarification(
            context=context,
            evidences=retrieved_evidences,
            fallback_reason=f"{fallback_reason}; reject_without_negative_proof".strip("; "),
        )
    if decision == "DEVIATION_ACCEPT" and not any(
        _text_has_partial_compliance_signal(str(item.get("excerpt") or ""))
        for item in proof_evidences
    ):
        return fallback_need_clarification(
            context=context,
            evidences=retrieved_evidences,
            fallback_reason=f"{fallback_reason}; deviation_without_partial_proof".strip("; "),
        )
    return None


def _apply_hard_assessment_rules(
    *,
    context: dict[str, Any],
    candidate: AssessmentOutputSchema,
    canonical_evidences: list[EvidenceRef],
    retrieved_evidences: list[dict[str, Any]],
    fallback_reason: str,
) -> dict[str, Any] | None:
    requirement_type = str(context.get("requirement_type") or "FUNCTIONAL").strip().upper()
    cited_evidences = _canonical_evidence_mappings(
        canonical_evidences,
        retrieved_evidences,
        limit=MAX_PERSISTED_EVIDENCES,
    )
    top_relevance = max(
        (
            float(
                item.get("retrieval_parent_relevance")
                if str(item.get("score_source") or "") == "adjacent_context"
                else item.get("relevance")
                or 0.0
            )
            for item in cited_evidences
        ),
        default=0.0,
    )
    has_negative = any(_text_has_negative_signal(str(item.get("excerpt") or "")) for item in cited_evidences)
    has_positive = any(
        _evidence_has_positive_outcome(item) for item in cited_evidences
    )
    has_compliance = any(_text_has_compliance_signal(str(item.get("excerpt") or "")) for item in cited_evidences)
    has_decision_proof = any(
        _evidence_has_decision_proof(item) for item in cited_evidences
    )
    decision = str(candidate.proposed_decision or "NEED_CLARIFICATION").strip().upper()

    if decision != "NEED_CLARIFICATION" and not has_decision_proof:
        return fallback_need_clarification(
            context=context,
            evidences=retrieved_evidences,
            fallback_reason=f"{fallback_reason}; decision_requires_outcome_proof".strip("; "),
        )

    if decision == "REJECT" and not has_negative:
        return fallback_need_clarification(
            context=context,
            evidences=retrieved_evidences,
            fallback_reason=f"{fallback_reason}; reject_without_negative_signal".strip("; "),
        )
    if decision == "ACCEPT" and not has_positive:
        return fallback_need_clarification(
            context=context,
            evidences=retrieved_evidences,
            fallback_reason=f"{fallback_reason}; accept_without_positive_proof".strip("; "),
        )
    if decision == "DEVIATION_ACCEPT" and not any(
        _text_has_partial_compliance_signal(str(item.get("excerpt") or ""))
        for item in cited_evidences
        if _evidence_has_decision_proof(item)
    ):
        return fallback_need_clarification(
            context=context,
            evidences=retrieved_evidences,
            fallback_reason=f"{fallback_reason}; deviation_without_partial_proof".strip("; "),
        )

    if decision != "NEED_CLARIFICATION" and top_relevance < QDRANT_MIN_DECISION_EVIDENCE_SCORE:
        return fallback_need_clarification(
            context=context,
            evidences=retrieved_evidences,
            fallback_reason=f"{fallback_reason}; decision_requires_stronger_evidence".strip("; "),
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
            "normalized_requirement": str(context.get("normalized_requirement") or ""),
            "evidences": cited_evidences,
            "validation_status": "hard_rule_override",
            "fallback_reason": f"{fallback_reason}; hard_rule_negative_reject".strip("; "),
        }
    return None


def _document_to_evidence(document: Any) -> dict[str, Any]:
    content = str(getattr(document, "content", "") or "")
    meta = _normalize_chunk_meta(getattr(document, "meta", {}) or {}, content=content)
    score = getattr(document, "score", None)
    return {
        "chunk_id": str(meta.get("chunk_id") or getattr(document, "id", "") or ""),
        "source_doc_id": str(meta.get("source_doc_id") or ""),
        "source_doc_name": str(meta.get("source_doc_name") or ""),
        "source_location": str(meta.get("source_location") or ""),
        "excerpt": content[:800],
        "relevance": round(float(score if score is not None else meta.get("relevance") or 0.0), 4),
        "score_source": "azure_haystack" if score is not None else str(meta.get("score_source") or ""),
        "source_doc_version": str(meta.get("source_doc_version") or ""),
        "content_kind": str(meta.get("content_kind") or ""),
        **_evidence_contract_fields(meta, content=content),
        **_evidence_lifecycle_fields(meta),
        "is_cover_like": bool(meta.get("is_cover_like")),
        "is_glossary_like": bool(meta.get("is_glossary_like")),
        "is_revision_history": bool(meta.get("is_revision_history")),
        "is_signature_block": bool(meta.get("is_signature_block")),
        "is_legal_disclaimer": bool(meta.get("is_legal_disclaimer")),
    }


def _extend_evidence_with_adjacent_chunks(
    evidences: list[dict[str, Any]],
    *,
    knowledge_docs: list[dict[str, Any]],
    max_excerpt_chars: int = 1200,
) -> list[dict[str, Any]]:
    """Add adjacent chunks as separate canonical evidence candidates.

    A previous implementation appended adjacent text to the retrieved excerpt
    while retaining the first chunk's evidence ID and content hash. That broke
    citation binding. Here every adjacent excerpt keeps its own server-owned
    identity, hash, provenance, and lifecycle metadata.
    """
    if not evidences or not knowledge_docs:
        return evidences

    chunk_lookup: dict[str, list[dict[str, Any]]] = {}
    for doc in knowledge_docs:
        doc_id = str(doc.get("id") or "").strip()
        if not doc_id or not evaluate_knowledge_evidence_eligibility(doc).allowed:
            continue
        payload = _knowledge_index_payload(doc)
        if payload is None:
            continue
        chunks = payload.get("chunks")
        if not isinstance(chunks, list):
            continue
        chunk_lookup[doc_id] = [item for item in chunks if isinstance(item, dict)]

    if not chunk_lookup:
        return evidences

    extended = [dict(item) for item in evidences]
    seen_evidence_ids = {
        str(item.get("evidence_id") or item.get("chunk_id") or "").strip()
        for item in extended
    }
    for ev in evidences:
        doc_id = str(ev.get("source_doc_id") or "").strip()
        location = ev.get("source_location", "")
        if not doc_id or doc_id not in chunk_lookup:
            continue
        doc_chunks = chunk_lookup[doc_id]
        current_evidence_id = str(ev.get("evidence_id") or ev.get("chunk_id") or "").strip()
        current_index = next(
            (
                index
                for index, chunk in enumerate(doc_chunks)
                if str(chunk.get("evidence_id") or chunk.get("chunk_id") or "").strip()
                == current_evidence_id
            ),
            -1,
        )
        if current_index < 0:
            current_index = next(
                (
                    index
                    for index, chunk in enumerate(doc_chunks)
                    if str((chunk.get("meta") or {}).get("source_location") or "").strip()
                    == str(location or "").strip()
                ),
                -1,
            )
        if current_index < 0 or current_index + 1 >= len(doc_chunks):
            continue

        current_chunk = doc_chunks[current_index]
        adjacent_chunk = doc_chunks[current_index + 1]
        current_kind = str(
            current_chunk.get("content_kind")
            or (current_chunk.get("meta") or {}).get("content_kind")
            or ""
        ).strip()
        adjacent_kind = str(
            adjacent_chunk.get("content_kind")
            or (adjacent_chunk.get("meta") or {}).get("content_kind")
            or ""
        ).strip()
        if current_kind != adjacent_kind:
            continue
        adjacent_id = str(
            adjacent_chunk.get("evidence_id") or adjacent_chunk.get("chunk_id") or ""
        ).strip()
        adjacent_content = str(adjacent_chunk.get("content") or "")
        adjacent_meta = _normalize_chunk_meta(
            dict(adjacent_chunk.get("meta") or {}),
            content=adjacent_content,
        )
        if (
            not adjacent_id
            or adjacent_id in seen_evidence_ids
            or not adjacent_content.strip()
            or str(adjacent_chunk.get("content_hash") or "").strip().lower()
            != compute_evidence_content_hash(adjacent_content)
        ):
            continue
        try:
            source_relevance = float(ev.get("relevance") or 0.0)
        except (TypeError, ValueError):
            source_relevance = 0.0
        adjacent_evidence = {
            "chunk_id": str(adjacent_chunk.get("chunk_id") or adjacent_id),
            "source_doc_id": str(adjacent_meta.get("source_doc_id") or doc_id),
            "source_doc_name": str(adjacent_meta.get("source_doc_name") or ""),
            "source_location": str(adjacent_meta.get("source_location") or ""),
            "excerpt": adjacent_content[:max_excerpt_chars],
            "relevance": 0.0,
            "score_source": "adjacent_context",
            "retrieval_parent_evidence_id": current_evidence_id,
            "retrieval_parent_relevance": round(
                max(0.0, min(1.0, source_relevance)),
                4,
            ),
            "source_doc_version": str(adjacent_meta.get("source_doc_version") or ""),
            "content_kind": adjacent_kind,
            **_evidence_contract_fields(adjacent_meta, content=adjacent_content),
            **_evidence_lifecycle_fields(adjacent_meta),
            "section_code": str(adjacent_meta.get("section_code") or ""),
            "section_title": str(adjacent_meta.get("section_title") or ""),
        }
        if not adjacent_evidence["evidence_contract_verified"]:
            continue
        extended.append(adjacent_evidence)
        seen_evidence_ids.add(adjacent_id)

    return extended


def _dashvector_doc_to_evidence(document: Any) -> dict[str, Any]:
    content = str((getattr(document, "fields", {}) or {}).get("content") or "")
    fields = _normalize_chunk_meta(getattr(document, "fields", {}) or {}, content=content)
    score = getattr(document, "score", None)
    return {
        "source_doc_id": str(fields.get("source_doc_id") or ""),
        "source_doc_name": str(fields.get("source_doc_name") or ""),
        "source_location": str(fields.get("source_location") or ""),
        "excerpt": content[:380],
        "relevance": round(_normalize_dashvector_relevance_score(score), 4),
        "source_doc_version": str(fields.get("source_doc_version") or ""),
        "content_kind": str(fields.get("content_kind") or ""),
        **_evidence_contract_fields(fields, content=content),
        **_evidence_lifecycle_fields(fields),
    }


def _qdrant_point_to_evidence(point: Any) -> dict[str, Any]:
    content = str((getattr(point, "payload", {}) or {}).get("content") or "")
    payload = _normalize_chunk_meta(getattr(point, "payload", {}) or {}, content=content)
    score = getattr(point, "score", None)
    return {
        "source_doc_id": str(payload.get("source_doc_id") or ""),
        "source_doc_name": str(payload.get("source_doc_name") or ""),
        "source_location": str(payload.get("source_location") or ""),
        "excerpt": content[:380],
        "relevance": round(_normalize_qdrant_relevance_score(score), 4),
        "source_doc_version": str(payload.get("source_doc_version") or ""),
        "content_kind": str(payload.get("content_kind") or ""),
        **_evidence_contract_fields(payload, content=content),
        **_evidence_lifecycle_fields(payload),
    }


def _evidence_contract_fields(value: dict[str, Any], *, content: str) -> dict[str, Any]:
    fields = {
        "evidence_id": str(value.get("evidence_id") or "").strip(),
        "content_hash": str(value.get("content_hash") or "").strip().lower(),
        "evidence_contract_version": str(value.get("evidence_contract_version") or "").strip(),
    }
    fields["evidence_contract_verified"] = bool(
        has_current_evidence_contract(fields)
        and fields["content_hash"] == compute_evidence_content_hash(content)
    )
    return fields


def _evidence_lifecycle_fields(value: dict[str, Any]) -> dict[str, str]:
    return {
        "doc_type": str(value.get("doc_type") or ""),
        "status": str(value.get("status") or ""),
        "effective_from": str(value.get("effective_from") or ""),
        "effective_to": str(value.get("effective_to") or ""),
        "authority": str(value.get("authority") or "UNKNOWN"),
        "claim_role": str(value.get("claim_role") or "UNKNOWN"),
        "applicability": str(value.get("applicability") or "UNKNOWN"),
        "table_id": str(value.get("table_id") or ""),
        "native_locator": str(value.get("native_locator") or ""),
        "native_format": str(value.get("native_format") or ""),
        "native_page": str(value.get("native_page") or ""),
        "native_table_index": str(value.get("native_table_index") or ""),
        "native_body_element_index": str(
            value.get("native_body_element_index") or ""
        ),
        "native_workbook": str(value.get("native_workbook") or ""),
        "native_sheet": str(value.get("native_sheet") or ""),
        "native_range": str(value.get("native_range") or ""),
    }


def _select_retriever(config: AICopilotConfig) -> EvidenceRetriever:
    if config.provider == DEFAULT_ALIYUN_PROFILE_ID and config.retrieval_backend == DISABLED_RETRIEVAL_BACKEND:
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
    if config.retrieval_backend in {"azure_sdk", "azure_haystack"}:
        raise RuntimeError("Azure retrieval runtime is not configured")
    return HeuristicEvidenceRetriever()


def _select_azure_indexer(config: AICopilotConfig) -> AzureHaystackIndexer | AzureSearchSDKIndexer:
    if config.retrieval_backend == "azure_sdk" or config.uses_rbac_for_search:
        return AzureSearchSDKIndexer(config)
    return AzureHaystackIndexer(config)


def _select_indexer(config: AICopilotConfig) -> AzureHaystackIndexer | AzureSearchSDKIndexer | DashVectorIndexer | QdrantIndexer:
    if config.provider == DEFAULT_ALIYUN_PROFILE_ID and config.retrieval_backend == DISABLED_RETRIEVAL_BACKEND:
        raise RuntimeError(ALIYUN_RETRIEVAL_DISABLED_MESSAGE)
    if config.retrieval_backend == "qdrant":
        return QdrantIndexer(config)
    if config.retrieval_backend == "dashvector":
        return DashVectorIndexer(config)
    return _select_azure_indexer(config)


def _dashvector_contract_collection_name(collection_name: str) -> str:
    base_name = str(collection_name or "").strip()
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{2,31}", base_name):
        raise ValueError(
            "DashVector collection name must start with a letter, contain only letters, digits, '_' or '-', "
            "and be 3-32 characters long."
        )
    suffix = f"-{EVIDENCE_INDEX_CONTRACT_VERSION}"
    if base_name.endswith(suffix):
        return base_name
    versioned_name = f"{base_name}{suffix}"
    if len(versioned_name) > 32:
        raise ValueError(
            f"DashVector collection base name must be at most {32 - len(suffix)} characters "
            f"when using the {EVIDENCE_INDEX_CONTRACT_VERSION} contract."
        )
    return versioned_name


def _dashvector_contract_config(config: AICopilotConfig) -> AICopilotConfig:
    return replace(
        config,
        dashvector_collection_name=_dashvector_contract_collection_name(config.dashvector_collection_name),
    )


def _evidence_index_resource(config: AICopilotConfig) -> str:
    if config.retrieval_backend == "dashvector":
        return _dashvector_contract_collection_name(config.dashvector_collection_name)
    if config.retrieval_backend == "qdrant":
        return str(config.qdrant_collection_name or "")
    return str(config.azure_search_index_name or "")


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
        "evidence_id": str,
        "content_hash": str,
        "evidence_contract_version": str,
        "source_doc_id": str,
        "source_doc_name": str,
        "source_doc_version": str,
        "doc_type": str,
        "status": str,
        "effective_from": str,
        "effective_to": str,
        "authority": str,
        "claim_role": str,
        "applicability": str,
        "section_code": str,
        "section_title": str,
        "source_location": str,
        "content_kind": str,
        "table_id": str,
        "native_locator": str,
        "native_format": str,
        "native_page": str,
        "native_table_index": str,
        "native_body_element_index": str,
        "native_workbook": str,
        "native_sheet": str,
        "native_range": str,
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
        "is_cover_like": bool(meta.get("is_cover_like")),
        "is_glossary_like": bool(meta.get("is_glossary_like")),
        "is_revision_history": bool(meta.get("is_revision_history")),
        "is_signature_block": bool(meta.get("is_signature_block")),
        "is_legal_disclaimer": bool(meta.get("is_legal_disclaimer")),
    }


@lru_cache(maxsize=2)
def _default_azure_credential(allow_interactive_login: bool):
    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential(
        exclude_interactive_browser_credential=not allow_interactive_login,
        exclude_shared_token_cache_credential=True,
        exclude_visual_studio_code_credential=True,
    )


@lru_cache(maxsize=1)
def _get_qdrant_local_reranker():
    try:
        from haystack.components.rankers import SentenceTransformersSimilarityRanker
    except ImportError:
        return None
    try:
        ranker = SentenceTransformersSimilarityRanker(
            model=QDRANT_LOCAL_RERANKER_MODEL,
            top_k=QDRANT_HYBRID_RERANK_TOP_K,
            scale_score=True,
        )
        ranker.warm_up()
        return ranker
    except Exception:
        return None


@lru_cache(maxsize=2)
def _azure_openai_token_provider(allow_interactive_login: bool):
    from azure.identity import get_bearer_token_provider

    return get_bearer_token_provider(
        _default_azure_credential(allow_interactive_login),
        DEFAULT_AZURE_OPENAI_SCOPE,
    )


def probe_runtime_azure_rbac() -> dict[str, Any]:
    from datetime import datetime, timezone

    config = load_ai_copilot_config(DEFAULT_AZURE_PROFILE_ID)
    missing = get_ai_profile_missing_config(config)
    credential = _default_azure_credential(config.azure_allow_interactive_login)
    checks: list[dict[str, Any]] = []
    for name, scope in (("azure_openai", DEFAULT_AZURE_OPENAI_SCOPE), ("azure_search", DEFAULT_AZURE_SEARCH_SCOPE)):
        try:
            token = credential.get_token(scope)
            expires_at = (
                datetime.fromtimestamp(
                    int(token.expires_on or 0), timezone.utc
                ).isoformat()
                if getattr(token, "expires_on", None)
                else ""
            )
            checks.append(
                {"name": name, "scope": scope, "status": "ok", "expires_at": expires_at}
            )
        except Exception as exc:  # pragma: no cover - provider interaction
            checks.append(
                {"name": name, "scope": scope, "status": "error", "detail": str(exc)}
            )
    return {
        "provider": DEFAULT_AZURE_PROFILE_ID,
        "auth_mode": config.azure_auth_mode or "rbac",
        "allow_interactive_login": config.azure_allow_interactive_login,
        "config_missing": missing,
        "checks": checks,
        "ok": all(str(item.get("status") or "") == "ok" for item in checks),
    }


def _normalize_azure_openai_endpoint(endpoint: str) -> str:
    base = str(endpoint or "").rstrip("/")
    for suffix in ("/openai/v1", "/openai/v1/"):
        if base.endswith(suffix):
            return base[: -len(suffix)].rstrip("/")
    return base


@lru_cache(maxsize=6)
def _azure_openai_client(config: AICopilotConfig):
    from openai import AzureOpenAI

    kwargs: dict[str, Any] = {
        "azure_endpoint": _normalize_azure_openai_endpoint(
            config.azure_openai_endpoint
        ),
        "api_version": config.azure_openai_api_version,
        "timeout": config.llm_timeout_seconds,
        "max_retries": max(int(config.openai_max_retries or 0), 0),
    }
    if config.uses_api_key_for_openai:
        kwargs["api_key"] = config.azure_openai_api_key
    else:
        kwargs["azure_ad_token_provider"] = _azure_openai_token_provider(
            config.azure_allow_interactive_login
        )
    return AzureOpenAI(**kwargs)


@lru_cache(maxsize=6)
def _aliyun_openai_client(config: AICopilotConfig):
    if not config.dashscope_api_key:
        raise RuntimeError("Aliyun DashScope API key is not configured.")
    if not is_ascii_runtime_secret(config.dashscope_api_key):
        raise RuntimeError(
            "Aliyun DashScope API key contains non-ASCII characters. Re-enter the raw key without Chinese punctuation or full-width characters."
        )
    safe_base_url = validate_dashscope_compatible_base_url(
        config.dashscope_base_url
    )

    from openai import OpenAI

    return OpenAI(
        api_key=config.dashscope_api_key,
        base_url=safe_base_url,
        timeout=config.llm_timeout_seconds,
        max_retries=max(int(config.openai_max_retries or 0), 0),
    )


def probe_runtime_aliyun_models(
    config: AICopilotConfig | None = None,
) -> dict[str, dict[str, Any]]:
    """Run one synthetic request against each configured DashScope capability.

    The probe never receives customer text and never returns provider output,
    credentials, endpoints, exception text, or request payloads.  Each check
    performs at most one provider request so an administrator click has a
    predictable cost and cannot trigger hidden retry traffic.
    """

    runtime_config = config or load_ai_copilot_config(DEFAULT_ALIYUN_PROFILE_ID)
    return {
        "screening": _probe_runtime_aliyun_chat(
            runtime_config,
            model_name=runtime_config.screening_model,
            role="screening",
        ),
        "assessment": _probe_runtime_aliyun_chat(
            runtime_config,
            model_name=runtime_config.chat_model,
            role="assessment",
        ),
        "ocr": _probe_runtime_aliyun_ocr(runtime_config),
        "embedding": _probe_runtime_aliyun_embedding(runtime_config),
    }


def _probe_runtime_aliyun_chat(
    config: AICopilotConfig,
    *,
    model_name: str,
    role: str,
) -> dict[str, Any]:
    configured = bool(
        config.dashscope_api_key and config.dashscope_base_url and model_name
    )
    if not configured:
        return _aliyun_probe_result(
            state="NOT_CONFIGURED",
            reason_code="aliyun_chat_not_configured",
            message="DashScope chat connection is not fully configured.",
            model=model_name,
            role=role,
        )
    probe_config = replace(
        config,
        llm_timeout_seconds=min(max(float(config.llm_timeout_seconds), 1.0), 15.0),
        openai_max_retries=0,
    )
    messages = [
        {
            "role": "system",
            "content": (
                "This is a ReqPilotX synthetic connection probe. No customer "
                "document or requirement text is included. Reply only OK."
            ),
        },
        {
            "role": "user",
            "content": (
                "Confirm the synthetic screening model connection."
                if role == "screening"
                else "Confirm the synthetic assessment model connection."
            ),
        },
    ]
    try:
        response = _aliyun_openai_client(probe_config).chat.completions.create(
            model=model_name,
            messages=messages,
            temperature=0,
            max_tokens=16,
            extra_body={"enable_thinking": False},
        )
        choices = list(getattr(response, "choices", []) or [])
        if not choices:
            raise RuntimeError("synthetic probe returned no choices")
    except Exception as exc:  # provider SDK boundary
        return _map_aliyun_probe_error(exc, model=model_name, role=role)
    return _aliyun_probe_result(
        state="READY",
        model=model_name,
        role=role,
    )


def _probe_runtime_aliyun_embedding(config: AICopilotConfig) -> dict[str, Any]:
    model_name = config.embedding_model
    configured = bool(
        config.dashscope_api_key and config.dashscope_base_url and model_name
    )
    if not configured:
        return _aliyun_probe_result(
            state="NOT_CONFIGURED",
            reason_code="aliyun_embedding_not_configured",
            message="DashScope embedding connection is not fully configured.",
            model=model_name,
            role="embedding",
        )
    probe_config = replace(
        config,
        llm_timeout_seconds=min(max(float(config.llm_timeout_seconds), 1.0), 15.0),
        openai_max_retries=0,
    )
    try:
        response = _aliyun_openai_client(probe_config).embeddings.create(
            model=model_name,
            input="ReqPilotX synthetic connection probe.",
        )
        vectors = _response_embeddings(response)
        dimensions = len(vectors[0]) if vectors and vectors[0] else 0
        if dimensions <= 0:
            raise RuntimeError("synthetic probe returned no embedding")
    except Exception as exc:  # provider SDK boundary
        return _map_aliyun_probe_error(
            exc,
            model=model_name,
            role="embedding",
        )
    return _aliyun_probe_result(
        state="READY",
        model=model_name,
        role="embedding",
        dimensions=dimensions,
    )


def _probe_runtime_aliyun_ocr(config: AICopilotConfig) -> dict[str, Any]:
    """Run one bounded OCR request with a synthetic in-memory page only."""

    model_name = config.visual_text_model
    if not config.dashscope_api_key or not config.dashscope_base_url or not model_name:
        return _aliyun_probe_result(
            state="NOT_CONFIGURED",
            reason_code="aliyun_ocr_not_configured",
            message="DashScope OCR connection is not fully configured.",
            model=model_name,
            role="ocr",
            request_count=0,
        )
    timeout_seconds = min(max(float(config.llm_timeout_seconds), 1.0), 15.0)
    page_input = _synthetic_aliyun_ocr_probe_page()
    telemetry: list[VisualTextProviderTelemetryV1] = []
    response_facts: dict[str, object] = {}

    def capture_safe_response_facts(response: httpx.Response) -> None:
        response_facts["status_code"] = int(response.status_code)
        if response.status_code < 400:
            return
        try:
            response.read()
            payload = response.json()
        except Exception:
            return
        if not isinstance(payload, dict):
            return
        nested = payload.get("error") if isinstance(payload.get("error"), dict) else payload
        code = nested.get("code")
        if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", code):
            response_facts["code"] = code

    http_client = httpx.Client(
        follow_redirects=False,
        event_hooks={"response": [capture_safe_response_facts]},
    )
    started_at = time.monotonic()
    try:
        try:
            provider = BailianVisualTextProvider(
                api_key=config.dashscope_api_key,
                base_url=config.dashscope_base_url,
                model=model_name,
                enable_rotate=False,
                timeout_seconds=timeout_seconds,
                max_retries=0,
                http_client=http_client,
                telemetry_callback=telemetry.append,
            )
        except (TypeError, ValueError):
            return _aliyun_probe_result(
                state="NOT_CONFIGURED",
                reason_code="aliyun_ocr_configuration_invalid",
                message="The DashScope OCR connection configuration is invalid.",
                model=model_name,
                role="ocr",
                request_count=0,
                latency_ms=_ocr_probe_latency_ms(
                    telemetry,
                    started_at=started_at,
                ),
            )
        provider.recognize_page(page_input)
    except VisualTextProviderRejected as exc:
        latency_ms = _ocr_probe_latency_ms(telemetry, started_at=started_at)
        if int(response_facts.get("status_code") or 0) >= 400:
            safe_error = _AliyunOcrProbeHttpError(
                status_code=int(response_facts.get("status_code") or 0),
                code=str(response_facts.get("code") or ""),
            )
            result = _map_aliyun_probe_error(
                safe_error,
                model=model_name,
                role="ocr",
            )
            result["request_count"] = 1
            result["latency_ms"] = latency_ms
            return result
        return _map_aliyun_ocr_probe_error(
            exc,
            model=model_name,
            latency_ms=latency_ms,
        )
    except Exception:
        return _aliyun_probe_result(
            state="UNREACHABLE",
            reason_code="aliyun_ocr_probe_failed",
            message="The DashScope OCR connection probe failed.",
            model=model_name,
            role="ocr",
            request_count=1,
            latency_ms=_ocr_probe_latency_ms(telemetry, started_at=started_at),
        )
    else:
        return _aliyun_probe_result(
            state="READY",
            message="Qwen OCR 文字定位探测成功",
            model=model_name,
            role="ocr",
            request_count=1,
            latency_ms=_ocr_probe_latency_ms(telemetry, started_at=started_at),
        )
    finally:
        http_client.close()


class _AliyunOcrProbeHttpError(RuntimeError):
    """Carry only bounded status/code facts into the shared safe mapper."""

    def __init__(self, *, status_code: int, code: str) -> None:
        self.status_code = int(status_code)
        self.code = str(code or "")
        super().__init__(self.code or f"HTTP_{self.status_code}")


def _synthetic_aliyun_ocr_probe_page() -> VisualTextPageInput:
    source_bytes = b"ReqPilotX synthetic OCR connection probe v1"
    image = PILImage.new("L", (128, 64), color=255)
    # Deterministic high-contrast marks exercise the OCR image path without
    # embedding customer text, a filesystem path, a credential, or an endpoint.
    image.paste(0, (18, 14, 26, 50))
    image.paste(0, (26, 14, 48, 22))
    image.paste(0, (26, 42, 48, 50))
    image.paste(0, (76, 14, 84, 50))
    image.paste(0, (56, 14, 76, 22))
    image.paste(0, (56, 42, 76, 50))
    buffer = BytesIO()
    image.save(buffer, format="PNG", optimize=False)
    image_bytes = buffer.getvalue()
    return VisualTextPageInput(
        source_bytes=source_bytes,
        source_sha256=hashlib.sha256(source_bytes).hexdigest(),
        page_number=1,
        page_bbox=(0.0, 0.0, 128.0, 64.0),
        page_image_bytes=image_bytes,
        page_image_sha256=hashlib.sha256(image_bytes).hexdigest(),
        page_image_media_type="image/png",
        page_image_width_px=128,
        page_image_height_px=64,
    )


def _ocr_probe_latency_ms(
    events: list[VisualTextProviderTelemetryV1],
    *,
    started_at: float,
) -> int:
    if events:
        return max(0, int(events[-1].latency_ms))
    return max(0, round((time.monotonic() - started_at) * 1_000))


def _map_aliyun_ocr_probe_error(
    exc: VisualTextProviderRejected,
    *,
    model: str,
    latency_ms: int,
) -> dict[str, Any]:
    if exc.code in {
        VisualTextProviderRejectionCode.UNAVAILABLE,
        VisualTextProviderRejectionCode.AUTHENTICATION_FAILED,
        VisualTextProviderRejectionCode.ACCESS_DENIED,
    }:
        state = "AUTH_FAILED"
        reason_code = "aliyun_ocr_auth_failed"
        message = "DashScope rejected the configured OCR credentials."
    elif exc.code in {
        VisualTextProviderRejectionCode.QUOTA_EXHAUSTED,
    }:
        state = "DEPENDENCY_UNAVAILABLE"
        reason_code = "aliyun_quota_unavailable"
        message = "The DashScope OCR account has no usable quota."
    elif exc.code is VisualTextProviderRejectionCode.RATE_LIMITED:
        state = "UNREACHABLE"
        reason_code = "aliyun_ocr_rate_limited"
        message = "DashScope temporarily rate-limited the OCR probe."
    elif exc.code is VisualTextProviderRejectionCode.PROVIDER_OUTPUT_INVALID:
        state = "UNREACHABLE"
        reason_code = "aliyun_ocr_invalid_response"
        message = "DashScope OCR returned an invalid synthetic probe response."
    else:
        state = "UNREACHABLE"
        reason_code = "aliyun_ocr_unreachable"
        message = "The DashScope OCR probe could not reach a usable service."
    return _aliyun_probe_result(
        state=state,
        reason_code=reason_code,
        message=message,
        model=model,
        role="ocr",
        request_count=1,
        latency_ms=latency_ms,
    )


def _aliyun_probe_result(
    *,
    state: str,
    model: str,
    role: str,
    reason_code: str = "",
    message: str = "",
    dimensions: int | None = None,
    request_count: int | None = None,
    latency_ms: int | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "state": state,
        "ready": state == "READY",
        "reason_code": reason_code,
        "message": message,
        "model": str(model or ""),
        "role": role,
    }
    if dimensions is not None:
        result["dimensions"] = dimensions
    if request_count is not None:
        result["request_count"] = max(0, int(request_count))
    if latency_ms is not None:
        result["latency_ms"] = max(0, int(latency_ms))
    return result


def _map_aliyun_probe_error(
    exc: Exception,
    *,
    model: str,
    role: str,
) -> dict[str, Any]:
    status_code = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    if status_code is None and response is not None:
        status_code = getattr(response, "status_code", None)
    body = getattr(exc, "body", None)
    error_code = str(getattr(exc, "code", "") or "")
    if isinstance(body, dict):
        nested = body.get("error") if isinstance(body.get("error"), dict) else body
        error_code = str(nested.get("code") or error_code)
    fingerprint = f"{error_code} {type(exc).__name__} {exc}".lower()

    if (
        any(
            token in fingerprint
            for token in (
                "modelnotfound",
                "model_not_found",
                "model_not_exist",
                "modelaccessdenied",
                "model_not_deployed",
                "invalidparametermodel",
            )
        )
        or status_code == 404
    ):
        state = "MODEL_NOT_DEPLOYED"
        reason_code = "aliyun_model_not_deployed"
        message = "The configured DashScope model is unavailable to this account."
    elif any(
        token in fingerprint
        for token in (
            "arrearage",
            "allocationquota",
            "free_tier",
            "freetier",
            "quotaexhausted",
            "quota_exhausted",
            "insufficient_balance",
            "insufficientbalance",
            "billing",
        )
    ):
        state = "DEPENDENCY_UNAVAILABLE"
        reason_code = "aliyun_quota_unavailable"
        message = "The DashScope account has no usable quota or is billing-restricted."
    elif status_code in {401, 403} or any(
        token in fingerprint
        for token in (
            "authenticationerror",
            "invalidapikey",
            "invalid_api_key",
            "unauthorized",
            "permissiondenied",
        )
    ):
        state = "AUTH_FAILED"
        reason_code = "aliyun_auth_failed"
        message = "DashScope rejected the configured API key."
    elif status_code == 429 or "ratelimit" in fingerprint or "rate_limit" in fingerprint:
        state = "UNREACHABLE"
        reason_code = "aliyun_rate_limited"
        message = "DashScope temporarily rate-limited the connection probe."
    elif "timeout" in fingerprint:
        state = "UNREACHABLE"
        reason_code = "aliyun_request_timeout"
        message = "The DashScope connection probe timed out."
    else:
        state = "UNREACHABLE"
        reason_code = "aliyun_network_unreachable"
        message = "The DashScope connection probe could not reach a usable service."
    return _aliyun_probe_result(
        state=state,
        reason_code=reason_code,
        message=message,
        model=model,
        role=role,
    )


def _build_json_compatible_messages(messages: list[dict[str, str]], *, required_keys: list[str]) -> list[dict[str, str]]:
    if not messages:
        return messages
    cloned = [dict(item) for item in messages]
    required_keys_text = ", ".join(required_keys)
    suffix = (
        "\n\n请只返回一个 json 对象，不要返回 Markdown 代码块、解释文字或前后缀。"
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


def _coerce_stream_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
        return "".join(parts)
    return str(value)


def _usage_payload_from_openai_object(usage: Any) -> dict[str, Any] | None:
    if usage is None:
        return None
    return {
        "prompt_tokens": getattr(usage, "prompt_tokens", None),
        "completion_tokens": getattr(usage, "completion_tokens", None),
        "total_tokens": getattr(usage, "total_tokens", None),
    }


def _progress_callback_with_metadata(
    progress_callback: Callable[[dict[str, Any]], None] | None,
    *,
    stage: str,
    mode: str,
    model_name: str,
    provider: str,
) -> Callable[[dict[str, Any]], None] | None:
    if progress_callback is None:
        return None

    def _wrapped(payload: dict[str, Any]) -> None:
        enriched = dict(payload or {})
        enriched.setdefault("stage", stage)
        enriched.setdefault("mode", mode)
        enriched.setdefault("model_name", model_name)
        enriched.setdefault("provider", provider)
        progress_callback(enriched)

    return _wrapped


def _emit_progress_stage_update(
    progress_callback: Callable[[dict[str, Any]], None] | None,
    *,
    stage: str,
    mode: str,
    model_name: str,
    provider: str,
    message: str,
) -> None:
    if progress_callback is None:
        return
    progress_callback(
        {
            "type": "stage",
            "stage": stage,
            "mode": mode,
            "model_name": model_name,
            "provider": provider,
            "message": message,
        }
    )


def _parsed_openai_payload_to_dict(parsed: Any) -> dict[str, Any]:
    if hasattr(parsed, "model_dump"):
        return dict(parsed.model_dump())
    if isinstance(parsed, dict):
        return dict(parsed)
    raise RuntimeError("Azure OpenAI returned an unsupported structured payload type.")


def _azure_json_completion(
    config: AICopilotConfig,
    messages: list[dict[str, str]],
    *,
    model_name: str,
    response_format: type[BaseModel],
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    client = _azure_openai_client(config)
    payload: dict[str, Any] = {
        "model": model_name,
        "messages": messages,
        "response_format": response_format,
        "temperature": 0.0,
    }
    answer_content = ""
    refusal_content = ""
    if progress_callback:
        with client.chat.completions.stream(**payload) as stream:
            for event in stream:
                if getattr(event, "type", "") == "content.delta":
                    answer_piece = _coerce_stream_text(getattr(event, "delta", None))
                    if not answer_piece:
                        continue
                    answer_content += answer_piece
                    progress_callback(
                        {
                            "type": "answer",
                            "delta": answer_piece,
                            "content": answer_content,
                            "characters_emitted": len(answer_content),
                        }
                    )
                elif getattr(event, "type", "") == "refusal.delta":
                    refusal_content += _coerce_stream_text(getattr(event, "delta", None))
            completion = stream.get_final_completion()
        message = completion.choices[0].message
        if not answer_content:
            answer_content = _coerce_stream_text(getattr(message, "content", None))
        progress_callback(
            {
                "type": "complete",
                "finish_reason": str(getattr(completion.choices[0], "finish_reason", "") or ""),
                "usage": _usage_payload_from_openai_object(getattr(completion, "usage", None)) or {},
                "reasoning_characters": 0,
                "answer_characters": len(answer_content),
            }
        )
    else:
        completion = client.chat.completions.parse(**payload)
        message = completion.choices[0].message
    parsed = getattr(message, "parsed", None)
    if parsed is None:
        refusal = str(getattr(message, "refusal", "") or refusal_content).strip()
        raise RuntimeError(refusal or "Azure OpenAI returned no structured JSON payload.")
    return _parsed_openai_payload_to_dict(parsed)


def _stream_dashscope_json_completion(
    client: Any,
    payload: dict[str, Any],
    *,
    progress_callback: Callable[[dict[str, Any]], None] | None,
    include_response_format: bool,
) -> dict[str, Any]:
    stream_payload = dict(payload)
    stream_payload["stream"] = True
    stream_payload["stream_options"] = {"include_usage": True}
    if include_response_format:
        stream_payload["response_format"] = {"type": "json_object"}

    reasoning_content = ""
    answer_content = ""
    usage_payload: dict[str, Any] | None = None
    finish_reason = ""
    for chunk in client.chat.completions.create(**stream_payload):
        choices = list(getattr(chunk, "choices", []) or [])
        if not choices:
            usage_payload = _usage_payload_from_openai_object(getattr(chunk, "usage", None))
            continue
        choice = choices[0]
        delta = getattr(choice, "delta", None)
        if delta is None:
            continue
        reasoning_piece = _coerce_stream_text(getattr(delta, "reasoning_content", None))
        if reasoning_piece:
            reasoning_content += reasoning_piece
            if progress_callback:
                progress_callback(
                    {
                        "type": "reasoning",
                        "delta": reasoning_piece,
                        "content": reasoning_content,
                        "characters_emitted": len(reasoning_content),
                    }
                )
        answer_piece = _coerce_stream_text(getattr(delta, "content", None))
        if answer_piece:
            answer_content += answer_piece
            if progress_callback:
                progress_callback(
                    {
                        "type": "answer",
                        "delta": answer_piece,
                        "content": answer_content,
                        "characters_emitted": len(answer_content),
                    }
                )
        finish_reason = str(getattr(choice, "finish_reason", "") or finish_reason)
    if progress_callback:
        progress_callback(
            {
                "type": "complete",
                "finish_reason": finish_reason,
                "usage": usage_payload or {},
                "reasoning_characters": len(reasoning_content),
                "answer_characters": len(answer_content),
            }
        )
    return _extract_json_object(answer_content)


def _dashscope_json_completion(
    config: AICopilotConfig,
    messages: list[dict[str, str]],
    *,
    model_name: str | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    client = _aliyun_openai_client(config)
    payload: dict[str, Any] = {
        "model": model_name or config.chat_model,
        "messages": messages,
        "temperature": 0.1,
    }
    if progress_callback:
        try:
            return _stream_dashscope_json_completion(
                client,
                payload,
                progress_callback=progress_callback,
                include_response_format=True,
            )
        except Exception as exc:
            if not _should_retry_dashscope_without_response_format(exc):
                raise
            return _stream_dashscope_json_completion(
                client,
                payload,
                progress_callback=progress_callback,
                include_response_format=False,
            )
    try:
        completion = client.chat.completions.create(
            **payload,
            response_format={"type": "json_object"},
        )
    except Exception as exc:
        if not _should_retry_dashscope_without_response_format(exc):
            raise
        completion = client.chat.completions.create(**payload)
    message = completion.choices[0].message
    content = getattr(message, "content", "")
    if isinstance(content, list):
        content = "".join(str(item.get("text") or "") for item in content if isinstance(item, dict))
    return _extract_json_object(str(content or ""))


def _should_retry_dashscope_without_response_format(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    if status_code is None and response is not None:
        status_code = getattr(response, "status_code", None)
    detail = str(exc or "").lower()
    if status_code != 400:
        return False
    return any(
        token in detail
        for token in (
            "response_format",
            "json_object",
            "invalid_parameter",
            "messages must contain the word 'json'",
        )
    )


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


def _build_dashvector_filter(
    *,
    project_id: str,
    knowledge_docs: list[dict[str, Any]],
    as_of: date | None = None,
) -> str | None:
    eligible_docs = _eligible_knowledge_documents(knowledge_docs, as_of=as_of)
    filters: list[str] = []
    if project_id:
        filters.append(f"project_id = '{str(project_id).replace("'", "''")}'")
    doc_ids = [str(doc.get("id") or "").strip() for doc in eligible_docs]
    if doc_ids:
        quoted = ", ".join(f"'{item.replace("'", "''")}'" for item in doc_ids)
        filters.append(f"source_doc_id in ({quoted})")
        filters.append(f"status = '{ACTIVE_KNOWLEDGE_STATUS}'")
        filters.append(f"evidence_contract_version = '{EVIDENCE_INDEX_CONTRACT_VERSION}'")
        doc_types = sorted({str(doc.get("doc_type") or "").strip().upper() for doc in eligible_docs})
        quoted_doc_types = ", ".join(f"'{item.replace("'", "''")}'" for item in doc_types)
        filters.append(f"doc_type in ({quoted_doc_types})")
    else:
        filters.append(f"source_doc_id = '{NO_ELIGIBLE_EVIDENCE_ID}'")
    return " and ".join(filters) if filters else None


def _build_qdrant_filter(
    *,
    project_id: str,
    knowledge_docs: list[dict[str, Any]],
    as_of: date | None = None,
):
    from qdrant_client.http import models as qdrant_models

    eligible_docs = _eligible_knowledge_documents(knowledge_docs, as_of=as_of)
    must: list[Any] = []
    if project_id:
        must.append(
            qdrant_models.FieldCondition(
                key="project_id",
                match=qdrant_models.MatchValue(value=project_id),
            )
        )
    doc_ids = [str(doc.get("id") or "").strip() for doc in eligible_docs]
    if not doc_ids:
        doc_ids = [NO_ELIGIBLE_EVIDENCE_ID]
    doc_condition = _build_qdrant_multi_value_condition("source_doc_id", doc_ids)
    if doc_condition is not None:
        must.append(doc_condition)
    if eligible_docs:
        status_condition = _build_qdrant_multi_value_condition("status", [ACTIVE_KNOWLEDGE_STATUS])
        if status_condition is not None:
            must.append(status_condition)
        contract_condition = _build_qdrant_multi_value_condition(
            "evidence_contract_version",
            [EVIDENCE_INDEX_CONTRACT_VERSION],
        )
        if contract_condition is not None:
            must.append(contract_condition)
        doc_types = sorted({str(doc.get("doc_type") or "").strip().upper() for doc in eligible_docs})
        doc_type_condition = _build_qdrant_multi_value_condition("doc_type", doc_types)
        if doc_type_condition is not None:
            must.append(doc_type_condition)
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
        if not is_ascii_runtime_secret(api_key):
            raise RuntimeError(
                "Qdrant API key contains non-ASCII characters. Re-enter the raw key without Chinese punctuation or full-width characters."
            )
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
    try:
        client.create_collection(
            collection_name=config.qdrant_collection_name,
            vectors_config=qdrant_models.VectorParams(
                size=dimension,
                distance=qdrant_models.Distance.COSINE,
            ),
        )
    except Exception as exc:
        if not _is_qdrant_collection_exists_error(exc):
            raise
        collection_info = client.get_collection(collection_name=config.qdrant_collection_name)
        _validate_qdrant_collection_dimension(
            collection_info,
            collection_name=config.qdrant_collection_name,
            dimension=dimension,
        )
    _ensure_qdrant_payload_indexes(client, config)


def _ensure_qdrant_payload_indexes(client: Any, config: AICopilotConfig) -> None:
    """Create payload indexes for frequently filtered fields to speed up filtered search."""
    from qdrant_client.http import models as qdrant_models

    index_fields = {
        "project_id": qdrant_models.PayloadSchemaType.KEYWORD,
        "source_doc_id": qdrant_models.PayloadSchemaType.KEYWORD,
        "status": qdrant_models.PayloadSchemaType.KEYWORD,
        "content_kind": qdrant_models.PayloadSchemaType.KEYWORD,
    }
    for field_name, schema_type in index_fields.items():
        try:
            client.create_payload_index(
                collection_name=config.qdrant_collection_name,
                field_name=field_name,
                field_schema=schema_type,
            )
        except Exception:
            pass  # Index may already exist or Qdrant may not support it


def _is_qdrant_collection_exists_error(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    content = getattr(exc, "content", b"")
    if isinstance(content, bytes):
        content = content.decode("utf-8", errors="ignore")
    detail = f"{exc} {content}".lower()
    return status_code == 409 and "already exists" in detail


def _qdrant_point_id(raw_id: Any) -> str:
    normalized = str(raw_id or "").strip()
    if not normalized:
        return str(uuid.uuid4())
    try:
        return str(uuid.UUID(normalized))
    except (ValueError, AttributeError, TypeError):
        return str(uuid.uuid5(QDRANT_POINT_ID_NAMESPACE, normalized))


def _azure_search_credential(config: AICopilotConfig):
    from azure.core.credentials import AzureKeyCredential

    if config.uses_api_key_for_search:
        return AzureKeyCredential(config.azure_search_api_key)
    return _default_azure_credential(config.azure_allow_interactive_login)


def _ensure_azure_index_contract(config: AICopilotConfig) -> None:
    from azure.search.documents.indexes import SearchIndexClient

    index_client = SearchIndexClient(
        endpoint=config.azure_search_endpoint,
        credential=_azure_search_credential(config),
    )
    _ensure_azure_search_index(index_client, config)


def _azure_search_item_to_evidence(item: Any) -> dict[str, Any]:
    content = str(dict(item).get("content") or "")
    payload = _normalize_chunk_meta(dict(item), content=content)
    score = payload.get("@search.reranker_score")
    is_reranker_score = score is not None
    if score is None:
        score = payload.get("@search.score")
    score_source = ""
    if is_reranker_score:
        score_source = "azure_reranker"
    elif score is not None:
        score_source = "azure_search_score"
    return {
        "chunk_id": str(payload.get("chunk_id") or ""),
        "source_doc_id": str(payload.get("source_doc_id") or ""),
        "source_doc_name": str(payload.get("source_doc_name") or ""),
        "source_location": str(payload.get("source_location") or ""),
        "excerpt": content[:380],
        "relevance": round(_normalize_azure_relevance_score(score, is_reranker_score=is_reranker_score), 4),
        "score_source": score_source,
        "source_doc_version": str(payload.get("source_doc_version") or ""),
        "content_kind": str(payload.get("content_kind") or ""),
        **_evidence_contract_fields(payload, content=content),
        **_evidence_lifecycle_fields(payload),
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
        "evidence_id": str(meta.get("evidence_id") or ""),
        "content_hash": str(meta.get("content_hash") or ""),
        "evidence_contract_version": str(meta.get("evidence_contract_version") or ""),
        "source_doc_id": str(meta.get("source_doc_id") or ""),
        "source_doc_name": str(meta.get("source_doc_name") or ""),
        "source_doc_version": str(meta.get("source_doc_version") or ""),
        "doc_type": str(meta.get("doc_type") or ""),
        "status": str(meta.get("status") or ""),
        "effective_from": str(meta.get("effective_from") or ""),
        "effective_to": str(meta.get("effective_to") or ""),
        "authority": str(meta.get("authority") or "UNKNOWN"),
        "claim_role": str(meta.get("claim_role") or "UNKNOWN"),
        "applicability": str(meta.get("applicability") or "UNKNOWN"),
        "section_code": str(meta.get("section_code") or ""),
        "section_title": str(meta.get("section_title") or ""),
        "source_location": str(meta.get("source_location") or ""),
        "content_kind": str(meta.get("content_kind") or ""),
        "table_id": str(meta.get("table_id") or ""),
        "native_locator": str(meta.get("native_locator") or ""),
        "native_format": str(meta.get("native_format") or ""),
        "native_page": str(meta.get("native_page") or ""),
        "native_table_index": str(meta.get("native_table_index") or ""),
        "native_body_element_index": str(
            meta.get("native_body_element_index") or ""
        ),
        "native_workbook": str(meta.get("native_workbook") or ""),
        "native_sheet": str(meta.get("native_sheet") or ""),
        "native_range": str(meta.get("native_range") or ""),
        "slice_ai_projection": str(meta.get("slice_ai_projection") or ""),
    }


def _normalize_chunk_meta(meta: dict[str, Any], *, content: str) -> dict[str, Any]:
    normalized = dict(meta or {})
    content_kind = str(normalized.get("content_kind") or "").strip().lower()
    text = normalize_requirement_text(content).lower()
    source_name = normalize_requirement_text(str(normalized.get("source_doc_name") or "")).lower()
    section_title = normalize_requirement_text(str(normalized.get("section_title") or "")).lower()
    normalized["is_cover_like"] = _coerce_chunk_flag(
        normalized.get("is_cover_like"),
        bool(
            content_kind == "section_summary"
            and (
                "封面" in text
                or "cover" in text
                or "report no." in text
                or "test report" in text
                or "封面" in source_name
                or "cover" in source_name
            )
        ),
    )
    normalized["is_glossary_like"] = _coerce_chunk_flag(
        normalized.get("is_glossary_like"),
        bool("glossary" in text or "术语" in text or "definitions" in text or "terms and definitions" in text),
    )
    normalized["is_revision_history"] = _coerce_chunk_flag(
        normalized.get("is_revision_history"),
        bool("revision history" in text or "版本历史" in text or "change history" in text or "修订记录" in text),
    )
    normalized["is_signature_block"] = _coerce_chunk_flag(
        normalized.get("is_signature_block"),
        bool("signature" in text or "签字" in text or "approved by" in text or "审核" in text or "批准" in section_title),
    )
    normalized["is_legal_disclaimer"] = _coerce_chunk_flag(
        normalized.get("is_legal_disclaimer"),
        bool(
            "disclaimer" in text
            or "免责声明" in text
            or "all rights reserved" in text
            or "copyright" in text
            or "未经许可" in text
        ),
    )
    return normalized


def _coerce_chunk_flag(value: Any, inferred: bool) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    return inferred


def _chunk_meta_is_noise_like(meta: dict[str, Any]) -> bool:
    return any(
        bool(meta.get(key))
        for key in (
            "is_cover_like",
            "is_glossary_like",
            "is_revision_history",
            "is_signature_block",
            "is_legal_disclaimer",
        )
    )


def _load_knowledge_chunk_records(knowledge_doc: dict[str, Any]) -> list[dict[str, Any]]:
    payload = _knowledge_index_payload(knowledge_doc)
    if payload is not None:
        chunks = payload.get("chunks")
        if isinstance(chunks, list):
            return [item for item in chunks if isinstance(item, dict)]
    return build_knowledge_chunks(str(knowledge_doc.get("project_id") or ""), knowledge_doc)


def _knowledge_index_payload(
    knowledge_doc: dict[str, Any],
) -> dict[str, Any] | None:
    embedded = knowledge_doc.get("_index_payload")
    if isinstance(embedded, dict):
        return embedded
    document_id = str(knowledge_doc.get("id") or "").strip()
    if not document_id:
        return None
    payload_path = INDEX_PAYLOAD_DIR / f"{document_id}.json"
    try:
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _normalize_bm25_relevance_score(score: Any) -> float:
    try:
        numeric_score = float(score or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if numeric_score <= 0.0:
        return 0.0
    return min(numeric_score / (1.0 + math.fabs(numeric_score)), 1.0)


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


def _text_has_partial_compliance_signal(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(hint in lowered for hint in PARTIAL_COMPLIANCE_HINTS)


def _text_has_decision_proof_signal(text: str) -> bool:
    """Identify an outcome record, not merely a normative requirement."""

    value = normalize_requirement_text(str(text or ""))
    lowered = value.lower()
    return bool(
        re.search(
            r"\b(?:passed|failed|verified|validated|certified|approved|"
            r"measured|observed|demonstrated|recorded)\b",
            lowered,
        )
        or re.search(
            r"\b(?:test|validation|inspection|verification|measurement)"
            r"\s+(?:result|record|report|evidence)s?\b",
            lowered,
        )
        or re.search(
            r"(?:测试|试验|验证|检验|检测|测量)(?:结果|记录|报告|证明)"
            r"|(?:报告|记录|结果)(?:显示|表明|确认)"
            r"|(?:已|经)(?:完成)?(?:测试|试验|验证|检验|检测)"
            r"|(?:已|未)(?:通过|批准|认证)"
            r"|(?:实测|测得|验证通过|测试通过|试验通过|验证失败|测试失败|试验失败)",
            value,
        )
    )


def _evidence_has_decision_proof(item: dict[str, Any]) -> bool:
    authority = str(item.get("authority") or "UNKNOWN").strip().upper()
    if authority in {"", "UNKNOWN", "UNVERIFIED"}:
        return False
    claim_role = str(item.get("claim_role") or "UNKNOWN").strip().upper()
    applicability = str(item.get("applicability") or "UNKNOWN").strip().upper()
    if applicability in {"NOT_APPLICABLE", "OUT_OF_SCOPE"}:
        return False
    if claim_role in DECISION_PROOF_CLAIM_ROLES:
        return True
    return _text_has_decision_proof_signal(str(item.get("excerpt") or ""))


def _evidence_has_positive_outcome(item: dict[str, Any]) -> bool:
    if not _evidence_has_decision_proof(item):
        return False
    text = str(item.get("excerpt") or "")
    if _text_has_negative_signal(text):
        return False
    claim_role = str(item.get("claim_role") or "UNKNOWN").strip().upper()
    return (
        claim_role in DECISION_PROOF_CLAIM_ROLES
        or _text_has_positive_signal(text)
    )


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
        SimpleField(name="evidence_id", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="content_hash", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="evidence_contract_version", type=SearchFieldDataType.String, filterable=True),
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
        SimpleField(name="authority", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="claim_role", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="applicability", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="section_code", type=SearchFieldDataType.String, filterable=True),
        SearchableField(name="section_title", type=SearchFieldDataType.String),
        SimpleField(name="source_location", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="content_kind", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="table_id", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="native_locator", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="native_format", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="native_page", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="native_table_index", type=SearchFieldDataType.String, filterable=True),
        SimpleField(
            name="native_body_element_index",
            type=SearchFieldDataType.String,
            filterable=True,
        ),
        SimpleField(name="native_workbook", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="native_sheet", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="native_range", type=SearchFieldDataType.String, filterable=True),
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


def _batched_texts(texts: list[str], *, batch_size: int) -> list[list[str]]:
    return [texts[index : index + batch_size] for index in range(0, len(texts), batch_size)]


def _response_embeddings(response: Any) -> list[list[float]]:
    items = list(getattr(response, "data", []) or [])
    items.sort(key=lambda item: int(getattr(item, "index", 0)))
    return [list(getattr(item, "embedding", []) or []) for item in items]



# DashScope text-embedding-v4 maximum input length (tokens ≈ chars for CJK text).
# Use 8000 as a conservative buffer below the 8192 hard limit.
_DASHSCOPE_MAX_EMBED_CHARS = 8000


def _truncate_for_dashscope(text: str) -> str:
    """Truncate text to fit within DashScope embedding model's input limit."""
    if len(text) <= _DASHSCOPE_MAX_EMBED_CHARS:
        return text
    # Try to truncate at a sentence boundary first
    truncated = text[:_DASHSCOPE_MAX_EMBED_CHARS]
    for sep in ("。", ".\n", ". ", "\n"):
        idx = truncated.rfind(sep)
        if idx > _DASHSCOPE_MAX_EMBED_CHARS // 2:
            return truncated[: idx + len(sep)].strip()
    return truncated.strip()


def _embed_texts(texts: list[str], *, config: AICopilotConfig) -> list[list[float]]:
    normalized = [str(text or "") for text in texts]
    if not normalized:
        return []
    batch_size = max(int(config.embedding_batch_size or 1), 1)
    if config.provider == DEFAULT_ALIYUN_PROFILE_ID:
        if not config.dashscope_embedding_ready:
            raise RuntimeError("Aliyun DashScope embeddings are not configured.")
        client = _aliyun_openai_client(config)
        embeddings: list[list[float]] = []
        for batch in _batched_texts(normalized, batch_size=batch_size):
            # Truncate each text to stay within DashScope's 8192-token input limit.
            safe_batch = [_truncate_for_dashscope(t) for t in batch]
            # Filter out any empty strings (API rejects empty input); track positions.
            index_map: list[int] = []
            non_empty: list[str] = []
            for i, t in enumerate(safe_batch):
                if t.strip():
                    index_map.append(i)
                    non_empty.append(t)
            if not non_empty:
                # All texts in this batch are empty — return zero vectors.
                embeddings.extend([[]] * len(safe_batch))
                continue
            payload: dict[str, Any] = {
                "model": config.embedding_model,
                "input": non_empty if len(non_empty) > 1 else non_empty[0],
            }
            if config.dashscope_embedding_dimensions > 0:
                payload["dimensions"] = config.dashscope_embedding_dimensions
            response = client.embeddings.create(**payload)
            batch_vecs = _response_embeddings(response)
            # Re-insert zero vectors for originally empty slots.
            dim = len(batch_vecs[0]) if batch_vecs else 0
            result: list[list[float]] = [[0.0] * dim] * len(safe_batch)
            for vec_idx, orig_idx in enumerate(index_map):
                result[orig_idx] = batch_vecs[vec_idx] if vec_idx < len(batch_vecs) else [0.0] * dim
            embeddings.extend(result)
        return embeddings

    if not config.azure_openai_embedding_ready:
        raise RuntimeError("Azure OpenAI embeddings are not configured.")

    client = _azure_openai_client(config)
    embeddings = []
    for batch in _batched_texts(normalized, batch_size=batch_size):
        payload = {
            "model": config.azure_openai_embedding_deployment,
            "input": batch if len(batch) > 1 else batch[0],
        }
        if config.azure_openai_embedding_dimensions > 0:
            payload["dimensions"] = config.azure_openai_embedding_dimensions
        response = client.embeddings.create(**payload)
        embeddings.extend(_response_embeddings(response))
    return embeddings


def _embed_text(text: str, *, config: AICopilotConfig) -> list[float]:
    return _embed_texts([text], config=config)[0]


def _estimate_token_count(text: str) -> int:
    """Estimate token count without requiring tiktoken.

    Heuristic: CJK chars ≈ 1.5 tokens each, Latin words ≈ 1.3 tokens each,
    numbers/symbols ≈ 1 token each.
    """
    if not text:
        return 0
    tokens = 0
    cjk_count = 0
    latin_words = 0
    other = 0
    for ch in text:
        cp = ord(ch)
        if 0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF or 0xF900 <= cp <= 0xFAFF:
            cjk_count += 1
        elif ch.isalpha() and cp < 0x4E00:
            latin_words += 1
        elif ch.isspace():
            continue
        else:
            other += 1
    tokens = int(cjk_count * 1.5 + latin_words * 0.25 + other * 0.5)
    return max(1, tokens)


def _is_semantic_boundary(line: str) -> bool:
    """Check if a line is a semantic boundary (paragraph break or section header)."""
    stripped = line.strip()
    if not stripped:
        return True
    if re.match(r"^(?:[A-Z]:(?:\d+(?:\.\d+)*)?|\d+(?:\.\d+){0,5})\s+", stripped):
        return True
    if stripped.startswith("#"):
        return True
    return False


def _is_table_line(line: str) -> bool:
    """Check if a line is part of a markdown-style table."""
    stripped = line.strip()
    return bool(stripped.startswith("|") and stripped.count("|") >= 2)


def _find_table_end(lines: list[str], start: int) -> int:
    """Find the end index of a contiguous table block starting at `start`."""
    idx = start
    while idx < len(lines) and _is_table_line(lines[idx]):
        idx += 1
    return idx


_CHUNK_TARGET_TOKENS = int(os.environ.get("REQPILOT_CHUNK_TARGET_TOKENS", "768"))
_CHUNK_OVERLAP_TOKENS = int(os.environ.get("REQPILOT_CHUNK_OVERLAP_TOKENS", "75"))
_CHUNK_MIN_TOKENS = int(os.environ.get("REQPILOT_CHUNK_MIN_TOKENS", "50"))


def _chunk_lines(
    lines: list[str],
    *,
    target_tokens: int = _CHUNK_TARGET_TOKENS,
    overlap_tokens: int = _CHUNK_OVERLAP_TOKENS,
    min_tokens: int = _CHUNK_MIN_TOKENS,
    lines_per_chunk: int = 6,
    overlap: int = 2,
) -> list[list[str]]:
    """Token-aware chunking with semantic boundary preference.

    Falls back to legacy line-based chunking if token estimation produces
    no valid chunks (e.g., very short documents).
    """
    if not lines:
        return []

    line_tokens = [_estimate_token_count(line) for line in lines]
    total_tokens = sum(line_tokens)

    # For very short documents, use legacy line-based chunking
    if total_tokens <= target_tokens * 1.5:
        return _chunk_lines_legacy(lines, lines_per_chunk=lines_per_chunk, overlap=overlap)

    chunks: list[list[str]] = []
    start = 0
    while start < len(lines):
        # Accumulate lines up to target_tokens
        current_tokens = 0
        end = start
        for idx in range(start, len(lines)):
            next_tokens = current_tokens + line_tokens[idx]
            if next_tokens > target_tokens and end > start:
                break
            current_tokens = next_tokens
            end = idx + 1

        # Try to break at a semantic boundary within the last 20% of the chunk
        if end < len(lines):
            boundary_zone_start = max(start + 1, end - max(1, (end - start) // 5))
            for boundary_idx in range(end - 1, boundary_zone_start - 1, -1):
                if _is_semantic_boundary(lines[boundary_idx]):
                    end = boundary_idx
                    break

        # Never split in the middle of a table — extend to include full table
        if end < len(lines) and end > start:
            # Check if the line at `end` is a table line (meaning we cut mid-table)
            if _is_table_line(lines[end]):
                table_end = _find_table_end(lines, end)
                # Only extend if the table isn't too large (< 2x target)
                table_tokens = sum(line_tokens[i] for i in range(end, table_end))
                if table_tokens < target_tokens:
                    end = table_end
            # Also check if line just before `end` is a table line (we started a table near the end)
            elif end > 0 and _is_table_line(lines[end - 1]):
                table_end = _find_table_end(lines, end - 1)
                if table_end > end:
                    table_tokens = sum(line_tokens[i] for i in range(end, table_end))
                    if table_tokens < target_tokens:
                        end = table_end

        chunk = lines[start:end]
        if not chunk:
            break
        chunk_text = " ".join(chunk)
        if _estimate_token_count(chunk_text) >= min_tokens:
            chunks.append(chunk)

        # Advance with overlap
        step_tokens = 0
        overlap_start = end
        for idx in range(end - 1, start - 1, -1):
            step_tokens += line_tokens[idx]
            if step_tokens >= overlap_tokens:
                overlap_start = idx
                break
        if overlap_start >= end or overlap_start <= start:
            overlap_start = max(start + 1, end - max(1, (end - start) // 8))

        start = overlap_start
        if start >= len(lines):
            break

    # Fallback: if token-aware chunking produced nothing, use legacy
    if not chunks:
        return _chunk_lines_legacy(lines, lines_per_chunk=lines_per_chunk, overlap=overlap)
    return chunks


def _chunk_lines_legacy(lines: list[str], *, lines_per_chunk: int = 6, overlap: int = 2) -> list[list[str]]:
    """Legacy fixed-line chunking — kept as fallback."""
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


def _without_markdown_table_blocks(lines: list[str]) -> list[str]:
    """Keep table facts out of paragraph chunks so they have one identity."""

    output: list[str] = []
    current: list[str] = []
    for line in lines:
        if line.startswith("|") and line.count("|") >= 2:
            current.append(line)
            continue
        if current and len(current) < 2:
            output.extend(current)
        current = []
        output.append(line)
    if current and len(current) < 2:
        output.extend(current)
    return output


def _parse_source_locator_metadata(table_lines: list[str]) -> dict[str, str]:
    if not table_lines:
        return {}
    match = re.search(r"\[source_locator\s+(.+?)\]", table_lines[0])
    if not match:
        return {}
    locator_text = f"[source_locator {match.group(1)}]"
    fields: dict[str, str] = {"native_locator": locator_text}
    for key, raw_value in re.findall(
        r"([A-Za-z_]+)=(\"(?:\\.|[^\"])*\"|[^\s\]]+)",
        match.group(1),
    ):
        value = raw_value
        if raw_value.startswith('"') and raw_value.endswith('"'):
            try:
                value = str(json.loads(raw_value))
            except json.JSONDecodeError:
                value = raw_value[1:-1]
        fields[key] = value.replace(r"\|", "|")
    native_format = fields.pop("format", "")
    if native_format:
        fields["native_format"] = native_format
    for key in (
        "page",
        "table_index",
        "body_element_index",
        "workbook",
        "sheet",
        "range",
    ):
        if key in fields:
            fields[f"native_{key}"] = fields.pop(key)
    return fields


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
    table_id: str = "",
    native_locator_metadata: dict[str, str] | None = None,
) -> dict[str, Any]:
    evidence_id = str(chunk_id or "").strip()
    if not evidence_id:
        raise ValueError("Knowledge chunk evidence_id is required.")
    content_hash = compute_evidence_content_hash(content)
    noise_meta = _normalize_chunk_meta(
        {
            "source_doc_name": str(knowledge_doc.get("file_name") or ""),
            "section_title": str(section.get("section_title") or ""),
            "content_kind": content_kind,
        },
        content=content,
    )
    locator_metadata = dict(native_locator_metadata or {})
    meta = {
        "project_id": project_id,
        "evidence_id": evidence_id,
        "content_hash": content_hash,
        "evidence_contract_version": EVIDENCE_INDEX_CONTRACT_VERSION,
        "source_doc_id": str(knowledge_doc.get("id") or ""),
        "source_doc_name": str(knowledge_doc.get("file_name") or ""),
        "source_doc_version": str(knowledge_doc.get("doc_version") or ""),
        "doc_type": str(knowledge_doc.get("doc_type") or "REFERENCE"),
        "status": str(knowledge_doc.get("status") or "ACTIVE"),
        "effective_from": str(knowledge_doc.get("effective_from") or ""),
        "effective_to": str(knowledge_doc.get("effective_to") or ""),
        "authority": str(knowledge_doc.get("authority") or "UNKNOWN").strip().upper(),
        "claim_role": str(knowledge_doc.get("claim_role") or "UNKNOWN").strip().upper(),
        "applicability": str(knowledge_doc.get("applicability") or "UNKNOWN").strip(),
        "section_code": str(section.get("section_code") or ""),
        "section_title": str(section.get("section_title") or ""),
        "source_location": source_location,
        "content_kind": content_kind,
        "table_id": str(table_id or locator_metadata.get("table_id") or ""),
        "native_locator": str(locator_metadata.get("native_locator") or ""),
        "native_format": str(locator_metadata.get("native_format") or ""),
        "native_page": str(locator_metadata.get("native_page") or ""),
        "native_table_index": str(
            locator_metadata.get("native_table_index") or ""
        ),
        "native_body_element_index": str(
            locator_metadata.get("native_body_element_index") or ""
        ),
        "native_workbook": str(locator_metadata.get("native_workbook") or ""),
        "native_sheet": str(locator_metadata.get("native_sheet") or ""),
        "native_range": str(locator_metadata.get("native_range") or ""),
        "slice_ai_projection": slice_ai_projection,
        "is_cover_like": bool(noise_meta.get("is_cover_like")),
        "is_glossary_like": bool(noise_meta.get("is_glossary_like")),
        "is_revision_history": bool(noise_meta.get("is_revision_history")),
        "is_signature_block": bool(noise_meta.get("is_signature_block")),
        "is_legal_disclaimer": bool(noise_meta.get("is_legal_disclaimer")),
    }
    return {
        "chunk_id": evidence_id,
        "evidence_id": evidence_id,
        "content_hash": content_hash,
        "evidence_contract_version": EVIDENCE_INDEX_CONTRACT_VERSION,
        "content": content,
        "content_kind": content_kind,
        "meta": meta,
    }


def _now_iso_utc() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()

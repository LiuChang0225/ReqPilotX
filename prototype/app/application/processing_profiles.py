from __future__ import annotations

import importlib.util
import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib import error as urllib_error
from urllib import request as urllib_request

from pydantic import ValidationError

from app import db
from app.application.errors import ApplicationError
from app.domain.extraction.processing_profile import (
    CapabilityState,
    DocumentProcessingProfileV1,
    ProjectProcessingProfileUpdateV1,
    ProjectProcessingProfileV1,
    ResolvedProcessingPlanV1,
    ResolvedProviderBindingsV1,
    RunProfileOverrideV1,
    RuntimeCapabilityV1,
    build_codex_agent_profile,
    build_local_rules_profile,
    build_project_processing_profile,
    build_resolved_processing_plan,
    provider_bindings_require_connection,
)
from app.runtime_readiness import build_ai_profile_check
from app.services.ai_copilot import (
    DEFAULT_ALIYUN_PROFILE_ID,
    DEFAULT_AZURE_PROFILE_ID,
    STORAGE_DIR,
    get_runtime_ai_config_value,
    get_runtime_connection_revision,
    load_ai_copilot_config,
    probe_runtime_aliyun_models,
    probe_runtime_azure_rbac,
)


CAPABILITY_LOCAL_PYMUPDF = "document.pymupdf"
CAPABILITY_CODEX_AGENT = "codex.agent"
CAPABILITY_AZURE_DI = "document.azure_document_intelligence"
CAPABILITY_BAILIAN_OCR = "document.bailian_ocr"
CAPABILITY_BAILIAN_LAYOUT = "document.bailian_layout"
CAPABILITY_AZURE_SOL = "azure_openai.gpt56_sol"
CAPABILITY_AZURE_TERRA = "azure_openai.gpt56_terra"
CAPABILITY_ALIYUN_LLM = "aliyun.llm"
CAPABILITY_BAILIAN_VISION = "aliyun.vision"
CAPABILITY_AZURE_EMBEDDING = "azure_openai.embedding"
CAPABILITY_AZURE_SEARCH = "azure_ai_search"
CAPABILITY_ALIYUN_EMBEDDING = "aliyun.embedding"
CAPABILITY_QDRANT = "qdrant"

_CAPABILITY_CACHE_TTL_SECONDS = 60.0
_CAPABILITY_CACHE_LOCK = threading.RLock()
_CAPABILITY_CACHE: dict[str, Any] = {
    "connection_revision": -1,
    "checked_at_monotonic": 0.0,
    "probe": {},
    "aliyun_probe_connection_revision": -1,
    "aliyun_probe": {},
    "aliyun_probe_receipt": None,
    "failures": {},
}


@dataclass(frozen=True, slots=True)
class _ProbeResult:
    state: CapabilityState
    reason_code: str = ""
    message: str = ""


def get_project_processing_profile(
    project_id: str,
) -> ProjectProcessingProfileV1:
    project = db.get_project(project_id)
    if project is None:
        raise ApplicationError(status_code=404, detail="Project not found.")
    payload_text = str(project.get("processing_profile_json") or "").strip()
    stored_revision = int(project.get("processing_profile_revision") or 0)
    stored_sha256 = str(project.get("processing_profile_sha256") or "").strip()
    if not payload_text:
        if stored_revision != 0 or stored_sha256:
            raise _processing_profile_corrupt(
                "The project processing profile metadata is incomplete."
            )
        from app.application.runtime import RUNTIME_STATE

        container = RUNTIME_STATE.container
        backend = str(
            getattr(
                getattr(
                    getattr(container, "settings", None), "agent_runtime_backend", None
                ),
                "value",
                "",
            )
        )
        return (
            build_codex_agent_profile(project_id=project_id)
            if backend == "codex-app-server"
            else build_local_rules_profile(project_id=project_id)
        )
    try:
        profile = ProjectProcessingProfileV1.model_validate_json(payload_text)
    except (ValidationError, ValueError) as exc:
        raise _processing_profile_corrupt(
            "The stored project processing profile is invalid."
        ) from exc
    if (
        profile.project_id != project_id
        or profile.revision != stored_revision
        or profile.canonical_sha256 != stored_sha256
    ):
        raise _processing_profile_corrupt(
            "The stored project processing profile identity does not match its row."
        )
    return profile


def get_project_processing_profile_route(
    project_id: str,
) -> dict[str, Any]:
    profile = get_project_processing_profile(project_id)
    return {
        "profile": profile.model_dump(mode="json"),
        "capabilities": [
            item.model_dump(mode="json")
            for item in build_runtime_capabilities(probe=False)
        ],
    }


def update_project_processing_profile_route(
    project_id: str,
    payload: ProjectProcessingProfileUpdateV1,
) -> dict[str, Any]:
    current = get_project_processing_profile(project_id)
    if payload.expected_revision != current.revision:
        raise ApplicationError(
            status_code=409,
            code="stale_processing_profile_revision",
            detail={
                "reason_code": "stale_processing_profile_revision",
                "message": "Project processing profile revision is stale.",
                "expected_revision": payload.expected_revision,
                "current_revision": current.revision,
            },
        )
    knowledge_changed = payload.knowledge_backend != current.knowledge_backend
    if knowledge_changed and not payload.reindex_knowledge:
        raise ApplicationError(
            status_code=409,
            code="knowledge_reindex_required",
            detail={
                "reason_code": "knowledge_reindex_required",
                "message": (
                    "Changing the project knowledge backend requires "
                    "reindex_knowledge=true."
                ),
                "blocked_by": ["reindex_knowledge"],
                "settings_path": f"/projects/{project_id}/settings",
            },
        )
    proposed = build_project_processing_profile(
        project_id=project_id,
        document=payload.document,
        llm_provider=payload.llm_provider,
        knowledge_backend=payload.knowledge_backend,
        revision=current.revision + 1,
    )
    require_capabilities(
        required_capability_ids_for_profile(proposed),
        operation="processing_profile.update",
    )
    updated = db.update_project_processing_profile(
        project_id=project_id,
        expected_revision=current.revision,
        profile_json=json.dumps(
            proposed.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        profile_sha256=proposed.canonical_sha256,
    )
    if updated is None:
        latest = get_project_processing_profile(project_id)
        raise ApplicationError(
            status_code=409,
            code="stale_processing_profile_revision",
            detail={
                "reason_code": "stale_processing_profile_revision",
                "message": "Project processing profile changed concurrently.",
                "current_revision": latest.revision,
            },
        )
    reindex_job = None
    if knowledge_changed and payload.reindex_knowledge:
        from app.application.knowledge import (
            reindex_project_knowledge_for_selected_backend,
        )

        reindex_job = reindex_project_knowledge_for_selected_backend(
            project_id=project_id,
            knowledge_backend=proposed.knowledge_backend,
        )
    return {
        "profile": proposed.model_dump(mode="json"),
        "reindex_job": reindex_job,
    }


def resolve_processing_plan(
    *,
    project_id: str,
    document_id: str,
    run_override: RunProfileOverrideV1 | None = None,
    llm_override: str | None = None,
    skip_document_capability_gate: bool = False,
) -> ResolvedProcessingPlanV1:
    document_row = db.get_sor_document(document_id)
    if document_row is None or str(document_row.get("project_id") or "") != project_id:
        raise ApplicationError(status_code=404, detail="Source document not found.")
    source_sha256 = str(document_row.get("source_sha256") or "").strip().lower()
    if len(source_sha256) != 64:
        raise ApplicationError(
            status_code=409,
            code="source_identity_not_registered",
            detail={
                "reason_code": "source_identity_not_registered",
                "message": (
                    "The source document must pass local byte preflight before "
                    "a processing plan can be frozen."
                ),
                "blocked_by": ["source_sha256"],
            },
        )
    profile = get_project_processing_profile(project_id)
    override = run_override or RunProfileOverrideV1()
    selected_document = override.document or profile.document
    if override.force_visual_pages and selected_document.complex_vision is None:
        raise ApplicationError(
            status_code=409,
            code="forced_visual_provider_not_selected",
            detail={
                "reason_code": "forced_visual_provider_not_selected",
                "message": (
                    "Select a ready complex_vision provider before forcing "
                    "individual visual pages."
                ),
                "blocked_by": ["document.complex_vision"],
                "settings_path": f"/projects/{project_id}/settings",
            },
        )
    selected_llm = str(llm_override or override.llm_provider or profile.llm_provider)
    if selected_llm not in {"none", "codex", "aliyun", "azure_openai"}:
        raise ApplicationError(
            status_code=422,
            detail="Unsupported LLM provider override.",
        )
    required = (
        required_llm_capability_ids(selected_llm)
        if skip_document_capability_gate
        else required_capability_ids(
            document=selected_document,
            llm_provider=selected_llm,
            knowledge_backend="none",
        )
    )
    require_capabilities(required, operation="extraction.start")
    provider_bindings = _resolve_provider_bindings(
        document=selected_document,
        llm_provider=selected_llm,
    )
    connection_revision = (
        get_runtime_connection_revision()
        if provider_bindings_require_connection(provider_bindings)
        else 0
    )
    return build_resolved_processing_plan(
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
        profile=profile,
        document=selected_document,
        llm_provider=selected_llm,  # type: ignore[arg-type]
        provider_bindings=provider_bindings,
        connection_revision=connection_revision,
        force_visual_pages=override.force_visual_pages,
    )


def get_runtime_capabilities_route() -> dict[str, Any]:
    capabilities = build_runtime_capabilities(probe=True)
    return {
        "schema_version": "runtime-capability-catalog/v1",
        "connection_revision": get_runtime_connection_revision(),
        "capabilities": [
            {
                **item.model_dump(mode="json"),
                "ready": item.ready,
            }
            for item in capabilities
        ],
    }


def build_runtime_capabilities(
    *,
    probe: bool,
) -> tuple[RuntimeCapabilityV1, ...]:
    connection_revision = get_runtime_connection_revision()
    probe_state = _get_probe_state(
        connection_revision=connection_revision,
        probe=probe,
    )
    failures = _capability_failures(connection_revision)
    azure_config = load_ai_copilot_config(DEFAULT_AZURE_PROFILE_ID)
    aliyun_config = load_ai_copilot_config(DEFAULT_ALIYUN_PROFILE_ID)

    local_pymupdf = (
        _ProbeResult(CapabilityState.READY)
        if importlib.util.find_spec("fitz") is not None
        else _ProbeResult(
            CapabilityState.DEPENDENCY_UNAVAILABLE,
            "pymupdf_dependency_unavailable",
            "PyMuPDF is not installed in the current runtime.",
        )
    )
    codex_agent = _codex_runtime_capability()

    azure_di_endpoint = get_runtime_ai_config_value(
        "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT"
    )
    azure_di_key = get_runtime_ai_config_value("AZURE_DOCUMENT_INTELLIGENCE_API_KEY")
    azure_di = _configured_or_probe(
        configured=bool(azure_di_endpoint and azure_di_key),
        missing=tuple(
            name
            for name, value in (
                ("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT", azure_di_endpoint),
                ("AZURE_DOCUMENT_INTELLIGENCE_API_KEY", azure_di_key),
            )
            if not value
        ),
        probed=probe_state.get("azure_di"),
    )

    azure_endpoint = get_runtime_ai_config_value("AZURE_OPENAI_ENDPOINT")
    sol_deployment = get_runtime_ai_config_value(
        "AZURE_OPENAI_SOL_DEPLOYMENT",
        get_runtime_ai_config_value("AZURE_OPENAI_CHAT_DEPLOYMENT"),
    )
    terra_deployment = get_runtime_ai_config_value(
        "AZURE_OPENAI_TERRA_DEPLOYMENT",
        get_runtime_ai_config_value("AZURE_OPENAI_CHAT_DEPLOYMENT"),
    )
    azure_auth = probe_state.get("azure_openai_auth")
    azure_sol = _azure_model_capability(
        endpoint=azure_endpoint,
        deployment=sol_deployment,
        deployment_setting="AZURE_OPENAI_SOL_DEPLOYMENT",
        auth_probe=azure_auth,
    )
    azure_terra = _azure_model_capability(
        endpoint=azure_endpoint,
        deployment=terra_deployment,
        deployment_setting="AZURE_OPENAI_TERRA_DEPLOYMENT",
        auth_probe=azure_auth,
    )
    azure_embedding = _azure_model_capability(
        endpoint=azure_endpoint,
        deployment=azure_config.azure_openai_embedding_deployment,
        deployment_setting="AZURE_OPENAI_EMBEDDING_DEPLOYMENT",
        auth_probe=azure_auth,
    )
    azure_search = _configured_or_probe(
        configured=bool(
            azure_config.azure_search_endpoint and azure_config.azure_search_index_name
        ),
        missing=tuple(
            name
            for name, value in (
                ("AZURE_AI_SEARCH_ENDPOINT", azure_config.azure_search_endpoint),
                ("AZURE_AI_SEARCH_INDEX_NAME", azure_config.azure_search_index_name),
            )
            if not value
        ),
        probed=probe_state.get("azure_search_auth"),
    )

    assessment_probe = probe_state.get("aliyun_assessment")
    screening_probe = probe_state.get("aliyun_screening")
    ocr_probe = probe_state.get("aliyun_ocr")
    aliyun_llm = _configured_or_probe(
        configured=bool(
            aliyun_config.dashscope_api_key
            and aliyun_config.dashscope_base_url
            and aliyun_config.chat_model
            and aliyun_config.screening_model
        ),
        missing=tuple(
            name
            for name, value in (
                ("REQPILOT_ALIYUN_API_KEY", aliyun_config.dashscope_api_key),
                ("REQPILOT_DASHSCOPE_BASE_URL", aliyun_config.dashscope_base_url),
                ("REQPILOT_ALIYUN_CHAT_MODEL", aliyun_config.chat_model),
                (
                    "REQPILOT_ALIYUN_SCREENING_CHAT_MODEL",
                    aliyun_config.screening_model,
                ),
            )
            if not value
        ),
        probed=_combine_probe_results(assessment_probe, screening_probe),
    )
    bailian_ocr = _configured_or_probe(
        configured=bool(
            aliyun_config.dashscope_api_key and aliyun_config.dashscope_base_url
        ),
        missing=tuple(
            name
            for name, value in (
                ("REQPILOT_ALIYUN_API_KEY", aliyun_config.dashscope_api_key),
                ("REQPILOT_DASHSCOPE_BASE_URL", aliyun_config.dashscope_base_url),
            )
            if not value
        ),
        probed=ocr_probe,
    )
    bailian_layout = _configured_or_probe(
        configured=bool(
            aliyun_config.dashscope_api_key
            and aliyun_config.dashscope_base_url
            and aliyun_config.screening_model
        ),
        missing=tuple(
            name
            for name, value in (
                ("REQPILOT_ALIYUN_API_KEY", aliyun_config.dashscope_api_key),
                ("REQPILOT_DASHSCOPE_BASE_URL", aliyun_config.dashscope_base_url),
                (
                    "REQPILOT_ALIYUN_SCREENING_CHAT_MODEL",
                    aliyun_config.screening_model,
                ),
            )
            if not value
        ),
        probed=screening_probe,
    )
    bailian_vision = _configured_or_probe(
        configured=bool(
            aliyun_config.dashscope_api_key
            and aliyun_config.dashscope_base_url
            and aliyun_config.chat_model
        ),
        missing=tuple(
            name
            for name, value in (
                ("REQPILOT_ALIYUN_API_KEY", aliyun_config.dashscope_api_key),
                ("REQPILOT_DASHSCOPE_BASE_URL", aliyun_config.dashscope_base_url),
                ("REQPILOT_ALIYUN_CHAT_MODEL", aliyun_config.chat_model),
            )
            if not value
        ),
        probed=assessment_probe,
    )
    aliyun_embedding = _configured_or_probe(
        configured=bool(
            aliyun_config.dashscope_api_key
            and aliyun_config.dashscope_base_url
            and aliyun_config.embedding_model
        ),
        missing=tuple(
            name
            for name, value in (
                ("REQPILOT_ALIYUN_API_KEY", aliyun_config.dashscope_api_key),
                ("REQPILOT_DASHSCOPE_BASE_URL", aliyun_config.dashscope_base_url),
                (
                    "REQPILOT_ALIYUN_EMBEDDING_MODEL",
                    aliyun_config.embedding_model,
                ),
            )
            if not value
        ),
        probed=probe_state.get("aliyun_embedding"),
    )
    qdrant = _configured_or_probe(
        configured=bool(
            aliyun_config.retrieval_backend == "qdrant"
            and aliyun_config.qdrant_url
            and aliyun_config.qdrant_collection_name
        ),
        missing=tuple(
            name
            for name, value in (
                (
                    "REQPILOT_ALIYUN_RETRIEVAL_BACKEND=qdrant",
                    aliyun_config.retrieval_backend == "qdrant",
                ),
                ("REQPILOT_ALIYUN_QDRANT_URL", aliyun_config.qdrant_url),
                (
                    "REQPILOT_ALIYUN_QDRANT_COLLECTION",
                    aliyun_config.qdrant_collection_name,
                ),
            )
            if not value
        ),
        probed=probe_state.get("qdrant"),
    )

    rows = (
        _capability(
            CAPABILITY_LOCAL_PYMUPDF,
            "document_parser",
            "local",
            "PyMuPDF",
            local_pymupdf,
            connection_revision,
        ),
        _capability(
            CAPABILITY_CODEX_AGENT,
            "llm",
            "codex",
            "Codex Agent",
            codex_agent,
            connection_revision,
        ),
        _capability(
            CAPABILITY_AZURE_DI,
            "document_enhancement",
            "azure",
            "Azure Document Intelligence",
            azure_di,
            connection_revision,
        ),
        _capability(
            CAPABILITY_BAILIAN_OCR,
            "document_enhancement",
            "aliyun",
            "Qwen OCR 结构恢复",
            bailian_ocr,
            connection_revision,
        ),
        _capability(
            CAPABILITY_BAILIAN_LAYOUT,
            "document_enhancement",
            "aliyun",
            "百炼 Layout",
            bailian_layout,
            connection_revision,
        ),
        _capability(
            CAPABILITY_AZURE_SOL,
            "llm",
            "azure",
            "Azure OpenAI GPT-5.6 Sol",
            azure_sol,
            connection_revision,
        ),
        _capability(
            CAPABILITY_AZURE_TERRA,
            "llm",
            "azure",
            "Azure OpenAI GPT-5.6 Terra",
            azure_terra,
            connection_revision,
        ),
        _capability(
            CAPABILITY_ALIYUN_LLM,
            "llm",
            "aliyun",
            "阿里云 LLM",
            aliyun_llm,
            connection_revision,
        ),
        _capability(
            CAPABILITY_BAILIAN_VISION,
            "complex_vision",
            "aliyun",
            "百炼视觉模型",
            bailian_vision,
            connection_revision,
        ),
        _capability(
            CAPABILITY_AZURE_EMBEDDING,
            "embedding",
            "azure",
            "Azure OpenAI Embedding",
            azure_embedding,
            connection_revision,
        ),
        _capability(
            CAPABILITY_AZURE_SEARCH,
            "vector_store",
            "azure",
            "Azure AI Search",
            azure_search,
            connection_revision,
        ),
        _capability(
            CAPABILITY_ALIYUN_EMBEDDING,
            "embedding",
            "aliyun",
            "DashScope Embedding",
            aliyun_embedding,
            connection_revision,
        ),
        _capability(
            CAPABILITY_QDRANT,
            "vector_store",
            "local",
            "Qdrant",
            qdrant,
            connection_revision,
        ),
    )
    return tuple(_apply_failure(item, failures) for item in rows)


def invalidate_runtime_capabilities() -> None:
    with _CAPABILITY_CACHE_LOCK:
        _CAPABILITY_CACHE["connection_revision"] = -1
        _CAPABILITY_CACHE["checked_at_monotonic"] = 0.0
        _CAPABILITY_CACHE["probe"] = {}
        _CAPABILITY_CACHE["aliyun_probe_connection_revision"] = -1
        _CAPABILITY_CACHE["aliyun_probe"] = {}
        _CAPABILITY_CACHE["aliyun_probe_receipt"] = None
        _CAPABILITY_CACHE["failures"] = {}


def probe_runtime_aliyun_connection() -> dict[str, Any]:
    """Start/probe local Qdrant and live-test every Aliyun runtime component."""

    connection_revision = get_runtime_connection_revision()
    config = load_ai_copilot_config(DEFAULT_ALIYUN_PROFILE_ID)
    try:
        from app.infrastructure.local_qdrant import (
            ensure_local_qdrant_running,
        )
    except ImportError:
        local_qdrant_error: Exception | None = RuntimeError(
            "local Qdrant lifecycle support is unavailable"
        )
    else:
        try:
            ensure_local_qdrant_running(
                storage_dir=STORAGE_DIR,
                url=config.qdrant_url,
            )
            local_qdrant_error = None
        except Exception as exc:  # local process/provider boundary
            local_qdrant_error = exc

    raw_model_checks = probe_runtime_aliyun_models(config)
    probe_results = {
        "aliyun_screening": _probe_result_from_payload(
            raw_model_checks.get("screening")
        ),
        "aliyun_assessment": _probe_result_from_payload(
            raw_model_checks.get("assessment")
        ),
        "aliyun_ocr": _probe_result_from_payload(raw_model_checks.get("ocr")),
        "aliyun_embedding": _probe_result_from_payload(
            raw_model_checks.get("embedding")
        ),
    }

    if config.retrieval_backend != "qdrant" or not config.qdrant_url:
        qdrant_result = _ProbeResult(
            CapabilityState.NOT_CONFIGURED,
            "runtime_not_configured",
            "Local Qdrant is not configured for the Aliyun profile.",
        )
    elif local_qdrant_error is not None:
        qdrant_result = _ProbeResult(
            CapabilityState.DEPENDENCY_UNAVAILABLE,
            "qdrant_start_failed",
            "Local Qdrant could not be started automatically.",
        )
    else:
        qdrant_check = build_ai_profile_check(config).get("qdrant_probe") or {}
        if str(qdrant_check.get("status") or "") == "ok":
            qdrant_result = _ProbeResult(CapabilityState.READY)
        else:
            qdrant_result = _ProbeResult(
                CapabilityState.UNREACHABLE,
                "qdrant_unreachable",
                "The configured Qdrant service did not pass its readiness probe.",
            )
    probe_results["qdrant"] = qdrant_result

    checks = {
        "screening": _public_probe_check(
            raw_model_checks.get("screening"),
            fallback=probe_results["aliyun_screening"],
        ),
        "assessment": _public_probe_check(
            raw_model_checks.get("assessment"),
            fallback=probe_results["aliyun_assessment"],
        ),
        "ocr": _public_probe_check(
            raw_model_checks.get("ocr"),
            fallback=probe_results["aliyun_ocr"],
        ),
        "embedding": _public_probe_check(
            raw_model_checks.get("embedding"),
            fallback=probe_results["aliyun_embedding"],
        ),
        "qdrant": _public_probe_result(qdrant_result, role="vector_store"),
    }
    receipt = {
        "schema_version": "runtime-aliyun-connection-probe/v2",
        "provider": "aliyun",
        "connection_revision": connection_revision,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "ready": all(bool(item.get("ready")) for item in checks.values()),
        "checks": checks,
    }
    now_monotonic = time.monotonic()
    with _CAPABILITY_CACHE_LOCK:
        if _CAPABILITY_CACHE.get("connection_revision") != connection_revision:
            _CAPABILITY_CACHE["failures"] = {}
        _CAPABILITY_CACHE["connection_revision"] = connection_revision
        _CAPABILITY_CACHE["checked_at_monotonic"] = now_monotonic
        current_probe = dict(_CAPABILITY_CACHE.get("probe") or {})
        current_probe.update(probe_results)
        _CAPABILITY_CACHE["probe"] = current_probe
        _CAPABILITY_CACHE["aliyun_probe_connection_revision"] = connection_revision
        _CAPABILITY_CACHE["aliyun_probe"] = dict(probe_results)
        _CAPABILITY_CACHE["aliyun_probe_receipt"] = json.loads(json.dumps(receipt))

    capabilities = build_runtime_capabilities(probe=False)
    return {
        **receipt,
        "capabilities": [
            {**item.model_dump(mode="json"), "ready": item.ready}
            for item in capabilities
        ],
    }


def get_runtime_aliyun_connection_probe_receipt() -> dict[str, Any] | None:
    """Return the last explicit Aliyun probe only for the active config revision."""

    connection_revision = get_runtime_connection_revision()
    with _CAPABILITY_CACHE_LOCK:
        receipt = _CAPABILITY_CACHE.get("aliyun_probe_receipt")
        if not isinstance(receipt, dict):
            return None
        if int(receipt.get("connection_revision") or -1) != connection_revision:
            return None
        return json.loads(json.dumps(receipt))


def record_runtime_capability_failure(
    *,
    capability_id: str,
    state: CapabilityState,
    reason_code: str,
    message: str,
) -> None:
    if state is CapabilityState.READY:
        raise ValueError("runtime capability failures cannot use READY")
    revision = get_runtime_connection_revision()
    with _CAPABILITY_CACHE_LOCK:
        if _CAPABILITY_CACHE.get("connection_revision") != revision:
            _CAPABILITY_CACHE["connection_revision"] = revision
            _CAPABILITY_CACHE["failures"] = {}
        failures = dict(_CAPABILITY_CACHE.get("failures") or {})
        failures[capability_id] = _ProbeResult(
            state=state,
            reason_code=reason_code,
            message=message,
        )
        _CAPABILITY_CACHE["failures"] = failures


def required_capability_ids_for_profile(
    profile: ProjectProcessingProfileV1,
) -> tuple[str, ...]:
    return required_capability_ids(
        document=profile.document,
        llm_provider=profile.llm_provider,
        knowledge_backend=profile.knowledge_backend,
    )


def required_capability_ids(
    *,
    document: DocumentProcessingProfileV1,
    llm_provider: str,
    knowledge_backend: str,
) -> tuple[str, ...]:
    required: list[str] = []
    if document.primary_parser == "pymupdf":
        required.append(CAPABILITY_LOCAL_PYMUPDF)
    else:
        required.append(CAPABILITY_AZURE_DI)
    for enhancer in document.enhancement_chain:
        required.append(
            {
                "azure_document_intelligence": CAPABILITY_AZURE_DI,
                "bailian_ocr": CAPABILITY_BAILIAN_OCR,
                "bailian_layout": CAPABILITY_BAILIAN_LAYOUT,
            }[enhancer]
        )
    if document.complex_vision == "azure_gpt56_sol":
        required.append(CAPABILITY_AZURE_SOL)
    elif document.complex_vision == "bailian_vision":
        required.append(CAPABILITY_BAILIAN_VISION)
    if llm_provider == "azure_openai":
        required.extend((CAPABILITY_AZURE_SOL, CAPABILITY_AZURE_TERRA))
    elif llm_provider == "aliyun":
        required.append(CAPABILITY_ALIYUN_LLM)
    elif llm_provider == "codex":
        required.append(CAPABILITY_CODEX_AGENT)
    if knowledge_backend == "azure_ai_search":
        required.extend((CAPABILITY_AZURE_EMBEDDING, CAPABILITY_AZURE_SEARCH))
    elif knowledge_backend == "aliyun_qdrant":
        required.extend((CAPABILITY_ALIYUN_EMBEDDING, CAPABILITY_QDRANT))
    return tuple(dict.fromkeys(required))


def required_llm_capability_ids(llm_provider: str) -> tuple[str, ...]:
    if llm_provider == "azure_openai":
        return (CAPABILITY_AZURE_SOL, CAPABILITY_AZURE_TERRA)
    if llm_provider == "aliyun":
        return (CAPABILITY_ALIYUN_LLM,)
    if llm_provider == "codex":
        return (CAPABILITY_CODEX_AGENT,)
    return ()


def require_capabilities(
    capability_ids: Iterable[str],
    *,
    operation: str,
) -> None:
    required = tuple(dict.fromkeys(str(item) for item in capability_ids if item))
    if not required:
        return
    by_id = {
        item.capability_id: item for item in build_runtime_capabilities(probe=False)
    }
    unknown = [item for item in required if item not in by_id]
    if unknown:
        raise ApplicationError(
            status_code=409,
            code="unknown_runtime_capability",
            detail={
                "reason_code": "unknown_runtime_capability",
                "message": "The operation requested an unknown runtime capability.",
                "operation": operation,
                "blocked_by": [
                    {
                        "capability_id": item,
                        "state": CapabilityState.NOT_CONFIGURED.value,
                        "reason_code": "unknown_runtime_capability",
                        "missing": [],
                    }
                    for item in unknown
                ],
                "settings_path": "/settings",
            },
        )
    blocked = [by_id[item] for item in required if not by_id[item].ready]
    if not blocked:
        return
    non_service_states = {
        CapabilityState.NOT_CONFIGURED,
        CapabilityState.MODEL_NOT_DEPLOYED,
        CapabilityState.DEPENDENCY_UNAVAILABLE,
    }
    status_code = (
        409 if all(item.state in non_service_states for item in blocked) else 503
    )
    primary = blocked[0]
    raise ApplicationError(
        status_code=status_code,
        code=primary.reason_code,
        detail={
            "reason_code": primary.reason_code,
            "message": primary.message
            or "A required runtime capability is unavailable.",
            "operation": operation,
            "blocked_by": [
                {
                    "capability_id": item.capability_id,
                    "state": item.state.value,
                    "reason_code": item.reason_code,
                    "missing": list(item.blocked_by),
                }
                for item in blocked
            ],
            "settings_path": primary.settings_path,
        },
    )


def require_knowledge_upload_capability(
    project_id: str,
) -> ProjectProcessingProfileV1:
    profile = get_project_processing_profile(project_id)
    if profile.knowledge_backend == "none":
        raise ApplicationError(
            status_code=409,
            code="knowledge_backend_not_configured",
            detail={
                "reason_code": "knowledge_backend_not_configured",
                "message": "Select a ready project knowledge backend before upload.",
                "blocked_by": ["knowledge_backend"],
                "settings_path": f"/projects/{project_id}/settings",
            },
        )
    require_capabilities(
        (
            (CAPABILITY_AZURE_EMBEDDING, CAPABILITY_AZURE_SEARCH)
            if profile.knowledge_backend == "azure_ai_search"
            else (CAPABILITY_ALIYUN_EMBEDDING, CAPABILITY_QDRANT)
        ),
        operation="knowledge.upload",
    )
    return profile


def selected_knowledge_profile_id(project_id: str) -> str:
    profile = require_knowledge_upload_capability(project_id)
    if profile.knowledge_backend == "aliyun_qdrant":
        return DEFAULT_ALIYUN_PROFILE_ID
    if profile.knowledge_backend == "azure_ai_search":
        return DEFAULT_AZURE_PROFILE_ID
    raise AssertionError("knowledge capability gate returned an unsupported backend")


def _resolve_provider_bindings(
    *,
    document: DocumentProcessingProfileV1,
    llm_provider: str,
) -> ResolvedProviderBindingsV1:
    primary = (
        "local:pymupdf"
        if document.primary_parser == "pymupdf"
        else "azure:document-intelligence:prebuilt-layout"
    )
    bailian_ocr_model = get_runtime_ai_config_value(
        "REQPILOT_ALIYUN_VISUAL_TEXT_MODEL",
        "qwen3.5-ocr",
    )
    bailian_layout_model = "qwen3.7-flash"
    if "bailian_layout" in document.enhancement_chain:
        aliyun_config = load_ai_copilot_config(DEFAULT_ALIYUN_PROFILE_ID)
        bailian_layout_model = str(
            getattr(aliyun_config, "screening_model", "") or bailian_layout_model
        )
    enhancement = tuple(
        {
            "azure_document_intelligence": (
                "azure:document-intelligence:prebuilt-layout"
            ),
            "bailian_ocr": f"aliyun:bailian-ocr:{bailian_ocr_model}",
            "bailian_layout": f"aliyun:bailian-layout:{bailian_layout_model}",
        }[item]
        for item in document.enhancement_chain
    )
    complex_vision = {
        "azure_gpt56_sol": "azure-openai:gpt56-sol:vision",
        "bailian_vision": "aliyun:bailian-vision",
        None: None,
    }[document.complex_vision]
    complex_vision_deployment = None
    if document.complex_vision == "azure_gpt56_sol":
        complex_vision_deployment = get_runtime_ai_config_value(
            "AZURE_OPENAI_SOL_DEPLOYMENT",
            get_runtime_ai_config_value("AZURE_OPENAI_CHAT_DEPLOYMENT"),
        )
    elif document.complex_vision == "bailian_vision":
        complex_vision_deployment = load_ai_copilot_config(
            DEFAULT_ALIYUN_PROFILE_ID
        ).chat_model
    if llm_provider == "codex":
        from app.adapters.agents.codex_app_server.semantic_app_server import (
            CODEX_APP_SERVER_SEMANTIC_PROVIDER_VERSION,
        )
        from app.application.runtime import RUNTIME_STATE

        model_id = str(
            getattr(
                getattr(RUNTIME_STATE.container, "settings", None),
                "agent_model_id",
                "gpt-5.6-sol",
            )
            or "gpt-5.6-sol"
        )
        return ResolvedProviderBindingsV1(
            primary_parser_provider=primary,
            enhancement_providers=enhancement,
            complex_vision_provider=complex_vision,
            complex_vision_deployment=complex_vision_deployment,
            llm_transport_provider="codex-app-server",
            structure_overview_deployment=("reqpilotx-structure-tree-generation@1.1.0"),
            structure_repair_deployment=("reqpilotx-structure-tree-generation@1.1.0"),
            semantic_deployment=(
                f"{model_id}:semantic@{CODEX_APP_SERVER_SEMANTIC_PROVIDER_VERSION}"
            ),
            atomization_deployment=(
                f"{model_id}:atomization@{CODEX_APP_SERVER_SEMANTIC_PROVIDER_VERSION}"
            ),
            refinement_deployment=None,
        )
    if llm_provider == "azure_openai":
        sol = get_runtime_ai_config_value(
            "AZURE_OPENAI_SOL_DEPLOYMENT",
            get_runtime_ai_config_value("AZURE_OPENAI_CHAT_DEPLOYMENT"),
        )
        terra = get_runtime_ai_config_value(
            "AZURE_OPENAI_TERRA_DEPLOYMENT",
            get_runtime_ai_config_value("AZURE_OPENAI_CHAT_DEPLOYMENT"),
        )
        return ResolvedProviderBindingsV1(
            primary_parser_provider=primary,
            enhancement_providers=enhancement,
            complex_vision_provider=complex_vision,
            complex_vision_deployment=complex_vision_deployment,
            llm_transport_provider="azure_openai",
            structure_overview_deployment=sol,
            structure_repair_deployment=terra,
            semantic_deployment=terra,
            atomization_deployment=terra,
            refinement_deployment=terra,
        )
    if llm_provider == "aliyun":
        model = load_ai_copilot_config(DEFAULT_ALIYUN_PROFILE_ID).chat_model
        return ResolvedProviderBindingsV1(
            primary_parser_provider=primary,
            enhancement_providers=enhancement,
            complex_vision_provider=complex_vision,
            complex_vision_deployment=complex_vision_deployment,
            llm_transport_provider="aliyun",
            structure_overview_deployment=model,
            structure_repair_deployment=model,
            semantic_deployment=model,
            atomization_deployment=model,
            refinement_deployment=model,
        )
    return ResolvedProviderBindingsV1(
        primary_parser_provider=primary,
        enhancement_providers=enhancement,
        complex_vision_provider=complex_vision,
        complex_vision_deployment=complex_vision_deployment,
    )


def _get_probe_state(
    *,
    connection_revision: int,
    probe: bool,
) -> dict[str, _ProbeResult]:
    now = time.monotonic()
    with _CAPABILITY_CACHE_LOCK:
        cache_revision = int(_CAPABILITY_CACHE.get("connection_revision") or -1)
        cache_age = now - float(_CAPABILITY_CACHE.get("checked_at_monotonic") or 0.0)
        cached = dict(_CAPABILITY_CACHE.get("probe") or {})
        aliyun_probe_revision = int(
            _CAPABILITY_CACHE.get("aliyun_probe_connection_revision") or -1
        )
        current_aliyun_probe = (
            dict(_CAPABILITY_CACHE.get("aliyun_probe") or {})
            if aliyun_probe_revision == connection_revision
            else {}
        )
        if (
            cache_revision == connection_revision
            and cached
            and cache_age < _CAPABILITY_CACHE_TTL_SECONDS
        ):
            return cached
    if not probe:
        return current_aliyun_probe
    probed = _probe_runtime_components()
    probed.update(current_aliyun_probe)
    with _CAPABILITY_CACHE_LOCK:
        _CAPABILITY_CACHE["connection_revision"] = connection_revision
        _CAPABILITY_CACHE["checked_at_monotonic"] = now
        _CAPABILITY_CACHE["probe"] = dict(probed)
    return probed


def _probe_runtime_components() -> dict[str, _ProbeResult]:
    results: dict[str, _ProbeResult] = {}
    di_endpoint = get_runtime_ai_config_value("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT")
    di_key = get_runtime_ai_config_value("AZURE_DOCUMENT_INTELLIGENCE_API_KEY")
    if di_endpoint and di_key:
        results["azure_di"] = _probe_azure_document_intelligence(
            endpoint=di_endpoint,
            api_key=di_key,
        )

    azure_endpoint = get_runtime_ai_config_value("AZURE_OPENAI_ENDPOINT")
    azure_search_endpoint = get_runtime_ai_config_value("AZURE_AI_SEARCH_ENDPOINT")
    if azure_endpoint or azure_search_endpoint:
        try:
            azure_probe = probe_runtime_azure_rbac()
        except Exception as exc:  # pragma: no cover - provider dependency boundary
            failure = _ProbeResult(
                CapabilityState.AUTH_FAILED,
                "azure_rbac_auth_failed",
                f"Azure RBAC credential probe failed: {type(exc).__name__}.",
            )
            results["azure_openai_auth"] = failure
            results["azure_search_auth"] = failure
        else:
            checks = {
                str(item.get("name") or ""): item
                for item in azure_probe.get("checks") or []
            }
            results["azure_openai_auth"] = _azure_auth_probe_result(
                checks.get("azure_openai")
            )
            results["azure_search_auth"] = _azure_auth_probe_result(
                checks.get("azure_search")
            )

    aliyun = load_ai_copilot_config(DEFAULT_ALIYUN_PROFILE_ID)
    if (
        aliyun.retrieval_backend == "qdrant"
        and aliyun.qdrant_url
        and aliyun.qdrant_collection_name
    ):
        check = build_ai_profile_check(aliyun)
        qdrant_probe = check.get("qdrant_probe") or {}
        if str(qdrant_probe.get("status") or "") == "ok":
            results["qdrant"] = _ProbeResult(CapabilityState.READY)
        else:
            results["qdrant"] = _ProbeResult(
                CapabilityState.UNREACHABLE,
                "qdrant_unreachable",
                str(qdrant_probe.get("detail") or "Qdrant is unreachable."),
            )
    return results


def _probe_azure_document_intelligence(
    *,
    endpoint: str,
    api_key: str,
) -> _ProbeResult:
    # Listing available document models is a read-only control-plane-free
    # request that validates both the endpoint and API key without sending
    # customer document bytes.
    url = (
        endpoint.rstrip("/")
        + "/documentintelligence/documentModels?api-version=2024-11-30"
    )
    request = urllib_request.Request(
        url,
        method="GET",
        headers={"Ocp-Apim-Subscription-Key": api_key},
    )
    try:
        with urllib_request.urlopen(request, timeout=3.0) as response:
            if 200 <= int(response.status) < 300:
                return _ProbeResult(CapabilityState.READY)
    except urllib_error.HTTPError as exc:
        if exc.code in {401, 403}:
            return _ProbeResult(
                CapabilityState.AUTH_FAILED,
                "azure_document_intelligence_auth_failed",
                "Azure Document Intelligence rejected the configured API key.",
            )
        return _ProbeResult(
            CapabilityState.UNREACHABLE,
            "azure_document_intelligence_unreachable",
            f"Azure Document Intelligence probe returned HTTP {exc.code}.",
        )
    except (OSError, urllib_error.URLError) as exc:
        return _ProbeResult(
            CapabilityState.UNREACHABLE,
            "azure_document_intelligence_unreachable",
            f"Azure Document Intelligence probe failed: {type(exc).__name__}.",
        )
    return _ProbeResult(
        CapabilityState.UNREACHABLE,
        "azure_document_intelligence_unreachable",
        "Azure Document Intelligence returned an unexpected probe response.",
    )


def _azure_auth_probe_result(payload: Any) -> _ProbeResult:
    if isinstance(payload, dict) and payload.get("status") == "ok":
        return _ProbeResult(CapabilityState.READY)
    return _ProbeResult(
        CapabilityState.AUTH_FAILED,
        "azure_rbac_auth_failed",
        "Azure RBAC authentication is unavailable.",
    )


def _azure_model_capability(
    *,
    endpoint: str,
    deployment: str,
    deployment_setting: str,
    auth_probe: _ProbeResult | None,
) -> _ProbeResult:
    if not endpoint:
        return _ProbeResult(
            CapabilityState.NOT_CONFIGURED,
            "runtime_not_configured",
            "Azure OpenAI endpoint is not configured.",
        )
    if not deployment:
        return _ProbeResult(
            CapabilityState.MODEL_NOT_DEPLOYED,
            "model_not_deployed",
            f"{deployment_setting} is not configured.",
        )
    if auth_probe is not None:
        return auth_probe
    return _ProbeResult(
        CapabilityState.CHECKING,
        "connection_check_pending",
        "Azure RBAC connection has not been checked for this revision.",
    )


def _probe_result_from_payload(payload: Any) -> _ProbeResult:
    if not isinstance(payload, dict):
        return _ProbeResult(
            CapabilityState.UNREACHABLE,
            "aliyun_probe_invalid_response",
            "The DashScope connection probe returned an invalid result.",
        )
    try:
        state = CapabilityState(str(payload.get("state") or ""))
    except ValueError:
        state = CapabilityState.UNREACHABLE
    reason_code = str(payload.get("reason_code") or "")
    message = str(payload.get("message") or "")
    if state is CapabilityState.READY:
        return _ProbeResult(state)
    return _ProbeResult(
        state,
        reason_code or "aliyun_probe_failed",
        message or "The DashScope connection probe failed.",
    )


def _combine_probe_results(
    *results: _ProbeResult | None,
) -> _ProbeResult | None:
    if any(item is None for item in results):
        return None
    resolved = tuple(item for item in results if item is not None)
    for item in resolved:
        if item.state is not CapabilityState.READY:
            return item
    return _ProbeResult(CapabilityState.READY)


def _public_probe_result(
    result: _ProbeResult,
    *,
    role: str,
) -> dict[str, Any]:
    return {
        "state": result.state.value,
        "ready": result.state is CapabilityState.READY,
        "reason_code": result.reason_code,
        "message": result.message,
        "role": role,
    }


def _public_probe_check(
    payload: Any,
    *,
    fallback: _ProbeResult,
) -> dict[str, Any]:
    allowed = {
        "state",
        "ready",
        "reason_code",
        "message",
        "model",
        "role",
        "dimensions",
        "request_count",
        "latency_ms",
    }
    if not isinstance(payload, dict):
        return _public_probe_result(fallback, role="unknown")
    return {key: payload[key] for key in allowed if key in payload}


def _configured_capability(
    *,
    values: dict[str, Any],
) -> _ProbeResult:
    missing = [name for name, value in values.items() if not value]
    if missing:
        return _ProbeResult(
            CapabilityState.NOT_CONFIGURED,
            "runtime_not_configured",
            "Missing runtime settings: " + ", ".join(missing),
        )
    return _ProbeResult(CapabilityState.READY)


def _codex_runtime_capability() -> _ProbeResult:
    """Read the already-composed local runtime without starting a model turn."""

    from app.application.runtime import RUNTIME_STATE

    container = RUNTIME_STATE.container
    if container is None:
        return _ProbeResult(
            CapabilityState.CHECKING,
            "codex_runtime_initializing",
            "Codex App Server runtime composition is still initializing.",
        )
    settings = container.settings
    backend = str(getattr(settings.agent_runtime_backend, "value", ""))
    if backend != "codex-app-server":
        return _ProbeResult(
            CapabilityState.NOT_CONFIGURED,
            "codex_runtime_not_configured",
            "Codex App Server is not the active document Agent runtime.",
        )
    if not settings.agent_customer_data_enabled:
        return _ProbeResult(
            CapabilityState.NOT_CONFIGURED,
            "codex_customer_data_not_authorized",
            "Local customer-document authorization for Codex is disabled.",
        )
    if (
        container.semantic_provider is None
        or container.requirement_atomization_provider is None
    ):
        return _ProbeResult(
            CapabilityState.DEPENDENCY_UNAVAILABLE,
            "codex_extraction_provider_unavailable",
            "Codex semantic or decomposition provider is not initialized.",
        )
    return _ProbeResult(CapabilityState.READY)


def _configured_or_probe(
    *,
    configured: bool,
    missing: tuple[str, ...],
    probed: _ProbeResult | None,
) -> _ProbeResult:
    if not configured:
        return _ProbeResult(
            CapabilityState.NOT_CONFIGURED,
            "runtime_not_configured",
            "Missing runtime settings: " + ", ".join(missing),
        )
    if probed is not None:
        return probed
    return _ProbeResult(
        CapabilityState.CHECKING,
        "connection_check_pending",
        "Connection has not been checked for this revision.",
    )


def _capability(
    capability_id: str,
    category: str,
    provider: str,
    label: str,
    result: _ProbeResult,
    connection_revision: int,
) -> RuntimeCapabilityV1:
    blocked_by: tuple[str, ...] = ()
    if result.state is not CapabilityState.READY:
        marker = "Missing runtime settings: "
        if result.message.startswith(marker):
            blocked_by = tuple(
                item.strip()
                for item in result.message[len(marker) :].split(",")
                if item.strip()
            )
    return RuntimeCapabilityV1(
        capability_id=capability_id,
        category=category,  # type: ignore[arg-type]
        provider=provider,
        label=label,
        state=result.state,
        reason_code=result.reason_code,
        message=result.message,
        blocked_by=blocked_by,
        connection_revision=connection_revision,
    )


def _capability_failures(
    connection_revision: int,
) -> dict[str, _ProbeResult]:
    with _CAPABILITY_CACHE_LOCK:
        if _CAPABILITY_CACHE.get("connection_revision") != connection_revision:
            return {}
        return dict(_CAPABILITY_CACHE.get("failures") or {})


def _apply_failure(
    capability: RuntimeCapabilityV1,
    failures: dict[str, _ProbeResult],
) -> RuntimeCapabilityV1:
    failure = failures.get(capability.capability_id)
    if failure is None:
        return capability
    return _capability(
        capability.capability_id,
        capability.category,
        capability.provider,
        capability.label,
        failure,
        capability.connection_revision,
    )


def _processing_profile_corrupt(message: str) -> ApplicationError:
    return ApplicationError(
        status_code=409,
        code="processing_profile_corrupt",
        detail={
            "reason_code": "processing_profile_corrupt",
            "message": message,
        },
    )


__all__ = [
    "build_runtime_capabilities",
    "get_project_processing_profile",
    "get_project_processing_profile_route",
    "get_runtime_capabilities_route",
    "get_runtime_aliyun_connection_probe_receipt",
    "invalidate_runtime_capabilities",
    "probe_runtime_aliyun_connection",
    "record_runtime_capability_failure",
    "require_capabilities",
    "require_knowledge_upload_capability",
    "required_capability_ids",
    "resolve_processing_plan",
    "selected_knowledge_profile_id",
    "update_project_processing_profile_route",
]

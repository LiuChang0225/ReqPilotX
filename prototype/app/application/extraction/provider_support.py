from __future__ import annotations

import logging
from typing import Any

from app import db
from app.adapters.documents.bailian_semantic_provider import (
    DEFAULT_BAILIAN_EVALUATION_MODEL,
    DEFAULT_BAILIAN_SEMANTIC_SEED,
    BailianQwenSemanticProvider,
)
from app.application.errors import ApplicationError
from app.domain.extraction.processing_profile import ResolvedProcessingPlanV1
from app.ports.semantic_provider import PreflightSemanticProviderPort
from app.services.ai_copilot import (
    is_ascii_runtime_secret,
    load_ai_copilot_config,
)

logger = logging.getLogger(__name__)


def _build_llm_shadow_semantic_provider(
    plan: ResolvedProcessingPlanV1 | None = None,
) -> PreflightSemanticProviderPort:
    """Build the provider frozen in the plan without exposing runtime secrets."""
    from app.application.runtime import _ASSET_RESPONSE_HEADERS

    provider_id = plan.llm_provider if plan is not None else "aliyun"
    if provider_id == "codex":
        from app.application.runtime import (
            _agent_customer_data_enabled,
            _codex_semantic_provider,
        )

        if not _agent_customer_data_enabled():
            raise ApplicationError(
                status_code=409,
                detail={
                    "reason_code": "codex_customer_data_not_authorized",
                    "message": (
                        "Codex document extraction is disabled until the local "
                        "customer-data authorization is enabled."
                    ),
                    "blocked_by": ["agent_customer_data_authorization"],
                    "settings_path": "/settings",
                },
                headers=_ASSET_RESPONSE_HEADERS,
            )
        try:
            provider = _codex_semantic_provider()
        except RuntimeError as exc:
            raise ApplicationError(
                status_code=503,
                detail={
                    "reason_code": "codex_semantic_runtime_unavailable",
                    "message": "Codex semantic extraction is not initialized.",
                    "blocked_by": ["codex.agent"],
                    "settings_path": "/settings",
                },
                headers=_ASSET_RESPONSE_HEADERS,
            ) from exc
        if not isinstance(provider, PreflightSemanticProviderPort):
            raise ApplicationError(
                status_code=503,
                detail={
                    "reason_code": "codex_semantic_runtime_invalid",
                    "message": "Codex semantic extraction has an invalid runtime binding.",
                    "blocked_by": ["codex.agent"],
                    "settings_path": "/settings",
                },
                headers=_ASSET_RESPONSE_HEADERS,
            )
        return provider
    if provider_id == "azure_openai":
        from app.adapters.documents.azure_openai_semantic_provider import (
            AzureOpenAISemanticProvider,
        )
        from app.services.ai_copilot import (
            _azure_openai_token_provider,
        )

        config = load_ai_copilot_config("azure")
        deployment = (
            plan.provider_bindings.semantic_deployment if plan is not None else ""
        ) or config.azure_openai_chat_deployment
        if (
            not config.azure_openai_endpoint
            or not deployment
            or not config.azure_openai_api_version
        ):
            raise ApplicationError(
                status_code=503,
                detail={
                    "reason_code": "azure_openai_semantic_not_configured",
                    "message": "Azure OpenAI semantic extraction is not configured.",
                    "blocked_by": ["azure_openai", "gpt56_terra"],
                    "settings_path": "/settings",
                },
                headers=_ASSET_RESPONSE_HEADERS,
            )
        try:
            return AzureOpenAISemanticProvider(
                endpoint=config.azure_openai_endpoint,
                deployment=deployment,
                api_version=config.azure_openai_api_version,
                token_provider=_azure_openai_token_provider(
                    config.azure_allow_interactive_login
                ),
                timeout_seconds=90.0,
                max_retries=1,
            )
        except (TypeError, ValueError) as exc:
            raise ApplicationError(
                status_code=503,
                detail={
                    "reason_code": "azure_openai_semantic_configuration_invalid",
                    "message": (
                        "Azure OpenAI semantic extraction configuration is invalid."
                    ),
                    "settings_path": "/settings",
                },
                headers=_ASSET_RESPONSE_HEADERS,
            ) from exc
    if provider_id != "aliyun":
        raise ApplicationError(
            status_code=409,
            detail={
                "reason_code": "llm_provider_not_selected",
                "message": "Select a ready LLM provider before AI extraction.",
                "blocked_by": ["llm_provider"],
                "settings_path": "/settings",
            },
            headers=_ASSET_RESPONSE_HEADERS,
        )

    config = load_ai_copilot_config("aliyun")
    api_key = str(config.dashscope_api_key or "").strip()
    base_url = str(config.dashscope_base_url or "").strip()
    if not api_key or not is_ascii_runtime_secret(api_key) or not base_url:
        raise ApplicationError(
            status_code=503,
            detail={
                "code": "llm_extraction_provider_not_configured",
                "message": "Aliyun Bailian semantic extraction is not configured.",
            },
            headers=_ASSET_RESPONSE_HEADERS,
        )
    try:
        return BailianQwenSemanticProvider(
            api_key=api_key,
            base_url=base_url,
            model=(
                plan.provider_bindings.semantic_deployment
                if plan is not None
                else DEFAULT_BAILIAN_EVALUATION_MODEL
            ),
            seed=DEFAULT_BAILIAN_SEMANTIC_SEED,
            timeout_seconds=90.0,
            max_retries=1,
        )
    except ValueError as exc:
        raise ApplicationError(
            status_code=503,
            detail={
                "code": "llm_extraction_provider_configuration_invalid",
                "message": "Aliyun Bailian semantic extraction configuration is invalid.",
            },
            headers=_ASSET_RESPONSE_HEADERS,
        ) from exc


def _build_structure_optimizer_provider(
    plan: ResolvedProcessingPlanV1 | None = None,
):
    """Build the server-owned two-call Structure optimizer."""

    from app.adapters.documents.bailian_structure_optimizer_provider import (
        BailianQwenStructureOptimizerProvider,
    )
    from app.application.runtime import _ASSET_RESPONSE_HEADERS

    provider_id = plan.llm_provider if plan is not None else "aliyun"
    if provider_id == "azure_openai":
        from app.adapters.documents.azure_openai_structure_optimizer_provider import (
            AzureOpenAIStructureOptimizerProvider,
        )
        from app.services.ai_copilot import _azure_openai_token_provider

        config = load_ai_copilot_config("azure")
        sol = (
            plan.provider_bindings.structure_overview_deployment
            if plan is not None
            else config.azure_openai_chat_deployment
        )
        terra = (
            plan.provider_bindings.structure_repair_deployment
            if plan is not None
            else config.azure_openai_chat_deployment
        )
        if (
            not config.azure_openai_endpoint
            or not sol
            or not terra
            or not config.azure_openai_api_version
        ):
            raise ApplicationError(
                status_code=503,
                detail={
                    "reason_code": "azure_structure_optimizer_not_configured",
                    "message": "Azure GPT-5.6 Sol/Terra deployments are unavailable.",
                    "blocked_by": ["gpt56_sol", "gpt56_terra"],
                    "settings_path": "/settings",
                },
                headers=_ASSET_RESPONSE_HEADERS,
            )
        return AzureOpenAIStructureOptimizerProvider(
            endpoint=config.azure_openai_endpoint,
            sol_deployment=sol,
            terra_deployment=terra,
            api_version=config.azure_openai_api_version,
            token_provider=_azure_openai_token_provider(
                config.azure_allow_interactive_login
            ),
            timeout_seconds=90.0,
            max_retries=1,
        )
    if provider_id != "aliyun":
        raise ApplicationError(
            status_code=409,
            detail={
                "reason_code": "llm_provider_not_selected",
                "message": "Select a ready LLM provider before Structure optimization.",
                "blocked_by": ["llm_provider"],
                "settings_path": "/settings",
            },
            headers=_ASSET_RESPONSE_HEADERS,
        )

    config = load_ai_copilot_config("aliyun")
    api_key = str(config.dashscope_api_key or "").strip()
    base_url = str(config.dashscope_base_url or "").strip()
    if not api_key or not is_ascii_runtime_secret(api_key) or not base_url:
        raise ApplicationError(
            status_code=503,
            detail={
                "code": "structure_optimizer_provider_not_configured",
                "message": "Aliyun Bailian Structure optimization is not configured.",
            },
            headers=_ASSET_RESPONSE_HEADERS,
        )
    try:
        return BailianQwenStructureOptimizerProvider(
            api_key=api_key,
            base_url=base_url,
            model=(
                plan.provider_bindings.structure_overview_deployment
                if plan is not None
                else DEFAULT_BAILIAN_EVALUATION_MODEL
            ),
            seed=DEFAULT_BAILIAN_SEMANTIC_SEED,
            timeout_seconds=90.0,
            max_retries=1,
        )
    except ValueError as exc:
        raise ApplicationError(
            status_code=503,
            detail={
                "code": "structure_optimizer_provider_configuration_invalid",
                "message": "Aliyun Bailian Structure optimization configuration is invalid.",
            },
            headers=_ASSET_RESPONSE_HEADERS,
        ) from exc


def _require_restoration_document(
    project_id: str,
    document_id: str,
) -> dict[str, Any]:
    from app.application.projects import _require_project

    _require_project(project_id)
    document = db.get_sor_document(document_id)
    if not document or document.get("project_id") != project_id:
        raise ApplicationError(
            status_code=404,
            detail="SOR document not found for this project.",
        )
    return document

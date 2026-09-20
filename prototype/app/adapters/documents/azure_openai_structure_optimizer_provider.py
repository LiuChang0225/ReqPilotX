from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.adapters.documents.azure_openai_semantic_provider import (
    AzureOpenAISemanticProvider,
)
from app.adapters.documents.bailian_structure_optimizer_provider import (
    BailianQwenStructureOptimizerProvider,
)
from app.ports.structure_optimizer_provider import (
    StructureOverviewProviderResultV1,
    StructureOverviewRequestV1,
    StructureRepairProviderResultV1,
    StructureRepairRequestV1,
)


AZURE_STRUCTURE_OPTIMIZER_PROVIDER_ID = "azure-openai-gpt56-structure-optimizer"
AZURE_STRUCTURE_OPTIMIZER_PROVIDER_VERSION = (
    "1.0.0;overview=sol;repair=terra;codec=shared"
)


class _AzureOpenAIStructureCodecProvider(
    BailianQwenStructureOptimizerProvider,
    AzureOpenAISemanticProvider,
):
    provider_id = AZURE_STRUCTURE_OPTIMIZER_PROVIDER_ID
    provider_version = AZURE_STRUCTURE_OPTIMIZER_PROVIDER_VERSION


class AzureOpenAIStructureOptimizerProvider:
    """Use Sol for whole-document overview and Terra for source-bound repair."""

    provider_id = AZURE_STRUCTURE_OPTIMIZER_PROVIDER_ID
    provider_version = AZURE_STRUCTURE_OPTIMIZER_PROVIDER_VERSION

    def __init__(
        self,
        *,
        endpoint: str,
        sol_deployment: str,
        terra_deployment: str,
        api_version: str,
        token_provider: Callable[[], str],
        timeout_seconds: float = 120.0,
        max_retries: int = 2,
        overview_http_client: Any | None = None,
        repair_http_client: Any | None = None,
    ) -> None:
        self.overview_model_id = str(sol_deployment or "").strip()
        self.repair_model_id = str(terra_deployment or "").strip()
        self.model_id = f"{self.overview_model_id}|{self.repair_model_id}"
        if not self.overview_model_id or not self.repair_model_id:
            raise ValueError("Sol and Terra deployments are required")
        self._overview = _AzureOpenAIStructureCodecProvider(
            endpoint=endpoint,
            deployment=self.overview_model_id,
            api_version=api_version,
            token_provider=token_provider,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            http_client=overview_http_client,
        )
        self._repair = _AzureOpenAIStructureCodecProvider(
            endpoint=endpoint,
            deployment=self.repair_model_id,
            api_version=api_version,
            token_provider=token_provider,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            http_client=repair_http_client,
        )

    def overview_structure(
        self,
        request: StructureOverviewRequestV1,
    ) -> StructureOverviewProviderResultV1:
        return self._overview.overview_structure(request)

    def repair_structure(
        self,
        request: StructureRepairRequestV1,
    ) -> StructureRepairProviderResultV1:
        return self._repair.repair_structure(request)


__all__ = [
    "AZURE_STRUCTURE_OPTIMIZER_PROVIDER_ID",
    "AZURE_STRUCTURE_OPTIMIZER_PROVIDER_VERSION",
    "AzureOpenAIStructureOptimizerProvider",
]

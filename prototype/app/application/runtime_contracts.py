from typing import Literal

from pydantic import BaseModel, ConfigDict


class ReadingVisualProviderSetupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["reading-visual-provider-setup/v1"]
    provider: Literal["bailian-qwen3.8-max-pdf-parsing"]
    model: Literal["qwen3.8-max"]
    timeout_seconds: Literal[300]
    max_retries: Literal[0]


class AzureProviderSetupRequest(BaseModel):
    auth_mode: str | None = None
    allow_interactive_login: bool = False
    document_intelligence_endpoint: str | None = None
    document_intelligence_api_key: str | None = None
    search_endpoint: str | None = None
    search_index_name: str | None = None
    openai_endpoint: str | None = None
    chat_deployment: str | None = None
    sol_deployment: str | None = None
    terra_deployment: str | None = None
    embedding_deployment: str | None = None
    screening_deployment: str | None = None


class AliyunProviderSetupRequest(BaseModel):
    api_key: str | None = None
    base_url: str | None = None
    chat_model: str | None = None
    screening_model: str | None = None
    embedding_model: str | None = None
    qdrant_url: str | None = None
    qdrant_collection: str | None = None
    qdrant_api_key: str | None = None
    retrieval_backend: str | None = None
    reading_visual: ReadingVisualProviderSetupRequest | None = None


class RuntimeAIProviderSetupRequest(BaseModel):
    default_profile_id: str | None = None
    azure: AzureProviderSetupRequest | None = None
    aliyun: AliyunProviderSetupRequest | None = None


__all__ = [
    "AliyunProviderSetupRequest",
    "AzureProviderSetupRequest",
    "RuntimeAIProviderSetupRequest",
    "ReadingVisualProviderSetupRequest",
]

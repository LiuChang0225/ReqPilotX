from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

import httpx

from app.adapters.documents.bailian_semantic_provider import (
    DEFAULT_BAILIAN_SEMANTIC_SEED,
    BailianQwenSemanticProvider,
    _post_streaming_request,
)


AZURE_OPENAI_SEMANTIC_PROVIDER_ID = "azure-openai-gpt56-semantic"
AZURE_OPENAI_SEMANTIC_PROVIDER_VERSION = "1.0.0;codec=bailian-shared;stream=1"


class AzureOpenAISemanticProvider(BailianQwenSemanticProvider):
    """Azure RBAC transport over ReqPilotX's shared semantic prompt/codec."""

    provider_id = AZURE_OPENAI_SEMANTIC_PROVIDER_ID
    provider_version = AZURE_OPENAI_SEMANTIC_PROVIDER_VERSION

    def __init__(
        self,
        *,
        endpoint: str,
        deployment: str,
        api_version: str,
        token_provider: Callable[[], str],
        seed: int = DEFAULT_BAILIAN_SEMANTIC_SEED,
        timeout_seconds: float = 120.0,
        max_retries: int = 2,
        http_client: Any | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        # Reuse the already hardened request budgeting, prompt and response
        # mapping. The DashScope endpoint set by the parent is never called.
        super().__init__(
            api_key="azure-rbac",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            model=deployment,
            seed=seed,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            http_client=http_client,
            clock=clock or (lambda: datetime.now(timezone.utc)),
            monotonic=monotonic,
            sleep=sleep,
        )
        if not callable(token_provider):
            raise TypeError("token_provider must be callable")
        self._endpoint = _azure_chat_completions_endpoint(
            endpoint=endpoint,
            deployment=deployment,
            api_version=api_version,
        )
        self._token_provider = token_provider
        self.provider_version = (
            f"{AZURE_OPENAI_SEMANTIC_PROVIDER_VERSION};"
            f"api={api_version};s={seed}"
        )

    def _post(self, payload: dict[str, object]) -> Any:
        token = str(self._token_provider() or "").strip()
        if not token:
            raise httpx.TransportError("Azure RBAC returned an empty token")
        azure_payload = {
            key: value
            for key, value in payload.items()
            if key
            not in {
                "enable_thinking",
                "model",
                "seed",
                "temperature",
            }
        }
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        if self._http_client is not None:
            if isinstance(self._http_client, httpx.Client):
                return _post_streaming_request(
                    self._http_client,
                    self._endpoint,
                    headers=headers,
                    payload=azure_payload,
                    timeout_seconds=self._timeout_seconds,
                    monotonic=self._monotonic,
                )
            return self._http_client.post(
                self._endpoint,
                headers=headers,
                json=azure_payload,
                timeout=self._timeout_seconds,
            )
        with httpx.Client(timeout=self._timeout_seconds) as client:
            return _post_streaming_request(
                client,
                self._endpoint,
                headers=headers,
                payload=azure_payload,
                timeout_seconds=self._timeout_seconds,
                monotonic=self._monotonic,
            )


def _azure_chat_completions_endpoint(
    *,
    endpoint: str,
    deployment: str,
    api_version: str,
) -> str:
    value = str(endpoint or "").strip().rstrip("/")
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Azure OpenAI endpoint has an invalid port") from exc
    hostname = str(parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or port not in {None, 443}
        or not (
            hostname.endswith(".openai.azure.com")
            or hostname.endswith(".services.ai.azure.com")
        )
    ):
        raise ValueError("Azure OpenAI endpoint must use an approved HTTPS origin")
    normalized_deployment = str(deployment or "").strip()
    normalized_version = str(api_version or "").strip()
    if not normalized_deployment or not normalized_version:
        raise ValueError("Azure OpenAI deployment and api_version are required")
    base_path = parsed.path.rstrip("/")
    if base_path.endswith("/openai"):
        base_path = base_path[: -len("/openai")]
    path = (
        f"{base_path}/openai/deployments/"
        f"{quote(normalized_deployment, safe='')}/chat/completions"
    )
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            path,
            urlencode({"api-version": normalized_version}),
            "",
        )
    )


__all__ = [
    "AZURE_OPENAI_SEMANTIC_PROVIDER_ID",
    "AZURE_OPENAI_SEMANTIC_PROVIDER_VERSION",
    "AzureOpenAISemanticProvider",
]

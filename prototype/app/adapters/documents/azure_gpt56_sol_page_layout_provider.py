from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx

from app.adapters.documents.azure_openai_semantic_provider import (
    _azure_chat_completions_endpoint,
)
from app.adapters.documents.bailian_page_layout_provider import (
    BailianPageLayoutProvider,
    _PageLayoutRequestDeadlineExceeded,
    _completion_from_sse_response,
)
from app.ports.visual_text_provider import (
    VisualTextProviderRejected,
    VisualTextProviderRejectionCode,
)


AZURE_GPT56_SOL_LAYOUT_PROVIDER_ID = "azure-openai-gpt56-sol-page-layout"
AZURE_GPT56_SOL_LAYOUT_PROVIDER_VERSION = "1.0.0;codec=page-layout-v1"


class AzureGpt56SolPageLayoutProvider(BailianPageLayoutProvider):
    """GPT-5.6 Sol vision transport using the existing strict layout codec."""

    provider_id = AZURE_GPT56_SOL_LAYOUT_PROVIDER_ID

    def __init__(
        self,
        *,
        endpoint: str,
        deployment: str,
        api_version: str,
        token_provider: Callable[[], str],
        timeout_seconds: float = 120.0,
        max_retries: int = 2,
        http_client: Any | None = None,
    ) -> None:
        super().__init__(
            api_key="azure-rbac",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            model=deployment,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            http_client=http_client,
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
            f"{AZURE_GPT56_SOL_LAYOUT_PROVIDER_VERSION};"
            f"deployment={deployment};api={api_version}"
        )

    def _post(self, request_payload: dict[str, object]) -> object:
        token = str(self._token_provider() or "").strip()
        if not token:
            raise VisualTextProviderRejected(
                VisualTextProviderRejectionCode.UNAVAILABLE,
                "Azure OpenAI RBAC token is unavailable.",
            )
        payload = _azure_layout_payload(request_payload)
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        owned_client = self._http_client is None
        client = self._http_client or httpx.Client(follow_redirects=False)
        try:
            for attempt in range(self._max_retries + 1):
                try:
                    deadline = self._monotonic() + self._timeout_seconds
                    with client.stream(
                        "POST",
                        self._endpoint,
                        headers=headers,
                        json=payload,
                        timeout=self._timeout_seconds,
                    ) as response:
                        status = int(getattr(response, "status_code", 0) or 0)
                        if status == 200:
                            return _completion_from_sse_response(
                                response,
                                deadline=deadline,
                                monotonic=self._monotonic,
                            )
                        if status in {401, 403}:
                            raise VisualTextProviderRejected(
                                VisualTextProviderRejectionCode.UNAVAILABLE,
                                "Azure OpenAI page-layout authorization failed.",
                            )
                        if status == 413:
                            raise VisualTextProviderRejected(
                                VisualTextProviderRejectionCode.INVALID_INPUT,
                                "The rendered page is too large for Azure OpenAI.",
                            )
                        if (
                            status == 429 or 500 <= status <= 599
                        ) and attempt < self._max_retries:
                            self._sleep(min(0.5 * (2**attempt), 2.0))
                            continue
                        raise VisualTextProviderRejected(
                            VisualTextProviderRejectionCode.PROVIDER_FAILED,
                            "Azure OpenAI page-layout recognition failed.",
                        )
                except VisualTextProviderRejected:
                    raise
                except (
                    _PageLayoutRequestDeadlineExceeded,
                    httpx.TransportError,
                ) as exc:
                    if attempt < self._max_retries:
                        self._sleep(min(0.5 * (2**attempt), 2.0))
                        continue
                    raise VisualTextProviderRejected(
                        VisualTextProviderRejectionCode.PROVIDER_FAILED,
                        "Azure OpenAI page-layout recognition failed.",
                    ) from exc
                except (UnicodeError, ValueError) as exc:
                    raise VisualTextProviderRejected(
                        VisualTextProviderRejectionCode.PROVIDER_OUTPUT_INVALID,
                        "Azure OpenAI returned an invalid page-layout stream.",
                    ) from exc
            raise AssertionError("bounded retry loop terminated unexpectedly")
        finally:
            if owned_client:
                client.close()


def _azure_layout_payload(
    request_payload: dict[str, object],
) -> dict[str, object]:
    payload = {
        key: value
        for key, value in request_payload.items()
        if key not in {"model", "enable_thinking", "temperature"}
    }
    messages = payload.get("messages")
    if isinstance(messages, list):
        normalized_messages: list[object] = []
        for message in messages:
            if not isinstance(message, dict):
                normalized_messages.append(message)
                continue
            normalized = dict(message)
            content = normalized.get("content")
            if isinstance(content, list):
                normalized["content"] = [
                    {
                        key: value
                        for key, value in item.items()
                        if key not in {"min_pixels", "max_pixels"}
                    }
                    if isinstance(item, dict)
                    else item
                    for item in content
                ]
            normalized_messages.append(normalized)
        payload["messages"] = normalized_messages
    return payload


__all__ = [
    "AZURE_GPT56_SOL_LAYOUT_PROVIDER_ID",
    "AZURE_GPT56_SOL_LAYOUT_PROVIDER_VERSION",
    "AzureGpt56SolPageLayoutProvider",
]

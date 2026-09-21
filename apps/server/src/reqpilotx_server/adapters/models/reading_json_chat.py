"""Narrow JSON chat transport owned by the isolated reading slice.

This module deliberately knows nothing about extraction processing profiles,
Requirement Sets, retrieval, project knowledge, or the AI copilot service.  It
may share deployment credentials with the rest of the process, but reading has
its own explicit provider selection and model settings.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, Protocol
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel

from reqpilotx_server.runtime_paths import resolve_storage_dir


DEFAULT_AZURE_API_VERSION = "2024-10-21"
DEFAULT_AZURE_SCOPE = "https://cognitiveservices.azure.com/.default"
DEFAULT_ALIYUN_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_ALIYUN_MODEL = "qwen3.8-max"
DEFAULT_READING_TIMEOUT_SECONDS = 180.0
DEFAULT_READING_MAX_RETRIES = 0
_ALLOWED_PROVIDERS = frozenset({"none", "codex", "azure", "aliyun"})
_DASHSCOPE_PUBLIC_HOSTS = frozenset(
    {"dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com"}
)
_DASHSCOPE_WORKSPACE_HOST = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.maas\.aliyuncs\.com$"
)
_AZURE_OPENAI_HOST = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.openai\.azure\.com$"
)
_SAFE_MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")


@dataclass(frozen=True, slots=True)
class ReadingAIConfig:
    provider: str = "none"
    model_name: str = ""
    endpoint: str = ""
    api_key: str = ""
    api_version: str = DEFAULT_AZURE_API_VERSION
    auth_mode: str = "auto"
    allow_interactive_login: bool = False
    timeout_seconds: float = DEFAULT_READING_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_READING_MAX_RETRIES
    configuration_source: str = "NONE"

    def __post_init__(self) -> None:
        if self.provider not in _ALLOWED_PROVIDERS:
            raise ValueError("unsupported reading AI provider")
        if self.auth_mode not in {"auto", "api_key", "rbac"}:
            raise ValueError("unsupported reading Azure auth mode")
        if self.configuration_source not in {
            "CODEX_RUNTIME",
            "READING_OVERRIDE",
            "SHARED_RUNTIME",
            "NONE",
        }:
            raise ValueError("unsupported reading AI configuration source")
        if not 1.0 <= self.timeout_seconds <= 300.0:
            raise ValueError("reading AI timeout must be between 1 and 300 seconds")
        if not 0 <= self.max_retries <= 5:
            raise ValueError("reading AI retries must be between 0 and 5")
        if self.model_name and not _SAFE_MODEL_NAME.fullmatch(self.model_name):
            raise ValueError("reading AI model name is invalid")

    @property
    def ready(self) -> bool:
        if self.provider == "none":
            return False
        if self.provider == "codex":
            return self.endpoint == "codex-app-server" and bool(self.model_name)
        if not self.endpoint or not self.model_name:
            return False
        if self.provider == "aliyun":
            return bool(self.api_key)
        if self.auth_mode == "api_key":
            return bool(self.api_key)
        return True


class ReadingJsonChatTransportPort(Protocol):
    def complete(
        self,
        *,
        config: ReadingAIConfig,
        messages: list[dict[str, str]],
        response_model: type[BaseModel],
    ) -> dict[str, Any]: ...


class OpenAIReadingJsonChatTransport:
    """Execute one bounded JSON-chat request without retrieval hooks."""

    def complete(
        self,
        *,
        config: ReadingAIConfig,
        messages: list[dict[str, str]],
        response_model: type[BaseModel],
    ) -> dict[str, Any]:
        if config.provider == "azure":
            return self._complete_azure(
                config=config,
                messages=messages,
                response_model=response_model,
            )
        if config.provider == "aliyun":
            return self._complete_aliyun(config=config, messages=messages)
        raise RuntimeError("reading AI provider is not configured")

    def _complete_azure(
        self,
        *,
        config: ReadingAIConfig,
        messages: list[dict[str, str]],
        response_model: type[BaseModel],
    ) -> dict[str, Any]:
        from openai import AzureOpenAI

        _enforce_payload_safe_openai_logging()

        kwargs: dict[str, Any] = {
            "azure_endpoint": normalize_azure_endpoint(config.endpoint),
            "api_version": config.api_version,
            "timeout": config.timeout_seconds,
            "max_retries": config.max_retries,
            "http_client": _no_redirect_http_client(
                timeout_seconds=config.timeout_seconds
            ),
        }
        use_api_key = config.auth_mode == "api_key" or (
            config.auth_mode == "auto" and bool(config.api_key)
        )
        if use_api_key:
            if not config.api_key:
                raise RuntimeError("reading Azure API key is not configured")
            kwargs["api_key"] = config.api_key
        else:
            kwargs["azure_ad_token_provider"] = _azure_token_provider(
                config.allow_interactive_login
            )
        client = AzureOpenAI(**kwargs)
        try:
            completion = client.chat.completions.parse(
                model=config.model_name,
                messages=messages,
                response_format=response_model,
                temperature=0.0,
                max_completion_tokens=4_096,
            )
        finally:
            client.close()
        choices = list(getattr(completion, "choices", []) or [])
        if not choices:
            raise RuntimeError("reading Azure chat returned no choices")
        message = choices[0].message
        parsed = getattr(message, "parsed", None)
        if parsed is None:
            refusal = str(getattr(message, "refusal", "") or "").strip()
            raise RuntimeError(refusal or "reading Azure chat returned no JSON payload")
        if isinstance(parsed, dict):
            return dict(parsed)
        if hasattr(parsed, "model_dump"):
            return dict(parsed.model_dump())
        raise RuntimeError("reading Azure chat returned an unsupported payload")

    def _complete_aliyun(
        self,
        *,
        config: ReadingAIConfig,
        messages: list[dict[str, str]],
    ) -> dict[str, Any]:
        from openai import OpenAI

        _enforce_payload_safe_openai_logging()

        base_url = normalize_dashscope_base_url(config.endpoint)
        client = OpenAI(
            api_key=config.api_key,
            base_url=base_url,
            timeout=config.timeout_seconds,
            max_retries=config.max_retries,
            http_client=_no_redirect_http_client(
                timeout_seconds=config.timeout_seconds
            ),
        )
        payload: dict[str, Any] = {
            "model": config.model_name,
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": 4_096,
            # DashScope's OpenAI-compatible qwen chat models otherwise may
            # enter a long reasoning phase for document-sized contexts.  The
            # reading UI needs one bounded, source-cited response, matching the
            # repository's established Aliyun provider contract.
            "extra_body": {"enable_thinking": False},
        }
        try:
            try:
                completion = client.chat.completions.create(
                    **payload,
                    response_format={"type": "json_object"},
                )
            except Exception as exc:
                if not _retry_without_json_response_format(exc):
                    raise
                completion = client.chat.completions.create(**payload)
        finally:
            client.close()
        choices = list(getattr(completion, "choices", []) or [])
        if not choices:
            raise RuntimeError("reading Aliyun chat returned no choices")
        content = getattr(choices[0].message, "content", "")
        if isinstance(content, list):
            content = "".join(
                str(item.get("text") or "")
                for item in content
                if isinstance(item, dict)
            )
        return _extract_json_object(str(content or ""))


def load_reading_ai_config(
    *,
    environment: Mapping[str, str] | None = None,
    config_values: Mapping[str, Any] | None = None,
) -> ReadingAIConfig:
    """Load the reading-owned provider selection and narrow chat settings."""

    source = os.environ if environment is None else environment
    file_values = (
        _read_runtime_config_json(environment=source)
        if config_values is None
        else dict(config_values)
    )

    def value(*names: str, default: str = "") -> str:
        for name in names:
            raw = source.get(name)
            if raw is None or not str(raw).strip():
                raw = file_values.get(name)
            if raw is not None and str(raw).strip():
                return str(raw).strip()
        return default

    explicit_provider = value("REQPILOT_READING_AI_PROVIDER", default="").lower()
    shared_aliyun_key = value(
        "REQPILOT_READING_ALIYUN_API_KEY",
        "REQPILOT_DASHSCOPE_API_KEY",
        "DASHSCOPE_API_KEY",
        "REQPILOT_ALIYUN_API_KEY",
    )
    provider = explicit_provider or ("aliyun" if shared_aliyun_key else "none")
    configuration_source = (
        "READING_OVERRIDE"
        if explicit_provider
        else "SHARED_RUNTIME"
        if provider != "none"
        else "NONE"
    )
    timeout = _bounded_float(
        value(
            "REQPILOT_READING_AI_TIMEOUT_SECONDS",
            default=str(int(DEFAULT_READING_TIMEOUT_SECONDS)),
        ),
        default=DEFAULT_READING_TIMEOUT_SECONDS,
        minimum=1.0,
        maximum=300.0,
    )
    retries = _bounded_int(
        value(
            "REQPILOT_READING_AI_MAX_RETRIES",
            default=str(DEFAULT_READING_MAX_RETRIES),
        ),
        default=DEFAULT_READING_MAX_RETRIES,
        minimum=0,
        maximum=5,
    )
    if provider == "aliyun":
        endpoint = normalize_dashscope_base_url(
            value(
                "REQPILOT_READING_ALIYUN_BASE_URL",
                "REQPILOT_DASHSCOPE_BASE_URL",
                "DASHSCOPE_BASE_URL",
                default=DEFAULT_ALIYUN_BASE_URL,
            )
        )
        return ReadingAIConfig(
            provider=provider,
            model_name=value(
                "REQPILOT_READING_ALIYUN_MODEL",
                "REQPILOT_ALIYUN_CHAT_MODEL",
                default=DEFAULT_ALIYUN_MODEL,
            ),
            endpoint=endpoint,
            api_key=shared_aliyun_key,
            timeout_seconds=timeout,
            max_retries=retries,
            configuration_source=configuration_source,
        )
    if provider == "azure":
        auth_mode = value(
            "REQPILOT_READING_AZURE_AUTH_MODE",
            default="auto",
        ).lower()
        return ReadingAIConfig(
            provider=provider,
            model_name=value(
                "REQPILOT_READING_AZURE_DEPLOYMENT",
            ),
            endpoint=normalize_azure_endpoint(value("AZURE_OPENAI_ENDPOINT")),
            api_key=value("AZURE_OPENAI_API_KEY"),
            api_version=value(
                "REQPILOT_READING_AZURE_API_VERSION",
                default=DEFAULT_AZURE_API_VERSION,
            ),
            auth_mode=auth_mode,
            allow_interactive_login=_as_bool(
                value(
                    "REQPILOT_READING_AZURE_ALLOW_INTERACTIVE_LOGIN",
                    default="false",
                )
            ),
            timeout_seconds=timeout,
            max_retries=retries,
            configuration_source=configuration_source,
        )
    return ReadingAIConfig(
        provider=provider,
        timeout_seconds=timeout,
        max_retries=retries,
        configuration_source=configuration_source,
    )


def normalize_dashscope_base_url(value: str) -> str:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
        hostname = str(parsed.hostname or "").lower()
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid reading Aliyun base URL") from exc
    host_allowed = hostname in _DASHSCOPE_PUBLIC_HOSTS or bool(
        _DASHSCOPE_WORKSPACE_HOST.fullmatch(hostname)
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
    ):
        raise ValueError("invalid reading Aliyun base URL")
    return urlunsplit(("https", hostname, "/compatible-mode/v1", "", ""))


def normalize_azure_endpoint(value: str) -> str:
    raw = str(value or "").strip().rstrip("/")
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
        hostname = str(parsed.hostname or "").lower()
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid reading Azure endpoint") from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or not _AZURE_OPENAI_HOST.fullmatch(hostname)
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("invalid reading Azure endpoint")
    path = parsed.path.rstrip("/")
    for suffix in ("/openai/v1", "/openai"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
    if path:
        raise ValueError("invalid reading Azure endpoint")
    return urlunsplit(("https", hostname, "", "", "")).rstrip("/")


def _enforce_payload_safe_openai_logging() -> None:
    """Disable SDK debug channels that serialize complete chat messages."""

    for logger_name in ("openai", "openai._base_client"):
        logger = logging.getLogger(logger_name)
        if logger.level < logging.INFO:
            logger.setLevel(logging.INFO)


def _no_redirect_http_client(*, timeout_seconds: float, transport=None):
    """Build an HTTP client that cannot replay source text across redirects."""

    import httpx

    return httpx.Client(
        timeout=timeout_seconds,
        follow_redirects=False,
        transport=transport,
    )


@lru_cache(maxsize=2)
def _azure_token_provider(allow_interactive_login: bool):
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider

    credential = DefaultAzureCredential(
        exclude_interactive_browser_credential=not allow_interactive_login,
        exclude_shared_token_cache_credential=True,
        exclude_visual_studio_code_credential=True,
    )
    return get_bearer_token_provider(credential, DEFAULT_AZURE_SCOPE)


def _read_runtime_config_json(
    *, environment: Mapping[str, str]
) -> dict[str, Any]:
    prototype_dir = Path(__file__).resolve().parents[3]
    path = resolve_storage_dir(prototype_dir, environment=environment) / "config.json"
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return dict(payload) if isinstance(payload, dict) else {}


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
        raise RuntimeError("reading chat returned an empty JSON payload")
    decoded = json.loads(payload)
    if not isinstance(decoded, dict):
        raise RuntimeError("reading chat returned a non-object JSON payload")
    return decoded


def _retry_without_json_response_format(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    if status_code is None and response is not None:
        status_code = getattr(response, "status_code", None)
    detail = str(exc or "").lower()
    return status_code == 400 and any(
        token in detail
        for token in (
            "response_format",
            "json_object",
            "invalid_parameter",
            "messages must contain the word 'json'",
        )
    )


def _as_bool(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _bounded_float(
    value: str,
    *,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return min(max(parsed, minimum), maximum)


def _bounded_int(
    value: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return min(max(parsed, minimum), maximum)


__all__ = [
    "OpenAIReadingJsonChatTransport",
    "DEFAULT_READING_MAX_RETRIES",
    "DEFAULT_READING_TIMEOUT_SECONDS",
    "ReadingAIConfig",
    "ReadingJsonChatTransportPort",
    "load_reading_ai_config",
    "normalize_azure_endpoint",
    "normalize_dashscope_base_url",
]

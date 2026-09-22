"""Neutral, reading-owned visual provider configuration.

The reading source-capture path reads these settings directly from its own
environment/config keys.  It does not depend on extraction profiles, route
files, or formal AI-copilot runtime objects.
"""

from __future__ import annotations

from reqpilotx_server.resource_paths import development_path

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from reqpilotx_server.runtime_paths import resolve_storage_dir


READING_VISUAL_SETUP_SCHEMA_VERSION = "reading-visual-provider-setup/v1"
READING_VISUAL_PROVIDER = "bailian-qwen3.8-max-pdf-parsing"
READING_VISUAL_MODEL = "qwen3.8-max"
READING_VISUAL_TIMEOUT_SECONDS = 300
READING_VISUAL_MAX_RETRIES = 0
READING_VISUAL_BILLING_BASIS = ("pdf_pages", "input_tokens", "output_tokens")

READING_VISUAL_PROVIDER_KEY = "REQPILOT_READING_VISUAL_PROVIDER"
READING_VISUAL_MODEL_KEY = "REQPILOT_READING_VISUAL_MODEL"
READING_VISUAL_TIMEOUT_KEY = "REQPILOT_READING_VISUAL_TIMEOUT_SECONDS"
READING_VISUAL_RETRIES_KEY = "REQPILOT_READING_VISUAL_MAX_RETRIES"

_FIXED_VALUES = {
    READING_VISUAL_PROVIDER_KEY: READING_VISUAL_PROVIDER,
    READING_VISUAL_MODEL_KEY: READING_VISUAL_MODEL,
    READING_VISUAL_TIMEOUT_KEY: str(READING_VISUAL_TIMEOUT_SECONDS),
    READING_VISUAL_RETRIES_KEY: str(READING_VISUAL_MAX_RETRIES),
}
_KEY_ALIASES = (
    "REQPILOT_READING_ALIYUN_API_KEY",
    "REQPILOT_DASHSCOPE_API_KEY",
    "DASHSCOPE_API_KEY",
    "REQPILOT_ALIYUN_API_KEY",
)
_BASE_URL_ALIASES = (
    "REQPILOT_READING_ALIYUN_BASE_URL",
    "REQPILOT_DASHSCOPE_BASE_URL",
    "DASHSCOPE_BASE_URL",
)
_DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


def load_reading_visual_environment(
    *,
    environment: Mapping[str, str] | None = None,
    config_values: Mapping[str, Any] | None = None,
    prototype_dir: Path | None = None,
) -> dict[str, str]:
    """Merge persisted values then environment overrides (environment wins).

    Passing an explicit ``environment`` keeps tests/callers isolated and does
    not implicitly read the process environment or production config file.
    """

    if environment is not None:
        persisted = dict(config_values or {})
        process_values = dict(environment)
    else:
        persisted = (
            dict(config_values)
            if config_values is not None
            else _read_config_json(prototype_dir=prototype_dir)
        )
        process_values = dict(os.environ)
    merged = {
        str(key): str(value)
        for key, value in persisted.items()
        if value is not None and str(value).strip()
    }
    merged.update(
        {
            str(key): str(value)
            for key, value in process_values.items()
            if value is not None and str(value).strip()
        }
    )
    return merged


def validate_reading_visual_setup_payload(value: Mapping[str, Any]) -> None:
    expected = {
        "schema_version": READING_VISUAL_SETUP_SCHEMA_VERSION,
        "provider": READING_VISUAL_PROVIDER,
        "model": READING_VISUAL_MODEL,
        "timeout_seconds": READING_VISUAL_TIMEOUT_SECONDS,
        "max_retries": READING_VISUAL_MAX_RETRIES,
    }
    if not isinstance(value, Mapping) or dict(value) != expected:
        raise ValueError("reading visual provider setup is immutable")


def reading_visual_persisted_values() -> dict[str, str]:
    return dict(_FIXED_VALUES)


def build_reading_visual_setup(
    *,
    connection_revision: int,
    environment: Mapping[str, str] | None = None,
    config_values: Mapping[str, Any] | None = None,
    prototype_dir: Path | None = None,
) -> dict[str, Any]:
    source = load_reading_visual_environment(
        environment=environment,
        config_values=config_values,
        prototype_dir=prototype_dir,
    )
    raw_values = {
        key: str(source.get(key) or "").strip() for key in _FIXED_VALUES
    }
    any_configured = any(raw_values.values())
    exact_configuration = raw_values == _FIXED_VALUES
    if not any_configured:
        configured = False
        ready = False
        reason_code = "reading_visual_not_configured"
        message = "Save the fixed reading visual provider settings."
    elif not exact_configuration:
        configured = False
        ready = False
        reason_code = "reading_visual_configuration_invalid"
        message = (
            "The reading visual provider settings do not match the supported policy."
        )
    else:
        configured = True
        credential = _first_value(source, _KEY_ALIASES)
        base_url = _first_value(source, _BASE_URL_ALIASES) or _DEFAULT_BASE_URL
        if not _is_plausible_dashscope_key(credential):
            ready = False
            reason_code = "reading_visual_credentials_unavailable"
            message = "Reading-owned DashScope credentials are unavailable."
        elif not _is_approved_dashscope_base_url(base_url):
            ready = False
            reason_code = "reading_visual_configuration_invalid"
            message = (
                "The reading visual provider settings do not match the supported policy."
            )
        else:
            ready = True
            reason_code = None
            message = None
    return {
        "schema_version": READING_VISUAL_SETUP_SCHEMA_VERSION,
        "provider": READING_VISUAL_PROVIDER,
        "model": READING_VISUAL_MODEL,
        "timeout_seconds": READING_VISUAL_TIMEOUT_SECONDS,
        "max_retries": READING_VISUAL_MAX_RETRIES,
        "billing_basis": list(READING_VISUAL_BILLING_BASIS),
        "configured": configured,
        "ready": ready,
        "reason_code": reason_code,
        "message": message,
        "connection_revision": int(connection_revision),
    }


def _read_config_json(*, prototype_dir: Path | None) -> dict[str, Any]:
    root = prototype_dir or development_path('prototype')
    path = resolve_storage_dir(root) / "config.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return dict(payload) if isinstance(payload, dict) else {}


def _first_value(source: Mapping[str, str], names: tuple[str, ...]) -> str:
    return next(
        (
            str(source.get(name) or "").strip()
            for name in names
            if str(source.get(name) or "").strip()
        ),
        "",
    )


def _is_approved_dashscope_base_url(value: str) -> bool:
    try:
        parsed = urlsplit(str(value or "").strip())
        hostname = str(parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        return False
    public = hostname in {
        "dashscope.aliyuncs.com",
        "dashscope-intl.aliyuncs.com",
    }
    workspace = hostname.endswith(".maas.aliyuncs.com") and hostname.count(".") >= 4
    return (
        parsed.scheme == "https"
        and parsed.username is None
        and parsed.password is None
        and port in {None, 443}
        and (public or workspace)
        and parsed.path.rstrip("/") == "/compatible-mode/v1"
        and not parsed.query
        and not parsed.fragment
    )


def _is_plausible_dashscope_key(value: str) -> bool:
    text = str(value or "")
    return 20 <= len(text) <= 256 and text.isascii() and not any(
        character.isspace() for character in text
    )


__all__ = [
    "READING_VISUAL_BILLING_BASIS",
    "READING_VISUAL_MAX_RETRIES",
    "READING_VISUAL_MODEL",
    "READING_VISUAL_MODEL_KEY",
    "READING_VISUAL_PROVIDER",
    "READING_VISUAL_PROVIDER_KEY",
    "READING_VISUAL_RETRIES_KEY",
    "READING_VISUAL_SETUP_SCHEMA_VERSION",
    "READING_VISUAL_TIMEOUT_KEY",
    "READING_VISUAL_TIMEOUT_SECONDS",
    "build_reading_visual_setup",
    "load_reading_visual_environment",
    "reading_visual_persisted_values",
    "validate_reading_visual_setup_payload",
]

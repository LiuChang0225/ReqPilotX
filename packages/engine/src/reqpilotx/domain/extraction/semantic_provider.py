from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)


SEMANTIC_PROVIDER_RUN_SCHEMA_VERSION = "semantic-provider-run/v2"
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_RUN_ID_PATTERN = re.compile(r"^semantic-run-[0-9a-f]{32}$")
_ERROR_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,119}$")


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class SemanticProviderRunStatus(str, Enum):
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    TIMEOUT = "TIMEOUT"
    INVALID_OUTPUT = "INVALID_OUTPUT"
    UNAVAILABLE = "UNAVAILABLE"
    SKIPPED = "SKIPPED"


class SemanticProviderRunRecord(_StrictFrozenModel):
    """Text-free audit metadata for one remote semantic-provider call."""

    schema_version: Literal["semantic-provider-run/v2"] = (
        SEMANTIC_PROVIDER_RUN_SCHEMA_VERSION
    )
    provider_run_id: str = Field(min_length=1, max_length=160)
    operation: str = Field(min_length=1, max_length=80)
    provider_id: str = Field(min_length=1, max_length=160)
    provider_version: str = Field(min_length=1, max_length=160)
    model_id: str = Field(min_length=1, max_length=160)
    prompt_version: str = Field(min_length=1, max_length=160)
    response_schema_version: str = Field(min_length=1, max_length=160)
    request_sha256: str
    response_sha256: str | None = None
    status: SemanticProviderRunStatus
    started_at: datetime
    finished_at: datetime
    latency_ms: StrictInt = Field(ge=0)
    request_count: StrictInt = Field(ge=0)
    retry_count: StrictInt = Field(ge=0)
    input_tokens: StrictInt | None = Field(default=None, ge=0)
    output_tokens: StrictInt | None = Field(default=None, ge=0)
    error_code: str | None = Field(default=None, min_length=1, max_length=120)
    shared_request_provider_run_id: str | None = Field(
        default=None,
        pattern=r"^semantic-run-[0-9a-f]{32}$",
    )

    @field_validator(
        "operation",
        "provider_id",
        "provider_version",
        "model_id",
        "prompt_version",
        "response_schema_version",
    )
    @classmethod
    def reject_blank_identity(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("provider run identity values must not be blank")
        return value

    @field_validator("request_sha256", "response_sha256")
    @classmethod
    def validate_digest(cls, value: str | None) -> str | None:
        if value is not None and not _SHA256_PATTERN.fullmatch(value):
            raise ValueError("provider run digests must be lowercase SHA-256 values")
        return value

    @field_validator("error_code")
    @classmethod
    def validate_error_code(cls, value: str | None) -> str | None:
        if value is not None and not _ERROR_CODE_PATTERN.fullmatch(value):
            raise ValueError("error_code must use a machine-readable key")
        return value

    @model_validator(mode="after")
    def validate_run(self) -> SemanticProviderRunRecord:
        for value in (self.started_at, self.finished_at):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("provider run timestamps must be timezone-aware")
        if self.finished_at < self.started_at:
            raise ValueError("finished_at must not precede started_at")
        not_configured = (
            self.status is SemanticProviderRunStatus.UNAVAILABLE
            and self.error_code == "provider_not_configured"
        )
        shared_request = self.shared_request_provider_run_id is not None
        if shared_request:
            if (
                self.status
                not in {
                    SemanticProviderRunStatus.SUCCESS,
                    SemanticProviderRunStatus.PARTIAL,
                }
                or self.request_count != 0
                or self.retry_count != 0
                or self.response_sha256 is None
                or self.input_tokens is not None
                or self.output_tokens is not None
                or self.shared_request_provider_run_id == self.provider_run_id
            ):
                raise ValueError("shared provider runs require a zero-request result")
        elif self.status is SemanticProviderRunStatus.SKIPPED:
            if self.request_count != 0 or self.retry_count != 0:
                raise ValueError("skipped provider runs cannot record remote requests")
            if self.latency_ms != 0:
                raise ValueError(
                    "skipped provider runs must record zero remote latency"
                )
            if self.response_sha256 is not None:
                raise ValueError("skipped provider runs cannot record a response")
            if self.input_tokens is not None or self.output_tokens is not None:
                raise ValueError("skipped provider runs cannot record token usage")
        elif not_configured:
            if self.request_count != 0 or self.retry_count != 0:
                raise ValueError(
                    "unconfigured provider runs cannot record remote requests"
                )
            if self.response_sha256 is not None:
                raise ValueError("unconfigured provider runs cannot record a response")
            if self.input_tokens is not None or self.output_tokens is not None:
                raise ValueError("unconfigured provider runs cannot record token usage")
        else:
            if self.request_count < 1:
                raise ValueError("non-skipped provider runs require a remote request")
            if self.retry_count >= self.request_count:
                raise ValueError("retry_count must be smaller than request_count")
        if self.status is SemanticProviderRunStatus.SUCCESS:
            if self.response_sha256 is None or self.error_code is not None:
                raise ValueError(
                    "successful provider runs require a response and no error"
                )
        elif self.error_code is None:
            raise ValueError("non-success provider runs require an error_code")
        expected = stable_semantic_provider_run_id(
            operation=self.operation,
            provider_id=self.provider_id,
            model_id=self.model_id,
            request_sha256=self.request_sha256,
            response_sha256=self.response_sha256,
            status=self.status,
            started_at=self.started_at,
            shared_request_provider_run_id=self.shared_request_provider_run_id,
        )
        if self.provider_run_id != expected:
            raise ValueError(
                "provider_run_id does not match its immutable run identity"
            )
        return self


def stable_semantic_provider_run_id(
    *,
    operation: str,
    provider_id: str,
    model_id: str,
    request_sha256: str,
    response_sha256: str | None,
    status: SemanticProviderRunStatus,
    started_at: datetime,
    shared_request_provider_run_id: str | None = None,
) -> str:
    if not all(
        isinstance(item, str) and item.strip()
        for item in (operation, provider_id, model_id)
    ):
        raise ValueError("provider run identity values must not be blank")
    if not _SHA256_PATTERN.fullmatch(request_sha256):
        raise ValueError("request_sha256 must be a lowercase SHA-256 value")
    if response_sha256 is not None and not _SHA256_PATTERN.fullmatch(response_sha256):
        raise ValueError("response_sha256 must be a lowercase SHA-256 value")
    if not isinstance(status, SemanticProviderRunStatus):
        raise TypeError("status must be a SemanticProviderRunStatus")
    if started_at.tzinfo is None or started_at.utcoffset() is None:
        raise ValueError("started_at must be timezone-aware")
    if shared_request_provider_run_id is not None and not _RUN_ID_PATTERN.fullmatch(
        shared_request_provider_run_id
    ):
        raise ValueError("shared_request_provider_run_id is invalid")
    identity: tuple[object, ...] = (
        operation,
        provider_id,
        model_id,
        request_sha256,
        response_sha256,
        status.value,
        started_at.isoformat(),
    )
    if shared_request_provider_run_id is not None:
        identity = (*identity, shared_request_provider_run_id)
    payload = json.dumps(
        identity,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"semantic-run-{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:32]}"


__all__ = [
    "SEMANTIC_PROVIDER_RUN_SCHEMA_VERSION",
    "SemanticProviderRunRecord",
    "SemanticProviderRunStatus",
    "stable_semantic_provider_run_id",
]

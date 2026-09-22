from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AnalyzeRequest(BaseModel):
    document_id: str


ASSESSMENT_SCOPE_REQUEST_SCHEMA_VERSION_V1 = "requirement-analysis-scope-request/v1"
ASSESSMENT_SCOPE_REQUEST_SCHEMA_VERSION_V2 = "requirement-analysis-scope-request/v2"

AnalysisScopeKind = Literal["CURRENT", "SELECTED", "ALL"]
RecoverableAnalysisScopeKind = Literal["CURRENT", "SELECTED", "ALL", "FAILED_ONLY"]


def _validate_scope_shape(
    *, scope: AnalysisScopeKind, node_ids: tuple[str, ...]
) -> None:
    if len(node_ids) != len(set(node_ids)):
        raise ValueError("analysis scope node IDs must be unique")
    if scope == "CURRENT" and len(node_ids) != 1:
        raise ValueError("CURRENT analysis scope requires exactly one node ID")
    if scope == "SELECTED" and not node_ids:
        raise ValueError("SELECTED analysis scope requires at least one node ID")
    if scope == "ALL" and node_ids:
        raise ValueError("ALL analysis scope must not include node IDs")


def _validate_recoverable_scope_shape(
    *,
    scope: RecoverableAnalysisScopeKind,
    node_ids: tuple[str, ...],
    source_job_id: str,
) -> None:
    if scope == "FAILED_ONLY":
        if node_ids:
            raise ValueError("FAILED_ONLY analysis scope must not include node IDs")
        if not source_job_id.strip():
            raise ValueError("FAILED_ONLY analysis scope requires a source job ID")
        return
    _validate_scope_shape(scope=scope, node_ids=node_ids)
    if source_job_id.strip():
        raise ValueError("only FAILED_ONLY analysis scope accepts a source job ID")


class RequirementAnalysisScopeRequestV1(BaseModel):
    """Pure scope-preview request.

    Preview commands deliberately carry an id even though preview itself is
    read-only.  The same immutable request can then be confirmed by the start
    endpoint without inventing a second identity in the browser.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["requirement-analysis-scope-request/v1"] = (
        ASSESSMENT_SCOPE_REQUEST_SCHEMA_VERSION_V1
    )
    command_id: str = Field(
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$",
    )
    requirement_set_id: str = Field(min_length=1, max_length=200)
    expected_tree_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope: AnalysisScopeKind
    node_ids: tuple[str, ...] = Field(default=(), max_length=2000)

    @model_validator(mode="after")
    def validate_scope(self) -> RequirementAnalysisScopeRequestV1:
        _validate_scope_shape(scope=self.scope, node_ids=self.node_ids)
        return self


class RequirementAnalysisScopeRequestV2(BaseModel):
    """Recoverable per-node scope preview request.

    ``FAILED_ONLY`` is a server-resolved scope.  Browsers identify the source
    job but never submit its failed node list, which prevents a stale client
    from silently widening or changing the retry set.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["requirement-analysis-scope-request/v2"] = (
        ASSESSMENT_SCOPE_REQUEST_SCHEMA_VERSION_V2
    )
    command_id: str = Field(
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$",
    )
    requirement_set_id: str = Field(min_length=1, max_length=200)
    expected_tree_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope: RecoverableAnalysisScopeKind
    node_ids: tuple[str, ...] = Field(default=(), max_length=2000)
    source_job_id: str = Field(default="", max_length=200)

    @model_validator(mode="after")
    def validate_scope(self) -> RequirementAnalysisScopeRequestV2:
        _validate_recoverable_scope_shape(
            scope=self.scope,
            node_ids=self.node_ids,
            source_job_id=self.source_job_id,
        )
        return self


RequirementAnalysisScopeRequest = (
    RequirementAnalysisScopeRequestV1 | RequirementAnalysisScopeRequestV2
)


class RunAIAnalysisRequest(BaseModel):
    """Start request with a narrow legacy bridge for the former ALL action."""

    model_config = ConfigDict(extra="forbid")

    profile_id: str | None = None
    requirement_set_id: str = Field(min_length=1, max_length=200)
    schema_version: Literal[
        "requirement-analysis-scope-request/v1",
        "requirement-analysis-scope-request/v2",
    ] = ASSESSMENT_SCOPE_REQUEST_SCHEMA_VERSION_V1
    command_id: str = Field(
        default="",
        max_length=200,
        pattern=r"^(?:[A-Za-z0-9][A-Za-z0-9._:-]{0,199})?$",
    )
    expected_tree_sha256: str = Field(
        default="",
        pattern=r"^(?:[0-9a-f]{64})?$",
    )
    scope: RecoverableAnalysisScopeKind = "ALL"
    node_ids: tuple[str, ...] = Field(default=(), max_length=2000)
    source_job_id: str = Field(default="", max_length=200)
    expected_scope_sha256: str = Field(
        default="",
        pattern=r"^(?:[0-9a-f]{64})?$",
    )

    @model_validator(mode="after")
    def validate_scope(self) -> RunAIAnalysisRequest:
        if self.schema_version == ASSESSMENT_SCOPE_REQUEST_SCHEMA_VERSION_V1:
            if self.scope == "FAILED_ONLY":
                raise ValueError("v1 analysis scope does not support FAILED_ONLY")
            _validate_scope_shape(scope=self.scope, node_ids=self.node_ids)
            if self.source_job_id.strip():
                raise ValueError("v1 analysis scope does not accept a source job ID")
        else:
            _validate_recoverable_scope_shape(
                scope=self.scope,
                node_ids=self.node_ids,
                source_job_id=self.source_job_id,
            )
        has_command = bool(self.command_id.strip())
        if (
            self.schema_version == ASSESSMENT_SCOPE_REQUEST_SCHEMA_VERSION_V2
            and not has_command
        ):
            raise ValueError("v2 analysis scope requires a non-blank command ID")
        if has_command and (
            not self.expected_tree_sha256 or not self.expected_scope_sha256
        ):
            raise ValueError(
                "confirmed analysis scope requires tree and scope preconditions"
            )
        if not has_command and (
            self.scope != "ALL"
            or bool(self.node_ids)
            or bool(self.expected_tree_sha256)
            or bool(self.expected_scope_sha256)
        ):
            raise ValueError("scoped analysis requires a non-blank command ID")
        return self


class StopAIAnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_job_id: str = Field(default="", max_length=200)


__all__ = [
    "AnalysisScopeKind",
    "ASSESSMENT_SCOPE_REQUEST_SCHEMA_VERSION_V1",
    "ASSESSMENT_SCOPE_REQUEST_SCHEMA_VERSION_V2",
    "AnalyzeRequest",
    "RecoverableAnalysisScopeKind",
    "RequirementAnalysisScopeRequest",
    "RequirementAnalysisScopeRequestV1",
    "RequirementAnalysisScopeRequestV2",
    "RunAIAnalysisRequest",
    "StopAIAnalysisRequest",
]

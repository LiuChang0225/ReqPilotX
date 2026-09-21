from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Literal, Mapping, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from reqpilotx_server.adapters.agents.d2_evidence_host import (
    D2EvidenceToolError,
    D2EvidenceToolHost,
)
from reqpilotx_server.adapters.agents.structure_failure_pattern_host import (
    StructureFailurePatternHost,
)
from reqpilotx_server.application.agent_workflow.d2_evidence_tools import (
    D2LogicalTableEvidenceInputV1,
    D2ResolveSourceHandlesInputV1,
    D2ResolverSuggestionsInputV1,
    D2SourceHandleSelectionV1,
)
from reqpilotx_server.application.agent_workflow.extraction_runtime import AgentExtractionStage
from reqpilotx_server.application.agent_workflow.structure_tool_loop import (
    StructureMaterializationObservationV1,
    StructureProjectionOperationV1,
    StructureRevisionBasisRole,
    StructureWorkItemDispositionV1,
)
from reqpilotx_server.application.extraction.agent_logical_table_scope import (
    AgentLogicalTableScopeV1,
)


CANONICAL_DYNAMIC_TOOL_RESULT_SCHEMA_VERSION = (
    "reqpilotx-canonical-dynamic-tool-result/v1"
)
MAX_CANONICAL_DYNAMIC_TOOL_RESULT_BYTES = 900_000

TOOL_D2_GET_LOGICAL_TABLE = "reqpilotx_d2_get_logical_table"
TOOL_D2_RESOLVE_SOURCE_HANDLES = "reqpilotx_d2_resolve_source_handles"
TOOL_D2_QUERY_RESOLVER_SUGGESTIONS = "reqpilotx_d2_query_resolver_suggestions"
TOOL_STRUCTURE_SELECT_FAILURE_PATTERNS = "reqpilotx_structure_select_failure_patterns"
TOOL_STRUCTURE_VALIDATE_MATERIALIZATION = "reqpilotx_structure_validate_materialization"
TOOL_STRUCTURE_PUBLISH_REVISION = "reqpilotx_structure_publish_revision"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_default=True)


class StructureFailurePatternSignalsInputV1(_StrictModel):
    signals: tuple[str, ...] = Field(min_length=1, max_length=16)

    @field_validator("signals")
    @classmethod
    def validate_signals(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))) or any(
            not item or not item.replace("_", "a").isalnum() for item in value
        ):
            raise ValueError("failure-pattern signals must be unique machine codes")
        return value


class CanonicalD2GetLogicalTableInputV1(_StrictModel):
    logical_table_scope: AgentLogicalTableScopeV1
    context_nodes_per_side: int = Field(default=3, ge=0, le=8)


class CanonicalD2ResolveSourceHandlesInputV1(_StrictModel):
    selections: tuple[D2SourceHandleSelectionV1, ...] = Field(
        min_length=1,
        max_length=512,
    )

    @field_validator("selections")
    @classmethod
    def validate_selections(
        cls,
        value: tuple[D2SourceHandleSelectionV1, ...],
    ) -> tuple[D2SourceHandleSelectionV1, ...]:
        keys = tuple(
            (
                item.handle_kind,
                item.source_handle,
                item.start_offset,
                item.end_offset,
            )
            for item in value
        )
        if len(keys) != len(set(keys)):
            raise ValueError("dynamic source selections must be unique")
        return value


class CanonicalD2ResolverSuggestionsInputV1(_StrictModel):
    physical_table_ids: tuple[str, ...] = Field(default=(), max_length=512)

    @field_validator("physical_table_ids")
    @classmethod
    def validate_table_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(not item.strip() for item in value):
            raise ValueError("dynamic resolver table IDs must be unique")
        return value


class StructureMaterializationValidationInputV1(_StrictModel):
    observation: StructureMaterializationObservationV1


class CanonicalStructureRevisionDraftV1(_StrictModel):
    target_scope_id: str = Field(min_length=1, max_length=200)
    basis_role: StructureRevisionBasisRole
    basis_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    operations: tuple[StructureProjectionOperationV1, ...] = Field(
        min_length=1,
        max_length=32,
    )
    evidence_refs: tuple[str, ...] = Field(default=(), max_length=4096)
    tool_call_ids: tuple[str, ...] = Field(default=(), max_length=32)

    @field_validator("evidence_refs", "tool_call_ids")
    @classmethod
    def validate_unique_sorted(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))) or any(
            not item.strip() for item in value
        ):
            raise ValueError("revision refs must be non-empty, unique, and sorted")
        return value


class CanonicalStructureRevisionSubmissionInputV1(_StrictModel):
    candidate_revisions: tuple[CanonicalStructureRevisionDraftV1, ...] = Field(
        min_length=1,
        max_length=64,
    )
    work_item_dispositions: tuple[StructureWorkItemDispositionV1, ...] = Field(
        default=(),
        max_length=128,
    )
    evidence_state: Literal["SUFFICIENT", "CONFLICTING", "INSUFFICIENT"]
    current_action: str = Field(min_length=1, max_length=500)
    public_summary: str = Field(min_length=1, max_length=1000)

    @field_validator("current_action", "public_summary")
    @classmethod
    def normalize_public_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or "\x00" in normalized:
            raise ValueError("revision activity text must be non-empty plain text")
        return normalized


@dataclass(frozen=True, slots=True)
class CanonicalDynamicToolSpec:
    name: str
    description: str
    input_schema: Mapping[str, Any]

    def app_server_payload(self) -> dict[str, Any]:
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "inputSchema": dict(self.input_schema),
            "deferLoading": False,
        }


@dataclass(frozen=True, slots=True)
class CanonicalDynamicToolExecution:
    success: bool
    result: Mapping[str, Any]


class CanonicalDynamicToolHost(Protocol):
    @property
    def supported_stages(self) -> tuple[AgentExtractionStage, ...]: ...

    @property
    def specs(self) -> tuple[CanonicalDynamicToolSpec, ...]: ...

    def invoke(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> CanonicalDynamicToolExecution: ...


class CanonicalStructureRevisionPublisher(Protocol):
    def publish(
        self,
        submission: CanonicalStructureRevisionSubmissionInputV1,
    ) -> Mapping[str, Any]: ...


class CanonicalStructureRevisionPublishError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class CanonicalD2StructureDynamicToolHost:
    """High-level D2 Host tools exposed through App Server dynamic tools."""

    def __init__(
        self,
        *,
        evidence_host: D2EvidenceToolHost,
        failure_pattern_host: StructureFailurePatternHost,
        revision_publisher: CanonicalStructureRevisionPublisher | None = None,
    ) -> None:
        self._evidence_host = evidence_host
        self._failure_pattern_host = failure_pattern_host
        self._revision_publisher = revision_publisher
        self._specs = _build_specs(
            enable_revision_publish=revision_publisher is not None
        )

    @property
    def specs(self) -> tuple[CanonicalDynamicToolSpec, ...]:
        return self._specs

    @property
    def supported_stages(self) -> tuple[AgentExtractionStage, ...]:
        return (AgentExtractionStage.LOGICAL_STRUCTURE,)

    def invoke(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> CanonicalDynamicToolExecution:
        try:
            if tool_name == TOOL_D2_GET_LOGICAL_TABLE:
                dynamic_request = CanonicalD2GetLogicalTableInputV1.model_validate(
                    arguments
                )
                request = D2LogicalTableEvidenceInputV1(
                    context=self._evidence_host.expected_context(),
                    logical_table_scope=dynamic_request.logical_table_scope,
                    context_nodes_per_side=dynamic_request.context_nodes_per_side,
                )
                result = self._evidence_host.get_logical_table(request).model_dump(
                    mode="json"
                )
            elif tool_name == TOOL_D2_RESOLVE_SOURCE_HANDLES:
                dynamic_request = CanonicalD2ResolveSourceHandlesInputV1.model_validate(
                    arguments
                )
                request = D2ResolveSourceHandlesInputV1(
                    context=self._evidence_host.expected_context(),
                    selections=dynamic_request.selections,
                )
                result = self._evidence_host.resolve_source_handles(request).model_dump(
                    mode="json"
                )
            elif tool_name == TOOL_D2_QUERY_RESOLVER_SUGGESTIONS:
                dynamic_request = CanonicalD2ResolverSuggestionsInputV1.model_validate(
                    arguments
                )
                request = D2ResolverSuggestionsInputV1(
                    context=self._evidence_host.expected_context(),
                    physical_table_ids=dynamic_request.physical_table_ids,
                )
                result = self._evidence_host.query_resolver_suggestions(
                    request
                ).model_dump(mode="json")
            elif tool_name == TOOL_STRUCTURE_SELECT_FAILURE_PATTERNS:
                request = StructureFailurePatternSignalsInputV1.model_validate(
                    arguments
                )
                result = self._failure_pattern_host.select(request.signals)
            elif tool_name == TOOL_STRUCTURE_VALIDATE_MATERIALIZATION:
                request = StructureMaterializationValidationInputV1.model_validate(
                    arguments
                )
                validation = self._failure_pattern_host.validate_and_select(
                    request.observation
                )
                result = {
                    "kit_version": validation.kit_version,
                    "kit_bundle_sha256": validation.kit_bundle_sha256,
                    "validation_report": validation.validation_report,
                    "observed_signals": list(validation.observed_signals),
                    "selected_failure_patterns": (validation.selected_failure_patterns),
                }
            elif tool_name == TOOL_STRUCTURE_PUBLISH_REVISION:
                if self._revision_publisher is None:
                    return _failure("dynamic_revision_publisher_unavailable")
                request = CanonicalStructureRevisionSubmissionInputV1.model_validate(
                    arguments
                )
                result = self._revision_publisher.publish(request)
            else:
                return _failure("dynamic_tool_unknown")
        except ValidationError:
            return _failure("dynamic_tool_arguments_invalid")
        except CanonicalStructureRevisionPublishError as exc:
            return _failure(exc.reason_code)
        except D2EvidenceToolError as exc:
            return _failure(exc.reason_code)
        return _bounded_success(tool_name, result)


def canonical_dynamic_tool_specs_sha256(
    specs: tuple[CanonicalDynamicToolSpec, ...],
) -> str:
    return _canonical_sha256([item.app_server_payload() for item in specs])


def _build_specs(
    *,
    enable_revision_publish: bool = False,
) -> tuple[CanonicalDynamicToolSpec, ...]:
    specs = [
        CanonicalDynamicToolSpec(
            name=TOOL_D2_GET_LOGICAL_TABLE,
            description=(
                "Read one Agent-selected logical table with exact physical cells "
                "and neighboring source evidence. Optional; source evidence is "
                "immutable and the tool never decides the table boundary."
            ),
            input_schema=CanonicalD2GetLogicalTableInputV1.model_json_schema(),
        ),
        CanonicalDynamicToolSpec(
            name=TOOL_D2_RESOLVE_SOURCE_HANDLES,
            description=(
                "Resolve a batch of selected source handles or exact ranges to "
                "Host-bound text and locators. Use only when exact wording is "
                "needed; one call may resolve many selections."
            ),
            input_schema=(CanonicalD2ResolveSourceHandlesInputV1.model_json_schema()),
        ),
        CanonicalDynamicToolSpec(
            name=TOOL_D2_QUERY_RESOLVER_SUGGESTIONS,
            description=(
                "Read optional resolver suggestions for selected physical tables. "
                "Suggestions are advisory and may be rejected or replaced by the "
                "Agent after checking source evidence."
            ),
            input_schema=CanonicalD2ResolverSuggestionsInputV1.model_json_schema(),
        ),
        CanonicalDynamicToolSpec(
            name=TOOL_STRUCTURE_SELECT_FAILURE_PATTERNS,
            description=(
                "Given current observed failure signals, return at most three "
                "relevant decision reminders. Do not call without a current signal."
            ),
            input_schema=StructureFailurePatternSignalsInputV1.model_json_schema(),
        ),
        CanonicalDynamicToolSpec(
            name=TOOL_STRUCTURE_VALIDATE_MATERIALIZATION,
            description=(
                "Validate the current changed-scope materialization observation. "
                "A failed result requires revision or explicit unresolved status; "
                "it never consumes the change automatically."
            ),
            input_schema=(
                StructureMaterializationValidationInputV1.model_json_schema()
            ),
        ),
    ]
    if enable_revision_publish:
        specs.append(
            CanonicalDynamicToolSpec(
                name=TOOL_STRUCTURE_PUBLISH_REVISION,
                description=(
                    "Publish one complete Agent-authored replacement candidate for "
                    "the Host-bound structure handoff. The Host injects source, "
                    "artifact, handoff, attempt, and persistence identities; validates "
                    "the operations; persists an immutable receipt and public Activity; "
                    "and keeps the result verification-required until the rendered "
                    "scope is reopened and checked. Never use this to edit source facts."
                ),
                input_schema=(
                    CanonicalStructureRevisionSubmissionInputV1.model_json_schema()
                ),
            )
        )
    return tuple(specs)


def _bounded_success(
    tool_name: str,
    result: Mapping[str, Any],
) -> CanonicalDynamicToolExecution:
    envelope = {
        "schema_version": CANONICAL_DYNAMIC_TOOL_RESULT_SCHEMA_VERSION,
        "tool_name": tool_name,
        "authority": "HOST_EVIDENCE_OR_VALIDATION",
        "result": dict(result),
    }
    encoded = _canonical_json(envelope).encode("utf-8")
    if len(encoded) > MAX_CANONICAL_DYNAMIC_TOOL_RESULT_BYTES:
        return _failure(
            "dynamic_tool_result_too_large",
            result_sha256=hashlib.sha256(encoded).hexdigest(),
        )
    return CanonicalDynamicToolExecution(success=True, result=envelope)


def _failure(
    reason_code: str,
    *,
    result_sha256: str | None = None,
) -> CanonicalDynamicToolExecution:
    result: dict[str, Any] = {
        "schema_version": CANONICAL_DYNAMIC_TOOL_RESULT_SCHEMA_VERSION,
        "authority": "HOST_FAILURE",
        "reason_code": reason_code,
    }
    if result_sha256 is not None:
        result["result_sha256"] = result_sha256
    return CanonicalDynamicToolExecution(success=False, result=result)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


__all__ = [
    "CANONICAL_DYNAMIC_TOOL_RESULT_SCHEMA_VERSION",
    "MAX_CANONICAL_DYNAMIC_TOOL_RESULT_BYTES",
    "CanonicalD2StructureDynamicToolHost",
    "CanonicalStructureRevisionPublisher",
    "CanonicalStructureRevisionPublishError",
    "CanonicalStructureRevisionDraftV1",
    "CanonicalStructureRevisionSubmissionInputV1",
    "CanonicalDynamicToolExecution",
    "CanonicalDynamicToolHost",
    "CanonicalDynamicToolSpec",
    "TOOL_D2_GET_LOGICAL_TABLE",
    "TOOL_D2_QUERY_RESOLVER_SUGGESTIONS",
    "TOOL_D2_RESOLVE_SOURCE_HANDLES",
    "TOOL_STRUCTURE_SELECT_FAILURE_PATTERNS",
    "TOOL_STRUCTURE_PUBLISH_REVISION",
    "TOOL_STRUCTURE_VALIDATE_MATERIALIZATION",
    "canonical_dynamic_tool_specs_sha256",
]

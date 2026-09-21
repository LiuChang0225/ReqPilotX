from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Any, Literal, TypeVar
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    ValidationError,
    model_validator,
)

from reqpilotx_server.domain.extraction.semantic_provider import (
    SemanticProviderRunRecord,
    SemanticProviderRunStatus,
    stable_semantic_provider_run_id,
)
from reqpilotx_server.domain.extraction.requirement_assembly import RequirementAssemblyStatus
from reqpilotx_server.domain.extraction.requirement_view import RequirementProjectionRole
from reqpilotx_server.domain.extraction.semantics import RequirementSubtype, SemanticClass
from reqpilotx_server.domain.extraction.structure import (
    StructureNodeKind,
    StructureRelationKind,
    StructureSourceRef,
)
from reqpilotx_server.domain.extraction.structure_revision import (
    AddStructureRelationOperationV1,
    BoundarySplitPartV1,
    BoundarySplitStructureOperationV1,
    ContinuationMergeStructureOperationV1,
    LogicalReorderStructureOperationV1,
    ReparentStructureOperationV1,
    StructurePatchOperationV1,
    StructureSourceSpanV1,
)
from reqpilotx_server.ports.semantic_provider import (
    RequirementAssemblyDecision,
    RequirementAssemblyProviderResult,
    RequirementAssemblyRequest,
    SemanticClassificationProviderResult,
    SemanticClassificationRequest,
    SemanticDecision,
    SemanticNodeInput,
    SemanticProviderRequestRejected,
    SemanticProviderRequestRejectionCode,
)
from reqpilotx_server.ports.structure_audit_provider import (
    StructureAuditCategory,
    StructureAuditDecision,
    StructureAuditProposalEvidence,
    StructureAuditProviderResult,
    StructureAuditRequest,
)


BAILIAN_SEMANTIC_PROVIDER_ID = "aliyun-bailian-qwen-semantic"
BAILIAN_SEMANTIC_PROVIDER_IMPLEMENTATION_VERSION = "2.18.1"
DEFAULT_BAILIAN_SEMANTIC_MODEL = "qwen3.7-plus"
DEFAULT_BAILIAN_EVALUATION_MODEL = "qwen3.7-plus-2026-05-26"
DEFAULT_BAILIAN_SEMANTIC_SEED = 1234
_MAX_BAILIAN_SEMANTIC_SEED = 2**31 - 1
BAILIAN_SEMANTIC_PROVIDER_VERSION = (
    f"{BAILIAN_SEMANTIC_PROVIDER_IMPLEMENTATION_VERSION};"
    f"s={DEFAULT_BAILIAN_SEMANTIC_SEED};t=0;think=0;stream=1"
)
SEMANTIC_CLASSIFICATION_PROMPT_VERSION = "reqpilotx-semantic-classification/v15"
REQUIREMENT_ASSEMBLY_PROMPT_VERSION = "reqpilotx-requirement-assembly/v12"
STRUCTURE_AUDIT_PROMPT_VERSION = "reqpilotx-structure-audit/v5"
SEMANTIC_CLASSIFICATION_RESPONSE_SCHEMA = "qwen-semantic-classification/v6"
REQUIREMENT_ASSEMBLY_RESPONSE_SCHEMA = "qwen-requirement-assembly/v2"
STRUCTURE_AUDIT_RESPONSE_SCHEMA = "qwen-structure-audit/v5"

_MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_STRUCTURE_NODE_ID_PATTERN = r"^structure-node-[0-9a-f]{32}$"
_AUDIT_NODE_ALIAS_PATTERN = r"^n(?:0|[1-9][0-9]{0,3})$"
_AUDIT_EVIDENCE_ALIAS_PATTERN = r"^e(?:0|[1-9][0-9]{0,3})$"
_AUDIT_EXPLICIT_BOUNDARY_PREFIX = re.compile(
    r"^\s*(?:"
    r"\d+(?:[.．]\d+)+(?:[.．])?(?=\s|\D)"
    r"|\d+\s+\S"
    r"|[（(]\d+[）)]\s*\S"
    r"|[一二三四五六七八九十百]+[、.．]\s*\S"
    r"|[表图]\s*\d+\s+\S"
    r")"
)
_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_MAX_TOTAL_IMAGE_BYTES = 24 * 1024 * 1024
_MAX_TOTAL_IMAGE_PIXELS = 3_000_000
_MAX_REQUEST_WIRE_BYTES = 10 * 1024 * 1024
_MAX_STREAM_RESPONSE_BYTES = 16 * 1024 * 1024
_MAX_EVIDENCE_UTF8_BYTES = 128 * 1024
_ALLOWED_DASHSCOPE_HOSTS = frozenset({"dashscope.aliyuncs.com"})
_RETRYABLE_INVALID_OUTPUT_CODES = frozenset(
    {
        "provider_response_envelope_invalid",
        "provider_response_content_missing",
        "provider_output_json_invalid",
        "provider_output_schema_invalid",
        "provider_output_truncated",
    }
)


class _StrictPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _SemanticDecisionWire(_StrictPayload):
    target_structure_node_id: str = Field(
        min_length=1,
        max_length=160,
        pattern=_STRUCTURE_NODE_ID_PATTERN,
    )
    semantic_class: SemanticClass
    requirement_subtype: RequirementSubtype | None = None
    is_continuation_fragment: StrictBool
    evidence_indexes: tuple[Annotated[StrictInt, Field(ge=0)], ...] = Field(
        min_length=1
    )
    reason_codes: tuple[str, ...] | None = None


class _AssemblyDecisionWire(_StrictPayload):
    target_structure_node_id: str = Field(
        min_length=1,
        max_length=160,
        pattern=_STRUCTURE_NODE_ID_PATTERN,
    )
    include_in_view: StrictBool
    role: RequirementProjectionRole | None = None
    parent_structure_node_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=160,
        pattern=_STRUCTURE_NODE_ID_PATTERN,
    )
    status: RequirementAssemblyStatus
    reason_codes: tuple[str, ...] | None = None


class _ClassificationPayload(_StrictPayload):
    decisions: tuple[_SemanticDecisionWire, ...]


class _AssemblyPayload(_StrictPayload):
    decisions: tuple[_AssemblyDecisionWire, ...]


class _StructureAuditEvidenceWire(_StrictPayload):
    node_alias: str = Field(
        min_length=1,
        max_length=8,
        pattern=_AUDIT_NODE_ALIAS_PATTERN,
    )
    evidence_aliases: tuple[
        Annotated[
            str,
            Field(
                min_length=1,
                max_length=8,
                pattern=_AUDIT_EVIDENCE_ALIAS_PATTERN,
            ),
        ],
        ...,
    ] = Field(min_length=1)


class _StructureAuditReparentProposalWire(_StrictPayload):
    operation: Literal["re_parent"]
    new_parent_node_alias: str = Field(
        min_length=1,
        max_length=8,
        pattern=_AUDIT_NODE_ALIAS_PATTERN,
    )
    new_sibling_index: Annotated[StrictInt, Field(ge=0)]
    evidence_level: StructureAuditProposalEvidence


class _StructureAuditLogicalReorderProposalWire(_StrictPayload):
    operation: Literal["logical_reorder"]
    parent_node_alias: str = Field(
        min_length=1,
        max_length=8,
        pattern=_AUDIT_NODE_ALIAS_PATTERN,
    )
    ordered_child_node_aliases: tuple[
        Annotated[
            str,
            Field(
                min_length=1,
                max_length=8,
                pattern=_AUDIT_NODE_ALIAS_PATTERN,
            ),
        ],
        ...,
    ] = Field(min_length=1)
    evidence_level: StructureAuditProposalEvidence


class _StructureAuditSourceSpanWire(_StrictPayload):
    evidence_alias: str = Field(
        min_length=1,
        max_length=8,
        pattern=_AUDIT_EVIDENCE_ALIAS_PATTERN,
    )
    start_offset: Annotated[StrictInt, Field(ge=0)]
    end_offset: Annotated[StrictInt, Field(ge=1)]

    @model_validator(mode="after")
    def validate_interval(self) -> _StructureAuditSourceSpanWire:
        if self.end_offset <= self.start_offset:
            raise ValueError("audit split spans require a non-empty interval")
        return self


class _StructureAuditBoundarySplitPartWire(_StrictPayload):
    source_spans: tuple[_StructureAuditSourceSpanWire, ...] = Field(min_length=1)


class _StructureAuditBoundarySplitProposalWire(_StrictPayload):
    operation: Literal["boundary_split"]
    parts: tuple[_StructureAuditBoundarySplitPartWire, ...] = Field(
        min_length=2,
        max_length=64,
    )
    evidence_level: StructureAuditProposalEvidence


class _StructureAuditContinuationMergeProposalWire(_StrictPayload):
    operation: Literal["continuation_merge"]
    primary_node_alias: str = Field(
        min_length=1,
        max_length=8,
        pattern=_AUDIT_NODE_ALIAS_PATTERN,
    )
    continuation_node_alias: str = Field(
        min_length=1,
        max_length=8,
        pattern=_AUDIT_NODE_ALIAS_PATTERN,
    )
    evidence_level: StructureAuditProposalEvidence


class _StructureAuditAddContinuationProposalWire(_StrictPayload):
    operation: Literal["add_continuation"]
    source_node_alias: str = Field(
        min_length=1,
        max_length=8,
        pattern=_AUDIT_NODE_ALIAS_PATTERN,
    )
    target_node_alias: str = Field(
        min_length=1,
        max_length=8,
        pattern=_AUDIT_NODE_ALIAS_PATTERN,
    )
    evidence_level: StructureAuditProposalEvidence


_StructureAuditProposalWire = Annotated[
    _StructureAuditReparentProposalWire
    | _StructureAuditLogicalReorderProposalWire
    | _StructureAuditBoundarySplitProposalWire
    | _StructureAuditContinuationMergeProposalWire
    | _StructureAuditAddContinuationProposalWire,
    Field(discriminator="operation"),
]


class _StructureAuditFindingWire(_StrictPayload):
    category: StructureAuditCategory
    target_node_alias: str = Field(
        min_length=1,
        max_length=8,
        pattern=_AUDIT_NODE_ALIAS_PATTERN,
    )
    evidence: tuple[_StructureAuditEvidenceWire, ...] = Field(min_length=1)
    reason_codes: tuple[str, ...] = Field(min_length=1)
    proposal: _StructureAuditProposalWire | None = None


class _StructureAuditPayload(_StrictPayload):
    findings: tuple[_StructureAuditFindingWire, ...] = Field(max_length=1)


_PayloadT = TypeVar("_PayloadT", bound=_StrictPayload)


class _InvocationResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    payload: Any | None
    run: SemanticProviderRunRecord


@dataclass(frozen=True)
class _BufferedProviderResponse:
    status_code: int
    payload: dict[str, object] | None
    content: bytes

    def json(self) -> dict[str, object]:
        if self.payload is None:
            raise ValueError("provider response did not contain a valid JSON envelope")
        return self.payload


@dataclass(frozen=True)
class _StructureAuditWireBinding:
    node_alias_by_id: dict[str, str]
    node_id_by_alias: dict[str, str]
    target_aliases: frozenset[str]
    node_kind_by_alias: dict[str, StructureNodeKind]
    parent_alias_by_alias: dict[str, str | None]
    allowed_categories_by_alias: dict[
        str,
        tuple[StructureAuditCategory, ...],
    ]
    evidence_ref_by_alias: dict[tuple[str, str], StructureSourceRef]
    evidence_text_by_alias: dict[tuple[str, str], str]
    evidence_aliases_by_node_alias: dict[str, tuple[str, ...]]


class BailianQwenSemanticProvider:
    """Fail-closed, two-pass qwen3.7-plus semantic provider.

    Customer text and images exist only in the in-memory request.  The returned
    audit record contains hashes and usage metadata, never provider payloads.
    """

    provider_id = BAILIAN_SEMANTIC_PROVIDER_ID
    provider_version = BAILIAN_SEMANTIC_PROVIDER_VERSION
    classification_prompt_version = SEMANTIC_CLASSIFICATION_PROMPT_VERSION
    classification_response_schema_version = SEMANTIC_CLASSIFICATION_RESPONSE_SCHEMA
    assembly_prompt_version = REQUIREMENT_ASSEMBLY_PROMPT_VERSION
    assembly_response_schema_version = REQUIREMENT_ASSEMBLY_RESPONSE_SCHEMA

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str = DEFAULT_BAILIAN_SEMANTIC_MODEL,
        seed: int = DEFAULT_BAILIAN_SEMANTIC_SEED,
        timeout_seconds: float = 120.0,
        max_retries: int = 2,
        http_client: Any | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        normalized_model = str(model or "").strip()
        if not _MODEL_PATTERN.fullmatch(normalized_model):
            raise ValueError("Bailian semantic model has an invalid identifier")
        if (
            isinstance(seed, bool)
            or not isinstance(seed, int)
            or not 0 <= seed <= _MAX_BAILIAN_SEMANTIC_SEED
        ):
            raise ValueError(
                "Bailian semantic seed must be an integer between zero and 2^31 - 1"
            )
        if isinstance(timeout_seconds, bool) or float(timeout_seconds) <= 0:
            raise ValueError("timeout_seconds must be positive")
        if isinstance(max_retries, bool) or not 0 <= int(max_retries) <= 5:
            raise ValueError("max_retries must be between zero and five")
        self._api_key = str(api_key or "").strip()
        self._endpoint = _chat_completions_endpoint(base_url)
        self.model_id = normalized_model
        self.sampling_seed = seed
        self.provider_version = (
            f"{BAILIAN_SEMANTIC_PROVIDER_IMPLEMENTATION_VERSION};"
            f"s={seed};t=0;think=0;stream=1"
        )
        self._timeout_seconds = float(timeout_seconds)
        self._max_retries = int(max_retries)
        self._http_client = http_client
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic
        self._sleep = sleep

    def preflight_classification_request(
        self,
        request: SemanticClassificationRequest,
    ) -> None:
        """Validate every local Bailian request budget without transport."""

        if not isinstance(request, SemanticClassificationRequest):
            raise TypeError("request must be a SemanticClassificationRequest")
        _validate_evidence_budget((*request.context_nodes, *request.nodes))
        _provider_request_payload(
            model_id=self.model_id,
            seed=self.sampling_seed,
            system_prompt=_classification_system_prompt(),
            user_payload=_classification_user_payload(request),
            images=request.images,
            payload_type=_ClassificationPayload,
        )

    def classify_nodes(
        self,
        request: SemanticClassificationRequest,
    ) -> SemanticClassificationProviderResult:
        if not isinstance(request, SemanticClassificationRequest):
            raise TypeError("request must be a SemanticClassificationRequest")
        _validate_evidence_budget((*request.context_nodes, *request.nodes))
        user_payload = _classification_user_payload(request)
        invocation = self._invoke_with_invalid_output_retry(
            operation="classify_nodes",
            prompt_version=SEMANTIC_CLASSIFICATION_PROMPT_VERSION,
            response_schema_version=SEMANTIC_CLASSIFICATION_RESPONSE_SCHEMA,
            system_prompt=_classification_system_prompt(),
            user_payload=user_payload,
            images=request.images,
            payload_type=_ClassificationPayload,
        )
        nodes_by_target = {
            node.target_structure_node_id: node for node in request.nodes
        }
        decisions = (
            tuple(
                _semantic_wire_to_domain(
                    item,
                    nodes_by_target.get(item.target_structure_node_id),
                )
                for item in invocation.payload.decisions
            )
            if isinstance(invocation.payload, _ClassificationPayload)
            else ()
        )
        return SemanticClassificationProviderResult(
            decisions=decisions,
            run=invocation.run,
        )

    def preflight_assembly_request(
        self,
        request: RequirementAssemblyRequest,
    ) -> None:
        """Validate the local Bailian assembly envelope without transport."""

        if not isinstance(request, RequirementAssemblyRequest):
            raise TypeError("request must be a RequirementAssemblyRequest")
        _validate_evidence_budget(request.nodes)
        _provider_request_payload(
            model_id=self.model_id,
            seed=self.sampling_seed,
            system_prompt=_assembly_system_prompt(),
            user_payload=_assembly_user_payload(
                request,
                constraints=_assembly_budget_constraints(request),
            ),
            images=(),
            payload_type=_AssemblyPayload,
        )

    def assemble_requirements(
        self,
        request: RequirementAssemblyRequest,
    ) -> RequirementAssemblyProviderResult:
        if not isinstance(request, RequirementAssemblyRequest):
            raise TypeError("request must be a RequirementAssemblyRequest")
        _validate_evidence_budget(request.nodes)
        constraints = _assembly_constraints(request)
        user_payload = _assembly_user_payload(request)
        invocation = self._invoke_with_invalid_output_retry(
            operation="assemble_requirements",
            prompt_version=REQUIREMENT_ASSEMBLY_PROMPT_VERSION,
            response_schema_version=REQUIREMENT_ASSEMBLY_RESPONSE_SCHEMA,
            system_prompt=_assembly_system_prompt(),
            user_payload=user_payload,
            images=(),
            payload_type=_AssemblyPayload,
        )
        decisions = (
            tuple(
                _validate_assembly_decision_against_constraints(
                    _assembly_wire_to_domain(item),
                    constraints.get(item.target_structure_node_id),
                )
                for item in invocation.payload.decisions
            )
            if isinstance(invocation.payload, _AssemblyPayload)
            else ()
        )
        return RequirementAssemblyProviderResult(
            decisions=decisions,
            run=invocation.run,
        )

    def preflight_structure_audit(
        self,
        request: StructureAuditRequest,
    ) -> None:
        """Validate one structure-audit envelope without remote transport."""

        if not isinstance(request, StructureAuditRequest):
            raise TypeError("request must be a StructureAuditRequest")
        evidence_request = request.evidence_request
        _validate_evidence_budget(
            (*evidence_request.context_nodes, *evidence_request.nodes)
        )
        _provider_request_payload(
            model_id=self.model_id,
            seed=self.sampling_seed,
            system_prompt=_structure_audit_system_prompt(),
            user_payload=_structure_audit_user_payload(request),
            images=evidence_request.images,
            payload_type=_StructureAuditPayload,
        )

    def audit_structure(
        self,
        request: StructureAuditRequest,
    ) -> StructureAuditProviderResult:
        """Report source-bound suspicions without changing structure facts."""

        if not isinstance(request, StructureAuditRequest):
            raise TypeError("request must be a StructureAuditRequest")
        evidence_request = request.evidence_request
        _validate_evidence_budget(
            (*evidence_request.context_nodes, *evidence_request.nodes)
        )
        invocation = self._invoke_with_invalid_output_retry(
            operation="audit_structure",
            prompt_version=STRUCTURE_AUDIT_PROMPT_VERSION,
            response_schema_version=STRUCTURE_AUDIT_RESPONSE_SCHEMA,
            system_prompt=_structure_audit_system_prompt(),
            user_payload=_structure_audit_user_payload(request),
            images=evidence_request.images,
            payload_type=_StructureAuditPayload,
        )
        if not isinstance(invocation.payload, _StructureAuditPayload):
            return StructureAuditProviderResult(findings=(), run=invocation.run)
        findings: list[StructureAuditDecision] = []
        identities: set[
            tuple[
                StructureAuditCategory,
                str,
                tuple[str, ...],
            ]
        ] = set()
        rejected_finding_count = 0
        for item in invocation.payload.findings:
            try:
                finding = _structure_audit_wire_to_domain(item, request=request)
                identity = (
                    finding.category,
                    finding.target_structure_node_id,
                    finding.related_structure_node_ids,
                )
                if identity in identities:
                    raise ValueError("structure audit finding is duplicated")
            except (IndexError, TypeError, ValueError):
                rejected_finding_count += 1
                continue
            identities.add(identity)
            findings.append(finding)
        run = (
            _partial_structure_audit_run(invocation.run)
            if rejected_finding_count
            else invocation.run
        )
        return StructureAuditProviderResult(
            findings=tuple(findings),
            run=run,
            rejected_finding_count=rejected_finding_count,
        )

    def _invoke(
        self,
        *,
        operation: str,
        prompt_version: str,
        response_schema_version: str,
        system_prompt: str,
        user_payload: dict[str, object],
        images: tuple[Any, ...],
        payload_type: type[_PayloadT],
    ) -> _InvocationResult:
        started_at = self._clock()
        started_monotonic = self._monotonic()
        request_payload = _provider_request_payload(
            model_id=self.model_id,
            seed=self.sampling_seed,
            system_prompt=system_prompt,
            user_payload=user_payload,
            images=images,
            payload_type=payload_type,
        )
        request_sha256 = _canonical_sha256(request_payload)
        if not self._api_key or not self._api_key.isascii():
            return _InvocationResult(
                payload=None,
                run=self._run_record(
                    operation=operation,
                    prompt_version=prompt_version,
                    response_schema_version=response_schema_version,
                    request_sha256=request_sha256,
                    response_sha256=None,
                    status=SemanticProviderRunStatus.UNAVAILABLE,
                    error_code="provider_not_configured",
                    started_at=started_at,
                    started_monotonic=started_monotonic,
                    request_count=0,
                    retry_count=0,
                ),
            )

        response: Any | None = None
        request_count = 0
        retry_count = 0
        try:
            for attempt in range(self._max_retries + 1):
                request_count += 1
                try:
                    response = self._post(request_payload)
                except (httpx.TimeoutException, TimeoutError):
                    if attempt >= self._max_retries:
                        return _InvocationResult(
                            payload=None,
                            run=self._run_record(
                                operation=operation,
                                prompt_version=prompt_version,
                                response_schema_version=response_schema_version,
                                request_sha256=request_sha256,
                                response_sha256=None,
                                status=SemanticProviderRunStatus.TIMEOUT,
                                error_code="provider_timeout",
                                started_at=started_at,
                                started_monotonic=started_monotonic,
                                request_count=request_count,
                                retry_count=retry_count,
                            ),
                        )
                    retry_count += 1
                    self._sleep(float(2**attempt))
                    continue
                except httpx.TransportError:
                    if attempt >= self._max_retries:
                        return _InvocationResult(
                            payload=None,
                            run=self._run_record(
                                operation=operation,
                                prompt_version=prompt_version,
                                response_schema_version=response_schema_version,
                                request_sha256=request_sha256,
                                response_sha256=None,
                                status=SemanticProviderRunStatus.UNAVAILABLE,
                                error_code="provider_transport_error",
                                started_at=started_at,
                                started_monotonic=started_monotonic,
                                request_count=request_count,
                                retry_count=retry_count,
                            ),
                        )
                    retry_count += 1
                    self._sleep(float(2**attempt))
                    continue
                status_code = int(getattr(response, "status_code", 0))
                if status_code == 429 or status_code >= 500:
                    if attempt < self._max_retries:
                        retry_count += 1
                        self._sleep(float(2**attempt))
                        continue
                    return _InvocationResult(
                        payload=None,
                        run=self._run_record(
                            operation=operation,
                            prompt_version=prompt_version,
                            response_schema_version=response_schema_version,
                            request_sha256=request_sha256,
                            response_sha256=None,
                            status=SemanticProviderRunStatus.UNAVAILABLE,
                            error_code="provider_http_retry_exhausted",
                            started_at=started_at,
                            started_monotonic=started_monotonic,
                            request_count=request_count,
                            retry_count=retry_count,
                        ),
                    )
                if status_code < 200 or status_code >= 300:
                    return _InvocationResult(
                        payload=None,
                        run=self._run_record(
                            operation=operation,
                            prompt_version=prompt_version,
                            response_schema_version=response_schema_version,
                            request_sha256=request_sha256,
                            response_sha256=None,
                            status=SemanticProviderRunStatus.UNAVAILABLE,
                            error_code="provider_http_error",
                            started_at=started_at,
                            started_monotonic=started_monotonic,
                            request_count=request_count,
                            retry_count=retry_count,
                        ),
                    )
                break
        except httpx.HTTPError:
            return _InvocationResult(
                payload=None,
                run=self._run_record(
                    operation=operation,
                    prompt_version=prompt_version,
                    response_schema_version=response_schema_version,
                    request_sha256=request_sha256,
                    response_sha256=None,
                    status=SemanticProviderRunStatus.UNAVAILABLE,
                    error_code="provider_transport_error",
                    started_at=started_at,
                    started_monotonic=started_monotonic,
                    request_count=max(request_count, 1),
                    retry_count=retry_count,
                ),
            )

        assert response is not None
        try:
            response_payload = response.json()
            response_sha256 = _canonical_sha256(response_payload)
        except (TypeError, ValueError):
            return _InvocationResult(
                payload=None,
                run=self._run_record(
                    operation=operation,
                    prompt_version=prompt_version,
                    response_schema_version=response_schema_version,
                    request_sha256=request_sha256,
                    response_sha256=_safe_response_sha256(response),
                    status=SemanticProviderRunStatus.INVALID_OUTPUT,
                    error_code="provider_response_envelope_invalid",
                    started_at=started_at,
                    started_monotonic=started_monotonic,
                    request_count=request_count,
                    retry_count=retry_count,
                ),
            )
        if not isinstance(response_payload, dict):
            return _InvocationResult(
                payload=None,
                run=self._run_record(
                    operation=operation,
                    prompt_version=prompt_version,
                    response_schema_version=response_schema_version,
                    request_sha256=request_sha256,
                    response_sha256=response_sha256,
                    status=SemanticProviderRunStatus.INVALID_OUTPUT,
                    error_code="provider_response_envelope_invalid",
                    started_at=started_at,
                    started_monotonic=started_monotonic,
                    request_count=request_count,
                    retry_count=retry_count,
                ),
            )
        input_tokens, output_tokens = _usage_tokens(response_payload)
        finish_error = _finish_reason_error(response_payload)
        if finish_error is not None:
            return _InvocationResult(
                payload=None,
                run=self._run_record(
                    operation=operation,
                    prompt_version=prompt_version,
                    response_schema_version=response_schema_version,
                    request_sha256=request_sha256,
                    response_sha256=response_sha256,
                    status=SemanticProviderRunStatus.INVALID_OUTPUT,
                    error_code=finish_error,
                    started_at=started_at,
                    started_monotonic=started_monotonic,
                    request_count=request_count,
                    retry_count=retry_count,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                ),
            )
        try:
            content_text = _response_content(response_payload)
        except (TypeError, ValueError, KeyError):
            return _InvocationResult(
                payload=None,
                run=self._run_record(
                    operation=operation,
                    prompt_version=prompt_version,
                    response_schema_version=response_schema_version,
                    request_sha256=request_sha256,
                    response_sha256=response_sha256,
                    status=SemanticProviderRunStatus.INVALID_OUTPUT,
                    error_code=(finish_error or "provider_response_content_missing"),
                    started_at=started_at,
                    started_monotonic=started_monotonic,
                    request_count=request_count,
                    retry_count=retry_count,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                ),
            )
        try:
            parsed_json = json.loads(content_text)
        except (TypeError, ValueError, json.JSONDecodeError):
            return _InvocationResult(
                payload=None,
                run=self._run_record(
                    operation=operation,
                    prompt_version=prompt_version,
                    response_schema_version=response_schema_version,
                    request_sha256=request_sha256,
                    response_sha256=response_sha256,
                    status=SemanticProviderRunStatus.INVALID_OUTPUT,
                    error_code=finish_error or "provider_output_json_invalid",
                    started_at=started_at,
                    started_monotonic=started_monotonic,
                    request_count=request_count,
                    retry_count=retry_count,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                ),
            )
        try:
            parsed = payload_type.model_validate(parsed_json)
        except ValidationError:
            return _InvocationResult(
                payload=None,
                run=self._run_record(
                    operation=operation,
                    prompt_version=prompt_version,
                    response_schema_version=response_schema_version,
                    request_sha256=request_sha256,
                    response_sha256=response_sha256,
                    status=SemanticProviderRunStatus.INVALID_OUTPUT,
                    error_code=finish_error or "provider_output_schema_invalid",
                    started_at=started_at,
                    started_monotonic=started_monotonic,
                    request_count=request_count,
                    retry_count=retry_count,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                ),
            )
        return _InvocationResult(
            payload=parsed,
            run=self._run_record(
                operation=operation,
                prompt_version=prompt_version,
                response_schema_version=response_schema_version,
                request_sha256=request_sha256,
                response_sha256=response_sha256,
                status=SemanticProviderRunStatus.SUCCESS,
                error_code=None,
                started_at=started_at,
                started_monotonic=started_monotonic,
                request_count=request_count,
                retry_count=retry_count,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            ),
        )

    def _invoke_with_invalid_output_retry(
        self,
        *,
        operation: str,
        prompt_version: str,
        response_schema_version: str,
        system_prompt: str,
        user_payload: dict[str, object],
        images: tuple[Any, ...],
        payload_type: type[_PayloadT],
    ) -> _InvocationResult:
        kwargs = {
            "operation": operation,
            "prompt_version": prompt_version,
            "response_schema_version": response_schema_version,
            "system_prompt": system_prompt,
            "user_payload": user_payload,
            "images": images,
            "payload_type": payload_type,
        }
        first = self._invoke(**kwargs)
        if (
            self._max_retries < 1
            or first.run.status is not SemanticProviderRunStatus.INVALID_OUTPUT
            or first.run.error_code not in _RETRYABLE_INVALID_OUTPUT_CODES
        ):
            return first
        self._sleep(1.0)
        second = self._invoke(**kwargs)
        return _merge_invocation_attempts(first, second)

    def _post(self, payload: dict[str, object]) -> Any:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        if self._http_client is not None:
            if isinstance(self._http_client, httpx.Client):
                return _post_streaming_request(
                    self._http_client,
                    self._endpoint,
                    headers=headers,
                    payload=payload,
                    timeout_seconds=self._timeout_seconds,
                    monotonic=self._monotonic,
                )
            return self._http_client.post(
                self._endpoint,
                headers=headers,
                json=payload,
                timeout=self._timeout_seconds,
            )
        with httpx.Client(timeout=self._timeout_seconds) as client:
            return _post_streaming_request(
                client,
                self._endpoint,
                headers=headers,
                payload=payload,
                timeout_seconds=self._timeout_seconds,
                monotonic=self._monotonic,
            )

    def _run_record(
        self,
        *,
        operation: str,
        prompt_version: str,
        response_schema_version: str,
        request_sha256: str,
        response_sha256: str | None,
        status: SemanticProviderRunStatus,
        error_code: str | None,
        started_at: datetime,
        started_monotonic: float,
        request_count: int,
        retry_count: int,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> SemanticProviderRunRecord:
        finished_at = self._clock()
        latency_ms = max(0, int(round((self._monotonic() - started_monotonic) * 1000)))
        run_id = stable_semantic_provider_run_id(
            operation=operation,
            provider_id=self.provider_id,
            model_id=self.model_id,
            request_sha256=request_sha256,
            response_sha256=response_sha256,
            status=status,
            started_at=started_at,
        )
        return SemanticProviderRunRecord(
            provider_run_id=run_id,
            operation=operation,
            provider_id=self.provider_id,
            provider_version=self.provider_version,
            model_id=self.model_id,
            prompt_version=prompt_version,
            response_schema_version=response_schema_version,
            request_sha256=request_sha256,
            response_sha256=response_sha256,
            status=status,
            started_at=started_at,
            finished_at=finished_at,
            latency_ms=latency_ms,
            request_count=request_count,
            retry_count=retry_count,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            error_code=error_code,
        )


def _post_streaming_request(
    client: httpx.Client,
    endpoint: str,
    *,
    headers: dict[str, str],
    payload: dict[str, object],
    timeout_seconds: float,
    monotonic: Callable[[], float],
) -> _BufferedProviderResponse:
    deadline = monotonic() + timeout_seconds
    with client.stream(
        "POST",
        endpoint,
        headers=headers,
        json=payload,
        timeout=timeout_seconds,
    ) as response:
        if response.status_code < 200 or response.status_code >= 300:
            content = response.read()
            return _BufferedProviderResponse(
                status_code=response.status_code,
                payload=None,
                content=content[:_MAX_STREAM_RESPONSE_BYTES],
            )
        return _buffer_streaming_response(
            response,
            deadline=deadline,
            monotonic=monotonic,
        )


def _buffer_streaming_response(
    response: httpx.Response,
    *,
    deadline: float,
    monotonic: Callable[[], float],
) -> _BufferedProviderResponse:
    content_parts: list[str] = []
    raw_lines: list[bytes] = []
    raw_size = 0
    event_count = 0
    finish_reason: str | None = None
    usage: dict[str, object] | None = None
    envelope_fields: dict[str, object] = {}
    invalid = False

    lines = iter(response.iter_lines())
    while True:
        _raise_if_stream_deadline_exceeded(
            deadline=deadline,
            monotonic=monotonic,
        )
        try:
            raw_line = next(lines)
        except StopIteration:
            break
        _raise_if_stream_deadline_exceeded(
            deadline=deadline,
            monotonic=monotonic,
        )
        line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
        stripped = line.strip()
        if not stripped or stripped.startswith(":"):
            continue
        if not stripped.startswith("data:"):
            continue
        data = stripped[5:].strip()
        if data == "[DONE]":
            break
        encoded = data.encode("utf-8")
        raw_size += len(encoded)
        if raw_size > _MAX_STREAM_RESPONSE_BYTES:
            invalid = True
            break
        raw_lines.append(encoded)
        try:
            event = json.loads(data)
        except (TypeError, ValueError, json.JSONDecodeError):
            invalid = True
            break
        if not isinstance(event, dict) or event.get("error") is not None:
            invalid = True
            break
        event_count += 1
        for key in ("id", "model", "created", "system_fingerprint"):
            value = event.get(key)
            if value is not None and key not in envelope_fields:
                envelope_fields[key] = value
        event_usage = event.get("usage")
        if isinstance(event_usage, dict):
            usage = event_usage
        choices = event.get("choices")
        if not isinstance(choices, list) or not choices:
            continue
        choice = choices[0]
        if not isinstance(choice, dict):
            invalid = True
            break
        value = str(choice.get("finish_reason") or "").strip()
        if value:
            finish_reason = value
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            continue
        content = delta.get("content")
        if content is None:
            continue
        if not isinstance(content, str):
            invalid = True
            break
        content_parts.append(content)

    raw_content = b"\n".join(raw_lines)
    if invalid or event_count == 0:
        return _BufferedProviderResponse(
            status_code=response.status_code,
            payload=None,
            content=raw_content,
        )

    payload: dict[str, object] = {
        **envelope_fields,
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "".join(content_parts),
                },
                "finish_reason": finish_reason,
            }
        ],
        "_reqpilotx_stream_sha256": hashlib.sha256(raw_content).hexdigest(),
        "_reqpilotx_stream_event_count": event_count,
    }
    if usage is not None:
        payload["usage"] = usage
    return _BufferedProviderResponse(
        status_code=response.status_code,
        payload=payload,
        content=raw_content,
    )


def _raise_if_stream_deadline_exceeded(
    *,
    deadline: float,
    monotonic: Callable[[], float],
) -> None:
    if monotonic() >= deadline:
        raise httpx.ReadTimeout(
            "Bailian semantic streaming response exceeded its wall-clock timeout."
        )


def _merge_invocation_attempts(
    first: _InvocationResult,
    second: _InvocationResult,
) -> _InvocationResult:
    """Return one text-free audit record for an invalid-output repair retry."""

    first_run = first.run
    final_run = second.run
    if (
        first_run.operation != final_run.operation
        or first_run.provider_id != final_run.provider_id
        or first_run.model_id != final_run.model_id
        or first_run.request_sha256 != final_run.request_sha256
    ):
        raise RuntimeError("semantic retry attempts do not share one request identity")

    def total_optional(left: int | None, right: int | None) -> int | None:
        if left is None and right is None:
            return None
        return int(left or 0) + int(right or 0)

    started_at = first_run.started_at
    provider_run_id = stable_semantic_provider_run_id(
        operation=final_run.operation,
        provider_id=final_run.provider_id,
        model_id=final_run.model_id,
        request_sha256=final_run.request_sha256,
        response_sha256=final_run.response_sha256,
        status=final_run.status,
        started_at=started_at,
    )
    elapsed_ms = max(
        first_run.latency_ms + final_run.latency_ms,
        int(round((final_run.finished_at - started_at).total_seconds() * 1000)),
    )
    merged_run = final_run.model_copy(
        update={
            "provider_run_id": provider_run_id,
            "started_at": started_at,
            "latency_ms": max(elapsed_ms, 0),
            "request_count": first_run.request_count + final_run.request_count,
            "retry_count": first_run.retry_count + final_run.retry_count + 1,
            "input_tokens": total_optional(
                first_run.input_tokens,
                final_run.input_tokens,
            ),
            "output_tokens": total_optional(
                first_run.output_tokens,
                final_run.output_tokens,
            ),
        }
    )
    return _InvocationResult(payload=second.payload, run=merged_run)


def _partial_structure_audit_run(
    run: SemanticProviderRunRecord,
) -> SemanticProviderRunRecord:
    """Mark schema-valid output with rejected findings as partial coverage."""

    if run.status is not SemanticProviderRunStatus.SUCCESS:
        raise ValueError("only a successful audit response can become partial")
    status = SemanticProviderRunStatus.PARTIAL
    return run.model_copy(
        update={
            "provider_run_id": stable_semantic_provider_run_id(
                operation=run.operation,
                provider_id=run.provider_id,
                model_id=run.model_id,
                request_sha256=run.request_sha256,
                response_sha256=run.response_sha256,
                status=status,
                started_at=run.started_at,
            ),
            "status": status,
            "error_code": "provider_audit_finding_binding_partial",
        }
    )


def _chat_completions_endpoint(base_url: str) -> str:
    value = str(base_url or "").strip().rstrip("/")
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Bailian base_url has an invalid port") from exc
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.hostname not in _ALLOWED_DASHSCOPE_HOSTS
        or port not in {None, 443}
    ):
        raise ValueError("Bailian base_url must use an approved DashScope HTTPS origin")
    path = parsed.path.rstrip("/")
    if path.endswith("/chat/completions"):
        endpoint_path = path
    else:
        endpoint_path = f"{path}/chat/completions"
    return urlunsplit((parsed.scheme, parsed.netloc, endpoint_path, "", ""))


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _safe_response_sha256(response: Any) -> str | None:
    try:
        return _canonical_sha256(response.json())
    except Exception:
        content = bytes(getattr(response, "content", b""))
        return hashlib.sha256(content).hexdigest() if content else None


def _response_content(payload: dict[str, object]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("provider response has no choices")
    choice = choices[0]
    if not isinstance(choice, dict):
        raise ValueError("provider choice is invalid")
    message = choice.get("message")
    if not isinstance(message, dict):
        raise ValueError("provider message is invalid")
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content
    if isinstance(content, list):
        texts = [
            str(item.get("text"))
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        ]
        joined = "".join(texts).strip()
        if joined:
            return joined
    raise ValueError("provider message has no structured content")


def _usage_tokens(payload: dict[str, object]) -> tuple[int | None, int | None]:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None, None
    input_value = usage.get("prompt_tokens", usage.get("input_tokens"))
    output_value = usage.get("completion_tokens", usage.get("output_tokens"))
    input_tokens = (
        input_value
        if isinstance(input_value, int) and not isinstance(input_value, bool)
        else None
    )
    output_tokens = (
        output_value
        if isinstance(output_value, int) and not isinstance(output_value, bool)
        else None
    )
    return input_tokens, output_tokens


def _classification_node_payload(node: SemanticNodeInput) -> dict[str, object]:
    target_source_refs = set(node.target_source_refs)
    return {
        "target_structure_node_id": node.target_structure_node_id,
        "source_order": node.source_order,
        "kind": node.kind.value,
        "parent_structure_node_id": node.parent_structure_node_id,
        "ancestor_structure_node_ids": list(node.ancestor_structure_node_ids),
        "explicit_label": node.explicit_label,
        "list_ordinal": node.list_ordinal,
        "image_page_numbers": list(node.image_page_numbers),
        "provision_structure_node_ids": list(node.provision_structure_node_ids),
        "allowed_evidence_indexes": list(range(len(node.evidence))),
        "evidence": [
            {
                "evidence_index": index,
                "scope": (
                    "TARGET" if item.source_ref in target_source_refs else "CONTEXT"
                ),
                "source_ref": item.source_ref.model_dump(mode="json"),
                "text": item.text,
                "text_sha256": item.text_sha256,
            }
            for index, item in enumerate(node.evidence)
        ],
    }


def _classification_user_payload(
    request: SemanticClassificationRequest,
) -> dict[str, object]:
    return {
        "source_ir_id": request.source_ir_id,
        "source_sha256": request.source_sha256,
        "structure_snapshot_id": request.structure_snapshot_id,
        "scope_structure_node_ids": list(request.scope_structure_node_ids),
        "nodes": [_classification_node_payload(node) for node in request.nodes],
        "context_nodes": [
            _classification_node_payload(node) for node in request.context_nodes
        ],
        "images": [
            {
                "content_part_index": index + 1,
                "source_sha256": image.source_sha256,
                "page_number": image.page_number,
                "width_px": image.width_px,
                "height_px": image.height_px,
                "image_sha256": image.image_sha256,
                "media_type": image.media_type,
            }
            for index, image in enumerate(request.images)
        ],
    }


def _structure_audit_user_payload(
    request: StructureAuditRequest,
) -> dict[str, object]:
    evidence_request = request.evidence_request
    binding = _structure_audit_wire_binding(request)
    return {
        "identity_binding_sha256": _structure_audit_identity_binding_sha256(
            request,
            binding=binding,
        ),
        "source_bound_structure": {
            "scope_node_aliases": [
                binding.node_alias_by_id[node_id]
                for node_id in evidence_request.scope_structure_node_ids
            ],
            "target_nodes": [
                _structure_audit_node_payload(
                    node,
                    binding=binding,
                )
                for node in evidence_request.nodes
            ],
            "context_nodes": [
                _structure_audit_node_payload(
                    node,
                    binding=binding,
                )
                for node in evidence_request.context_nodes
            ],
        },
        "physical_order": [
            {
                "node_alias": binding.node_alias_by_id[item.structure_node_id],
                "physical_source_orders": list(item.physical_source_orders),
            }
            for item in request.physical_order
        ],
        "outline_spine_node_aliases": [
            binding.node_alias_by_id[node_id]
            for node_id in request.outline_spine_structure_node_ids
        ],
        "current_relations": [
            {
                "kind": "continues",
                "source_node_alias": binding.node_alias_by_id[
                    item.source_structure_node_id
                ],
                "target_node_alias": binding.node_alias_by_id[
                    item.target_structure_node_id
                ],
                "grounding": [
                    {
                        "node_alias": node_alias,
                        "evidence_alias": evidence_alias,
                    }
                    for node_alias, evidence_alias in _audit_relation_grounding_aliases(
                        item.source_refs,
                        binding=binding,
                    )
                ],
            }
            for item in request.current_continuations
        ],
        "allowed_categories": [item.value for item in StructureAuditCategory],
        "max_findings": 1,
    }


def _structure_audit_wire_binding(
    request: StructureAuditRequest,
) -> _StructureAuditWireBinding:
    evidence_request = request.evidence_request
    nodes = (*evidence_request.nodes, *evidence_request.context_nodes)
    node_alias_by_id = {
        node.target_structure_node_id: f"n{index}" for index, node in enumerate(nodes)
    }
    node_id_by_alias = {alias: node_id for node_id, alias in node_alias_by_id.items()}
    evidence_ref_by_alias: dict[tuple[str, str], StructureSourceRef] = {}
    evidence_text_by_alias: dict[tuple[str, str], str] = {}
    evidence_aliases_by_node_alias: dict[str, tuple[str, ...]] = {}
    for node in nodes:
        node_alias = node_alias_by_id[node.target_structure_node_id]
        evidence_aliases_by_node_alias[node_alias] = tuple(
            f"e{index}" for index in range(len(node.evidence))
        )
        for index, evidence in enumerate(node.evidence):
            alias_key = (node_alias, f"e{index}")
            evidence_ref_by_alias[alias_key] = evidence.source_ref
            evidence_text_by_alias[alias_key] = evidence.text
    return _StructureAuditWireBinding(
        node_alias_by_id=node_alias_by_id,
        node_id_by_alias=node_id_by_alias,
        target_aliases=frozenset(
            node_alias_by_id[node_id]
            for node_id in evidence_request.scope_structure_node_ids
        ),
        node_kind_by_alias={
            node_alias_by_id[node.target_structure_node_id]: node.kind for node in nodes
        },
        parent_alias_by_alias={
            node_alias_by_id[node.target_structure_node_id]: (
                node_alias_by_id.get(node.parent_structure_node_id or "")
            )
            for node in nodes
        },
        allowed_categories_by_alias={
            node_alias_by_id[node.target_structure_node_id]: (
                _allowed_structure_audit_target_categories(node)
            )
            for node in nodes
        },
        evidence_ref_by_alias=evidence_ref_by_alias,
        evidence_text_by_alias=evidence_text_by_alias,
        evidence_aliases_by_node_alias=evidence_aliases_by_node_alias,
    )


def _structure_audit_node_payload(
    node: SemanticNodeInput,
    *,
    binding: _StructureAuditWireBinding,
) -> dict[str, object]:
    node_alias = binding.node_alias_by_id[node.target_structure_node_id]
    return {
        "node_alias": node_alias,
        "source_order": node.source_order,
        "kind": node.kind.value,
        "parent_node_alias": binding.node_alias_by_id.get(
            node.parent_structure_node_id or ""
        ),
        "ancestor_node_aliases": [
            binding.node_alias_by_id[node_id]
            for node_id in node.ancestor_structure_node_ids
            if node_id in binding.node_alias_by_id
        ],
        "explicit_label": node.explicit_label,
        "list_ordinal": node.list_ordinal,
        "allowed_target_categories": [
            item.value for item in binding.allowed_categories_by_alias[node_alias]
        ],
        "allowed_evidence_aliases": [
            f"e{index}" for index in range(len(node.evidence))
        ],
        "evidence": [
            {
                "evidence_alias": f"e{index}",
                "text": evidence.text,
                "text_length_codepoints": len(evidence.text),
            }
            for index, evidence in enumerate(node.evidence)
        ],
    }


def _structure_audit_identity_binding_sha256(
    request: StructureAuditRequest,
    *,
    binding: _StructureAuditWireBinding,
) -> str:
    nodes = (
        *request.evidence_request.nodes,
        *request.evidence_request.context_nodes,
    )
    payload = {
        "nodes": [
            {
                "node_alias": binding.node_alias_by_id[node.target_structure_node_id],
                "structure_node_id": node.target_structure_node_id,
                "evidence": [
                    {
                        "evidence_alias": f"e{index}",
                        "source_ref": evidence.source_ref.model_dump(mode="json"),
                        "text_sha256": evidence.text_sha256,
                    }
                    for index, evidence in enumerate(node.evidence)
                ],
            }
            for node in nodes
        ],
        "current_continuations": [
            {
                "source_structure_node_id": item.source_structure_node_id,
                "target_structure_node_id": item.target_structure_node_id,
                "source_refs": [
                    source_ref.model_dump(mode="json")
                    for source_ref in item.source_refs
                ],
            }
            for item in request.current_continuations
        ],
    }
    return _canonical_sha256(payload)


def _audit_relation_grounding_aliases(
    source_refs: tuple[StructureSourceRef, ...],
    *,
    binding: _StructureAuditWireBinding,
) -> tuple[tuple[str, str], ...]:
    source_ref_set = set(source_refs)
    return tuple(
        key
        for key, source_ref in binding.evidence_ref_by_alias.items()
        if source_ref in source_ref_set
    )


def _assembly_user_payload(
    request: RequirementAssemblyRequest,
    *,
    constraints: dict[str, dict[str, object]] | None = None,
) -> dict[str, object]:
    effective_constraints = constraints or _assembly_constraints(request)
    return {
        "source_ir_id": request.source_ir_id,
        "structure_snapshot_id": request.structure_snapshot_id,
        "semantic_snapshot_id": request.semantic_snapshot_id,
        "scope_structure_node_ids": list(request.scope_structure_node_ids),
        "nodes": [
            {
                "target_structure_node_id": node.target_structure_node_id,
                "source_order": node.source_order,
                "kind": node.kind.value,
                "structural_parent_target_structure_node_id": (
                    node.parent_structure_node_id
                ),
                "structural_ancestor_structure_node_ids": list(
                    node.ancestor_structure_node_ids
                ),
                "explicit_label": node.explicit_label,
                "list_ordinal": node.list_ordinal,
                "semantic_annotation_ids": list(node.semantic_annotation_ids),
                "semantic_class": node.semantic_class.value,
                "is_continuation_fragment": node.is_continuation_fragment,
                "requirement_subtype": (
                    node.requirement_subtype.value
                    if node.requirement_subtype is not None
                    else None
                ),
                "allowed_dispositions": effective_constraints[
                    node.target_structure_node_id
                ]["allowed_dispositions"],
                "allowed_parent_structure_node_ids_by_role": effective_constraints[
                    node.target_structure_node_id
                ]["allowed_parent_structure_node_ids_by_role"],
                "allowed_decisions": _allowed_assembly_decisions(
                    effective_constraints[node.target_structure_node_id]
                ),
                "evidence": [
                    {
                        "source_ref": item.source_ref.model_dump(mode="json"),
                        "text": item.text,
                        "text_sha256": item.text_sha256,
                    }
                    for item in node.evidence
                ],
            }
            for node in request.nodes
        ],
    }


def _assembly_budget_constraints(
    request: RequirementAssemblyRequest,
) -> dict[str, dict[str, object]]:
    """Return a structural superset of every model-valid assembly choice.

    This payload is never transported.  It deliberately allows every role and
    every other scoped target as a parent, so its serialized constraint fields
    dominate all semantic/subtype-dependent constraints used by the real call.
    """

    target_ids = tuple(node.target_structure_node_id for node in request.nodes)
    dispositions = [
        "PRIMARY",
        "CHILD",
        "CONTENT",
        "PARAMETER",
        "REFERENCE",
        "EXCLUDE",
        "UNRESOLVED",
    ]
    return {
        target_id: {
            "allowed_dispositions": dispositions,
            "allowed_parent_structure_node_ids_by_role": {
                role: [candidate for candidate in target_ids if candidate != target_id]
                for role in ("CHILD", "CONTENT", "PARAMETER", "REFERENCE")
            },
        }
        for target_id in target_ids
    }


def _provider_request_payload(
    *,
    model_id: str,
    seed: int,
    system_prompt: str,
    user_payload: dict[str, object],
    images: tuple[Any, ...],
    payload_type: type[_PayloadT],
) -> dict[str, object]:
    response_schema = payload_type.model_json_schema()
    content: list[dict[str, object]] = [
        {
            "type": "text",
            "text": json.dumps(
                {
                    "input": user_payload,
                    "output_json_schema": response_schema,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        }
    ]
    total_image_bytes = 0
    total_image_pixels = 0
    for image in images:
        image_bytes = bytes(image.image_bytes)
        if len(image_bytes) > _MAX_IMAGE_BYTES:
            raise SemanticProviderRequestRejected(
                SemanticProviderRequestRejectionCode.IMAGE_BYTE_BUDGET_EXCEEDED,
                "one semantic page image exceeds the in-memory limit",
            )
        total_image_bytes += len(image_bytes)
        if total_image_bytes > _MAX_TOTAL_IMAGE_BYTES:
            raise SemanticProviderRequestRejected(
                SemanticProviderRequestRejectionCode.IMAGE_BYTE_BUDGET_EXCEEDED,
                "semantic page images exceed the per-request limit",
            )
        total_image_pixels += int(image.width_px) * int(image.height_px)
        if total_image_pixels > _MAX_TOTAL_IMAGE_PIXELS:
            raise SemanticProviderRequestRejected(
                SemanticProviderRequestRejectionCode.IMAGE_PIXEL_BUDGET_EXCEEDED,
                "semantic page images exceed the per-request pixel limit",
            )
        encoded = base64.b64encode(image_bytes).decode("ascii")
        content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{image.media_type};base64,{encoded}",
                },
            }
        )

    request_payload: dict[str, object] = {
        "model": model_id,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content},
        ],
        "response_format": {"type": "json_object"},
        "seed": seed,
        "temperature": 0,
        "stream": True,
        "stream_options": {"include_usage": True},
        "enable_thinking": False,
    }
    wire_bytes = json.dumps(
        request_payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(wire_bytes) > _MAX_REQUEST_WIRE_BYTES:
        raise SemanticProviderRequestRejected(
            SemanticProviderRequestRejectionCode.WIRE_BUDGET_EXCEEDED,
            "semantic provider request exceeds the wire-byte limit",
        )
    return request_payload


def _validate_evidence_budget(nodes: tuple[Any, ...]) -> None:
    total = sum(
        len(item.text.encode("utf-8")) for node in nodes for item in node.evidence
    )
    if total > _MAX_EVIDENCE_UTF8_BYTES:
        raise SemanticProviderRequestRejected(
            SemanticProviderRequestRejectionCode.EVIDENCE_BUDGET_EXCEEDED,
            "semantic evidence exceeds the per-request byte limit",
        )


def _finish_reason_error(payload: dict[str, object]) -> str | None:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None
    value = str(choices[0].get("finish_reason") or "").strip().casefold()
    if value in {"length", "max_tokens"}:
        return "provider_output_truncated"
    if value in {"content_filter", "safety", "sensitive"}:
        return "provider_output_refused"
    return None


def _semantic_wire_to_domain(
    item: _SemanticDecisionWire,
    node: SemanticNodeInput | None,
) -> SemanticDecision:
    evidence_indexes = tuple(dict.fromkeys(item.evidence_indexes or ()))
    if node is None or any(index >= len(node.evidence) for index in evidence_indexes):
        return SemanticDecision(
            target_structure_node_id=item.target_structure_node_id,
            semantic_class=SemanticClass.UNRESOLVED,
            requirement_subtype=None,
            evidence_source_refs=(),
            reason_codes=("provider_evidence_index_invalid",),
        )
    evidence_source_refs = tuple(
        node.evidence[index].source_ref for index in evidence_indexes
    )
    if (
        item.semantic_class is SemanticClass.REQUIREMENT
        and item.requirement_subtype is None
    ):
        return SemanticDecision(
            target_structure_node_id=item.target_structure_node_id,
            semantic_class=SemanticClass.UNRESOLVED,
            requirement_subtype=None,
            evidence_source_refs=evidence_source_refs,
            reason_codes=("provider_requirement_subtype_missing",),
        )
    if (
        item.semantic_class is not SemanticClass.REQUIREMENT
        and item.requirement_subtype is not None
    ):
        return SemanticDecision(
            target_structure_node_id=item.target_structure_node_id,
            semantic_class=SemanticClass.UNRESOLVED,
            requirement_subtype=None,
            evidence_source_refs=evidence_source_refs,
            reason_codes=("provider_requirement_subtype_invalid",),
        )
    requirement_subtype = (
        item.requirement_subtype
        if item.semantic_class is SemanticClass.REQUIREMENT
        else None
    )
    continuation_flag_not_applicable = (
        item.is_continuation_fragment
        and item.semantic_class is not SemanticClass.REQUIREMENT
    )
    return SemanticDecision(
        target_structure_node_id=item.target_structure_node_id,
        semantic_class=item.semantic_class,
        requirement_subtype=requirement_subtype,
        is_continuation_fragment=(
            item.is_continuation_fragment
            and item.semantic_class is SemanticClass.REQUIREMENT
        ),
        evidence_source_refs=evidence_source_refs,
        reason_codes=(
            ("provider_model_unresolved",)
            if item.semantic_class is SemanticClass.UNRESOLVED
            else (
                ("continuation_flag_not_applicable",)
                if continuation_flag_not_applicable
                else ("model_classification",)
            )
        ),
    )


def _structure_audit_wire_to_domain(
    item: _StructureAuditFindingWire,
    *,
    request: StructureAuditRequest,
) -> StructureAuditDecision:
    binding = _structure_audit_wire_binding(request)
    target_alias = item.target_node_alias
    if target_alias not in binding.target_aliases:
        raise ValueError("structure audit target alias is outside the writable window")
    target_id = binding.node_id_by_alias[target_alias]
    evidence_source_refs: list[StructureSourceRef] = []
    evidence_node_aliases: list[str] = []
    evidence_refs_by_node: dict[str, set[StructureSourceRef]] = {}
    evidence_aliases_by_node: dict[str, set[str]] = {}
    seen_evidence_nodes: set[str] = set()
    for selection in item.evidence:
        node_alias = selection.node_alias
        if node_alias in seen_evidence_nodes:
            raise ValueError("structure audit evidence node is duplicated")
        seen_evidence_nodes.add(node_alias)
        if node_alias not in binding.node_id_by_alias:
            raise ValueError(
                "structure audit evidence node alias is outside the request"
            )
        evidence_aliases = tuple(dict.fromkeys(selection.evidence_aliases))
        if evidence_aliases != selection.evidence_aliases:
            raise ValueError("structure audit evidence aliases are duplicated")
        for evidence_alias in evidence_aliases:
            source_ref = binding.evidence_ref_by_alias.get((node_alias, evidence_alias))
            if source_ref is None:
                raise ValueError(
                    "structure audit evidence alias is outside the request"
                )
            evidence_source_refs.append(source_ref)
            evidence_refs_by_node.setdefault(node_alias, set()).add(source_ref)
            evidence_aliases_by_node.setdefault(node_alias, set()).add(evidence_alias)
        evidence_node_aliases.append(node_alias)
    if target_alias not in seen_evidence_nodes:
        raise ValueError("structure audit target requires its own source evidence")
    _validate_structure_audit_category_anchors(
        item.category,
        target_alias=target_alias,
        evidence_node_aliases=tuple(evidence_node_aliases),
        evidence_refs_by_node=evidence_refs_by_node,
        request=request,
        binding=binding,
    )
    unique_refs = tuple(dict.fromkeys(evidence_source_refs))
    if not unique_refs:
        raise ValueError("structure audit finding has no source evidence")
    related_ids = tuple(
        binding.node_id_by_alias[node_alias]
        for node_alias in evidence_node_aliases
        if node_alias != target_alias
    )
    proposed_operations, proposal_evidence = _structure_audit_proposal_to_domain(
        item.proposal,
        category=item.category,
        target_alias=target_alias,
        evidence_node_aliases=tuple(evidence_node_aliases),
        evidence_source_refs=unique_refs,
        evidence_aliases_by_node=evidence_aliases_by_node,
        request=request,
        binding=binding,
    )
    return StructureAuditDecision(
        category=item.category,
        target_structure_node_id=target_id,
        related_structure_node_ids=related_ids,
        evidence_source_refs=unique_refs,
        reason_codes=tuple(sorted(set(item.reason_codes))),
        proposed_operations=proposed_operations,
        proposal_evidence=proposal_evidence,
    )


def _structure_audit_proposal_to_domain(
    proposal: _StructureAuditProposalWire | None,
    *,
    category: StructureAuditCategory,
    target_alias: str,
    evidence_node_aliases: tuple[str, ...],
    evidence_source_refs: tuple[StructureSourceRef, ...],
    evidence_aliases_by_node: dict[str, set[str]],
    request: StructureAuditRequest,
    binding: _StructureAuditWireBinding,
) -> tuple[
    tuple[StructurePatchOperationV1, ...],
    StructureAuditProposalEvidence | None,
]:
    if proposal is None:
        return (), None
    cited_aliases = set(evidence_node_aliases)
    operation: StructurePatchOperationV1
    if isinstance(proposal, _StructureAuditReparentProposalWire):
        if category is not StructureAuditCategory.POSSIBLE_MISPLACED_PARENT:
            raise ValueError("re-parent proposal requires misplaced-parent evidence")
        if (
            proposal.new_parent_node_alias == target_alias
            or proposal.new_parent_node_alias not in cited_aliases
        ):
            raise ValueError("re-parent proposal must cite a distinct new parent")
        operation = ReparentStructureOperationV1(
            structure_node_id=binding.node_id_by_alias[target_alias],
            new_parent_structure_node_id=binding.node_id_by_alias[
                proposal.new_parent_node_alias
            ],
            new_sibling_index=proposal.new_sibling_index,
        )
    elif isinstance(proposal, _StructureAuditLogicalReorderProposalWire):
        if category is not StructureAuditCategory.POSSIBLE_SOURCE_ORDER_ERROR:
            raise ValueError("logical reorder requires source-order evidence")
        aliases = proposal.ordered_child_node_aliases
        if (
            len(aliases) != len(set(aliases))
            or target_alias not in aliases
            or proposal.parent_node_alias not in cited_aliases
            or not set(aliases).issubset(cited_aliases)
        ):
            raise ValueError(
                "logical reorder must use the cited parent and exact cited children"
            )
        operation = LogicalReorderStructureOperationV1(
            parent_structure_node_id=binding.node_id_by_alias[
                proposal.parent_node_alias
            ],
            ordered_child_structure_node_ids=tuple(
                binding.node_id_by_alias[alias] for alias in aliases
            ),
        )
    elif isinstance(proposal, _StructureAuditBoundarySplitProposalWire):
        if category is not StructureAuditCategory.POSSIBLE_MISSED_BOUNDARY:
            raise ValueError("boundary split requires missed-boundary evidence")
        target_evidence_aliases = evidence_aliases_by_node.get(target_alias, set())
        _validate_structure_audit_boundary_split_partition(
            proposal,
            target_alias=target_alias,
            selected_target_evidence_aliases=target_evidence_aliases,
            binding=binding,
        )
        operation = BoundarySplitStructureOperationV1(
            structure_node_id=binding.node_id_by_alias[target_alias],
            parts=tuple(
                BoundarySplitPartV1(
                    source_spans=tuple(
                        StructureSourceSpanV1(
                            source_ref=binding.evidence_ref_by_alias[
                                (target_alias, span.evidence_alias)
                            ],
                            start_offset=span.start_offset,
                            end_offset=span.end_offset,
                        )
                        for span in part.source_spans
                    )
                )
                for part in proposal.parts
            ),
        )
    elif isinstance(proposal, _StructureAuditContinuationMergeProposalWire):
        if category is not StructureAuditCategory.POSSIBLE_DUPLICATE_CONTENT:
            raise ValueError("continuation merge requires duplicate-structure evidence")
        endpoint_aliases = {
            proposal.primary_node_alias,
            proposal.continuation_node_alias,
        }
        if (
            target_alias not in endpoint_aliases
            or len(endpoint_aliases) != 2
            or not endpoint_aliases.issubset(cited_aliases)
            or any(
                binding.node_kind_by_alias[alias]
                not in {
                    StructureNodeKind.CONTENT,
                    StructureNodeKind.CONTINUATION,
                }
                for alias in endpoint_aliases
            )
        ):
            raise ValueError(
                "continuation merge must cite two distinct content endpoints"
            )
        expected_pair = (
            binding.node_id_by_alias[proposal.primary_node_alias],
            binding.node_id_by_alias[proposal.continuation_node_alias],
        )
        if expected_pair not in {
            (
                item.source_structure_node_id,
                item.target_structure_node_id,
            )
            for item in request.current_continuations
        }:
            raise ValueError(
                "continuation merge requires the exact grounded CONTINUES relation"
            )
        operation = ContinuationMergeStructureOperationV1(
            primary_structure_node_id=expected_pair[0],
            continuation_structure_node_id=expected_pair[1],
        )
    else:
        if category is not StructureAuditCategory.POSSIBLE_TABLE_CONTINUATION_ERROR:
            raise ValueError(
                "add-continuation proposal requires table-continuation evidence"
            )
        endpoint_aliases = {
            proposal.source_node_alias,
            proposal.target_node_alias,
        }
        if (
            target_alias not in endpoint_aliases
            or len(endpoint_aliases) != 2
            or not endpoint_aliases.issubset(cited_aliases)
            or any(
                binding.node_kind_by_alias[alias] is not StructureNodeKind.TABLE
                for alias in endpoint_aliases
            )
        ):
            raise ValueError("add-continuation must cite two distinct table endpoints")
        operation = AddStructureRelationOperationV1(
            kind=StructureRelationKind.CONTINUES,
            source_structure_node_id=binding.node_id_by_alias[
                proposal.source_node_alias
            ],
            target_structure_node_id=binding.node_id_by_alias[
                proposal.target_node_alias
            ],
            source_refs=evidence_source_refs,
        )
    return (operation,), proposal.evidence_level


def _validate_structure_audit_boundary_split_partition(
    proposal: _StructureAuditBoundarySplitProposalWire,
    *,
    target_alias: str,
    selected_target_evidence_aliases: set[str],
    binding: _StructureAuditWireBinding,
) -> None:
    expected_aliases = binding.evidence_aliases_by_node_alias[target_alias]
    if not expected_aliases or not set(expected_aliases).issubset(
        selected_target_evidence_aliases
    ):
        raise ValueError("boundary split must cite every target source evidence anchor")
    flattened = tuple(span for part in proposal.parts for span in part.source_spans)
    expected_index = 0
    cursor = 0
    for span in flattened:
        if expected_index >= len(expected_aliases):
            raise ValueError("boundary split exceeds target source coverage")
        expected_alias = expected_aliases[expected_index]
        if span.evidence_alias != expected_alias or span.start_offset != cursor:
            raise ValueError(
                "boundary split must exactly cover target evidence in source order"
            )
        text = binding.evidence_text_by_alias[(target_alias, expected_alias)]
        if span.end_offset > len(text):
            raise ValueError("boundary split offset exceeds immutable source text")
        cursor = span.end_offset
        if cursor == len(text):
            expected_index += 1
            cursor = 0
    if expected_index != len(expected_aliases) or cursor != 0:
        raise ValueError("boundary split must cover the complete target evidence")


def _validate_structure_audit_category_anchors(
    category: StructureAuditCategory,
    *,
    target_alias: str,
    evidence_node_aliases: tuple[str, ...],
    evidence_refs_by_node: dict[str, set[StructureSourceRef]],
    request: StructureAuditRequest,
    binding: _StructureAuditWireBinding,
) -> None:
    if category not in binding.allowed_categories_by_alias[target_alias]:
        raise ValueError(
            "structure audit category is not applicable to the target kind"
        )
    if category is StructureAuditCategory.POSSIBLE_DUPLICATE_CONTENT:
        if len(evidence_node_aliases) < 2:
            raise ValueError("duplicate-content suspicion requires two evidence nodes")
        if any(
            binding.node_kind_by_alias[node_alias]
            in {StructureNodeKind.TABLE, StructureNodeKind.TABLE_ROW}
            for node_alias in evidence_node_aliases
        ):
            raise ValueError("table row repetition is outside duplicate-content audit")
        node_ref_sets = tuple(evidence_refs_by_node.values())
        if any(
            left.intersection(right)
            for index, left in enumerate(node_ref_sets)
            for right in node_ref_sets[index + 1 :]
        ):
            raise ValueError("shared source grounding is not duplicated source content")
    if category is StructureAuditCategory.POSSIBLE_TABLE_CONTINUATION_ERROR:
        if binding.node_kind_by_alias[target_alias] is not StructureNodeKind.TABLE:
            raise ValueError(
                "table-continuation suspicion must target a table endpoint"
            )
        table_aliases = {
            node_alias
            for node_alias in evidence_node_aliases
            if binding.node_kind_by_alias[node_alias] is StructureNodeKind.TABLE
        }
        if len(table_aliases) < 2:
            raise ValueError(
                "table-continuation suspicion requires two table endpoints"
            )
        current_pairs = {
            frozenset(
                {
                    binding.node_alias_by_id[item.source_structure_node_id],
                    binding.node_alias_by_id[item.target_structure_node_id],
                }
            )
            for item in request.current_continuations
        }
        if any(pair.issubset(table_aliases) for pair in current_pairs):
            raise ValueError(
                "grounded continuation cannot be challenged without geometry"
            )
    if category is StructureAuditCategory.POSSIBLE_SOURCE_ORDER_ERROR:
        if len(evidence_node_aliases) < 3:
            raise ValueError("source-order suspicion requires two comparison nodes")
        if binding.node_kind_by_alias[target_alias] not in {
            StructureNodeKind.SECTION,
            StructureNodeKind.TABLE,
        }:
            raise ValueError("source-order audit is limited to structural boundaries")
        if not any(
            binding.node_kind_by_alias[node_alias]
            in {StructureNodeKind.SECTION, StructureNodeKind.TABLE}
            for node_alias in evidence_node_aliases
            if node_alias != target_alias
        ):
            raise ValueError("source-order suspicion lacks a structural comparison")
    if category is StructureAuditCategory.POSSIBLE_MISPLACED_PARENT:
        parent_alias = binding.parent_alias_by_alias[target_alias]
        minimum_anchors = 3 if parent_alias is not None else 2
        if len(evidence_node_aliases) < minimum_anchors:
            raise ValueError(
                "misplaced-parent suspicion lacks current-parent comparison"
            )
        if parent_alias is not None and parent_alias not in evidence_node_aliases:
            raise ValueError("misplaced-parent suspicion must cite the current parent")


def _allowed_structure_audit_target_categories(
    node: SemanticNodeInput,
) -> tuple[StructureAuditCategory, ...]:
    kind = node.kind
    if kind is StructureNodeKind.CONTENT:
        categories = [
            StructureAuditCategory.POSSIBLE_MISPLACED_PARENT,
            StructureAuditCategory.POSSIBLE_DUPLICATE_CONTENT,
        ]
        if any(
            _AUDIT_EXPLICIT_BOUNDARY_PREFIX.match(evidence.text)
            for evidence in node.evidence
        ):
            categories.insert(
                0,
                StructureAuditCategory.POSSIBLE_MISSED_BOUNDARY,
            )
        return tuple(categories)
    if kind is StructureNodeKind.SECTION:
        return (
            StructureAuditCategory.POSSIBLE_MISPLACED_PARENT,
            StructureAuditCategory.POSSIBLE_SOURCE_ORDER_ERROR,
            StructureAuditCategory.POSSIBLE_DUPLICATE_CONTENT,
        )
    if kind in {
        StructureNodeKind.CLAUSE,
        StructureNodeKind.GROUP,
        StructureNodeKind.LIST_GROUP,
    }:
        return (
            StructureAuditCategory.POSSIBLE_MISPLACED_PARENT,
            StructureAuditCategory.POSSIBLE_DUPLICATE_CONTENT,
        )
    if kind is StructureNodeKind.TABLE:
        return (
            StructureAuditCategory.POSSIBLE_MISPLACED_PARENT,
            StructureAuditCategory.POSSIBLE_SOURCE_ORDER_ERROR,
            StructureAuditCategory.POSSIBLE_TABLE_CONTINUATION_ERROR,
        )
    return ()


def _assembly_wire_to_domain(
    item: _AssemblyDecisionWire,
) -> RequirementAssemblyDecision:
    if item.status is RequirementAssemblyStatus.UNRESOLVED:
        return _unresolved_assembly_decision(item.target_structure_node_id)
    if not item.include_in_view:
        if item.role is not None or item.parent_structure_node_id is not None:
            return _unresolved_assembly_decision(item.target_structure_node_id)
        return RequirementAssemblyDecision(
            target_structure_node_id=item.target_structure_node_id,
            include_in_view=False,
            role=None,
            parent_structure_node_id=None,
            status=RequirementAssemblyStatus.CANDIDATE,
            reason_codes=("model_exclusion",),
        )
    try:
        return RequirementAssemblyDecision(
            target_structure_node_id=item.target_structure_node_id,
            include_in_view=True,
            role=item.role,
            parent_structure_node_id=item.parent_structure_node_id,
            status=RequirementAssemblyStatus.CANDIDATE,
            reason_codes=("model_assembly",),
        )
    except ValidationError:
        return _unresolved_assembly_decision(item.target_structure_node_id)


def _validate_assembly_decision_against_constraints(
    decision: RequirementAssemblyDecision,
    constraints: dict[str, object] | None,
) -> RequirementAssemblyDecision:
    if decision.status is RequirementAssemblyStatus.UNRESOLVED:
        return decision
    if constraints is None:
        return _unresolved_assembly_decision(
            decision.target_structure_node_id,
            "provider_assembly_constraint_invalid",
        )
    disposition = (
        decision.role.value
        if decision.include_in_view and decision.role is not None
        else "EXCLUDE"
    )
    allowed = constraints["allowed_dispositions"]
    parents_by_role = constraints["allowed_parent_structure_node_ids_by_role"]
    if disposition not in allowed:
        return _unresolved_assembly_decision(
            decision.target_structure_node_id,
            "provider_assembly_constraint_invalid",
        )
    if (
        decision.include_in_view
        and decision.role is not RequirementProjectionRole.PRIMARY
    ):
        allowed_parents = parents_by_role.get(disposition, ())
        if decision.parent_structure_node_id not in allowed_parents:
            return _unresolved_assembly_decision(
                decision.target_structure_node_id,
                "provider_assembly_constraint_invalid",
            )
    return decision


def _unresolved_assembly_decision(
    target_id: str,
    reason_code: str = "provider_assembly_shape_invalid",
) -> RequirementAssemblyDecision:
    return RequirementAssemblyDecision(
        target_structure_node_id=target_id,
        include_in_view=False,
        role=None,
        parent_structure_node_id=None,
        status=RequirementAssemblyStatus.UNRESOLVED,
        reason_codes=(reason_code,),
    )


def _assembly_constraints(
    request: RequirementAssemblyRequest,
) -> dict[str, dict[str, object]]:
    """Compute model-visible role/parent choices from immutable local inputs."""

    nodes_by_id = {item.target_structure_node_id: item for item in request.nodes}
    numbered_container_ids = {
        item.parent_structure_node_id
        for item in request.nodes
        if item.kind is StructureNodeKind.CLAUSE
        and item.list_ordinal is not None
        and item.parent_structure_node_id in nodes_by_id
        and nodes_by_id[item.parent_structure_node_id].kind is StructureNodeKind.SECTION
    }
    result: dict[str, dict[str, object]] = {}
    for node in request.nodes:
        ancestor_requirements = tuple(
            target_id
            for target_id in node.ancestor_structure_node_ids
            if target_id in nodes_by_id
            and nodes_by_id[target_id].source_order < node.source_order
            and nodes_by_id[target_id].semantic_class is SemanticClass.REQUIREMENT
        )
        numbered_clause_ancestors = tuple(
            target_id
            for target_id in node.ancestor_structure_node_ids
            if target_id in nodes_by_id
            and nodes_by_id[target_id].kind is StructureNodeKind.CLAUSE
            and nodes_by_id[target_id].list_ordinal is not None
        )
        inside_numbered_clause = bool(numbered_clause_ancestors)
        if inside_numbered_clause:
            parent_eligible_requirements = tuple(
                target_id
                for target_id in ancestor_requirements
                if target_id not in numbered_clause_ancestors
            )
        else:
            parent_eligible_requirements = ancestor_requirements
        nearest_ancestor_requirement = (
            parent_eligible_requirements[-1] if parent_eligible_requirements else None
        )
        direct_numbered_child = (
            node.kind is StructureNodeKind.CLAUSE
            and node.list_ordinal is not None
            and node.parent_structure_node_id in numbered_container_ids
        )
        inside_numbered_container = any(
            target_id in numbered_container_ids
            for target_id in node.ancestor_structure_node_ids
        )
        numbered_clause = (
            nodes_by_id[numbered_clause_ancestors[-1]]
            if numbered_clause_ancestors
            else None
        )
        direct_unnumbered_numbered_body = (
            numbered_clause is not None
            and node.parent_structure_node_id
            == numbered_clause.target_structure_node_id
            and node.kind is StructureNodeKind.CONTENT
            and node.explicit_label is None
            and node.list_ordinal is None
            and node.semantic_class is SemanticClass.REQUIREMENT
        )
        allowed_dispositions = ["EXCLUDE", "UNRESOLVED"]
        parents_by_role: dict[str, list[str]] = {}
        if node.is_continuation_fragment:
            pass
        elif direct_numbered_child:
            parent = nodes_by_id[node.parent_structure_node_id]
            if (
                node.semantic_class is SemanticClass.REQUIREMENT
                and parent.semantic_class is SemanticClass.REQUIREMENT
            ):
                if node.requirement_subtype is parent.requirement_subtype:
                    roles = ("CHILD", "CONTENT")
                elif (
                    node.requirement_subtype
                    is RequirementSubtype.PERFORMANCE_PARAMETER_REQUIREMENT
                ):
                    roles = ("PARAMETER",)
                elif (
                    node.requirement_subtype is RequirementSubtype.ACCEPTANCE_CRITERION
                ):
                    roles = ("CHILD",)
                else:
                    roles = ("CHILD", "CONTENT", "PARAMETER")
                for role in roles:
                    allowed_dispositions.insert(-2, role)
                    parents_by_role[role] = [parent.target_structure_node_id]
        elif direct_unnumbered_numbered_body:
            if nearest_ancestor_requirement is not None:
                allowed_dispositions.insert(0, "CHILD")
                parents_by_role["CHILD"] = [nearest_ancestor_requirement]
        elif (
            inside_numbered_container
            and node.semantic_class is not SemanticClass.REQUIREMENT
        ):
            pass
        elif node.semantic_class is SemanticClass.REQUIREMENT:
            if nearest_ancestor_requirement is None:
                allowed_dispositions.insert(0, "PRIMARY")
            else:
                for role in ("CHILD", "CONTENT", "PARAMETER"):
                    allowed_dispositions.insert(-2, role)
                    parents_by_role[role] = [nearest_ancestor_requirement]
        elif node.semantic_class in {
            SemanticClass.HEADING,
            SemanticClass.NOISE,
            SemanticClass.VERSION_METADATA,
            SemanticClass.ATTACHMENT,
        }:
            pass
        elif node.semantic_class is SemanticClass.REFERENCE:
            if nearest_ancestor_requirement is not None and not inside_numbered_clause:
                allowed_dispositions.insert(0, "REFERENCE")
                parents_by_role["REFERENCE"] = [nearest_ancestor_requirement]
        else:
            if nearest_ancestor_requirement is not None and not inside_numbered_clause:
                allowed_dispositions.insert(0, "CONTENT")
                parents_by_role["CONTENT"] = [nearest_ancestor_requirement]
        result[node.target_structure_node_id] = {
            "allowed_dispositions": allowed_dispositions,
            "allowed_parent_structure_node_ids_by_role": parents_by_role,
        }
    return result


def _allowed_assembly_decisions(
    constraints: dict[str, object],
) -> list[dict[str, object]]:
    """Expand role/parent constraints into exact model-copyable choices."""

    parents_by_role = constraints["allowed_parent_structure_node_ids_by_role"]
    if not isinstance(parents_by_role, dict):
        raise TypeError("assembly parent constraints must be a mapping")
    result: list[dict[str, object]] = []
    for disposition in constraints["allowed_dispositions"]:
        if disposition in {"PRIMARY", "EXCLUDE", "UNRESOLVED"}:
            result.append(
                {
                    "disposition": disposition,
                    "parent_structure_node_id": None,
                }
            )
            continue
        for parent_id in parents_by_role.get(disposition, ()):
            result.append(
                {
                    "disposition": disposition,
                    "parent_structure_node_id": parent_id,
                }
            )
    return result


def _classification_system_prompt() -> str:
    return """You are the semantic classification stage of ReqPilotX.
Classify every supplied structure node exactly once and preserve input order.
Use only supplied text, source refs, structure, ancestors, tables and images.
The input.context_nodes are read-only ancestor evidence; never return decisions for them.
The input.images metadata binds each following image content part to one source hash and page; use content_part_index to associate them and never infer an unlisted page.
Never rewrite source text, invent a requirement, infer document authority from a filename, or create an ID.
REQUIREMENT requires exactly one supported requirement_subtype. UNRESOLVED requires a concise machine-readable reason code.
Each node lists allowed_evidence_indexes and evidence items with zero-based evidence_index plus scope TARGET or CONTEXT. Every decision must return one or more unique evidence_indexes copied only from that same node's allowed_evidence_indexes; never use another node's list position, a provision_structure_node_ids position, or a source ref. For a non-SECTION node, cite TARGET evidence only. For a SECTION node, cite the TARGET and/or provision CONTEXT evidence that materially supports the decision; if its conclusion depends on descendant substance, cite the relevant CONTEXT indexes instead of citing only the title anchor.
Return is_continuation_fragment for every decision. It is valid only when semantic_class is REQUIREMENT and means specifically that this TARGET is a dependent requirement tail/body to absorb into the requirement identified by parent_structure_node_id, not merely that some later node continues this TARGET. Set true only for an unlabeled CONTENT/continuation requirement that is already represented by its structural parent: for example a wrapped tail that cannot stand alone or a sole Section body that completes the owner's canonical provision. Compare the parent ID and evidence with corresponding supplied nodes. When true, semantic_class and requirement_subtype MUST exactly equal the decision for that structural parent because both fragments form one canonical requirement; if the TARGET has a different subtype or obligation, set false and classify it independently. A normative phrase inside a dependent tail does not make the tail independent. Always set false for every non-REQUIREMENT class, canonical head, numbered/labeled clause, Section, complete distinct obligation, heading, noise or unresolved node, even when that head is followed by a continuation child.
For SECTION nodes, judge the complete structural provision. Evidence from every node named in provision_structure_node_ids is repeated inside the SECTION node as CONTEXT with SECTION-local evidence indexes so the selected basis remains auditable. A SECTION with listed descendants that together state a judgeable obligation, ordered test method, equipment need, objective, acceptance rule or parameter set is REQUIREMENT even when its own TARGET line is title-like or merely says requirements/method/equipment. Use HEADING only when the owner and all listed provision nodes are title-only and contain no substantive provision. Do not return extra decisions for provision IDs beyond their normal entries in input.nodes.
For non-SECTION nodes, classify the TARGET evidence itself. Ancestor context explains meaning but cannot by itself promote a caption, page number, chart label, continuation fragment or descriptive line into a requirement.
A non-SECTION lead-in that only announces following siblings or numbered items and states no independently judgeable obligation is not a REQUIREMENT or HEADING. Classify a generic requirements-introduction lead-in as BACKGROUND; classify a procedure, method or sequence-introduction lead-in as TECHNICAL_INFORMATION.
TABLE and TABLE_ROW are structural kinds, never semantic_class values. Choose semantic_class only from the enumerated semantic classes below. Treat a TABLE target as a structural container, not as a substitute for its independently judgeable data rows; classify the container as TECHNICAL_INFORMATION unless its own TARGET evidence independently states a requirement. A table header row is HEADING or TECHNICAL_INFORMATION, not a requirement. A data row that binds a parameter identifier to values, limits, tolerances or units is REQUIREMENT / PERFORMANCE_PARAMETER_REQUIREMENT. A shared note or calculation rule is TECHNICAL_INFORMATION unless it independently defines a pass/fail result; it remains context for every applicable parameter row and is not duplicated as another requirement. Do not classify every row as ACCEPTANCE_CRITERION merely because the enclosing section contains acceptance language.
Reason codes must be lowercase machine keys matching [a-z][a-z0-9_]*, unique and sorted.
Semantic classes: HEADING is a structural title; REQUIREMENT is a normative or independently judgeable obligation; BACKGROUND is context; TECHNICAL_INFORMATION explains a product or principle without imposing an obligation; SCHEDULE is project timing; REFERENCE identifies referenced material; PROJECT_RISK is project/risk metadata; VERSION_METADATA is revision history; ATTACHMENT is attachment identity; NOISE is header/footer/watermark/TOC repetition; UNRESOLVED means evidence is insufficient.
Requirement subtypes: PRODUCT_REQUIREMENT is a product behavior or property; PERFORMANCE_PARAMETER_REQUIREMENT is a measurable parameter/limit; TEST_METHOD_REQUIREMENT prescribes procedure; TEST_EQUIPMENT_REQUIREMENT prescribes test equipment; TEST_OBJECTIVE_REQUIREMENT states a required verification objective; ACCEPTANCE_CRITERION defines pass/fail acceptance; REGULATION_REQUIREMENT defines regulatory or normative applicability; INTERFACE_REQUIREMENT defines an interface contract.
Modal words are evidence but not mandatory: test equipment and test objectives can be requirements without shall/must/应/必须. A table row is not automatically a requirement; use its heading, columns, parent section and applicability.
This stage returns semantic_class, requirement_subtype, is_continuation_fragment, evidence_indexes and reason_codes only. Never return assembly fields such as include_in_view, status, role or parent_structure_node_id.
Return only the response JSON required by the schema; do not return reasoning or markdown."""


def _structure_audit_system_prompt() -> str:
    return """You are the independent structure-quality audit stage of ReqPilotX.
Your job is to report a small number of high-confidence structural suspicions and, only when the supplied evidence completely determines one of the explicitly allowed operations, an optional patch proposal. A proposal is untrusted intent: the server validates it and may reject it without changing the current tree. You do not classify requirement semantics, rewrite text, repair numbering, or claim that an empty result proves correctness.
Use only source_bound_structure, physical_order, outline_spine_node_aliases and current_relations. Short node/evidence aliases are request-local handles; never invent an alias. The server binds them back to immutable Structure IDs and SourceIR refs after your answer. The physical order is authoritative for where source anchors occurred; the current node kind, explicit label, parent and ancestors are hypotheses that may be challenged.
Look for generic evidence of: a numbered boundary represented as undifferentiated content; a node whose current parent conflicts with nearby physical outline context; source-order placement inconsistent with the surrounding outline; duplicated source content; or a broken table continuation. Do not use customer, product, filename, fixed page, or fixed clause-name knowledge.
possible_missed_boundary is available only when the target's own evidence begins with an explicit generic numbering/table/figure boundary cue. A wrapped sentence tail, ordinary prose, isolated page number or semantic sub-requirement is not a missing structural boundary. A boundary_split proposal is allowed only when at least two explicit structural items occur inside the same target evidence and the supplied text alone determines every boundary. Its source_spans use zero-based Unicode code-point half-open offsets into the named target evidence_alias. The ordered parts must preserve every character and exactly cover every target evidence item from offset zero to text_length_codepoints with no gap, overlap, trimming or rewrite. Punctuation, modal verbs, sentences and semantic obligations are not split evidence.
possible_source_order_error must cite the target plus two structural comparison nodes that demonstrate a real outline inversion; an ordinary predecessor/successor pair in monotonic order is not an error.
current_relations lists CONTINUES relations already grounded by server-side page geometry and column topology. Treat those relations as present. A suspected missing table continuation must cite two unlinked TABLE endpoints and evidence from both sides. Do not use possible_table_continuation_error for one table's rows, empty cells, captions or ordinary topology. Repeated headers, merged-cell inheritance and TABLE/TABLE_ROW projections are outside duplicate-content audit; duplicate content must cite two non-table nodes with disjoint source evidence. When two cited non-table content nodes are already linked by an exact directed current_relations CONTINUES edge and clearly represent one logical source provision, possible_duplicate_content may propose continuation_merge using that exact primary-to-continuation direction. Never propose continuation_merge for tables, without the existing directed relation, or from text similarity alone. A misplaced-parent finding must cite the target, its supplied current parent when present, and a separate physical comparison node. Source-order audit is limited to SECTION/TABLE boundaries and must cite another SECTION/TABLE comparison; do not report interleaved row/cell/content projections.
Return at most one finding: only the single strongest high-confidence suspicion in this window. Prefer no finding over a weak, redundant or purely semantic suspicion. Empty findings is valid and means only that this bounded audit found no high-confidence suspicion.
source_bound_structure.target_nodes are the only writable targets; source_bound_structure.context_nodes are read-only evidence and must never be returned as target_node_alias. target_node_alias must be copied from source_bound_structure.scope_node_aliases and the category must be copied from that target node's allowed_target_categories. Each finding must cite the target's own evidence and may cite additional supplied nodes. Every evidence selection copies one supplied node_alias and one or more unique evidence_aliases from that node's allowed_evidence_aliases. The server derives related stable nodes from those evidence anchors; do not return a separate related-node list.
proposal is optional. Omit it when evidence is incomplete. The allowed proposal operations are re_parent for possible_misplaced_parent, logical_reorder for possible_source_order_error, boundary_split for possible_missed_boundary, continuation_merge for possible_duplicate_content with an exact current CONTINUES edge, and add_continuation for possible_table_continuation_error. re_parent must keep the finding target as the moved node, cite the proposed parent, and provide a zero-based sibling index. logical_reorder must cite one parent and provide the complete exact ordered child list for that parent; never return a partial list. boundary_split uses only evidence_alias values cited from the writable target and must provide an exact full-text partition. continuation_merge must copy the exact directed current relation endpoints. add_continuation must cite two distinct unlinked TABLE endpoints. Context nodes may be a proposed parent or comparison endpoint but are never themselves writable targets. Set evidence_level HIGH only when the supplied source anchors completely determine the operation; set LOW for a plausible intent that still needs human evidence. LOW proposals are retained but never auto-applied. Never propose relation removal, renumbering, text edits, invented nodes or an operation absent from the schema.
category must be one of allowed_categories. reason_codes must be concise lowercase machine keys matching [a-z][a-z0-9_]*, unique and sorted.
Findings are non-blocking review hints. Do not return severity, free-form confidence, replacement text, corrected labels, repaired tables, explanations, markdown or fields absent from the schema.
Return only the response JSON required by the schema."""


def _assembly_system_prompt() -> str:
    return """You are the requirement assembly stage of ReqPilotX.
Cover every supplied semantic node exactly once and preserve input order.
Decide whether it belongs in a reviewable requirement view and, if included, assign PRIMARY, CHILD, CONTENT, PARAMETER or REFERENCE plus a structural parent target.
PRIMARY has no parent. CHILD, CONTENT and PARAMETER must point to an earlier supplied structural ancestor. REFERENCE may instead point to one earlier requirement that shares the same reference container. Never invent or rewrite IDs.
A compound provision remains a complete parent requirement with independently judgeable children. Natural paragraphs and punctuation are not automatic requirement boundaries. Table rows are not automatically top-level requirements.
For a requirement table, never choose a TABLE container as CHILD or PARAMETER instead of its independently judgeable parameter rows. Include each semantic PERFORMANCE_PARAMETER_REQUIREMENT data row as PARAMETER under the enclosing requirement when PARAMETER is allowed. Exclude header rows and shared note rows as separate review items; their source context and applicability remain attached without inflating the requirement count.
Only REQUIREMENT nodes may be PRIMARY, CHILD or PARAMETER. Non-requirement context may only be CONTENT or REFERENCE under a requirement.
Prefer one PRIMARY for one complete enclosing provision. When an included requirement SECTION contains explicit numbered clauses, keep the SECTION as PRIMARY. Use CONTENT for a constituent item or ordered step that is judged only as part of the enclosing provision; use CHILD for a distinct independently judgeable obligation when both roles are allowed. Do not emit either as another PRIMARY merely because it is classified REQUIREMENT.
An unnumbered body node that only supplies the substantive sentence already represented by its enclosing requirement SECTION or numbered clause is not a second review item; exclude it. If it adds a distinct independently judgeable obligation, choose an allowed CHILD, CONTENT or PARAMETER disposition instead of silently absorbing it. Lead-in phrases such as requirements/method/sequence follows are also excluded. Coordinate decisions so every non-PRIMARY parent is itself included with PRIMARY, CHILD or PARAMETER; never attach a child to a CONTENT item.
Inclusion creates a distinct review item; full source context is retained automatically. Exclude headings, lead-in phrases, page numbers, headers/footers, figure captions, chart-axis labels, isolated diagram text and other context that is not itself needed as a requirement item. Include non-requirement CONTENT or REFERENCE only when it materially carries the meaning needed to understand or judge its parent requirement.
For a normal exclusion, return status CANDIDATE, include_in_view false, role null and parent_structure_node_id null.
For an included node, its non-PRIMARY parent must be earlier, included, classified REQUIREMENT, and assigned PRIMARY, CHILD or PARAMETER.
Each node supplies structural_parent_target_structure_node_id, structural_ancestor_structure_node_ids and is_continuation_fragment as read-only semantic context, plus allowed_decisions as the complete list of legal review-projection choices. A true continuation flag is already restricted to EXCLUDE/UNRESOLVED and must be EXCLUDE when evidence is sufficient. For all other nodes, use the structural parent to compare adjacent evidence but never copy it into the output unless the exact same ID occurs in the chosen allowed_decisions object. Copy exactly one allowed_decisions object: its disposition determines role/include/status and its parent_structure_node_id must be copied exactly. The parallel allowed_dispositions and allowed_parent_structure_node_ids_by_role fields explain the contract but never authorize a combination absent from allowed_decisions. EXCLUDE maps to the normal exclusion shape below, and UNRESOLVED maps to the unresolved shape below. Before returning, verify every decision is an exact listed pair.
For an unnumbered direct body under a numbered clause, CONTENT is intentionally unavailable. Compare it with the named structural parent: EXCLUDE a wrapped line, incomplete sentence tail, continuation, qualification or remainder that belongs to the parent's single obligation. A normative phrase inside such a fragment does not make it independent. Choose the listed CHILD only when the body text states a complete, distinct and independently judgeable obligation beyond the parent clause.
allowed_dispositions is a hard local contract, not a suggestion. If it contains only EXCLUDE and UNRESOLVED and the evidence is sufficient, you MUST choose EXCLUDE even when semantic_class is REQUIREMENT; ReqPilotX has already absorbed that node into its enclosing canonical requirement. Never return a role that is absent from allowed_dispositions.
If the evidence cannot support one legal assembly, return status UNRESOLVED, include_in_view false, role null, parent_structure_node_id null, and at least one lowercase machine reason code.
Reason codes must match [a-z][a-z0-9_]*, be unique and sorted.
This stage returns include_in_view, role, parent_structure_node_id, status and reason_codes only. Never return semantic_class, requirement_subtype, is_continuation_fragment or evidence_indexes.
Return only the response JSON required by the schema; do not return reasoning or markdown."""


__all__ = [
    "BAILIAN_SEMANTIC_PROVIDER_ID",
    "BAILIAN_SEMANTIC_PROVIDER_IMPLEMENTATION_VERSION",
    "BAILIAN_SEMANTIC_PROVIDER_VERSION",
    "DEFAULT_BAILIAN_EVALUATION_MODEL",
    "DEFAULT_BAILIAN_SEMANTIC_MODEL",
    "DEFAULT_BAILIAN_SEMANTIC_SEED",
    "BailianQwenSemanticProvider",
    "REQUIREMENT_ASSEMBLY_PROMPT_VERSION",
    "SEMANTIC_CLASSIFICATION_PROMPT_VERSION",
    "STRUCTURE_AUDIT_PROMPT_VERSION",
]

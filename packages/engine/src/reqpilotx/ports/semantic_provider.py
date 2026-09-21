from __future__ import annotations

import hashlib
import re
from enum import Enum
from typing import Callable, Protocol, runtime_checkable

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBytes,
    field_validator,
    model_validator,
)

from reqpilotx.domain.extraction.requirement_assembly import RequirementAssemblyStatus
from reqpilotx.domain.extraction.requirement_view import RequirementProjectionRole
from reqpilotx.domain.extraction.semantic_provider import SemanticProviderRunRecord
from reqpilotx.domain.extraction.semantics import RequirementSubtype, SemanticClass
from reqpilotx.domain.extraction.structure import StructureNodeKind, StructureSourceRef


_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_REASON_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,119}$")
_STRUCTURE_NODE_ID_PATTERN = re.compile(r"^structure-node-[0-9a-f]{32}$")
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class SemanticProviderRequestRejectionCode(str, Enum):
    NODE_BUDGET_EXCEEDED = "semantic_node_budget_exceeded"
    IMAGE_COUNT_BUDGET_EXCEEDED = "semantic_image_count_budget_exceeded"
    EVIDENCE_BUDGET_EXCEEDED = "semantic_evidence_budget_exceeded"
    IMAGE_BYTE_BUDGET_EXCEEDED = "semantic_image_byte_budget_exceeded"
    IMAGE_PIXEL_BUDGET_EXCEEDED = "semantic_image_pixel_budget_exceeded"
    WIRE_BUDGET_EXCEEDED = "semantic_wire_budget_exceeded"
    PROVIDER_OUTPUT_INVALID = "semantic_provider_output_invalid"


class SemanticProviderRequestRejected(ValueError):
    """Known bounded provider rejection eligible for source-bound bisection."""

    def __init__(
        self,
        code: SemanticProviderRequestRejectionCode,
        message: str,
    ) -> None:
        self.code = code
        super().__init__(message)


class SemanticProviderCancelled(RuntimeError):
    """The owning extraction stopped while a semantic request was active."""


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class SemanticEvidenceInput(_StrictFrozenModel):
    source_ref: StructureSourceRef
    text: str = Field(repr=False)
    text_sha256: str

    @model_validator(mode="after")
    def validate_text_digest(self) -> SemanticEvidenceInput:
        expected = hashlib.sha256(self.text.encode("utf-8")).hexdigest()
        if self.text_sha256 != expected:
            raise ValueError("semantic evidence text hash does not match its text view")
        return self


class SemanticImageInput(_StrictFrozenModel):
    source_sha256: str
    page_number: int = Field(ge=1)
    width_px: int = Field(ge=1, le=16_384)
    height_px: int = Field(ge=1, le=16_384)
    image_bytes: StrictBytes = Field(min_length=1, repr=False, exclude=True)
    image_sha256: str
    media_type: str = "image/png"

    @field_validator("source_sha256", "image_sha256")
    @classmethod
    def validate_image_digest(cls, value: str) -> str:
        if not _SHA256_PATTERN.fullmatch(value):
            raise ValueError("image_sha256 must be a lowercase SHA-256 value")
        return value

    @model_validator(mode="after")
    def validate_image(self) -> SemanticImageInput:
        if self.media_type != "image/png":
            raise ValueError("semantic page images must be in-memory PNG payloads")
        if hashlib.sha256(self.image_bytes).hexdigest() != self.image_sha256:
            raise ValueError("semantic page image hash does not match its bytes")
        if (
            len(self.image_bytes) < 24
            or self.image_bytes[:8] != _PNG_SIGNATURE
            or self.image_bytes[8:12] != b"\x00\x00\x00\r"
            or self.image_bytes[12:16] != b"IHDR"
        ):
            raise ValueError("semantic page image must contain a valid PNG IHDR")
        png_width = int.from_bytes(self.image_bytes[16:20], "big")
        png_height = int.from_bytes(self.image_bytes[20:24], "big")
        if (png_width, png_height) != (self.width_px, self.height_px):
            raise ValueError("semantic page image dimensions do not match its PNG IHDR")
        if self.width_px * self.height_px > 16_000_000:
            raise ValueError("semantic page image exceeds the pixel limit")
        return self


class SemanticNodeInput(_StrictFrozenModel):
    target_structure_node_id: str = Field(min_length=1, max_length=160)
    source_order: int = Field(ge=0)
    kind: StructureNodeKind
    parent_structure_node_id: str | None = Field(
        default=None, min_length=1, max_length=160
    )
    ancestor_structure_node_ids: tuple[str, ...] = ()
    explicit_label: str | None = None
    list_ordinal: int | None = Field(default=None, ge=1)
    image_page_numbers: tuple[int, ...] = ()
    provision_structure_node_ids: tuple[str, ...] = ()
    target_source_refs: tuple[StructureSourceRef, ...] = ()
    evidence: tuple[SemanticEvidenceInput, ...] = ()

    @field_validator("image_page_numbers")
    @classmethod
    def validate_image_page_numbers(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(item < 1 for item in value):
            raise ValueError("semantic node image pages must be positive")
        if value != tuple(sorted(set(value))):
            raise ValueError("semantic node image pages must be unique and sorted")
        return value

    @field_validator(
        "target_structure_node_id",
        "parent_structure_node_id",
    )
    @classmethod
    def validate_structure_node_id(cls, value: str | None) -> str | None:
        if value is not None and not _STRUCTURE_NODE_ID_PATTERN.fullmatch(value):
            raise ValueError("semantic structure node IDs must use stable identities")
        return value

    @field_validator("ancestor_structure_node_ids")
    @classmethod
    def validate_ancestor_structure_node_ids(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        if any(not _STRUCTURE_NODE_ID_PATTERN.fullmatch(item) for item in value):
            raise ValueError("semantic ancestor IDs must use stable identities")
        if len(value) != len(set(value)):
            raise ValueError("semantic ancestor IDs must be unique")
        return value

    @field_validator("provision_structure_node_ids")
    @classmethod
    def validate_provision_structure_node_ids(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        if any(not _STRUCTURE_NODE_ID_PATTERN.fullmatch(item) for item in value):
            raise ValueError("semantic provision IDs must use stable identities")
        if len(value) != len(set(value)):
            raise ValueError("semantic provision IDs must be unique")
        return value

    @model_validator(mode="after")
    def validate_target_source_refs(self) -> SemanticNodeInput:
        if len(self.target_source_refs) != len(set(self.target_source_refs)):
            raise ValueError("semantic target source refs must be unique")
        evidence_refs = {item.source_ref for item in self.evidence}
        if not set(self.target_source_refs).issubset(evidence_refs):
            raise ValueError(
                "semantic target source refs must be present in node evidence"
            )
        return self


class SemanticClassificationRequest(_StrictFrozenModel):
    source_ir_id: str = Field(min_length=1, max_length=160)
    source_sha256: str
    structure_snapshot_id: str = Field(min_length=1, max_length=160)
    scope_structure_node_ids: tuple[str, ...]
    nodes: tuple[SemanticNodeInput, ...]
    context_nodes: tuple[SemanticNodeInput, ...] = ()
    images: tuple[SemanticImageInput, ...] = ()

    @model_validator(mode="after")
    def validate_scope(self) -> SemanticClassificationRequest:
        targets = tuple(item.target_structure_node_id for item in self.nodes)
        if targets != self.scope_structure_node_ids:
            raise ValueError(
                "semantic provider nodes must cover scope exactly once and in order"
            )
        orders = tuple(item.source_order for item in self.nodes)
        if orders != tuple(sorted(orders)) or len(orders) != len(set(orders)):
            raise ValueError(
                "semantic provider nodes require unique ascending source_order values"
            )
        context_targets = tuple(
            item.target_structure_node_id for item in self.context_nodes
        )
        if len(context_targets) != len(set(context_targets)) or set(
            context_targets
        ).intersection(targets):
            raise ValueError(
                "semantic context nodes must be unique and outside the target scope"
            )
        context_orders = tuple(item.source_order for item in self.context_nodes)
        if context_orders != tuple(sorted(context_orders)) or len(
            context_orders
        ) != len(set(context_orders)):
            raise ValueError(
                "semantic context nodes require unique ascending source_order values"
            )
        if len((*orders, *context_orders)) != len(set((*orders, *context_orders))):
            raise ValueError(
                "semantic target and context source_order values must be unique"
            )
        nodes_by_target = {item.target_structure_node_id: item for item in self.nodes}
        for node in self.nodes:
            if node.target_structure_node_id in node.provision_structure_node_ids:
                raise ValueError("semantic provision IDs cannot include their owner")
            for provision_id in node.provision_structure_node_ids:
                provision = nodes_by_target.get(provision_id)
                if (
                    provision is None
                    or node.target_structure_node_id
                    not in provision.ancestor_structure_node_ids
                ):
                    raise ValueError(
                        "semantic provision IDs must identify scoped descendants"
                    )
        image_pages = tuple(item.page_number for item in self.images)
        if image_pages != tuple(sorted(image_pages)) or len(image_pages) != len(
            set(image_pages)
        ):
            raise ValueError(
                "semantic page images require unique ascending page numbers"
            )
        if any(item.source_sha256 != self.source_sha256 for item in self.images):
            raise ValueError("semantic page images must be bound to the request source")
        supplied_image_pages = set(image_pages)
        referenced_image_pages = {
            page_number
            for node in (*self.nodes, *self.context_nodes)
            for page_number in node.image_page_numbers
        }
        if referenced_image_pages != supplied_image_pages:
            raise ValueError(
                "semantic node image pages must exactly match the supplied request images"
            )
        if not _SHA256_PATTERN.fullmatch(self.source_sha256):
            raise ValueError("source_sha256 must be a lowercase SHA-256 value")
        return self


class SemanticDecision(_StrictFrozenModel):
    target_structure_node_id: str = Field(min_length=1, max_length=160)
    semantic_class: SemanticClass
    requirement_subtype: RequirementSubtype | None = None
    is_continuation_fragment: bool = False
    evidence_source_refs: tuple[StructureSourceRef, ...] = ()
    reason_codes: tuple[str, ...] = ()

    @field_validator("reason_codes")
    @classmethod
    def validate_reason_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not _REASON_CODE_PATTERN.fullmatch(item) for item in value):
            raise ValueError("semantic reason codes must use machine-readable keys")
        if value != tuple(sorted(set(value))):
            raise ValueError("semantic reason codes must be unique and sorted")
        return value

    @field_validator("target_structure_node_id")
    @classmethod
    def validate_target_structure_node_id(cls, value: str) -> str:
        if not _STRUCTURE_NODE_ID_PATTERN.fullmatch(value):
            raise ValueError("semantic target ID must use a stable structure identity")
        return value

    @model_validator(mode="after")
    def validate_decision(self) -> SemanticDecision:
        if self.semantic_class is SemanticClass.REQUIREMENT:
            if self.requirement_subtype is None:
                raise ValueError("requirement decisions require a subtype")
        elif self.requirement_subtype is not None:
            raise ValueError("only requirement decisions may carry a subtype")
        if (
            self.is_continuation_fragment
            and self.semantic_class is not SemanticClass.REQUIREMENT
        ):
            raise ValueError("only requirement decisions may be continuation fragments")
        if self.semantic_class is SemanticClass.UNRESOLVED and not self.reason_codes:
            raise ValueError("unresolved decisions require a reason code")
        if (
            self.semantic_class is SemanticClass.UNRESOLVED
            and self.is_continuation_fragment
        ):
            raise ValueError("unresolved decisions cannot claim a continuation")
        if len(self.evidence_source_refs) != len(set(self.evidence_source_refs)):
            raise ValueError("semantic evidence source refs must be unique")
        return self


class SemanticClassificationProviderResult(_StrictFrozenModel):
    decisions: tuple[SemanticDecision, ...]
    run: SemanticProviderRunRecord


class RequirementAssemblyNodeInput(_StrictFrozenModel):
    target_structure_node_id: str = Field(min_length=1, max_length=160)
    source_order: int = Field(ge=0)
    kind: StructureNodeKind
    parent_structure_node_id: str | None = Field(
        default=None, min_length=1, max_length=160
    )
    ancestor_structure_node_ids: tuple[str, ...] = ()
    explicit_label: str | None = None
    list_ordinal: int | None = Field(default=None, ge=1)
    semantic_annotation_ids: tuple[str, ...]
    semantic_class: SemanticClass
    requirement_subtype: RequirementSubtype | None = None
    is_continuation_fragment: bool = False
    evidence: tuple[SemanticEvidenceInput, ...]

    @field_validator(
        "target_structure_node_id",
        "parent_structure_node_id",
    )
    @classmethod
    def validate_structure_node_id(cls, value: str | None) -> str | None:
        if value is not None and not _STRUCTURE_NODE_ID_PATTERN.fullmatch(value):
            raise ValueError("assembly structure node IDs must use stable identities")
        return value

    @field_validator("ancestor_structure_node_ids")
    @classmethod
    def validate_ancestor_structure_node_ids(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        if any(not _STRUCTURE_NODE_ID_PATTERN.fullmatch(item) for item in value):
            raise ValueError("assembly ancestor IDs must use stable identities")
        if len(value) != len(set(value)):
            raise ValueError("assembly ancestor IDs must be unique")
        return value

    @model_validator(mode="after")
    def validate_continuation_fragment_shape(self) -> RequirementAssemblyNodeInput:
        if not self.is_continuation_fragment:
            return self
        if (
            self.semantic_class is not SemanticClass.REQUIREMENT
            or self.kind
            not in {StructureNodeKind.CONTENT, StructureNodeKind.CONTINUATION}
            or self.parent_structure_node_id is None
            or self.explicit_label is not None
            or self.list_ordinal is not None
        ):
            raise ValueError(
                "assembly continuation fragments require an unlabeled requirement "
                "body with a structural parent"
            )
        return self


class RequirementAssemblyRequest(_StrictFrozenModel):
    source_ir_id: str = Field(min_length=1, max_length=160)
    structure_snapshot_id: str = Field(min_length=1, max_length=160)
    semantic_snapshot_id: str = Field(min_length=1, max_length=160)
    scope_structure_node_ids: tuple[str, ...]
    nodes: tuple[RequirementAssemblyNodeInput, ...]

    @model_validator(mode="after")
    def validate_scope(self) -> RequirementAssemblyRequest:
        targets = tuple(item.target_structure_node_id for item in self.nodes)
        if targets != self.scope_structure_node_ids:
            raise ValueError(
                "assembly provider nodes must cover scope exactly once and in order"
            )
        orders = tuple(item.source_order for item in self.nodes)
        if orders != tuple(sorted(orders)) or len(orders) != len(set(orders)):
            raise ValueError(
                "assembly provider nodes require unique ascending source_order values"
            )
        return self


class RequirementAssemblyDecision(_StrictFrozenModel):
    target_structure_node_id: str = Field(min_length=1, max_length=160)
    include_in_view: bool
    role: RequirementProjectionRole | None = None
    parent_structure_node_id: str | None = Field(
        default=None, min_length=1, max_length=160
    )
    status: RequirementAssemblyStatus
    reason_codes: tuple[str, ...] = ()

    @field_validator("reason_codes")
    @classmethod
    def validate_reason_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not _REASON_CODE_PATTERN.fullmatch(item) for item in value):
            raise ValueError("assembly reason codes must use machine-readable keys")
        if value != tuple(sorted(set(value))):
            raise ValueError("assembly reason codes must be unique and sorted")
        return value

    @field_validator(
        "target_structure_node_id",
        "parent_structure_node_id",
    )
    @classmethod
    def validate_structure_node_id(cls, value: str | None) -> str | None:
        if value is not None and not _STRUCTURE_NODE_ID_PATTERN.fullmatch(value):
            raise ValueError(
                "assembly decision IDs must use stable structure identities"
            )
        return value

    @model_validator(mode="after")
    def validate_decision(self) -> RequirementAssemblyDecision:
        if self.status is RequirementAssemblyStatus.UNRESOLVED:
            if (
                self.include_in_view
                or self.role is not None
                or self.parent_structure_node_id is not None
            ):
                raise ValueError("unresolved assembly decisions cannot enter the view")
            if not self.reason_codes:
                raise ValueError("unresolved assembly decisions require a reason code")
            return self
        if not self.include_in_view:
            if self.role is not None or self.parent_structure_node_id is not None:
                raise ValueError(
                    "excluded assembly decisions cannot define a role or parent"
                )
            return self
        if self.role is None:
            raise ValueError("included assembly decisions require a role")
        if self.role is RequirementProjectionRole.PRIMARY:
            if self.parent_structure_node_id is not None:
                raise ValueError("PRIMARY assembly decisions cannot define a parent")
        elif self.parent_structure_node_id is None:
            raise ValueError("non-PRIMARY assembly decisions require a parent")
        if self.parent_structure_node_id == self.target_structure_node_id:
            raise ValueError("assembly decisions cannot be self-parented")
        return self


class RequirementAssemblyProviderResult(_StrictFrozenModel):
    decisions: tuple[RequirementAssemblyDecision, ...]
    run: SemanticProviderRunRecord


@runtime_checkable
class SemanticProviderPort(Protocol):
    provider_id: str
    provider_version: str
    model_id: str

    def classify_nodes(
        self,
        request: SemanticClassificationRequest,
    ) -> SemanticClassificationProviderResult: ...

    def assemble_requirements(
        self,
        request: RequirementAssemblyRequest,
    ) -> RequirementAssemblyProviderResult: ...


@runtime_checkable
class PreflightSemanticProviderPort(SemanticProviderPort, Protocol):
    """Provider contract required by bounded multi-provision execution."""

    def preflight_classification_request(
        self,
        request: SemanticClassificationRequest,
    ) -> None: ...


@runtime_checkable
class CancellableSemanticProviderPort(PreflightSemanticProviderPort, Protocol):
    def set_cancellation_requested(
        self,
        callback: Callable[[], bool] | None,
    ) -> None: ...

    def preflight_assembly_request(
        self,
        request: RequirementAssemblyRequest,
    ) -> None: ...


@runtime_checkable
class DocumentSessionSemanticProviderPort(Protocol):
    """Optional lifecycle owned by one whole-document extraction attempt."""

    def prepare_document_session(
        self,
        *,
        source_ir_id: str,
        structure_snapshot_id: str,
        images: tuple[SemanticImageInput, ...],
    ) -> None: ...

    def close_document_session(self) -> None: ...


__all__ = [
    "PreflightSemanticProviderPort",
    "CancellableSemanticProviderPort",
    "DocumentSessionSemanticProviderPort",
    "RequirementAssemblyDecision",
    "RequirementAssemblyNodeInput",
    "RequirementAssemblyProviderResult",
    "RequirementAssemblyRequest",
    "SemanticClassificationProviderResult",
    "SemanticClassificationRequest",
    "SemanticDecision",
    "SemanticEvidenceInput",
    "SemanticImageInput",
    "SemanticNodeInput",
    "SemanticProviderPort",
    "SemanticProviderCancelled",
    "SemanticProviderRequestRejected",
    "SemanticProviderRequestRejectionCode",
]

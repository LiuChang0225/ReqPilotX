from __future__ import annotations

from reqpilotx_server.resource_paths import development_path

import hashlib
import json
import re
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Literal

import jsonschema
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from reqpilotx_server.private_eval import private_eval_path

from reqpilotx_server.application.extraction.assemble_requirements import (
    validate_requirement_assembly_against_inputs,
)
from reqpilotx_server.application.extraction.classify_content import (
    validate_semantic_classification_against_inputs,
)
from reqpilotx_server.application.extraction.llm_semantics import LlmRequirementExtractionResult
from reqpilotx_server.application.extraction.project_requirements import (
    _continuation_components,
    _projection_units,
    validate_assembled_requirement_view_against_inputs,
)
from reqpilotx_server.application.extraction.resolve_structure import (
    validate_structure_tree_against_source_ir,
)
from reqpilotx_server.domain.extraction.requirement_assembly import (
    RequirementAssemblySnapshot,
    RequirementAssemblyStatus,
)
from reqpilotx_server.domain.extraction.requirement_view import (
    RequirementDecisionMode,
    RequirementProjectionRole,
    RequirementViewSnapshot,
)
from reqpilotx_server.domain.extraction.semantic_provider import (
    SemanticProviderRunRecord,
    SemanticProviderRunStatus,
)
from reqpilotx_server.domain.extraction.semantics import (
    RequirementSubtype,
    SemanticAnnotationStatus,
    SemanticClass,
    SemanticClassificationSnapshot,
)
from reqpilotx_server.domain.extraction.source_ir import (
    NativeDocumentFormat,
    NativeLocator,
    SourceIRSnapshot,
)
from reqpilotx_server.domain.extraction.structure import (
    StructureNodeKind,
    StructureSourceRef,
    StructureSourceRefKind,
    StructureTreeSnapshot,
)


LLM_REQUIREMENT_SILVER_SCHEMA_VERSION = "llm-requirement-silver/v1"
LLM_REQUIREMENT_SILVER_EVALUATION_SCHEMA_VERSION = (
    "llm-requirement-silver-evaluation/v2"
)
DEFAULT_LLM_REQUIREMENT_SILVER_PATH = private_eval_path("extraction_cases/llm_silver.json")
DEFAULT_LLM_REQUIREMENT_SILVER_SCHEMA_PATH = (
    development_path('prototype') / "eval" / "extraction_cases" / "llm_silver.schema.json"
)

_DOCUMENT_ID_PATTERN = re.compile(r"^DOC-[A-Z0-9-]+$")
_TARGET_ID_PATTERN = re.compile(r"^structure-node-[0-9a-f]{32}$")
_SPAN_ID_PATTERN = re.compile(r"^semantic-span-[0-9a-f]{32}$")
_CHECK_ID_PATTERN = re.compile(r"^[a-z0-9_.:-]{1,240}$")
_REASON_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,119}$")
_MACHINE_VALUE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/+\-;=]{0,239}$")
_PIPELINE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/+\-;=]{0,159}$")
_REQUIRED_LIMITATIONS = frozenset(
    {
        "not_human_gold",
        "not_a_serving_activation_gate",
        "source_hash_and_pipeline_version_bound",
        "text_free_locator_registry",
    }
)


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class LlmRequirementSilverRegistryError(ValueError):
    """The LLM Silver registry or its schema cannot be trusted."""


class LlmRequirementSilverComponentBinding(_StrictFrozenModel):
    component_id: str = Field(min_length=1, max_length=160)
    component_version: str = Field(min_length=1, max_length=160)

    @field_validator("component_id", "component_version")
    @classmethod
    def reject_blank_component_identity(cls, value: str) -> str:
        if not _PIPELINE_ID_PATTERN.fullmatch(value):
            raise ValueError("component identity values must be machine-readable")
        return value


class LlmRequirementSilverClassificationRunBinding(_StrictFrozenModel):
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_structure_node_ids: tuple[str, ...]

    @field_validator("target_structure_node_ids")
    @classmethod
    def validate_target_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("classification run targets must not be empty")
        if any(not _TARGET_ID_PATTERN.fullmatch(item) for item in value):
            raise ValueError("classification run targets must use stable target IDs")
        if len(value) != len(set(value)):
            raise ValueError("classification run targets must be unique")
        return value


class LlmRequirementSilverSemanticProviderBinding(_StrictFrozenModel):
    provider_id: str = Field(min_length=1, max_length=160)
    provider_version: str = Field(min_length=1, max_length=160)
    model_id: str = Field(min_length=1, max_length=160)
    classification_prompt_version: str = Field(min_length=1, max_length=160)
    classification_response_schema_version: str = Field(min_length=1, max_length=160)
    classification_run_count: int = Field(ge=1)
    classification_runs: tuple[LlmRequirementSilverClassificationRunBinding, ...]
    assembly_prompt_version: str = Field(min_length=1, max_length=160)
    assembly_response_schema_version: str = Field(min_length=1, max_length=160)
    assembly_run_count: Literal[1] = 1
    assembly_request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator(
        "provider_id",
        "provider_version",
        "model_id",
        "classification_prompt_version",
        "classification_response_schema_version",
        "assembly_prompt_version",
        "assembly_response_schema_version",
    )
    @classmethod
    def reject_blank_provider_identity(cls, value: str) -> str:
        if not _PIPELINE_ID_PATTERN.fullmatch(value):
            raise ValueError("provider identity values must be machine-readable")
        return value

    @model_validator(mode="after")
    def validate_run_bindings(self) -> LlmRequirementSilverSemanticProviderBinding:
        if len(self.classification_runs) != self.classification_run_count:
            raise ValueError(
                "classification run bindings must match classification_run_count"
            )
        request_hashes = tuple(item.request_sha256 for item in self.classification_runs)
        if len(request_hashes) != len(set(request_hashes)):
            raise ValueError("classification request hashes must be unique")
        target_ids = tuple(
            target_id
            for run in self.classification_runs
            for target_id in run.target_structure_node_ids
        )
        if len(target_ids) != len(set(target_ids)):
            raise ValueError(
                "classification run bindings cannot repeat semantic targets"
            )
        return self


class LlmRequirementSilverPdfNativeLocator(_StrictFrozenModel):
    locator_kind: Literal["PDF_NATIVE"]
    page_number: int = Field(ge=1)
    block_index: int | None = Field(
        default=None,
        ge=0,
        exclude_if=lambda value: value is None,
    )
    bbox: tuple[float, float, float, float] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    table_index: int | None = Field(
        default=None,
        ge=0,
        exclude_if=lambda value: value is None,
    )
    row_index: int | None = Field(
        default=None,
        ge=0,
        exclude_if=lambda value: value is None,
    )
    column_index: int | None = Field(
        default=None,
        ge=0,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def require_valid_pdf_locator(self) -> LlmRequirementSilverPdfNativeLocator:
        NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=self.page_number,
            block_index=self.block_index,
            bbox=self.bbox,
            table_index=self.table_index,
            row_index=self.row_index,
            column_index=self.column_index,
        )
        return self


class LlmRequirementSilverPdfOcrLocator(_StrictFrozenModel):
    locator_kind: Literal["PDF_OCR"]
    page_number: int = Field(ge=1)
    bbox: tuple[float, float, float, float]
    provider_id: str = Field(min_length=1, max_length=160)
    provider_version: str = Field(min_length=1, max_length=160)
    page_image_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("bbox")
    @classmethod
    def validate_bbox(
        cls,
        value: tuple[float, float, float, float],
    ) -> tuple[float, float, float, float]:
        x0, y0, x1, y1 = value
        if x1 < x0 or y1 < y0:
            raise ValueError("OCR bbox must be ordered")
        return value

    @field_validator("provider_id", "provider_version")
    @classmethod
    def reject_blank_ocr_provider_identity(cls, value: str) -> str:
        if not _PIPELINE_ID_PATTERN.fullmatch(value):
            raise ValueError("OCR provider identity values must be machine-readable")
        return value


LlmRequirementSilverLocator = Annotated[
    LlmRequirementSilverPdfNativeLocator | LlmRequirementSilverPdfOcrLocator,
    Field(discriminator="locator_kind"),
]


class LlmRequirementSilverBoundSourceRef(_StrictFrozenModel):
    kind: StructureSourceRefKind
    source_id: str = Field(min_length=1, max_length=160)
    locator: LlmRequirementSilverLocator

    @model_validator(mode="after")
    def validate_ref_locator_pair(self) -> LlmRequirementSilverBoundSourceRef:
        StructureSourceRef(kind=self.kind, source_id=self.source_id)
        if self.kind is StructureSourceRefKind.VISUAL_TEXT_OBSERVATION:
            if not isinstance(self.locator, LlmRequirementSilverPdfOcrLocator):
                raise ValueError("visual text refs require an OCR locator")
        elif not isinstance(self.locator, LlmRequirementSilverPdfNativeLocator):
            raise ValueError("native source refs require a native PDF locator")
        return self

    @property
    def source_ref(self) -> StructureSourceRef:
        return StructureSourceRef(kind=self.kind, source_id=self.source_id)


class LlmRequirementSilverItem(_StrictFrozenModel):
    target_structure_node_id: str = Field(pattern=r"^structure-node-[0-9a-f]{32}$")
    target_kind: StructureNodeKind
    source_order: int = Field(ge=0)
    semantic_class: SemanticClass
    requirement_subtype: RequirementSubtype | None
    semantic_reason_codes: tuple[str, ...]
    is_continuation_fragment: bool
    include_in_view: bool
    role: RequirementProjectionRole | None
    parent_target_structure_node_id: str | None = Field(
        default=None,
        pattern=r"^structure-node-[0-9a-f]{32}$",
    )
    decision_mode: RequirementDecisionMode | None
    source_span_ids: tuple[str, ...]
    selected_evidence_source_refs: tuple[LlmRequirementSilverBoundSourceRef, ...]
    source_refs: tuple[LlmRequirementSilverBoundSourceRef, ...]
    full_context_source_refs: tuple[LlmRequirementSilverBoundSourceRef, ...]

    @field_validator("source_span_ids")
    @classmethod
    def validate_span_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not _SPAN_ID_PATTERN.fullmatch(item) for item in value):
            raise ValueError("source span IDs must use stable semantic identities")
        if len(value) != len(set(value)):
            raise ValueError("source span IDs must be unique")
        return value

    @field_validator("semantic_reason_codes")
    @classmethod
    def validate_semantic_reason_codes(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        if not value or any(not _REASON_CODE_PATTERN.fullmatch(item) for item in value):
            raise ValueError("semantic reason codes must use machine-readable keys")
        if value != tuple(sorted(set(value))):
            raise ValueError("semantic reason codes must be unique and sorted")
        return value

    @model_validator(mode="after")
    def validate_expected_item_shape(self) -> LlmRequirementSilverItem:
        for name, refs in (
            ("selected evidence refs", self.selected_evidence_source_refs),
            ("source refs", self.source_refs),
            ("full-context source refs", self.full_context_source_refs),
        ):
            keys = tuple((item.kind, item.source_id) for item in refs)
            if len(keys) != len(set(keys)):
                raise ValueError(f"{name} must be unique")

        if self.target_kind is StructureNodeKind.DOCUMENT:
            raise ValueError("LLM Silver cannot target the document root")
        if not self.source_span_ids or not self.source_refs:
            raise ValueError("every LLM Silver item requires source spans and refs")
        if not self.selected_evidence_source_refs:
            raise ValueError("every LLM Silver item requires selected model evidence")
        if self.semantic_class is SemanticClass.UNRESOLVED:
            raise ValueError("accepted LLM Silver cannot encode UNRESOLVED semantics")
        if self.semantic_class is SemanticClass.REQUIREMENT:
            if self.requirement_subtype is None:
                raise ValueError("REQUIREMENT Silver items require a subtype")
        elif self.requirement_subtype is not None:
            raise ValueError("non-REQUIREMENT Silver items cannot have a subtype")

        if not self.include_in_view:
            if any(
                value is not None
                for value in (
                    self.role,
                    self.parent_target_structure_node_id,
                    self.decision_mode,
                )
            ):
                raise ValueError("excluded Silver items cannot define view fields")
            if self.full_context_source_refs:
                raise ValueError(
                    "excluded Silver items cannot define view context refs"
                )
            return self

        if self.role is None or self.decision_mode is None:
            raise ValueError("included Silver items require role and decision mode")
        if not self.source_refs or not self.full_context_source_refs:
            raise ValueError(
                "included Silver items require exact and full-context refs"
            )
        if not set(self.source_refs).issubset(self.full_context_source_refs):
            raise ValueError("exact source refs must be contained by full context")
        if self.role is RequirementProjectionRole.PRIMARY:
            if self.parent_target_structure_node_id is not None:
                raise ValueError("PRIMARY Silver items cannot have a parent")
        elif self.parent_target_structure_node_id is None:
            raise ValueError("non-PRIMARY Silver items require a stable target parent")
        if self.parent_target_structure_node_id == self.target_structure_node_id:
            raise ValueError("a Silver item cannot be its own parent")
        if self.semantic_class is SemanticClass.REQUIREMENT:
            if self.role is RequirementProjectionRole.CONTENT:
                if self.decision_mode is not RequirementDecisionMode.NONE:
                    raise ValueError("requirement CONTENT must use decision NONE")
            elif self.role in {
                RequirementProjectionRole.PRIMARY,
                RequirementProjectionRole.CHILD,
                RequirementProjectionRole.PARAMETER,
            }:
                if self.decision_mode is RequirementDecisionMode.NONE:
                    raise ValueError("requirement decision targets cannot use NONE")
            else:
                raise ValueError("requirement Silver items use requirement roles only")
        else:
            if self.role not in {
                RequirementProjectionRole.CONTENT,
                RequirementProjectionRole.REFERENCE,
            }:
                raise ValueError("context Silver items require CONTENT or REFERENCE")
            if self.decision_mode is not RequirementDecisionMode.NONE:
                raise ValueError("context Silver items must use decision NONE")
        return self


class LlmRequirementSilverDocument(_StrictFrozenModel):
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewed_source_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_adapter: LlmRequirementSilverComponentBinding
    structure_resolver: LlmRequirementSilverComponentBinding
    semantic_provider: LlmRequirementSilverSemanticProviderBinding
    semantic_classifier: LlmRequirementSilverComponentBinding
    requirement_assembler: LlmRequirementSilverComponentBinding
    requirement_projector: LlmRequirementSilverComponentBinding
    semantic_scope_structure_node_ids: tuple[str, ...]
    assembly_scope_structure_node_ids: tuple[str, ...]
    items: dict[str, LlmRequirementSilverItem]

    @field_validator(
        "semantic_scope_structure_node_ids",
        "assembly_scope_structure_node_ids",
    )
    @classmethod
    def validate_scope_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not _TARGET_ID_PATTERN.fullmatch(item) for item in value):
            raise ValueError("Silver scope values must use stable target IDs")
        if len(value) != len(set(value)):
            raise ValueError("Silver scopes must not contain duplicate targets")
        if not value:
            raise ValueError("Silver scopes must not be empty")
        return value

    @model_validator(mode="after")
    def validate_document_expectations(self) -> LlmRequirementSilverDocument:
        if any(not _TARGET_ID_PATTERN.fullmatch(key) for key in self.items):
            raise ValueError("Silver item keys must use stable target IDs")
        if set(self.items) != set(self.assembly_scope_structure_node_ids):
            raise ValueError("Silver items must exactly cover the assembly scope")
        if not set(self.assembly_scope_structure_node_ids).issubset(
            self.semantic_scope_structure_node_ids
        ):
            raise ValueError("assembly scope must be contained by semantic scope")
        classified_target_ids = tuple(
            target_id
            for run in self.semantic_provider.classification_runs
            for target_id in run.target_structure_node_ids
        )
        if classified_target_ids != self.semantic_scope_structure_node_ids:
            raise ValueError(
                "classification run targets must exactly cover semantic scope in order"
            )
        ordered_items = tuple(
            self.items[target_id]
            for target_id in self.assembly_scope_structure_node_ids
        )
        if any(
            key != item.target_structure_node_id for key, item in self.items.items()
        ):
            raise ValueError("Silver item keys must equal their stable target IDs")
        orders = tuple(item.source_order for item in ordered_items)
        if orders != tuple(sorted(orders)) or len(orders) != len(set(orders)):
            raise ValueError("Silver items must preserve unique source order")
        for item in ordered_items:
            parent_id = item.parent_target_structure_node_id
            if parent_id is not None:
                parent = self.items.get(parent_id)
                if parent is None or not parent.include_in_view:
                    raise ValueError("Silver parents must be included stable targets")

        locator_by_ref: dict[tuple[StructureSourceRefKind, str], object] = {}
        for item in ordered_items:
            for bound_ref in (
                *item.selected_evidence_source_refs,
                *item.source_refs,
                *item.full_context_source_refs,
            ):
                key = (bound_ref.kind, bound_ref.source_id)
                previous = locator_by_ref.setdefault(key, bound_ref.locator)
                if previous != bound_ref.locator:
                    raise ValueError("one source ref cannot have conflicting locators")
        return self


class LlmRequirementSilverProvenance(_StrictFrozenModel):
    label: Literal["AI_REVIEWED_SILVER"]
    reviewed_by: Literal["ai_assistant"]
    reviewed_at_utc: datetime
    is_human_gold: Literal[False]
    is_serving_activation_gate: Literal[False]
    limitations: tuple[
        Literal[
            "not_human_gold",
            "not_a_serving_activation_gate",
            "source_hash_and_pipeline_version_bound",
            "text_free_locator_registry",
        ],
        ...,
    ]

    @model_validator(mode="after")
    def require_accepted_silver_provenance(self) -> LlmRequirementSilverProvenance:
        if (
            self.reviewed_at_utc.tzinfo is None
            or self.reviewed_at_utc.utcoffset() is None
        ):
            raise ValueError("reviewed_at_utc must be timezone-aware")
        if self.reviewed_at_utc.utcoffset() != timedelta(0):
            raise ValueError("reviewed_at_utc must use UTC")
        if set(self.limitations) != _REQUIRED_LIMITATIONS:
            raise ValueError("LLM Silver must retain every safety limitation")
        return self


class LlmRequirementSilverRegistry(_StrictFrozenModel):
    schema_version: Literal["llm-requirement-silver/v1"]
    provenance: LlmRequirementSilverProvenance
    documents: dict[str, LlmRequirementSilverDocument]

    @model_validator(mode="after")
    def validate_document_ids(self) -> LlmRequirementSilverRegistry:
        if not self.documents:
            raise ValueError("LLM Silver requires at least one document")
        if any(not _DOCUMENT_ID_PATTERN.fullmatch(item) for item in self.documents):
            raise ValueError("LLM Silver document IDs must be machine-readable")
        return self


class LlmRequirementSilverCheckStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"


LlmRequirementSilverCheckCategory = Literal[
    "binding",
    "pipeline",
    "provider",
    "scope",
    "item",
    "locator",
]
SilverCheckValue = str | int | bool | tuple[str, ...] | tuple[int, ...] | None


class LlmRequirementSilverCheck(_StrictFrozenModel):
    check_id: str = Field(min_length=1, max_length=240)
    category: LlmRequirementSilverCheckCategory
    status: LlmRequirementSilverCheckStatus
    reason_code: str = Field(min_length=1, max_length=120)
    expected: SilverCheckValue
    observed: SilverCheckValue

    @model_validator(mode="after")
    def validate_text_free_check(self) -> LlmRequirementSilverCheck:
        if not _CHECK_ID_PATTERN.fullmatch(self.check_id):
            raise ValueError("check_id must be a machine-readable key")
        if not _REASON_CODE_PATTERN.fullmatch(self.reason_code):
            raise ValueError("reason_code must be a machine-readable key")
        _require_machine_value(self.expected)
        _require_machine_value(self.observed)
        return self


class LlmRequirementSilverReport(_StrictFrozenModel):
    schema_version: Literal["llm-requirement-silver-evaluation/v2"] = (
        LLM_REQUIREMENT_SILVER_EVALUATION_SCHEMA_VERSION
    )
    provenance_label: Literal["AI_REVIEWED_SILVER"] = "AI_REVIEWED_SILVER"
    is_human_gold: Literal[False] = False
    is_serving_activation_gate: Literal[False] = False
    registry_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    document_id: str = Field(pattern=r"^DOC-[A-Z0-9-]+$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    semantic_unit_plan_id: str = Field(pattern=r"^semantic-unit-plan-[0-9a-f]{32}$")
    semantic_unit_ids: tuple[str, ...]
    owner_structure_node_ids: tuple[str, ...]
    status: LlmRequirementSilverCheckStatus
    passed_check_count: int = Field(ge=0)
    failed_check_count: int = Field(ge=0)
    checks: tuple[LlmRequirementSilverCheck, ...]

    @field_validator("semantic_unit_ids")
    @classmethod
    def validate_semantic_unit_ids(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        if not value or any(
            not re.fullmatch(r"semantic-unit-[0-9a-f]{32}", item) for item in value
        ):
            raise ValueError("Silver reports require semantic unit identities")
        if len(value) != len(set(value)):
            raise ValueError("Silver report semantic units must be unique")
        return value

    @field_validator("owner_structure_node_ids")
    @classmethod
    def validate_owner_structure_node_ids(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        if not value or any(not _TARGET_ID_PATTERN.fullmatch(item) for item in value):
            raise ValueError("Silver report owners require Structure identities")
        if len(value) != len(set(value)):
            raise ValueError("Silver report owners must be unique")
        return value

    @model_validator(mode="after")
    def validate_rollup(self) -> LlmRequirementSilverReport:
        passed = sum(
            item.status is LlmRequirementSilverCheckStatus.PASS for item in self.checks
        )
        failed = len(self.checks) - passed
        status = (
            LlmRequirementSilverCheckStatus.PASS
            if self.checks and failed == 0
            else LlmRequirementSilverCheckStatus.FAIL
        )
        if (
            self.passed_check_count != passed
            or self.failed_check_count != failed
            or self.status is not status
        ):
            raise ValueError("LLM Silver report rollup does not match checks")
        return self


def load_llm_requirement_silver(
    path: Path = DEFAULT_LLM_REQUIREMENT_SILVER_PATH,
    *,
    schema_path: Path = DEFAULT_LLM_REQUIREMENT_SILVER_SCHEMA_PATH,
) -> LlmRequirementSilverRegistry:
    """Load only accepted, text-free, AI-reviewed LLM Silver.

    Draft payloads are deliberately not a subtype of this registry. Duplicate
    JSON keys, unknown fields, free-form notes, and altered safety provenance
    all fail before a typed registry can be returned.
    """

    try:
        payload = _load_json_object_strict(path)
        provenance = payload.get("provenance")
        if isinstance(provenance, dict) and provenance.get("label") == "DRAFT":
            raise LlmRequirementSilverRegistryError(
                "Draft LLM Silver cannot be loaded as accepted evidence."
            )
        schema = _load_json_object_strict(schema_path)
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.Draft202012Validator(
            schema,
            format_checker=jsonschema.FormatChecker(),
        ).validate(payload)
        return LlmRequirementSilverRegistry.model_validate(payload)
    except LlmRequirementSilverRegistryError:
        raise
    except (
        OSError,
        json.JSONDecodeError,
        jsonschema.SchemaError,
        jsonschema.ValidationError,
        ValidationError,
        TypeError,
        ValueError,
    ) as exc:
        raise LlmRequirementSilverRegistryError(
            "The LLM requirement Silver registry failed strict validation."
        ) from exc


def canonical_llm_requirement_silver_sha256(
    registry: LlmRequirementSilverRegistry,
) -> str:
    if not isinstance(registry, LlmRequirementSilverRegistry):
        raise TypeError("registry must be a validated LlmRequirementSilverRegistry")
    payload = json.dumps(
        registry.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def canonical_reviewed_source_content_sha256(
    source_ir: SourceIRSnapshot,
    source_refs: tuple[StructureSourceRef, ...],
) -> str:
    """Hash the complete reviewed text set without exposing per-item digests."""

    if not isinstance(source_ir, SourceIRSnapshot):
        raise TypeError("source_ir must be a SourceIRSnapshot")
    if not source_refs:
        raise ValueError("reviewed source refs must not be empty")
    if any(not isinstance(item, StructureSourceRef) for item in source_refs):
        raise TypeError("source_refs must contain StructureSourceRef values")
    if len(source_refs) != len(set(source_refs)):
        raise ValueError("reviewed source refs must be unique")
    source_node_text = {item.source_node_id: item.raw_text for item in source_ir.nodes}
    table_cell_text = {
        cell.cell_id: cell.raw_text
        for node in source_ir.nodes
        if node.table_payload is not None
        for cell in node.table_payload.cells
    }
    visual_text = {
        observation.observation_id: observation.raw_text
        for snapshot in source_ir.visual_text_snapshots
        for observation in snapshot.observations
    }
    text_by_kind = {
        StructureSourceRefKind.SOURCE_NODE: source_node_text,
        StructureSourceRefKind.TABLE_CELL: table_cell_text,
        StructureSourceRefKind.VISUAL_TEXT_OBSERVATION: visual_text,
    }
    reviewed_content: list[tuple[str, str, str]] = []
    for source_ref in sorted(
        source_refs,
        key=lambda item: (item.kind.value, item.source_id),
    ):
        try:
            raw_text = text_by_kind[source_ref.kind][source_ref.source_id]
        except KeyError as exc:
            raise ValueError("reviewed source ref is absent from SourceIR") from exc
        reviewed_content.append((source_ref.kind.value, source_ref.source_id, raw_text))
    payload = json.dumps(
        reviewed_content,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_llm_requirement_silver_document(
    *,
    source_ir: SourceIRSnapshot,
    structure_tree: StructureTreeSnapshot,
    result: LlmRequirementExtractionResult,
) -> LlmRequirementSilverDocument:
    """Build one text-free Silver candidate from a validated in-memory run.

    This function deliberately returns only a document payload.  It cannot add
    AI-reviewed provenance or turn a model result into accepted evidence; that
    remains an explicit review and registry-publication step.
    """

    _require_pipeline_input_types(
        source_ir=source_ir,
        structure_tree=structure_tree,
        result=result,
    )
    validate_structure_tree_against_source_ir(structure_tree, source_ir)
    validate_semantic_classification_against_inputs(
        result.semantics,
        source_ir,
        structure_tree,
    )
    validate_requirement_assembly_against_inputs(
        result.assembly,
        source_ir,
        structure_tree,
        result.semantics,
    )
    validate_assembled_requirement_view_against_inputs(
        result.requirement_view,
        source_ir,
        structure_tree,
        result.semantics,
        result.assembly,
    )

    classification_runs = tuple(
        run for run in result.provider_runs if run.operation == "classify_nodes"
    )
    assembly_runs = tuple(
        run for run in result.provider_runs if run.operation == "assemble_requirements"
    )
    if not classification_runs or len(assembly_runs) != 1:
        raise ValueError(
            "Silver candidates require classification runs and one assembly run"
        )
    if len(classification_runs) + len(assembly_runs) != len(result.provider_runs):
        raise ValueError("Silver candidates cannot contain unsupported provider runs")
    if any(
        run.status is not SemanticProviderRunStatus.SUCCESS
        for run in result.provider_runs
    ):
        raise ValueError("Silver candidates require successful provider runs")
    if any(
        annotation.status is SemanticAnnotationStatus.UNRESOLVED
        for annotation in result.semantics.annotations
    ):
        raise ValueError("Silver candidates cannot contain unresolved semantics")
    if any(
        item.status is not RequirementAssemblyStatus.CANDIDATE
        for item in result.assembly.items
    ):
        raise ValueError("Silver candidates cannot contain unresolved assembly items")

    classification = classification_runs[0]
    classification_binding = _provider_binding_key(classification)
    if any(
        _provider_binding_key(run) != classification_binding
        for run in classification_runs[1:]
    ):
        raise ValueError("classification runs must share one provider binding")
    assembly = assembly_runs[0]
    if (
        assembly.provider_id,
        assembly.provider_version,
        assembly.model_id,
    ) != (
        classification.provider_id,
        classification.provider_version,
        classification.model_id,
    ):
        raise ValueError("classification and assembly must share one provider model")
    if result.assembly.provider_run_id != assembly.provider_run_id:
        raise ValueError("assembly snapshot must bind the recorded assembly run")

    annotations_by_run: dict[str, list[str]] = {
        run.provider_run_id: [] for run in classification_runs
    }
    for annotation in result.semantics.annotations:
        if annotation.provider_run_id not in annotations_by_run:
            raise ValueError("semantic annotation must bind a classification run")
        annotations_by_run[annotation.provider_run_id].append(
            annotation.target_structure_node_id
        )
    classification_run_bindings = tuple(
        LlmRequirementSilverClassificationRunBinding(
            request_sha256=run.request_sha256,
            target_structure_node_ids=tuple(annotations_by_run[run.provider_run_id]),
        )
        for run in classification_runs
    )

    nodes_by_id = {item.structure_node_id: item for item in structure_tree.nodes}
    component_for, component_members = _continuation_components(structure_tree)
    units = _projection_units(
        result.semantics.annotations,
        nodes_by_id=nodes_by_id,
        component_for=component_for,
        component_members=component_members,
    )
    units_by_target = {item.target_structure_node_id: item for item in units}
    assembly_by_target = {
        item.target_structure_node_id: item for item in result.assembly.items
    }
    view_by_target = {
        item.target_structure_node_id: item for item in result.requirement_view.nodes
    }
    view_id_to_target = {
        item.requirement_view_node_id: item.target_structure_node_id
        for item in result.requirement_view.nodes
    }
    annotations_by_id = {
        item.semantic_annotation_id: item for item in result.semantics.annotations
    }

    items: dict[str, LlmRequirementSilverItem] = {}
    for target_id in result.assembly.scope_structure_node_ids:
        unit = units_by_target[target_id]
        assembly_item = assembly_by_target[target_id]
        view = view_by_target.get(target_id)
        source_refs = view.source_refs if view is not None else unit.source_refs
        full_context_refs = view.full_context_source_refs if view is not None else ()
        evidence_annotations = (
            tuple(
                annotations_by_id[annotation_id]
                for annotation_id in view.semantic_annotation_ids
            )
            if view is not None
            else unit.annotations
        )
        selected_evidence_refs = tuple(
            dict.fromkeys(
                source_ref
                for annotation in evidence_annotations
                for source_ref in annotation.selected_evidence_source_refs
            )
        )
        parent_target = None
        decision_mode = None
        if view is not None:
            decision_mode = view.decision_mode
            if view.parent_requirement_view_node_id is not None:
                parent_target = view_id_to_target[view.parent_requirement_view_node_id]
        items[target_id] = LlmRequirementSilverItem(
            target_structure_node_id=target_id,
            target_kind=nodes_by_id[target_id].kind,
            source_order=assembly_item.source_order,
            semantic_class=unit.semantic_class,
            requirement_subtype=unit.requirement_subtype,
            semantic_reason_codes=tuple(
                sorted(
                    {
                        reason_code
                        for annotation in unit.annotations
                        for reason_code in annotation.reason_codes
                    }
                )
            ),
            is_continuation_fragment=all(
                annotation.is_continuation_fragment for annotation in unit.annotations
            ),
            include_in_view=assembly_item.include_in_view,
            role=assembly_item.role,
            parent_target_structure_node_id=parent_target,
            decision_mode=decision_mode,
            source_span_ids=(
                view.source_span_ids if view is not None else unit.source_span_ids
            ),
            selected_evidence_source_refs=tuple(
                _bind_source_ref(source_ir, source_ref)
                for source_ref in selected_evidence_refs
            ),
            source_refs=tuple(
                _bind_source_ref(source_ir, source_ref) for source_ref in source_refs
            ),
            full_context_source_refs=tuple(
                _bind_source_ref(source_ir, source_ref)
                for source_ref in full_context_refs
            ),
        )

    reviewed_source_refs = tuple(
        dict.fromkeys(
            bound_ref.source_ref
            for target_id in result.assembly.scope_structure_node_ids
            for bound_ref in (
                *items[target_id].selected_evidence_source_refs,
                *items[target_id].source_refs,
                *items[target_id].full_context_source_refs,
            )
        )
    )
    return LlmRequirementSilverDocument(
        source_sha256=source_ir.source_sha256,
        reviewed_source_content_sha256=canonical_reviewed_source_content_sha256(
            source_ir,
            reviewed_source_refs,
        ),
        source_adapter=LlmRequirementSilverComponentBinding(
            component_id=source_ir.adapter_id,
            component_version=source_ir.adapter_version,
        ),
        structure_resolver=LlmRequirementSilverComponentBinding(
            component_id=structure_tree.resolver_id,
            component_version=structure_tree.resolver_version,
        ),
        semantic_provider=LlmRequirementSilverSemanticProviderBinding(
            provider_id=classification.provider_id,
            provider_version=classification.provider_version,
            model_id=classification.model_id,
            classification_prompt_version=classification.prompt_version,
            classification_response_schema_version=(
                classification.response_schema_version
            ),
            classification_run_count=len(classification_runs),
            classification_runs=classification_run_bindings,
            assembly_prompt_version=assembly.prompt_version,
            assembly_response_schema_version=assembly.response_schema_version,
            assembly_request_sha256=assembly.request_sha256,
        ),
        semantic_classifier=LlmRequirementSilverComponentBinding(
            component_id=result.semantics.classifier_id,
            component_version=_implementation_version(
                result.semantics.classifier_version,
                expected_model_id=classification.model_id,
                expected_prompt_version=classification.prompt_version,
            ),
        ),
        requirement_assembler=LlmRequirementSilverComponentBinding(
            component_id=result.assembly.assembler_id,
            component_version=_implementation_version(
                result.assembly.assembler_version,
                expected_model_id=assembly.model_id,
                expected_prompt_version=assembly.prompt_version,
            ),
        ),
        requirement_projector=LlmRequirementSilverComponentBinding(
            component_id=result.requirement_view.projector_id,
            component_version=result.requirement_view.projector_version,
        ),
        semantic_scope_structure_node_ids=(result.semantics.scope_structure_node_ids),
        assembly_scope_structure_node_ids=(result.assembly.scope_structure_node_ids),
        items=items,
    )


def evaluate_llm_requirement_silver(
    *,
    document_id: str,
    source_ir: SourceIRSnapshot,
    structure_tree: StructureTreeSnapshot,
    result: LlmRequirementExtractionResult,
    registry: LlmRequirementSilverRegistry,
) -> LlmRequirementSilverReport:
    """Evaluate one in-memory LLM extraction against accepted Silver.

    No snapshots are reconstructed from a report. The evaluator validates the
    supplied SourceIR -> StructureTree -> semantics -> assembly -> view chain,
    then checks exact scopes and every target's text-free grounding contract.
    """

    _require_input_types(
        source_ir=source_ir,
        structure_tree=structure_tree,
        result=result,
        registry=registry,
    )
    if not _DOCUMENT_ID_PATTERN.fullmatch(document_id):
        raise ValueError("document_id must be a machine-readable key")

    checks: list[LlmRequirementSilverCheck] = []
    silver = registry.documents.get(document_id)
    checks.append(
        _check(
            "binding.document_id",
            "binding",
            document_id,
            document_id if silver is not None else None,
            silver is not None,
            "document_not_registered",
        )
    )
    if silver is None:
        return _report(
            registry,
            document_id,
            source_ir.source_sha256,
            result,
            checks,
        )

    _append_binding_checks(checks, source_ir, structure_tree, result, silver)
    pipeline_valid = _append_pipeline_validation_checks(
        checks,
        source_ir,
        structure_tree,
        result,
    )
    _append_provider_checks(checks, result, silver)
    _append_scope_checks(checks, result, silver)
    if pipeline_valid:
        _append_item_checks(checks, source_ir, structure_tree, result, silver)
    else:
        checks.append(
            _check(
                "pipeline.item_evaluation",
                "pipeline",
                "validated",
                "blocked",
                False,
                "pipeline_validation_failed",
            )
        )
    return _report(
        registry,
        document_id,
        source_ir.source_sha256,
        result,
        checks,
    )


def _append_binding_checks(
    checks: list[LlmRequirementSilverCheck],
    source_ir: SourceIRSnapshot,
    tree: StructureTreeSnapshot,
    result: LlmRequirementExtractionResult,
    silver: LlmRequirementSilverDocument,
) -> None:
    reviewed_source_refs = tuple(
        dict.fromkeys(
            bound_ref.source_ref
            for target_id in silver.assembly_scope_structure_node_ids
            for bound_ref in (
                *silver.items[target_id].selected_evidence_source_refs,
                *silver.items[target_id].source_refs,
                *silver.items[target_id].full_context_source_refs,
            )
        )
    )
    observed_reviewed_content_sha256 = canonical_reviewed_source_content_sha256(
        source_ir,
        reviewed_source_refs,
    )
    values = (
        ("source_sha256", silver.source_sha256, source_ir.source_sha256),
        (
            "reviewed_source_content_sha256",
            silver.reviewed_source_content_sha256,
            observed_reviewed_content_sha256,
        ),
        ("adapter_id", silver.source_adapter.component_id, source_ir.adapter_id),
        (
            "adapter_version",
            silver.source_adapter.component_version,
            source_ir.adapter_version,
        ),
        (
            "resolver_id",
            silver.structure_resolver.component_id,
            tree.resolver_id,
        ),
        (
            "resolver_version",
            silver.structure_resolver.component_version,
            tree.resolver_version,
        ),
        (
            "classifier_id",
            silver.semantic_classifier.component_id,
            result.semantics.classifier_id,
        ),
        (
            "classifier_version",
            silver.semantic_classifier.component_version,
            _implementation_version(
                result.semantics.classifier_version,
                expected_model_id=silver.semantic_provider.model_id,
                expected_prompt_version=(
                    silver.semantic_provider.classification_prompt_version
                ),
            ),
        ),
        (
            "assembler_id",
            silver.requirement_assembler.component_id,
            result.assembly.assembler_id,
        ),
        (
            "assembler_version",
            silver.requirement_assembler.component_version,
            _implementation_version(
                result.assembly.assembler_version,
                expected_model_id=silver.semantic_provider.model_id,
                expected_prompt_version=(
                    silver.semantic_provider.assembly_prompt_version
                ),
            ),
        ),
        (
            "projector_id",
            silver.requirement_projector.component_id,
            result.requirement_view.projector_id,
        ),
        (
            "projector_version",
            silver.requirement_projector.component_version,
            result.requirement_view.projector_version,
        ),
    )
    for name, expected, observed in values:
        checks.append(
            _check(
                f"binding.{name}",
                "binding",
                expected,
                observed,
                expected == observed,
                f"{name}_mismatch",
            )
        )


def _implementation_version(
    version: str,
    *,
    expected_model_id: str,
    expected_prompt_version: str,
) -> str:
    """Return the stable implementation version for a known run suffix.

    Unknown or malformed suffixes remain part of the observed value so that the
    component binding fails closed instead of silently accepting new metadata.
    """

    if ";" not in version:
        return version
    implementation_version, suffix = version.split(";", 1)
    match = re.fullmatch(
        r"model=(?P<model>[^;]+);prompt=(?P<prompt>[^;]+);"
        r"result=[0-9a-f]{12}",
        suffix,
    )
    expected_prompt_tail = expected_prompt_version.rsplit("/", 1)[-1]
    if (
        match is not None
        and match.group("model") == expected_model_id
        and match.group("prompt") == expected_prompt_tail
    ):
        return implementation_version
    return version


def _append_pipeline_validation_checks(
    checks: list[LlmRequirementSilverCheck],
    source_ir: SourceIRSnapshot,
    tree: StructureTreeSnapshot,
    result: LlmRequirementExtractionResult,
) -> bool:
    validators = (
        (
            "structure",
            lambda: validate_structure_tree_against_source_ir(tree, source_ir),
        ),
        (
            "semantics",
            lambda: validate_semantic_classification_against_inputs(
                result.semantics,
                source_ir,
                tree,
            ),
        ),
        (
            "assembly",
            lambda: validate_requirement_assembly_against_inputs(
                result.assembly,
                source_ir,
                tree,
                result.semantics,
            ),
        ),
        (
            "view",
            lambda: validate_assembled_requirement_view_against_inputs(
                result.requirement_view,
                source_ir,
                tree,
                result.semantics,
                result.assembly,
            ),
        ),
    )
    valid = True
    for name, validator in validators:
        try:
            validator()
            matched = True
        except (TypeError, ValueError, RuntimeError):
            matched = False
            valid = False
        checks.append(
            _check(
                f"pipeline.{name}",
                "pipeline",
                "valid",
                "valid" if matched else "invalid",
                matched,
                f"{name}_validation_failed",
            )
        )
    return valid


def _append_provider_checks(
    checks: list[LlmRequirementSilverCheck],
    result: LlmRequirementExtractionResult,
    silver: LlmRequirementSilverDocument,
) -> None:
    runs = result.provider_runs
    provider = silver.semantic_provider
    classification_runs = tuple(
        run for run in runs if run.operation == "classify_nodes"
    )
    assembly_runs = tuple(
        run for run in runs if run.operation == "assemble_requirements"
    )
    observed_operations = tuple(sorted({run.operation for run in runs}))
    expected_operations = ("assemble_requirements", "classify_nodes")
    checks.append(
        _check(
            "provider.operation_set",
            "provider",
            expected_operations,
            observed_operations,
            observed_operations == expected_operations,
            "provider_operation_set_mismatch",
        )
    )
    for operation, expected_count, operation_runs in (
        (
            "classification",
            provider.classification_run_count,
            classification_runs,
        ),
        ("assembly", provider.assembly_run_count, assembly_runs),
    ):
        checks.append(
            _check(
                f"provider.{operation}_run_count",
                "provider",
                expected_count,
                len(operation_runs),
                len(operation_runs) == expected_count,
                "provider_run_count_mismatch",
            )
        )

    statuses = tuple(run.status.value for run in runs)
    checks.append(
        _check(
            "provider.success",
            "provider",
            tuple("SUCCESS" for _ in runs),
            statuses,
            bool(runs)
            and all(run.status is SemanticProviderRunStatus.SUCCESS for run in runs),
            "provider_run_not_successful",
        )
    )
    run_ids = tuple(run.provider_run_id for run in runs)
    checks.append(
        _check(
            "provider.run_id_uniqueness",
            "provider",
            len(run_ids),
            len(set(run_ids)),
            len(run_ids) == len(set(run_ids)),
            "provider_run_id_duplicate",
        )
    )
    for index, expected_run in enumerate(provider.classification_runs):
        observed_run = (
            classification_runs[index] if index < len(classification_runs) else None
        )
        observed_request_sha256 = (
            observed_run.request_sha256 if observed_run is not None else None
        )
        checks.append(
            _check(
                f"provider.classification_run_{index}.request_sha256",
                "provider",
                expected_run.request_sha256,
                observed_request_sha256,
                observed_request_sha256 == expected_run.request_sha256,
                "provider_request_hash_mismatch",
            )
        )
        observed_target_ids = (
            tuple(
                annotation.target_structure_node_id
                for annotation in result.semantics.annotations
                if annotation.provider_run_id == observed_run.provider_run_id
            )
            if observed_run is not None
            else ()
        )
        checks.append(
            _check(
                f"provider.classification_run_{index}.target_structure_node_ids",
                "provider",
                expected_run.target_structure_node_ids,
                observed_target_ids,
                observed_target_ids == expected_run.target_structure_node_ids,
                "provider_classification_targets_mismatch",
            )
        )

    observed_assembly_request_sha256 = (
        assembly_runs[0].request_sha256 if len(assembly_runs) == 1 else None
    )
    checks.append(
        _check(
            "provider.assembly_request_sha256",
            "provider",
            provider.assembly_request_sha256,
            observed_assembly_request_sha256,
            observed_assembly_request_sha256 == provider.assembly_request_sha256,
            "provider_request_hash_mismatch",
        )
    )
    semantic_unresolved = sum(
        annotation.semantic_class is SemanticClass.UNRESOLVED
        or annotation.status is SemanticAnnotationStatus.UNRESOLVED
        for annotation in result.semantics.annotations
    )
    assembly_unresolved = sum(
        item.status is RequirementAssemblyStatus.UNRESOLVED
        for item in result.assembly.items
    )
    for name, observed in (
        ("semantic_unresolved_count", semantic_unresolved),
        ("assembly_unresolved_count", assembly_unresolved),
    ):
        checks.append(
            _check(
                f"provider.{name}",
                "provider",
                0,
                observed,
                observed == 0,
                "unresolved_output_present",
            )
        )

    for operation, operation_runs, prompt_version, schema_version in (
        (
            "classification",
            classification_runs,
            provider.classification_prompt_version,
            provider.classification_response_schema_version,
        ),
        (
            "assembly",
            assembly_runs,
            provider.assembly_prompt_version,
            provider.assembly_response_schema_version,
        ),
    ):
        observed_bindings = tuple(
            sorted({_provider_binding_key(run) for run in operation_runs})
        )
        expected_binding = (
            f"{provider.provider_id}:{provider.provider_version}:"
            f"{provider.model_id}:{prompt_version}:{schema_version}"
        )
        checks.append(
            _check(
                f"provider.{operation}_binding",
                "provider",
                (expected_binding,),
                observed_bindings,
                observed_bindings == (expected_binding,),
                "provider_version_binding_mismatch",
            )
        )

    classification_run_ids = {run.provider_run_id for run in classification_runs}
    observed_annotation_run_ids = {
        annotation.provider_run_id
        for annotation in result.semantics.annotations
        if annotation.provider_run_id is not None
    }
    checks.append(
        _check(
            "provider.semantic_run_binding",
            "provider",
            tuple(sorted(classification_run_ids)),
            tuple(sorted(observed_annotation_run_ids)),
            observed_annotation_run_ids == classification_run_ids,
            "semantic_provider_run_binding_mismatch",
        )
    )
    assembly_run_ids = {run.provider_run_id for run in assembly_runs}
    checks.append(
        _check(
            "provider.assembly_run_binding",
            "provider",
            tuple(sorted(assembly_run_ids)),
            (result.assembly.provider_run_id,),
            assembly_run_ids == {result.assembly.provider_run_id},
            "assembly_provider_run_binding_mismatch",
        )
    )


def _append_scope_checks(
    checks: list[LlmRequirementSilverCheck],
    result: LlmRequirementExtractionResult,
    silver: LlmRequirementSilverDocument,
) -> None:
    scopes = (
        (
            "semantic",
            silver.semantic_scope_structure_node_ids,
            result.semantics.scope_structure_node_ids,
        ),
        (
            "assembly",
            silver.assembly_scope_structure_node_ids,
            result.assembly.scope_structure_node_ids,
        ),
    )
    for name, expected, observed in scopes:
        _append_exact_sequence_checks(checks, name, expected, observed)

    expected_items = tuple(silver.assembly_scope_structure_node_ids)
    observed_items = tuple(
        item.target_structure_node_id for item in result.assembly.items
    )
    _append_exact_sequence_checks(checks, "items", expected_items, observed_items)


def _append_exact_sequence_checks(
    checks: list[LlmRequirementSilverCheck],
    name: str,
    expected: tuple[str, ...],
    observed: tuple[str, ...],
) -> None:
    expected_set = set(expected)
    observed_set = set(observed)
    values = (
        (
            "exact_order",
            expected,
            observed,
            expected == observed,
            "scope_order_mismatch",
        ),
        (
            "missing",
            (),
            tuple(sorted(expected_set - observed_set)),
            expected_set.issubset(observed_set),
            "scope_target_missing",
        ),
        (
            "extra",
            (),
            tuple(sorted(observed_set - expected_set)),
            observed_set.issubset(expected_set),
            "scope_target_extra",
        ),
        (
            "duplicate",
            (),
            tuple(sorted(_duplicates(observed))),
            len(observed) == len(observed_set),
            "scope_target_duplicate",
        ),
    )
    for suffix, expected_value, observed_value, matched, failure in values:
        checks.append(
            _check(
                f"scope.{name}.{suffix}",
                "scope",
                expected_value,
                observed_value,
                matched,
                failure,
            )
        )


def _append_item_checks(
    checks: list[LlmRequirementSilverCheck],
    source_ir: SourceIRSnapshot,
    tree: StructureTreeSnapshot,
    result: LlmRequirementExtractionResult,
    silver: LlmRequirementSilverDocument,
) -> None:
    tree_nodes = {item.structure_node_id: item for item in tree.nodes}
    assembly_items = {
        item.target_structure_node_id: item for item in result.assembly.items
    }
    view_nodes = {
        item.target_structure_node_id: item for item in result.requirement_view.nodes
    }
    view_id_to_target = {
        item.requirement_view_node_id: item.target_structure_node_id
        for item in result.requirement_view.nodes
    }
    component_for, component_members = _continuation_components(tree)
    units = _projection_units(
        result.semantics.annotations,
        nodes_by_id=tree_nodes,
        component_for=component_for,
        component_members=component_members,
    )
    units_by_target = {item.target_structure_node_id: item for item in units}
    annotations_by_id = {
        item.semantic_annotation_id: item for item in result.semantics.annotations
    }

    for target_id in silver.assembly_scope_structure_node_ids:
        expected = silver.items[target_id]
        tree_node = tree_nodes.get(target_id)
        assembly_item = assembly_items.get(target_id)
        unit = units_by_target.get(target_id)
        view_node = view_nodes.get(target_id)
        available = (
            tree_node is not None and assembly_item is not None and unit is not None
        )
        checks.append(
            _check(
                f"item.{target_id}.available",
                "item",
                True,
                available,
                available,
                "item_target_unavailable",
            )
        )
        if not available:
            continue
        assert tree_node is not None
        assert assembly_item is not None
        assert unit is not None

        observed_parent = None
        observed_decision = None
        observed_source_refs: tuple[StructureSourceRef, ...] = unit.source_refs
        observed_source_span_ids = unit.source_span_ids
        observed_selected_evidence_refs = tuple(
            dict.fromkeys(
                source_ref
                for annotation in unit.annotations
                for source_ref in annotation.selected_evidence_source_refs
            )
        )
        observed_is_continuation_fragment = all(
            annotation.is_continuation_fragment for annotation in unit.annotations
        )
        observed_semantic_reason_codes = tuple(
            sorted(
                {
                    reason_code
                    for annotation in unit.annotations
                    for reason_code in annotation.reason_codes
                }
            )
        )
        observed_full_context_refs: tuple[StructureSourceRef, ...] = ()
        if view_node is not None:
            if view_node.parent_requirement_view_node_id is not None:
                observed_parent = view_id_to_target.get(
                    view_node.parent_requirement_view_node_id
                )
            observed_decision = view_node.decision_mode.value
            observed_source_refs = view_node.source_refs
            observed_source_span_ids = view_node.source_span_ids
            observed_selected_evidence_refs = tuple(
                dict.fromkeys(
                    source_ref
                    for annotation_id in view_node.semantic_annotation_ids
                    for source_ref in annotations_by_id[
                        annotation_id
                    ].selected_evidence_source_refs
                )
            )
            observed_full_context_refs = view_node.full_context_source_refs

        values: tuple[tuple[str, object, object], ...] = (
            ("target_kind", expected.target_kind.value, tree_node.kind.value),
            ("source_order", expected.source_order, assembly_item.source_order),
            (
                "semantic_class",
                expected.semantic_class.value,
                unit.semantic_class.value,
            ),
            (
                "requirement_subtype",
                _enum_value(expected.requirement_subtype),
                _enum_value(unit.requirement_subtype),
            ),
            (
                "semantic_reason_codes",
                expected.semantic_reason_codes,
                observed_semantic_reason_codes,
            ),
            (
                "continuation_fragment",
                expected.is_continuation_fragment,
                observed_is_continuation_fragment,
            ),
            ("include", expected.include_in_view, assembly_item.include_in_view),
            ("role", _enum_value(expected.role), _enum_value(assembly_item.role)),
            (
                "parent",
                expected.parent_target_structure_node_id,
                observed_parent,
            ),
            ("decision", _enum_value(expected.decision_mode), observed_decision),
            (
                "span_ids",
                expected.source_span_ids,
                observed_source_span_ids,
            ),
            (
                "selected_evidence_refs",
                _bound_ref_keys(expected.selected_evidence_source_refs),
                _source_ref_keys(observed_selected_evidence_refs),
            ),
            (
                "source_refs",
                _bound_ref_keys(expected.source_refs),
                _source_ref_keys(observed_source_refs),
            ),
            (
                "full_context_refs",
                _bound_ref_keys(expected.full_context_source_refs),
                _source_ref_keys(observed_full_context_refs),
            ),
        )
        for suffix, expected_value, observed_value in values:
            matched = expected_value == observed_value
            checks.append(
                _check(
                    f"item.{target_id}.{suffix}",
                    "item",
                    expected_value,  # type: ignore[arg-type]
                    observed_value,  # type: ignore[arg-type]
                    matched,
                    f"item_{suffix}_mismatch",
                )
            )

        observed_view_presence = view_node is not None
        checks.append(
            _check(
                f"item.{target_id}.view_presence",
                "item",
                expected.include_in_view,
                observed_view_presence,
                expected.include_in_view == observed_view_presence,
                "item_view_presence_mismatch",
            )
        )
        if (
            assembly_item.parent_structure_node_id
            != expected.parent_target_structure_node_id
        ):
            checks.append(
                _check(
                    f"item.{target_id}.assembly_parent",
                    "item",
                    expected.parent_target_structure_node_id,
                    assembly_item.parent_structure_node_id,
                    False,
                    "item_assembly_parent_mismatch",
                )
            )
        else:
            checks.append(
                _check(
                    f"item.{target_id}.assembly_parent",
                    "item",
                    expected.parent_target_structure_node_id,
                    assembly_item.parent_structure_node_id,
                    True,
                    "item_assembly_parent_mismatch",
                )
            )

        locator_refs = _dedupe_bound_refs(
            (
                *expected.selected_evidence_source_refs,
                *expected.source_refs,
                *expected.full_context_source_refs,
            )
        )
        for bound_ref in locator_refs:
            observed_locator = _resolve_source_locator(source_ir, bound_ref.source_ref)
            expected_digest = _locator_digest(bound_ref.locator)
            observed_digest = (
                _locator_digest(observed_locator)
                if observed_locator is not None
                else None
            )
            ref_key = f"{bound_ref.kind.value}:{bound_ref.source_id}"
            checks.append(
                _check(
                    f"locator.{target_id}.{hashlib.sha256(ref_key.encode()).hexdigest()[:16]}",
                    "locator",
                    expected_digest,
                    observed_digest,
                    bound_ref.locator == observed_locator,
                    "source_locator_mismatch",
                )
            )


def _resolve_source_locator(
    source_ir: SourceIRSnapshot,
    source_ref: StructureSourceRef,
) -> LlmRequirementSilverLocator | None:
    if source_ref.kind is StructureSourceRefKind.SOURCE_NODE:
        source = next(
            (
                item
                for item in source_ir.nodes
                if item.source_node_id == source_ref.source_id
            ),
            None,
        )
        if source is None:
            return None
        return _native_locator(source.native_locator)
    if source_ref.kind is StructureSourceRefKind.TABLE_CELL:
        cell = next(
            (
                cell
                for node in source_ir.nodes
                if node.table_payload is not None
                for cell in node.table_payload.cells
                if cell.cell_id == source_ref.source_id
            ),
            None,
        )
        if cell is None:
            return None
        return _native_locator(cell.native_locator)
    observation = next(
        (
            item
            for snapshot in source_ir.visual_text_snapshots
            for item in snapshot.observations
            if item.observation_id == source_ref.source_id
        ),
        None,
    )
    if observation is None:
        return None
    return LlmRequirementSilverPdfOcrLocator(
        locator_kind="PDF_OCR",
        page_number=observation.page_number,
        bbox=observation.bbox,
        provider_id=observation.provider_id,
        provider_version=observation.provider_version,
        page_image_sha256=observation.page_image_sha256,
    )


def _bind_source_ref(
    source_ir: SourceIRSnapshot,
    source_ref: StructureSourceRef,
) -> LlmRequirementSilverBoundSourceRef:
    locator = _resolve_source_locator(source_ir, source_ref)
    if locator is None:
        raise ValueError(
            "Silver source refs require a resolvable PDF native or OCR locator"
        )
    return LlmRequirementSilverBoundSourceRef(
        kind=source_ref.kind,
        source_id=source_ref.source_id,
        locator=locator,
    )


def _native_locator(locator: NativeLocator) -> LlmRequirementSilverLocator | None:
    if locator.format is not NativeDocumentFormat.PDF or locator.page_number is None:
        return None
    return LlmRequirementSilverPdfNativeLocator(
        locator_kind="PDF_NATIVE",
        page_number=locator.page_number,
        block_index=locator.block_index,
        bbox=locator.bbox,
        table_index=locator.table_index,
        row_index=locator.row_index,
        column_index=locator.column_index,
    )


def _provider_binding_key(run: object) -> str:
    return ":".join(
        (
            str(getattr(run, "provider_id")),
            str(getattr(run, "provider_version")),
            str(getattr(run, "model_id")),
            str(getattr(run, "prompt_version")),
            str(getattr(run, "response_schema_version")),
        )
    )


def _bound_ref_keys(
    refs: tuple[LlmRequirementSilverBoundSourceRef, ...],
) -> tuple[str, ...]:
    return tuple(f"{item.kind.value}:{item.source_id}" for item in refs)


def _source_ref_keys(refs: tuple[StructureSourceRef, ...]) -> tuple[str, ...]:
    return tuple(f"{item.kind.value}:{item.source_id}" for item in refs)


def _dedupe_bound_refs(
    refs: tuple[LlmRequirementSilverBoundSourceRef, ...],
) -> tuple[LlmRequirementSilverBoundSourceRef, ...]:
    return tuple({(item.kind, item.source_id): item for item in refs}.values())


def _locator_digest(locator: LlmRequirementSilverLocator) -> str:
    payload = json.dumps(
        locator.model_dump(mode="json", exclude_none=True),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _enum_value(value: object) -> str | None:
    if value is None:
        return None
    return str(getattr(value, "value"))


def _duplicates(values: tuple[str, ...]) -> set[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return duplicates


def _check(
    check_id: str,
    category: LlmRequirementSilverCheckCategory,
    expected: SilverCheckValue,
    observed: SilverCheckValue,
    matches: bool,
    failure_reason: str,
) -> LlmRequirementSilverCheck:
    return LlmRequirementSilverCheck(
        check_id=check_id,
        category=category,
        status=(
            LlmRequirementSilverCheckStatus.PASS
            if matches
            else LlmRequirementSilverCheckStatus.FAIL
        ),
        reason_code="matched" if matches else failure_reason,
        expected=_safe_machine_value(expected),
        observed=_safe_machine_value(observed),
    )


def _report(
    registry: LlmRequirementSilverRegistry,
    document_id: str,
    source_sha256: str,
    result: LlmRequirementExtractionResult,
    checks: list[LlmRequirementSilverCheck],
) -> LlmRequirementSilverReport:
    passed = sum(item.status is LlmRequirementSilverCheckStatus.PASS for item in checks)
    failed = len(checks) - passed
    return LlmRequirementSilverReport(
        registry_sha256=canonical_llm_requirement_silver_sha256(registry),
        document_id=document_id,
        source_sha256=source_sha256,
        semantic_unit_plan_id=result.semantics.semantic_unit_plan_id,
        semantic_unit_ids=result.semantics.scope_semantic_unit_ids,
        owner_structure_node_ids=(result.semantics.scope_owner_structure_node_ids),
        status=(
            LlmRequirementSilverCheckStatus.PASS
            if checks and failed == 0
            else LlmRequirementSilverCheckStatus.FAIL
        ),
        passed_check_count=passed,
        failed_check_count=failed,
        checks=tuple(checks),
    )


def _load_json_object_strict(path: Path) -> dict[str, Any]:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise LlmRequirementSilverRegistryError(
                    "The LLM Silver JSON contains a duplicate key."
                )
            result[key] = value
        return result

    payload = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=reject_duplicate_keys,
    )
    if not isinstance(payload, dict):
        raise LlmRequirementSilverRegistryError(
            "The LLM Silver JSON root must be an object."
        )
    return payload


def _require_machine_value(value: SilverCheckValue) -> None:
    if value is None or isinstance(value, (bool, int)):
        return
    values = value if isinstance(value, tuple) else (value,)
    for item in values:
        if isinstance(item, (bool, int)):
            continue
        if not isinstance(item, str) or not _MACHINE_VALUE_PATTERN.fullmatch(item):
            raise ValueError("LLM Silver report values must be machine-readable")


def _safe_machine_value(value: SilverCheckValue) -> SilverCheckValue:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, tuple):
        return tuple(
            _safe_machine_string(item) if isinstance(item, str) else item
            for item in value
        )
    return _safe_machine_string(value)


def _safe_machine_string(value: str) -> str:
    if _MACHINE_VALUE_PATTERN.fullmatch(value):
        return value
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _require_pipeline_input_types(
    *,
    source_ir: SourceIRSnapshot,
    structure_tree: StructureTreeSnapshot,
    result: LlmRequirementExtractionResult,
) -> None:
    expected = (
        (source_ir, SourceIRSnapshot, "source_ir"),
        (structure_tree, StructureTreeSnapshot, "structure_tree"),
        (result, LlmRequirementExtractionResult, "result"),
    )
    for value, expected_type, name in expected:
        if not isinstance(value, expected_type):
            raise TypeError(f"{name} must be a {expected_type.__name__}")
    result_values = (
        (result.semantics, SemanticClassificationSnapshot, "result.semantics"),
        (result.assembly, RequirementAssemblySnapshot, "result.assembly"),
        (
            result.requirement_view,
            RequirementViewSnapshot,
            "result.requirement_view",
        ),
    )
    for value, expected_type, name in result_values:
        if not isinstance(value, expected_type):
            raise TypeError(f"{name} must be a {expected_type.__name__}")
    if not isinstance(result.provider_runs, tuple) or any(
        not isinstance(item, SemanticProviderRunRecord) for item in result.provider_runs
    ):
        raise TypeError("result.provider_runs must contain provider run records")


def _require_input_types(
    *,
    source_ir: SourceIRSnapshot,
    structure_tree: StructureTreeSnapshot,
    result: LlmRequirementExtractionResult,
    registry: LlmRequirementSilverRegistry,
) -> None:
    _require_pipeline_input_types(
        source_ir=source_ir,
        structure_tree=structure_tree,
        result=result,
    )
    if not isinstance(registry, LlmRequirementSilverRegistry):
        raise TypeError("registry must be a LlmRequirementSilverRegistry")


__all__ = [
    "DEFAULT_LLM_REQUIREMENT_SILVER_PATH",
    "DEFAULT_LLM_REQUIREMENT_SILVER_SCHEMA_PATH",
    "LLM_REQUIREMENT_SILVER_EVALUATION_SCHEMA_VERSION",
    "LLM_REQUIREMENT_SILVER_SCHEMA_VERSION",
    "LlmRequirementSilverBoundSourceRef",
    "LlmRequirementSilverCheck",
    "LlmRequirementSilverCheckStatus",
    "LlmRequirementSilverDocument",
    "LlmRequirementSilverItem",
    "LlmRequirementSilverPdfNativeLocator",
    "LlmRequirementSilverPdfOcrLocator",
    "LlmRequirementSilverRegistry",
    "LlmRequirementSilverRegistryError",
    "LlmRequirementSilverReport",
    "build_llm_requirement_silver_document",
    "canonical_llm_requirement_silver_sha256",
    "canonical_reviewed_source_content_sha256",
    "evaluate_llm_requirement_silver",
    "load_llm_requirement_silver",
]

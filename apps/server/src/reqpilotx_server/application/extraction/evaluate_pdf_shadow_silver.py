from __future__ import annotations

from reqpilotx_server.resource_paths import development_path

import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Literal

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

from reqpilotx_server.application.extraction.classify_content import (
    validate_semantic_classification_against_inputs,
)
from reqpilotx_server.application.extraction.project_requirements import (
    validate_requirement_view_against_inputs,
)
from reqpilotx_server.application.extraction.resolve_structure import (
    validate_structure_tree_against_source_ir,
)
from reqpilotx_server.domain.extraction.requirement_view import (
    RequirementDecisionMode,
    RequirementProjectionPolicy,
    RequirementProjectionRole,
    RequirementViewNode,
    RequirementViewSnapshot,
)
from reqpilotx_server.domain.extraction.semantics import (
    RequirementSubtype,
    SemanticAnnotationStatus,
    SemanticClass,
    SemanticClassificationSnapshot,
)
from reqpilotx_server.domain.extraction.source_ir import (
    NativeDocumentFormat,
    SourceAssetDerivationKind,
    SourceDiagnosticSeverity,
    SourceIRSnapshot,
    SourceNode,
    SourceNodeKind,
    TableCellIR,
    stable_document_id,
    stable_source_ir_id,
)
from reqpilotx_server.domain.extraction.structure import (
    StructureNode,
    StructureNodeKind,
    StructureOptionState,
    StructureRelationKind,
    StructureSourceRef,
    StructureSourceRefKind,
    StructureTreeSnapshot,
)
from reqpilotx_server.domain.extraction.visual_text import (
    VISUAL_CONTENT_TYPES,
    VisualContentType,
    VisualTextObservation,
    VisualTextSnapshot,
)


PDF_SHADOW_SILVER_SCHEMA_VERSION = "pdf-shadow-silver/v1"
PDF_SHADOW_SILVER_EVALUATION_SCHEMA_VERSION = "pdf-shadow-silver-evaluation/v2"
PDF_SHADOW_SILVER_SUBSET_EVALUATION_SCHEMA_VERSION = (
    "pdf-shadow-silver-subset-evaluation/v1"
)
DEFAULT_PDF_SHADOW_SILVER_PATH = private_eval_path("extraction_cases/silver.json")
DEFAULT_PDF_SHADOW_SILVER_SCHEMA_PATH = (
    development_path('prototype') / "eval" / "extraction_cases" / "silver.schema.json"
)
_CHECK_ID_PATTERN = re.compile(r"^[a-z0-9_.:-]{1,240}$")
_DOCUMENT_ID_PATTERN = re.compile(r"^DOC-[A-Z0-9-]+$")
_MACHINE_REPORT_VALUE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/+\-]{0,239}$")
_SQUARE_OPTION_MARKER = re.compile(r"^\s*\[\s*(?P<mark>[^\]\r\n]{0,4})\s*\]")
_PAREN_OPTION_MARKER = re.compile(r"^\s*[（(]\s*(?P<mark>[√✓✔xX×]?)\s*[）)]")
_BOX_OPTION_MARKER = re.compile(r"^\s*(?P<mark>[☐☑☒□])")
_CHECKED_OPTION_MARKS = frozenset({"√", "✓", "✔", "x", "X", "×", "☑", "☒"})
_UNCHECKED_OPTION_MARKS = frozenset({"", "☐", "□"})
_REQUIRED_LIMITATIONS = frozenset(
    {
        "not_human_gold",
        "not_a_serving_activation_gate",
        "source_hash_and_visual_asset_bound",
    }
)
_SUPPORTED_NATIVE_BLOCK_DISPOSITION_DIAGNOSTIC_CODES = frozenset(
    {"pdf_tiling_pattern_tile_excluded"}
)
_PDF_IMAGE_CELL_MIN_COVERAGE = 0.98
_PDF_IMAGE_CELL_MAX_COMPETING_COVERAGE = 0.02
_PDF_IMAGE_CELL_MIN_DIMENSION_POINTS = 8.0
_PDF_IMAGE_CELL_MIN_MULTI_DIMENSION_POINTS = 16.0
_PDF_IMAGE_CELL_MIN_OPAQUE_MULTI_CELL_COVERAGE = 0.25
_PDF_IMAGE_CELL_MAX_MULTI_CELL_COUNT = 16
_PDF_IMAGE_CELL_MAX_MULTI_CELL_GRID_SLOTS = 64
_PDF_IMAGE_CELL_NATIVE_BOUNDARY_TOLERANCE_POINTS = 0.25
_PDF_IMAGE_CELL_MIN_SHARED_BOUNDARY_POINTS = 1.0


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class PdfShadowSilverRegistryError(ValueError):
    """The silver sidecar or its schema cannot be trusted."""


class PdfShadowSilverPageAsset(_StrictFrozenModel):
    page_number: int = Field(ge=1)
    input_rotation_cw_degrees: Literal[0, 90, 180, 270] = 0
    source_asset_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    page_image_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )

    @field_validator("input_rotation_cw_degrees", mode="before")
    @classmethod
    def reject_boolean_rotation(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("input page rotation must be an integer angle")
        return value

    @model_validator(mode="after")
    def require_exactly_one_hash_binding(self) -> PdfShadowSilverPageAsset:
        if (self.source_asset_sha256 is None) == (self.page_image_sha256 is None):
            raise ValueError(
                "page assets require exactly one source-asset or rendered-page hash"
            )
        return self


class PdfShadowSilverVisualTextRegionExpectation(_StrictFrozenModel):
    """Reviewed visual subregion, for example a handwritten approval area."""

    region_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,79}$")
    bbox_q100: tuple[int, int, int, int]
    observation_count: int = Field(ge=1)
    character_count: int = Field(ge=1)
    raw_text_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    layout_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def require_positive_region(self) -> PdfShadowSilverVisualTextRegionExpectation:
        _validate_bbox_q100(self.bbox_q100)
        return self


class PdfShadowSilverVisualTextFidelityExpectation(_StrictFrozenModel):
    """One provider-specific, text-free reviewed OCR page profile."""

    case_id: str = Field(pattern=r"^[A-Z]+-[0-9]{3}$")
    page_number: int = Field(ge=1)
    policy_id: Literal["visual-text-page-fidelity/v1"]
    provider_id: Literal[
        "aliyun-bailian-qwen-ocr",
        "azure-document-intelligence",
    ]
    provider_version_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observation_count: int = Field(ge=1)
    character_count: int = Field(ge=1)
    raw_text_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    layout_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewed_regions: tuple[PdfShadowSilverVisualTextRegionExpectation, ...]
    projected_requirement_count: Literal[0] = 0

    @model_validator(mode="after")
    def require_unique_reviewed_regions(
        self,
    ) -> PdfShadowSilverVisualTextFidelityExpectation:
        _require_unique(
            (region.region_id for region in self.reviewed_regions),
            "visual text reviewed region ids",
        )
        return self


class PdfShadowSilverVisualTextAnchor(_StrictFrozenModel):
    """One unique reviewed token anchored on the declared physical axis."""

    token_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    axis_center_q100: int
    max_axis_delta_q100: int = Field(ge=0, le=200)


class PdfShadowSilverVisualTextRequiredToken(_StrictFrozenModel):
    token_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    exact_occurrences: int = Field(ge=1)


class PdfShadowSilverVisualTextTokenRegion(_StrictFrozenModel):
    """Reviewed token membership selected only by observation centroid."""

    region_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,79}$")
    bbox_q100: tuple[int, int, int, int]
    membership: Literal["centroid"]
    required_tokens: tuple[PdfShadowSilverVisualTextRequiredToken, ...] = Field(
        min_length=1
    )

    @model_validator(mode="after")
    def require_valid_unique_tokens(self) -> PdfShadowSilverVisualTextTokenRegion:
        _validate_bbox_q100(self.bbox_q100)
        _require_unique(
            (item.token_sha256 for item in self.required_tokens),
            "visual text region token hashes",
        )
        return self


class PdfShadowSilverVisualTextAnchorFidelityExpectation(_StrictFrozenModel):
    """Text-free OCR guard tolerant only to reviewed axis-position jitter."""

    case_id: str = Field(pattern=r"^[A-Z]+-[0-9]{3}$")
    page_number: int = Field(ge=1)
    policy_id: Literal["visual-text-anchor-fidelity/v2"]
    provider_id: Literal[
        "aliyun-bailian-qwen-ocr",
        "azure-document-intelligence",
    ]
    provider_version_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    coverage_claim: Literal["reviewed_anchor_sequence"]
    observation_count_min: int = Field(ge=1)
    observation_count_max: int = Field(ge=1)
    character_count_min: int = Field(ge=1)
    character_count_max: int = Field(ge=1)
    ordered_raw_text_bundle_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    anchor_order_axis: Literal["x", "y"]
    ordered_anchors: tuple[PdfShadowSilverVisualTextAnchor, ...] = Field(min_length=1)
    reviewed_token_regions: tuple[
        PdfShadowSilverVisualTextTokenRegion,
        ...,
    ]
    projected_requirement_count: Literal[0] = 0

    @model_validator(mode="after")
    def require_bounded_unique_review_scope(
        self,
    ) -> PdfShadowSilverVisualTextAnchorFidelityExpectation:
        if self.observation_count_min > self.observation_count_max:
            raise ValueError("visual text observation count range is inverted")
        if self.character_count_min > self.character_count_max:
            raise ValueError("visual text character count range is inverted")
        _require_unique(
            (item.token_sha256 for item in self.ordered_anchors),
            "visual text ordered anchor token hashes",
        )
        anchor_centers = tuple(item.axis_center_q100 for item in self.ordered_anchors)
        if anchor_centers != tuple(sorted(anchor_centers)) or len(
            anchor_centers
        ) != len(set(anchor_centers)):
            raise ValueError(
                "visual text ordered anchor centers must be strictly increasing"
            )
        _require_unique(
            (item.region_id for item in self.reviewed_token_regions),
            "visual text token region ids",
        )
        return self


class PdfShadowSilverOutlineChain(_StrictFrozenModel):
    case_id: str = Field(pattern=r"^[A-Z]+-[0-9]{3}$")
    target_label: str
    ancestor_labels: tuple[str, ...]


class PdfShadowSilverContinuation(_StrictFrozenModel):
    case_id: str = Field(pattern=r"^[A-Z]+-[0-9]{3}$")
    section_label: str
    from_page: int = Field(ge=1)
    to_page: int = Field(ge=1)
    node_kind: StructureNodeKind | None = None
    numbered_child_count: int | None = Field(default=None, ge=0)
    pseudo_primary_requirement_count: int | None = Field(default=None, ge=0)


class PdfShadowSilverNumberedChildren(_StrictFrozenModel):
    case_id: str = Field(pattern=r"^[A-Z]+-[0-9]{3}$")
    section_label: str
    ordinals: tuple[int, ...]


class PdfShadowSilverSemanticExpectation(_StrictFrozenModel):
    case_id: str = Field(pattern=r"^[A-Z]+-[0-9]{3}$")
    section_label: str
    semantic_class: SemanticClass
    requirement_subtype: RequirementSubtype | None

    @model_validator(mode="after")
    def validate_requirement_subtype(self) -> PdfShadowSilverSemanticExpectation:
        if self.semantic_class is SemanticClass.REQUIREMENT:
            if self.requirement_subtype is None:
                raise ValueError("REQUIREMENT expectations require a subtype")
        elif self.requirement_subtype is not None:
            raise ValueError("non-REQUIREMENT expectations cannot have a subtype")
        return self


class PdfShadowSilverProjectionExpectation(_StrictFrozenModel):
    case_id: str = Field(pattern=r"^[A-Z]+-[0-9]{3}$")
    section_label: str
    primary_count: int = Field(ge=0)
    content_child_count: int = Field(ge=0)
    top_level_requirement_count: int = Field(ge=0)
    primary_decision_mode: RequirementDecisionMode


class PdfShadowSilverFindingExpectation(_StrictFrozenModel):
    case_id: str = Field(pattern=r"^[A-Z]+-[0-9]{3}$")
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,119}$")
    count: int = Field(ge=0)
    section_label: str | None = None


class PdfShadowSilverProjectionScopeExpectation(_StrictFrozenModel):
    case_id: str = Field(pattern=r"^[A-Z]+-[0-9]{3}$")
    section_label: str
    count: int = Field(ge=0)
    role: RequirementProjectionRole | None = None
    target_kind: StructureNodeKind | None = None

    @model_validator(mode="after")
    def require_a_scope_dimension(self) -> PdfShadowSilverProjectionScopeExpectation:
        if self.role is None and self.target_kind is None:
            raise ValueError("projection scope requires a role or target kind")
        if self.target_kind is StructureNodeKind.DOCUMENT:
            raise ValueError("projection scope cannot target the document root")
        return self


class PdfShadowSilverDiagnosticExpectation(_StrictFrozenModel):
    case_id: str = Field(pattern=r"^[A-Z]+-[0-9]{3}$")
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,119}$")
    count: int = Field(ge=0)
    page_number: int | None = Field(default=None, ge=1)


class PdfShadowSilverPdfBlockLocator(_StrictFrozenModel):
    page_number: int = Field(ge=1)
    block_index: int = Field(ge=0)


class PdfShadowSilverNativeBlockDispositionExpectation(_StrictFrozenModel):
    disposition: Literal["exclude_tiling_pattern_tile_from_content"]
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    locators: tuple[PdfShadowSilverPdfBlockLocator, ...] = Field(min_length=1)
    expected_source_status: Literal["COMPLETE"]
    source_node_count: Literal[0] = 0
    asset_count: int = Field(ge=0)
    diagnostic_code: Literal["pdf_tiling_pattern_tile_excluded"]
    forbidden_diagnostic_code: Literal["pdf_block_bbox_missing"]

    @model_validator(mode="after")
    def validate_exact_locator_set(
        self,
    ) -> PdfShadowSilverNativeBlockDispositionExpectation:
        locator_keys = tuple(
            (locator.page_number, locator.block_index) for locator in self.locators
        )
        if locator_keys != tuple(sorted(locator_keys)):
            raise ValueError("native block disposition locators must be sorted")
        if len(locator_keys) != len(set(locator_keys)):
            raise ValueError("native block disposition locators must be unique")
        return self


class PdfShadowSilverStructureScopeExpectation(_StrictFrozenModel):
    case_id: str = Field(pattern=r"^[A-Z]+-[0-9]{3}$")
    section_label: str
    target_kind: StructureNodeKind
    direct_only: bool = False
    count: int = Field(ge=0)

    @model_validator(mode="after")
    def reject_document_root(self) -> PdfShadowSilverStructureScopeExpectation:
        if self.target_kind is StructureNodeKind.DOCUMENT:
            raise ValueError("structure scope cannot target the document root")
        return self


class PdfShadowSilverTableExpectation(_StrictFrozenModel):
    case_id: str = Field(pattern=r"^[A-Z]+-[0-9]{3}$")
    section_label: str
    page_number: int = Field(ge=1)
    row_count: int = Field(ge=1)
    column_count: int = Field(ge=1)
    count: int = Field(ge=0)
    parent_list_ordinal: int | None = Field(default=None, ge=1)
    parent_ordinal_occurrence: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def require_complete_parent_occurrence(self) -> PdfShadowSilverTableExpectation:
        if (self.parent_list_ordinal is None) != (
            self.parent_ordinal_occurrence is None
        ):
            raise ValueError("table parent ordinal binding must be complete")
        return self


class PdfShadowSilverTableNoteExpectation(_StrictFrozenModel):
    case_id: str = Field(pattern=r"^[A-Z]+-[0-9]{3}$")
    section_label: str
    page_number: int = Field(ge=1)
    row_count: int = Field(ge=3)
    column_count: int = Field(ge=2)
    note_row_index: int = Field(ge=2)
    applies_to_row_indices: tuple[int, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_row_scope(self) -> PdfShadowSilverTableNoteExpectation:
        if self.note_row_index != self.row_count - 1:
            raise ValueError("table note must bind the trailing row")
        if self.applies_to_row_indices != tuple(sorted(self.applies_to_row_indices)):
            raise ValueError("table note target rows must be sorted")
        if len(self.applies_to_row_indices) != len(set(self.applies_to_row_indices)):
            raise ValueError("table note target rows must be unique")
        if any(
            row_index < 1 or row_index >= self.note_row_index
            for row_index in self.applies_to_row_indices
        ):
            raise ValueError("table note targets must be body rows before the note")
        return self


class PdfShadowSilverRevisionHistoryExpectation(_StrictFrozenModel):
    case_id: str = Field(pattern=r"^[A-Z]+-[0-9]{3}$")
    page_number: int = Field(ge=1)
    table_index: int = Field(ge=0)
    row_count: int = Field(ge=3)
    column_count: int = Field(ge=2)
    revision_row_indices: tuple[int, ...] = Field(min_length=1)
    projected_requirement_count: Literal[0] = 0

    @model_validator(mode="after")
    def validate_revision_row_scope(
        self,
    ) -> PdfShadowSilverRevisionHistoryExpectation:
        if self.revision_row_indices != tuple(sorted(self.revision_row_indices)):
            raise ValueError("revision row indices must be sorted")
        if len(self.revision_row_indices) != len(set(self.revision_row_indices)):
            raise ValueError("revision row indices must be unique")
        if any(
            row_index < 0 or row_index >= self.row_count
            for row_index in self.revision_row_indices
        ):
            raise ValueError("revision row indices must stay inside the source table")
        return self


class PdfShadowSilverFigureExpectation(_StrictFrozenModel):
    case_id: str = Field(pattern=r"^[A-Z]+-[0-9]{3}$")
    section_label: str
    page_number: int = Field(ge=1)
    source_node_kind: SourceNodeKind
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    asset_count: int = Field(ge=0)
    placement_count: int = Field(ge=0)
    structure_count: int = Field(ge=0)
    reference_relation_count: int = Field(ge=0)
    projected_requirement_count: int = Field(ge=0)
    projection_role: RequirementProjectionRole
    decision_mode: RequirementDecisionMode

    @model_validator(mode="after")
    def require_visual_source_kind(self) -> PdfShadowSilverFigureExpectation:
        if self.source_node_kind not in {
            SourceNodeKind.IMAGE,
            SourceNodeKind.FIGURE,
        }:
            raise ValueError(
                "figure expectations require an image or figure source node"
            )
        return self


class PdfShadowSilverImageMaskCompositeExpectation(_StrictFrozenModel):
    page_number: int = Field(ge=1)
    block_indices: tuple[int, ...] = Field(min_length=1)
    composite_content_sha256: tuple[str, ...] = Field(min_length=1)
    base_content_sha256: tuple[str, ...] = Field(min_length=1)
    mask_content_sha256: tuple[str, ...] = Field(min_length=1)
    media_type: Literal["image/png"] = "image/png"
    derivation_kind: Literal["alpha_mask_composite"] = "alpha_mask_composite"
    expected_source_status: Literal["COMPLETE", "PARTIAL"]
    projected_requirement_count: Literal[0] = 0

    @model_validator(mode="after")
    def validate_parallel_source_bindings(
        self,
    ) -> PdfShadowSilverImageMaskCompositeExpectation:
        if self.block_indices != tuple(sorted(self.block_indices)):
            raise ValueError("image-mask block indices must be sorted")
        if len(self.block_indices) != len(set(self.block_indices)):
            raise ValueError("image-mask block indices must be unique")
        expected_count = len(self.block_indices)
        bindings = (
            self.composite_content_sha256,
            self.base_content_sha256,
            self.mask_content_sha256,
        )
        if any(len(items) != expected_count for items in bindings):
            raise ValueError(
                "image-mask hash bindings must align one-to-one with block indices"
            )
        if any(
            re.fullmatch(r"[0-9a-f]{64}", digest) is None
            for items in bindings
            for digest in items
        ):
            raise ValueError("image-mask bindings require lowercase SHA-256 values")
        return self


class PdfShadowSilverImageCellImageBinding(_StrictFrozenModel):
    page_number: int = Field(ge=1)
    block_index: int = Field(ge=0)
    kind: SourceNodeKind
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bbox_q100: tuple[int, int, int, int]

    @model_validator(mode="after")
    def require_visual_kind_and_bbox(self) -> PdfShadowSilverImageCellImageBinding:
        if self.kind not in {SourceNodeKind.IMAGE, SourceNodeKind.FIGURE}:
            raise ValueError("image-cell bindings require an image or figure")
        _validate_bbox_q100(self.bbox_q100)
        return self


class PdfShadowSilverImageCellTableBinding(_StrictFrozenModel):
    index: int = Field(ge=0)
    bbox_q100: tuple[int, int, int, int]
    shape: tuple[int, int]

    @model_validator(mode="after")
    def require_positive_shape_and_bbox(
        self,
    ) -> PdfShadowSilverImageCellTableBinding:
        _validate_bbox_q100(self.bbox_q100)
        if any(dimension < 1 for dimension in self.shape):
            raise ValueError("table shape dimensions must be positive")
        return self


class PdfShadowSilverImageCellCellBinding(_StrictFrozenModel):
    row: int = Field(ge=0)
    column: int = Field(ge=0)
    span: tuple[int, int]
    bbox_q100: tuple[int, int, int, int]

    @model_validator(mode="after")
    def require_positive_span_and_bbox(
        self,
    ) -> PdfShadowSilverImageCellCellBinding:
        _validate_bbox_q100(self.bbox_q100)
        if any(dimension < 1 for dimension in self.span):
            raise ValueError("cell span dimensions must be positive")
        return self


class PdfShadowSilverImageCellOwnershipAssignment(_StrictFrozenModel):
    image: PdfShadowSilverImageCellImageBinding
    table: PdfShadowSilverImageCellTableBinding
    cell: PdfShadowSilverImageCellCellBinding | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    cells: tuple[PdfShadowSilverImageCellCellBinding, ...] = Field(
        default=(),
        exclude_if=lambda value: not value,
    )
    relation_kind: Literal["placed_in_cell", "placed_over_cells"] = "placed_in_cell"

    @model_validator(mode="after")
    def require_exact_single_or_multi_cell_shape(
        self,
    ) -> PdfShadowSilverImageCellOwnershipAssignment:
        if self.relation_kind == "placed_in_cell":
            if self.cell is None or self.cells:
                raise ValueError("placed_in_cell assignments require exactly one cell")
            return self
        if self.cell is not None or not 2 <= len(self.cells) <= 16:
            raise ValueError(
                "placed_over_cells assignments require 2..16 cells and no cell"
            )
        cell_keys = tuple(
            (
                cell.row,
                cell.column,
                cell.span,
                cell.bbox_q100,
            )
            for cell in self.cells
        )
        if cell_keys != tuple(sorted(cell_keys)):
            raise ValueError("multi-cell assignment cells must be sorted")
        if len(cell_keys) != len(set(cell_keys)):
            raise ValueError("multi-cell assignment cells must be unique")
        return self

    @property
    def cell_bindings(self) -> tuple[PdfShadowSilverImageCellCellBinding, ...]:
        if self.cell is not None:
            return (self.cell,)
        return self.cells


class PdfShadowSilverImageCellOwnershipExpectation(_StrictFrozenModel):
    page_number: int = Field(ge=1)
    policy_id: Literal["pdf-image-cell-geometry/v1"]
    assignments: tuple[PdfShadowSilverImageCellOwnershipAssignment, ...] = Field(
        min_length=1
    )

    @model_validator(mode="after")
    def require_exact_reviewed_page(
        self,
    ) -> PdfShadowSilverImageCellOwnershipExpectation:
        if any(
            assignment.image.page_number != self.page_number
            for assignment in self.assignments
        ):
            raise ValueError("assignment image pages must match the reviewed page")
        assignment_keys = tuple(
            _image_cell_assignment_sort_key(assignment)
            for assignment in self.assignments
        )
        if assignment_keys != tuple(sorted(assignment_keys)):
            raise ValueError("image-cell assignments must be sorted")
        if len(assignment_keys) != len(set(assignment_keys)):
            raise ValueError("image-cell assignments must be unique")
        return self


class PdfShadowSilverOptionStateExpectation(_StrictFrozenModel):
    case_id: str = Field(pattern=r"^[A-Z]+-[0-9]{3}$")
    section_label: str
    page_number: int = Field(ge=1)
    states: tuple[StructureOptionState, ...] = Field(min_length=2)
    projected_checked_count: int = Field(ge=0)
    projected_unchecked_count: int = Field(ge=0)
    projected_uncertain_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_projection_bounds(self) -> PdfShadowSilverOptionStateExpectation:
        projected = {
            StructureOptionState.CHECKED: self.projected_checked_count,
            StructureOptionState.UNCHECKED: self.projected_unchecked_count,
            StructureOptionState.UNCERTAIN: self.projected_uncertain_count,
        }
        for state, count in projected.items():
            if count > self.states.count(state):
                raise ValueError(
                    "projected option count cannot exceed the source state count"
                )
        return self


class PdfShadowSilverDocument(_StrictFrozenModel):
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    case_ids: tuple[str, ...]
    projection_policy: Literal[
        RequirementProjectionPolicy.SHADOW_CANDIDATES,
        RequirementProjectionPolicy.NON_REQUIREMENT_ARTIFACT,
    ]
    allowed_visual_provider_ids: tuple[
        Literal[
            "aliyun-bailian-qwen-ocr",
            "azure-document-intelligence",
        ],
        ...,
    ] = Field(min_length=1)
    page_assets: tuple[PdfShadowSilverPageAsset, ...]
    visual_text_fidelity_expectations: tuple[
        PdfShadowSilverVisualTextFidelityExpectation
        | PdfShadowSilverVisualTextAnchorFidelityExpectation,
        ...,
    ]
    outline_chains: tuple[PdfShadowSilverOutlineChain, ...]
    continuations: tuple[PdfShadowSilverContinuation, ...]
    numbered_children: tuple[PdfShadowSilverNumberedChildren, ...]
    semantic_expectations: tuple[PdfShadowSilverSemanticExpectation, ...]
    projection_expectations: tuple[PdfShadowSilverProjectionExpectation, ...]
    finding_expectations: tuple[PdfShadowSilverFindingExpectation, ...]
    projection_scope_expectations: tuple[
        PdfShadowSilverProjectionScopeExpectation,
        ...,
    ]
    diagnostic_expectations: tuple[PdfShadowSilverDiagnosticExpectation, ...]
    native_block_disposition_expectations: tuple[
        PdfShadowSilverNativeBlockDispositionExpectation,
        ...,
    ]
    structure_scope_expectations: tuple[
        PdfShadowSilverStructureScopeExpectation,
        ...,
    ]
    table_expectations: tuple[PdfShadowSilverTableExpectation, ...]
    table_note_expectations: tuple[PdfShadowSilverTableNoteExpectation, ...]
    revision_history_expectations: tuple[
        PdfShadowSilverRevisionHistoryExpectation,
        ...,
    ]
    figure_expectations: tuple[PdfShadowSilverFigureExpectation, ...]
    image_mask_composite_expectations: tuple[
        PdfShadowSilverImageMaskCompositeExpectation,
        ...,
    ]
    image_cell_ownership_expectations: tuple[
        PdfShadowSilverImageCellOwnershipExpectation,
        ...,
    ]
    option_state_expectations: tuple[PdfShadowSilverOptionStateExpectation, ...]

    @model_validator(mode="after")
    def reject_ambiguous_expectations(self) -> PdfShadowSilverDocument:
        _require_unique(self.case_ids, "document case ids")
        _require_unique(
            (asset.page_number for asset in self.page_assets),
            "page asset page numbers",
        )
        _require_unique(
            self.allowed_visual_provider_ids,
            "allowed visual provider ids",
        )
        _require_unique(
            (item.page_number for item in self.visual_text_fidelity_expectations),
            "visual text fidelity pages",
        )
        page_asset_numbers = {asset.page_number for asset in self.page_assets}
        fidelity_page_numbers = {
            item.page_number for item in self.visual_text_fidelity_expectations
        }
        if fidelity_page_numbers and fidelity_page_numbers != page_asset_numbers:
            raise ValueError(
                "visual text fidelity pages must exactly cover the reviewed page scope"
            )
        allowed_provider_ids = set(self.allowed_visual_provider_ids)
        if any(
            item.provider_id not in allowed_provider_ids
            for item in self.visual_text_fidelity_expectations
        ):
            raise ValueError(
                "visual text fidelity profiles require an allowed provider"
            )
        _require_unique(
            (item.target_label for item in self.outline_chains),
            "outline targets",
        )
        _require_unique(
            (
                (item.section_label, item.from_page, item.to_page)
                for item in self.continuations
            ),
            "continuation targets",
        )
        _require_unique(
            (item.section_label for item in self.numbered_children),
            "numbered-child targets",
        )
        _require_unique(
            (item.section_label for item in self.semantic_expectations),
            "semantic targets",
        )
        _require_unique(
            (item.section_label for item in self.projection_expectations),
            "projection targets",
        )
        _require_unique(
            ((item.section_label, item.code) for item in self.finding_expectations),
            "finding targets",
        )
        _require_unique(
            (
                (item.section_label, item.role, item.target_kind)
                for item in self.projection_scope_expectations
            ),
            "projection-scope targets",
        )
        _require_unique(
            ((item.code, item.page_number) for item in self.diagnostic_expectations),
            "diagnostic targets",
        )
        _require_unique(
            (
                (item.disposition, item.content_sha256)
                for item in self.native_block_disposition_expectations
            ),
            "native block disposition targets",
        )
        _require_unique(
            (
                (locator.page_number, locator.block_index)
                for item in self.native_block_disposition_expectations
                for locator in item.locators
            ),
            "native block disposition locators",
        )
        _require_unique(
            (
                (item.section_label, item.target_kind, item.direct_only)
                for item in self.structure_scope_expectations
            ),
            "structure-scope targets",
        )
        _require_unique(
            (
                (
                    item.section_label,
                    item.page_number,
                    item.row_count,
                    item.column_count,
                    item.parent_list_ordinal,
                    item.parent_ordinal_occurrence,
                )
                for item in self.table_expectations
            ),
            "table targets",
        )
        _require_unique(
            (
                (
                    item.section_label,
                    item.page_number,
                    item.row_count,
                    item.column_count,
                    item.note_row_index,
                )
                for item in self.table_note_expectations
            ),
            "table-note targets",
        )
        _require_unique(
            (
                (item.page_number, item.table_index)
                for item in self.revision_history_expectations
            ),
            "revision-history targets",
        )
        _require_unique(
            (
                (
                    item.section_label,
                    item.page_number,
                    item.source_node_kind,
                    item.content_sha256,
                )
                for item in self.figure_expectations
            ),
            "figure targets",
        )
        _require_unique(
            (item.page_number for item in self.image_mask_composite_expectations),
            "image-mask composite pages",
        )
        _require_unique(
            (item.page_number for item in self.image_cell_ownership_expectations),
            "image-cell ownership pages",
        )
        _require_unique(
            (
                (item.section_label, item.page_number)
                for item in self.option_state_expectations
            ),
            "option-state targets",
        )
        expectations = (
            *self.visual_text_fidelity_expectations,
            *self.outline_chains,
            *self.continuations,
            *self.numbered_children,
            *self.semantic_expectations,
            *self.projection_expectations,
            *self.finding_expectations,
            *self.projection_scope_expectations,
            *self.diagnostic_expectations,
            *self.structure_scope_expectations,
            *self.table_expectations,
            *self.table_note_expectations,
            *self.revision_history_expectations,
            *self.figure_expectations,
            *self.option_state_expectations,
        )
        registered_case_ids = set(self.case_ids)
        expectation_case_ids = {item.case_id for item in expectations}
        if not expectation_case_ids.issubset(registered_case_ids):
            raise ValueError("expectations must reference a registered document case")
        if registered_case_ids != expectation_case_ids:
            raise ValueError("every registered document case requires an expectation")
        return self


class PdfShadowSilverProvenance(_StrictFrozenModel):
    label: Literal["AI_REVIEWED_SILVER"]
    reviewed_by: Literal["ai_assistant"]
    reviewed_at_utc: str
    limitations: tuple[
        Literal[
            "not_human_gold",
            "not_a_serving_activation_gate",
            "source_hash_and_visual_asset_bound",
        ],
        ...,
    ]

    @model_validator(mode="after")
    def require_all_limitations(self) -> PdfShadowSilverProvenance:
        if set(self.limitations) != _REQUIRED_LIMITATIONS:
            raise ValueError("silver provenance must retain all safety limitations")
        return self


class PdfShadowSilverRegistry(_StrictFrozenModel):
    schema_version: Literal["pdf-shadow-silver/v1"]
    provenance: PdfShadowSilverProvenance
    documents: dict[str, PdfShadowSilverDocument]

    @model_validator(mode="after")
    def require_machine_document_ids(self) -> PdfShadowSilverRegistry:
        if any(not _DOCUMENT_ID_PATTERN.fullmatch(item) for item in self.documents):
            raise ValueError("silver document ids must be machine-readable keys")
        return self


class PdfShadowSilverCheckStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"


SilverCheckValue = str | int | bool | tuple[str, ...] | tuple[int, ...] | None
PdfShadowSilverCheckCategory = Literal[
    "binding",
    "source_ir",
    "structure",
    "finding",
    "semantic",
    "projection",
]


class PdfShadowSilverCheck(_StrictFrozenModel):
    check_id: str = Field(min_length=1, max_length=240)
    category: PdfShadowSilverCheckCategory
    status: PdfShadowSilverCheckStatus
    reason_code: str = Field(min_length=1, max_length=120)
    expected: SilverCheckValue
    observed: SilverCheckValue

    @model_validator(mode="after")
    def validate_machine_keys(self) -> PdfShadowSilverCheck:
        if not _CHECK_ID_PATTERN.fullmatch(self.check_id):
            raise ValueError("check_id must be a machine-readable key")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,119}", self.reason_code):
            raise ValueError("reason_code must be a machine-readable key")
        _require_machine_report_value(self.expected)
        _require_machine_report_value(self.observed)
        if (
            self.reason_code == "silver_binding_failed"
            and self.status is not PdfShadowSilverCheckStatus.BLOCKED
        ):
            raise ValueError("silver_binding_failed checks must be BLOCKED")
        if (
            self.status is PdfShadowSilverCheckStatus.BLOCKED
            and self.observed is not None
        ):
            raise ValueError("blocked checks must not claim an observed value")
        return self


class PdfShadowSilverSourceIRProvenance(_StrictFrozenModel):
    source_ir_id: str = Field(min_length=1, max_length=160)
    adapter_id: str = Field(min_length=1, max_length=160)
    adapter_version: str = Field(min_length=1, max_length=160)

    @field_validator("source_ir_id", "adapter_id", "adapter_version")
    @classmethod
    def reject_whitespace_only(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("SourceIR provenance values must not be blank")
        return value


class PdfShadowSilverReport(_StrictFrozenModel):
    schema_version: Literal["pdf-shadow-silver-evaluation/v2"] = (
        PDF_SHADOW_SILVER_EVALUATION_SCHEMA_VERSION
    )
    provenance_label: Literal["AI_REVIEWED_SILVER"] = "AI_REVIEWED_SILVER"
    is_human_gold: Literal[False] = False
    is_serving_activation_gate: Literal[False] = False
    registry_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    document_id: str = Field(min_length=1, max_length=160)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_ir_provenance: PdfShadowSilverSourceIRProvenance
    status: PdfShadowSilverCheckStatus
    passed_check_count: int = Field(ge=0)
    failed_check_count: int = Field(ge=0)
    blocked_check_count: int = Field(ge=0)
    checks: tuple[PdfShadowSilverCheck, ...]

    @model_validator(mode="after")
    def validate_rollup(self) -> PdfShadowSilverReport:
        if not _DOCUMENT_ID_PATTERN.fullmatch(self.document_id):
            raise ValueError("report document_id must be a machine-readable key")
        check_ids = tuple(check.check_id for check in self.checks)
        if len(check_ids) != len(set(check_ids)):
            raise ValueError("silver report check_id values must be unique")
        passed = sum(
            check.status is PdfShadowSilverCheckStatus.PASS for check in self.checks
        )
        failed = sum(
            check.status is PdfShadowSilverCheckStatus.FAIL for check in self.checks
        )
        blocked = sum(
            check.status is PdfShadowSilverCheckStatus.BLOCKED for check in self.checks
        )
        expected_status = (
            PdfShadowSilverCheckStatus.PASS
            if self.checks and failed == 0 and blocked == 0
            else PdfShadowSilverCheckStatus.FAIL
        )
        expected_source_ir_id = stable_source_ir_id(
            document_id=stable_document_id(self.source_sha256),
            source_sha256=self.source_sha256,
            adapter_id=self.source_ir_provenance.adapter_id,
            adapter_version=self.source_ir_provenance.adapter_version,
        )
        if self.source_ir_provenance.source_ir_id != expected_source_ir_id:
            raise ValueError(
                "SourceIR provenance does not match source and adapter identity"
            )
        if (
            self.passed_check_count != passed
            or self.failed_check_count != failed
            or self.blocked_check_count != blocked
            or self.status is not expected_status
        ):
            raise ValueError("silver report rollup does not match its checks")
        return self


class PdfShadowSilverSubsetPageEvidence(_StrictFrozenModel):
    """Content-free evidence for one explicitly selected authorized page."""

    page_number: int = Field(ge=1)
    input_rotation_cw_degrees: Literal[0, 90, 180, 270]
    binding_kind: Literal["source_asset_sha256", "page_image_sha256"]
    binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_provider_id: str | None = Field(default=None, max_length=160)
    observed_provider_version: str | None = Field(default=None, max_length=160)
    observed_provider_version_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    observed_page_image_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    evaluated_content_types: tuple[VisualContentType, ...]
    observation_count: int = Field(ge=0)
    table_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_observed_provider_identity(
        self,
    ) -> PdfShadowSilverSubsetPageEvidence:
        provider_values = (
            self.observed_provider_id,
            self.observed_provider_version,
            self.observed_provider_version_sha256,
            self.observed_page_image_sha256,
        )
        if any(value is None for value in provider_values) and any(
            value is not None for value in provider_values
        ):
            raise ValueError("subset page provider evidence must be complete or absent")
        if self.observed_provider_id is not None:
            assert self.observed_provider_version is not None
            assert self.observed_provider_version_sha256 is not None
            if not self.observed_provider_id.strip():
                raise ValueError("subset page provider id must not be blank")
            if not self.observed_provider_version.strip():
                raise ValueError("subset page provider version must not be blank")
            actual_version_sha256 = hashlib.sha256(
                self.observed_provider_version.encode("utf-8")
            ).hexdigest()
            if actual_version_sha256 != self.observed_provider_version_sha256:
                raise ValueError("subset page provider version hash does not match")
        if self.evaluated_content_types != tuple(
            item
            for item in VISUAL_CONTENT_TYPES
            if item in self.evaluated_content_types
        ) or len(self.evaluated_content_types) != len(
            set(self.evaluated_content_types)
        ):
            raise ValueError(
                "subset page evaluated content types must be canonical and unique"
            )
        if self.table_count > self.observation_count:
            raise ValueError("subset page table count exceeds its observations")
        return self


class PdfShadowSilverSubsetAuthorizedPageBinding(_StrictFrozenModel):
    """One immutable page authorization copied from the full Silver route."""

    page_number: int = Field(ge=1)
    input_rotation_cw_degrees: Literal[0, 90, 180, 270]
    binding_kind: Literal["source_asset_sha256", "page_image_sha256"]
    binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class PdfShadowSilverSubsetReport(_StrictFrozenModel):
    """A page-subset capture report that cannot represent full-document review."""

    schema_version: Literal["pdf-shadow-silver-subset-evaluation/v1"] = (
        PDF_SHADOW_SILVER_SUBSET_EVALUATION_SCHEMA_VERSION
    )
    provenance_label: Literal["AI_REVIEWED_SILVER"] = "AI_REVIEWED_SILVER"
    artifact_scope: Literal["PAGE_SUBSET"] = "PAGE_SUBSET"
    quality_claim: Literal["CAPTURE_BINDING_ONLY"] = "CAPTURE_BINDING_ONLY"
    unselected_page_disposition: Literal["NOT_EVALUATED"] = "NOT_EVALUATED"
    is_full_document_checklist: Literal[False] = False
    is_content_acceptance: Literal[False] = False
    is_human_gold: Literal[False] = False
    is_serving_activation_gate: Literal[False] = False
    registry_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    document_id: str = Field(min_length=1, max_length=160)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_ir_provenance: PdfShadowSilverSourceIRProvenance
    provider_identity_scope: Literal["FULL_AUTHORIZED_ROUTE"] = "FULL_AUTHORIZED_ROUTE"
    authorized_page_numbers: tuple[int, ...] = Field(min_length=1)
    authorized_page_bindings: tuple[
        PdfShadowSilverSubsetAuthorizedPageBinding,
        ...,
    ] = Field(min_length=1)
    selected_page_numbers: tuple[int, ...] = Field(min_length=1)
    unevaluated_authorized_page_numbers: tuple[int, ...]
    provider_id: str = Field(min_length=1, max_length=160)
    provider_version: str = Field(min_length=1, max_length=160)
    provider_version_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: PdfShadowSilverCheckStatus
    passed_check_count: int = Field(ge=0)
    failed_check_count: int = Field(ge=0)
    blocked_check_count: Literal[0] = 0
    page_evidence: tuple[PdfShadowSilverSubsetPageEvidence, ...]
    checks: tuple[PdfShadowSilverCheck, ...]

    @model_validator(mode="after")
    def validate_subset_scope_and_rollup(self) -> PdfShadowSilverSubsetReport:
        if not _DOCUMENT_ID_PATTERN.fullmatch(self.document_id):
            raise ValueError("report document_id must be a machine-readable key")
        for label, pages in (
            ("authorized", self.authorized_page_numbers),
            ("selected", self.selected_page_numbers),
            ("unevaluated", self.unevaluated_authorized_page_numbers),
        ):
            if (
                pages != tuple(sorted(pages))
                or len(pages) != len(set(pages))
                or any(
                    isinstance(page, bool) or not isinstance(page, int) or page < 1
                    for page in pages
                )
            ):
                raise ValueError(f"subset {label} pages must be positive and sorted")
        authorized = frozenset(self.authorized_page_numbers)
        selected = frozenset(self.selected_page_numbers)
        unevaluated = frozenset(self.unevaluated_authorized_page_numbers)
        if (
            tuple(item.page_number for item in self.authorized_page_bindings)
            != self.authorized_page_numbers
        ):
            raise ValueError(
                "subset authorized bindings must exactly cover the full route"
            )
        if not selected.issubset(authorized):
            raise ValueError("subset selected pages exceed the authorized route")
        if unevaluated != authorized - selected:
            raise ValueError("subset unevaluated pages do not close the route scope")
        if tuple(item.page_number for item in self.page_evidence) != (
            self.selected_page_numbers
        ):
            raise ValueError("subset page evidence must exactly cover selected pages")
        bindings_by_page = {
            item.page_number: item for item in self.authorized_page_bindings
        }
        for evidence in self.page_evidence:
            authorized_binding = bindings_by_page[evidence.page_number]
            if (
                evidence.input_rotation_cw_degrees
                != authorized_binding.input_rotation_cw_degrees
                or evidence.binding_kind != authorized_binding.binding_kind
                or evidence.binding_sha256 != authorized_binding.binding_sha256
            ):
                raise ValueError(
                    "subset page evidence does not match its authorized binding"
                )
        if not self.provider_id.strip() or not self.provider_version.strip():
            raise ValueError("subset provider identity must not be blank")
        if hashlib.sha256(self.provider_version.encode("utf-8")).hexdigest() != (
            self.provider_version_sha256
        ):
            raise ValueError("subset provider version hash does not match")
        check_ids = tuple(check.check_id for check in self.checks)
        if len(check_ids) != len(set(check_ids)):
            raise ValueError("subset report check_id values must be unique")
        if any(
            check.status is PdfShadowSilverCheckStatus.BLOCKED for check in self.checks
        ):
            raise ValueError("subset reports do not support blocked checks")
        expected_source_ir_id = stable_source_ir_id(
            document_id=stable_document_id(self.source_sha256),
            source_sha256=self.source_sha256,
            adapter_id=self.source_ir_provenance.adapter_id,
            adapter_version=self.source_ir_provenance.adapter_version,
        )
        if self.source_ir_provenance.source_ir_id != expected_source_ir_id:
            raise ValueError(
                "subset SourceIR provenance does not match source and adapter identity"
            )
        passed = sum(
            check.status is PdfShadowSilverCheckStatus.PASS for check in self.checks
        )
        failed = sum(
            check.status is PdfShadowSilverCheckStatus.FAIL for check in self.checks
        )
        expected_status = (
            PdfShadowSilverCheckStatus.PASS
            if self.checks and failed == 0 and self.blocked_check_count == 0
            else PdfShadowSilverCheckStatus.FAIL
        )
        if (
            self.passed_check_count != passed
            or self.failed_check_count != failed
            or self.status is not expected_status
        ):
            raise ValueError("subset report rollup does not match its checks")
        return self


def load_pdf_shadow_silver(
    path: Path = DEFAULT_PDF_SHADOW_SILVER_PATH,
    *,
    schema_path: Path = DEFAULT_PDF_SHADOW_SILVER_SCHEMA_PATH,
) -> PdfShadowSilverRegistry:
    """Load a duplicate-key-safe registry and validate it against JSON Schema.

    The returned typed object can only represent AI-reviewed silver.  It is
    deliberately not accepted as human Gold or a serving activation gate.
    """

    try:
        payload = _load_json_object_strict(path)
        schema = _load_json_object_strict(schema_path)
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.Draft202012Validator(
            schema,
            format_checker=jsonschema.FormatChecker(),
        ).validate(payload)
        return PdfShadowSilverRegistry.model_validate(payload)
    except PdfShadowSilverRegistryError:
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
        raise PdfShadowSilverRegistryError(
            "The PDF shadow silver registry failed strict validation."
        ) from exc


def canonical_pdf_shadow_silver_sha256(
    registry: PdfShadowSilverRegistry,
) -> str:
    if not isinstance(registry, PdfShadowSilverRegistry):
        raise TypeError("registry must be a validated PdfShadowSilverRegistry")
    payload = json.dumps(
        registry.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _native_block_disposition_coverage_expectations(
    silver: PdfShadowSilverDocument,
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    bindings_by_diagnostic_code: dict[str, list[tuple[str, int, int]]] = {
        diagnostic_code: []
        for diagnostic_code in _SUPPORTED_NATIVE_BLOCK_DISPOSITION_DIAGNOSTIC_CODES
    }
    for expectation in silver.native_block_disposition_expectations:
        bindings = bindings_by_diagnostic_code.setdefault(
            expectation.diagnostic_code,
            [],
        )
        bindings.extend(
            (
                expectation.content_sha256,
                locator.page_number,
                locator.block_index,
            )
            for locator in expectation.locators
        )
    return tuple(
        (
            diagnostic_code,
            tuple(
                f"{content_sha256}:{page_number}:{block_index}"
                for content_sha256, page_number, block_index in sorted(bindings)
            ),
        )
        for diagnostic_code, bindings in sorted(bindings_by_diagnostic_code.items())
    )


def evaluate_pdf_shadow_silver(
    *,
    document_id: str,
    source_ir: SourceIRSnapshot,
    structure_tree: StructureTreeSnapshot,
    semantics: SemanticClassificationSnapshot,
    requirement_view: RequirementViewSnapshot,
    registry: PdfShadowSilverRegistry,
) -> PdfShadowSilverReport:
    """Return a text-free PASS/FAIL/BLOCKED checklist for source-bound PDF silver.

    All four snapshots are revalidated against one another.  Missing or
    ambiguous labels, duplicate page observations, mismatched source/page
    hashes, and altered grounding fail closed; the evaluator never chooses a
    plausible candidate from ambiguous data.
    """

    _require_input_types(
        source_ir=source_ir,
        structure_tree=structure_tree,
        semantics=semantics,
        requirement_view=requirement_view,
        registry=registry,
    )
    if not isinstance(document_id, str) or not _DOCUMENT_ID_PATTERN.fullmatch(
        document_id
    ):
        raise ValueError("document_id must be a machine-readable key")

    checks: list[PdfShadowSilverCheck] = []
    silver = registry.documents.get(document_id)
    checks.append(
        _check(
            check_id="binding.document_id",
            category="binding",
            expected=document_id,
            observed=document_id if silver is not None else None,
            matches=silver is not None,
            failure_reason="document_not_registered",
        )
    )
    if silver is None:
        return _report(
            registry=registry,
            document_id=document_id,
            source_ir=source_ir,
            checks=checks,
        )

    checks.append(
        _check(
            check_id="binding.source_sha256",
            category="binding",
            expected=silver.source_sha256,
            observed=source_ir.source_sha256,
            matches=source_ir.source_sha256 == silver.source_sha256,
            failure_reason="source_hash_mismatch",
        )
    )

    allowed_provider_ids = frozenset(silver.allowed_visual_provider_ids)
    observed_provider_ids = tuple(
        sorted({snapshot.provider_id for snapshot in source_ir.visual_text_snapshots})
    )
    forbidden_provider_ids = tuple(
        provider_id
        for provider_id in observed_provider_ids
        if provider_id not in allowed_provider_ids
    )
    # ``page_assets`` is the exact source-bound remote OCR scope.  An empty
    # scope explicitly permits a native-only run and forbids every visual
    # snapshot. Each scoped page is bound either to embedded source-image
    # bytes or to the independently rendered PNG hash, never both. Per-page
    # checks also require exactly one visual snapshot.
    expected_visual_pages = {asset.page_number for asset in silver.page_assets}
    observed_visual_pages = {
        snapshot.page_number for snapshot in source_ir.visual_text_snapshots
    }
    missing_visual_pages = expected_visual_pages - observed_visual_pages
    unexpected_visual_pages = observed_visual_pages - expected_visual_pages
    if forbidden_provider_ids:
        visual_provider_failure_reason = "visual_provider_not_allowed"
    elif expected_visual_pages and not observed_visual_pages:
        visual_provider_failure_reason = "visual_snapshot_missing"
    elif missing_visual_pages and unexpected_visual_pages:
        visual_provider_failure_reason = "visual_page_scope_mismatch"
    elif missing_visual_pages:
        visual_provider_failure_reason = "visual_page_coverage_missing"
    elif unexpected_visual_pages:
        visual_provider_failure_reason = "visual_page_out_of_scope"
    else:
        visual_provider_failure_reason = "visual_page_scope_mismatch"
    checks.append(
        _check(
            check_id="binding.visual_provider_policy",
            category="binding",
            expected=silver.allowed_visual_provider_ids,
            observed=observed_provider_ids,
            matches=(
                not forbidden_provider_ids
                and not missing_visual_pages
                and not unexpected_visual_pages
            ),
            failure_reason=visual_provider_failure_reason,
        )
    )

    visual_by_page: dict[int, list[str]] = {}
    for snapshot in source_ir.visual_text_snapshots:
        visual_by_page.setdefault(snapshot.page_number, []).append(
            snapshot.page_image_sha256
        )
    source_assets_by_page = _large_image_asset_hashes_by_page(source_ir)
    for page_asset in silver.page_assets:
        if page_asset.source_asset_sha256 is not None:
            expected_page_hash = page_asset.source_asset_sha256
            observed_hashes = source_assets_by_page.get(page_asset.page_number, ())
            matching_asset_count = observed_hashes.count(expected_page_hash)
            if not observed_hashes:
                reason = "page_asset_missing"
                observed: SilverCheckValue = None
                matches = False
            elif matching_asset_count > 1:
                reason = "page_asset_ambiguous"
                observed = observed_hashes
                matches = False
            else:
                reason = "page_asset_hash_mismatch"
                observed = (
                    expected_page_hash if matching_asset_count == 1 else observed_hashes
                )
                matches = matching_asset_count == 1
        else:
            expected_page_hash = page_asset.page_image_sha256
            if expected_page_hash is None:
                raise AssertionError("validated page asset lost its hash binding")
            observed_hashes = tuple(
                sorted(visual_by_page.get(page_asset.page_number, ()))
            )
            if not observed_hashes:
                reason = "page_image_missing"
                observed = None
                matches = False
            elif len(observed_hashes) > 1:
                reason = "page_image_ambiguous"
                observed = observed_hashes
                matches = False
            else:
                reason = "page_image_hash_mismatch"
                observed = observed_hashes[0]
                matches = observed == expected_page_hash
        checks.append(
            _check(
                check_id=f"binding.page_asset:{page_asset.page_number}",
                category="binding",
                expected=expected_page_hash,
                observed=observed,
                matches=matches,
                failure_reason=reason,
            )
        )
        visual_snapshot_count = len(visual_by_page.get(page_asset.page_number, ()))
        checks.append(
            _check(
                check_id=f"binding.visual_page:{page_asset.page_number}",
                category="binding",
                expected=1,
                observed=visual_snapshot_count,
                matches=visual_snapshot_count == 1,
                failure_reason=(
                    "visual_page_missing"
                    if visual_snapshot_count == 0
                    else "visual_page_ambiguous"
                ),
            )
        )

    checks.extend(
        (
            _validation_check(
                check_id="binding.structure_grounding",
                callback=lambda: validate_structure_tree_against_source_ir(
                    structure_tree,
                    source_ir,
                ),
                failure_reason="structure_grounding_invalid",
            ),
            _validation_check(
                check_id="binding.semantic_grounding",
                callback=lambda: validate_semantic_classification_against_inputs(
                    semantics,
                    source_ir,
                    structure_tree,
                ),
                failure_reason="semantic_grounding_invalid",
            ),
            _validation_check(
                check_id="binding.projection_grounding",
                callback=lambda: validate_requirement_view_against_inputs(
                    requirement_view,
                    source_ir,
                    structure_tree,
                    semantics,
                ),
                failure_reason="projection_grounding_invalid",
            ),
            _check(
                check_id="binding.projection_policy",
                category="binding",
                expected=silver.projection_policy.value,
                observed=requirement_view.projection_policy.value,
                matches=requirement_view.projection_policy is silver.projection_policy,
                failure_reason="projection_policy_mismatch",
            ),
        )
    )

    visual_fidelity_checks = _visual_text_fidelity_checks(
        silver=silver,
        source_ir=source_ir,
        requirement_view=requirement_view,
    )
    checks.extend(visual_fidelity_checks)
    projection_policy_checks = _document_projection_policy_checks(
        silver=silver,
        requirement_view=requirement_view,
    )
    checks.extend(projection_policy_checks)

    binding_ok = all(
        check.status is PdfShadowSilverCheckStatus.PASS
        for check in checks
        if check.category == "binding"
    ) and all(
        check.status is PdfShadowSilverCheckStatus.PASS
        for check in (*visual_fidelity_checks, *projection_policy_checks)
    )
    if not binding_ok:
        checks.extend(_binding_blocked_expectation_checks(silver))
        return _report(
            registry=registry,
            document_id=document_id,
            source_ir=source_ir,
            checks=checks,
        )

    section_index = _section_index(structure_tree)
    page_index = _SourcePageIndex(source_ir)
    assets_by_id = {asset.asset_id: asset for asset in source_ir.assets}

    for expectation in silver.image_cell_ownership_expectations:
        page_prefix = f"image_cell_ownership:{expectation.page_number}"
        expected_assignment_fingerprints = tuple(
            sorted(
                _image_cell_assignment_sha256(assignment)
                for assignment in expectation.assignments
            )
        )
        observed_assignments = _enumerate_pdf_image_cell_assignments(
            source_ir,
            page_number=expectation.page_number,
        )
        observed_assignment_fingerprints = tuple(
            sorted(
                _image_cell_assignment_sha256(assignment)
                for assignment in observed_assignments
            )
        )
        checks.append(
            _check(
                check_id=f"{page_prefix}.exact_assignments",
                category="source_ir",
                expected=expected_assignment_fingerprints,
                observed=observed_assignment_fingerprints,
                matches=(
                    observed_assignment_fingerprints == expected_assignment_fingerprints
                ),
                failure_reason="image_cell_reviewed_page_coverage_mismatch",
            )
        )

        for assignment_index, assignment in enumerate(expectation.assignments):
            prefix = f"{page_prefix}:{assignment_index}"
            placements = _matching_pdf_image_placements(
                source_ir,
                assignment=assignment,
                assets_by_id=assets_by_id,
            )
            checks.append(
                _check(
                    check_id=f"{prefix}.placement_count",
                    category="source_ir",
                    expected=1,
                    observed=len(placements),
                    matches=len(placements) == 1,
                    failure_reason="image_cell_placement_count_mismatch",
                )
            )

            resolved_table: SourceNode | None = None
            resolved_cells: tuple[TableCellIR, ...] = ()
            table_resolution_count = 0
            cell_resolution_count = 0
            if len(placements) == 1:
                if assignment.relation_kind == "placed_in_cell":
                    (
                        resolved_table,
                        resolved_cell,
                        table_resolution_count,
                        cell_resolution_count,
                    ) = _resolve_pdf_image_cell_geometry(
                        source_ir,
                        image_node=placements[0],
                    )
                    if resolved_cell is not None:
                        resolved_cells = (resolved_cell,)
                else:
                    (
                        resolved_table,
                        resolved_cells,
                        table_resolution_count,
                        cell_resolution_count,
                    ) = _resolve_pdf_image_multi_cell_geometry(
                        source_ir,
                        image_node=placements[0],
                    )
            checks.extend(
                (
                    _check(
                        check_id=f"{prefix}.unique_table_count",
                        category="source_ir",
                        expected=1,
                        observed=table_resolution_count,
                        matches=resolved_table is not None,
                        failure_reason="image_cell_table_ownership_not_unique",
                    ),
                    _check(
                        check_id=f"{prefix}.unique_cell_count",
                        category="source_ir",
                        expected=len(assignment.cell_bindings),
                        observed=cell_resolution_count,
                        matches=(len(resolved_cells) == len(assignment.cell_bindings)),
                        failure_reason="image_cell_cell_ownership_not_unique",
                    ),
                )
            )

            independent_structure_nodes: tuple[StructureNode, ...] = ()
            if len(placements) == 1:
                image_ref = StructureSourceRef(
                    kind=StructureSourceRefKind.SOURCE_NODE,
                    source_id=placements[0].source_node_id,
                )
                independent_structure_nodes = tuple(
                    node
                    for node in structure_tree.nodes
                    if node.source_refs == (image_ref,)
                )
            checks.append(
                _check(
                    check_id=f"{prefix}.independent_structure_count",
                    category="structure",
                    expected=1,
                    observed=len(independent_structure_nodes),
                    matches=len(independent_structure_nodes) == 1,
                    failure_reason="image_cell_independent_structure_mismatch",
                )
            )

            relation_count = _exact_image_cell_relation_count(
                structure_tree,
                assignment=assignment,
                image_node=placements[0] if len(placements) == 1 else None,
                table_node=resolved_table,
                cells=resolved_cells,
                independent_structure_nodes=independent_structure_nodes,
            )
            checks.append(
                _check(
                    check_id=f"{prefix}.relation_count",
                    category="structure",
                    expected=1,
                    observed=relation_count,
                    matches=relation_count == 1,
                    failure_reason="image_cell_relation_grounding_mismatch",
                )
            )

            projected_requirement_count = 0
            if len(placements) == 1:
                projected_requirement_count = _projected_source_ref_count(
                    requirement_view,
                    source_node_id=placements[0].source_node_id,
                )
            checks.append(
                _check(
                    check_id=f"{prefix}.projected_requirement_count",
                    category="projection",
                    expected=0,
                    observed=projected_requirement_count,
                    matches=projected_requirement_count == 0,
                    failure_reason="image_cell_projection_count_mismatch",
                )
            )

    for expectation in silver.diagnostic_expectations:
        page_key = expectation.page_number or "document"
        observed_count = sum(
            diagnostic.code == expectation.code
            and (
                expectation.page_number is None
                or (
                    diagnostic.native_locator is not None
                    and diagnostic.native_locator.page_number == expectation.page_number
                )
            )
            for diagnostic in source_ir.diagnostics
        )
        checks.append(
            _check(
                check_id=(f"source_ir.diagnostic:{expectation.code}:{page_key}.count"),
                category="source_ir",
                expected=expectation.count,
                observed=observed_count,
                matches=observed_count == expectation.count,
                failure_reason="diagnostic_count_mismatch",
            )
        )

    for expectation in silver.native_block_disposition_expectations:
        prefix = (
            f"source_ir.native_block_disposition:{expectation.disposition}:"
            f"{expectation.content_sha256}"
        )
        expected_locator_keys = tuple(
            f"{locator.page_number}:{locator.block_index}"
            for locator in expectation.locators
        )
        expected_locator_set = {
            (locator.page_number, locator.block_index)
            for locator in expectation.locators
        }
        matching_nodes = tuple(
            node
            for node in source_ir.nodes
            if (
                node.native_locator.page_number,
                node.native_locator.block_index,
            )
            in expected_locator_set
        )
        matching_assets = tuple(
            asset
            for asset in source_ir.assets
            if asset.content_sha256 == expectation.content_sha256
        )
        disposition_diagnostics = tuple(
            diagnostic
            for diagnostic in source_ir.diagnostics
            if diagnostic.code == expectation.diagnostic_code
            and (
                diagnostic.evidence_sha256 == expectation.content_sha256
                or (
                    diagnostic.native_locator is not None
                    and (
                        diagnostic.native_locator.page_number,
                        diagnostic.native_locator.block_index,
                    )
                    in expected_locator_set
                )
            )
        )
        observed_locator_pairs = tuple(
            sorted(
                (
                    diagnostic.native_locator.page_number,
                    diagnostic.native_locator.block_index,
                )
                for diagnostic in disposition_diagnostics
                if diagnostic.native_locator is not None
                and diagnostic.native_locator.page_number is not None
                and diagnostic.native_locator.block_index is not None
            )
        )
        missing_locator_count = len(disposition_diagnostics) - len(
            observed_locator_pairs
        )
        observed_locator_keys = (
            tuple(
                f"{page_number}:{block_index}"
                for page_number, block_index in observed_locator_pairs
            )
            + ("missing",) * missing_locator_count
        )
        diagnostic_grounding_matches = (
            len(disposition_diagnostics) == len(expectation.locators)
            and observed_locator_keys == expected_locator_keys
            and all(
                diagnostic.severity is SourceDiagnosticSeverity.INFO
                and diagnostic.source_node_id is None
                and diagnostic.native_locator is not None
                and diagnostic.native_locator.bbox is None
                for diagnostic in disposition_diagnostics
            )
        )
        observed_evidence_sha256: SilverCheckValue = (
            expectation.content_sha256
            if len(disposition_diagnostics) == len(expectation.locators)
            and all(
                diagnostic.evidence_sha256 == expectation.content_sha256
                for diagnostic in disposition_diagnostics
            )
            else tuple(
                sorted(
                    diagnostic.evidence_sha256 or "missing"
                    for diagnostic in disposition_diagnostics
                )
            )
        )
        observed_metrics: tuple[
            tuple[str, SilverCheckValue, SilverCheckValue, str],
            ...,
        ] = (
            (
                "source_status",
                expectation.expected_source_status,
                source_ir.status.value,
                "native_block_disposition_source_status_mismatch",
            ),
            (
                "source_node_count",
                expectation.source_node_count,
                len(matching_nodes),
                "native_block_disposition_source_node_count_mismatch",
            ),
            (
                "asset_count",
                expectation.asset_count,
                len(matching_assets),
                "native_block_disposition_asset_count_mismatch",
            ),
            (
                "diagnostic_count",
                len(expectation.locators),
                len(disposition_diagnostics),
                "native_block_disposition_diagnostic_count_mismatch",
            ),
            (
                "diagnostic_locator_keys",
                expected_locator_keys,
                observed_locator_keys,
                "native_block_disposition_locator_mismatch",
            ),
            (
                "diagnostic_grounding",
                True,
                diagnostic_grounding_matches,
                "native_block_disposition_grounding_mismatch",
            ),
            (
                "content_sha256",
                expectation.content_sha256,
                observed_evidence_sha256,
                "native_block_disposition_content_sha256_mismatch",
            ),
            (
                "forbidden_diagnostic_count",
                0,
                sum(
                    diagnostic.code == expectation.forbidden_diagnostic_code
                    for diagnostic in source_ir.diagnostics
                ),
                "native_block_disposition_forbidden_diagnostic_present",
            ),
        )
        for suffix, expected, observed, failure_reason in observed_metrics:
            checks.append(
                _check(
                    check_id=f"{prefix}.{suffix}",
                    category="source_ir",
                    expected=expected,
                    observed=observed,
                    matches=observed == expected,
                    failure_reason=failure_reason,
                )
            )

    for (
        diagnostic_code,
        expected_bindings,
    ) in _native_block_disposition_coverage_expectations(silver):
        observed_binding_parts: list[tuple[str, int, int]] = []
        missing_binding_count = 0
        for diagnostic in source_ir.diagnostics:
            if diagnostic.code != diagnostic_code:
                continue
            if (
                diagnostic.evidence_sha256 is None
                or diagnostic.native_locator is None
                or diagnostic.native_locator.page_number is None
                or diagnostic.native_locator.block_index is None
            ):
                missing_binding_count += 1
                continue
            observed_binding_parts.append(
                (
                    diagnostic.evidence_sha256,
                    diagnostic.native_locator.page_number,
                    diagnostic.native_locator.block_index,
                )
            )
        observed_bindings = (
            tuple(
                f"{content_sha256}:{page_number}:{block_index}"
                for content_sha256, page_number, block_index in sorted(
                    observed_binding_parts
                )
            )
            + ("missing",) * missing_binding_count
        )
        checks.append(
            _check(
                check_id=(
                    "source_ir.native_block_disposition_coverage:"
                    f"{diagnostic_code}.exact_bindings"
                ),
                category="source_ir",
                expected=expected_bindings,
                observed=observed_bindings,
                matches=observed_bindings == expected_bindings,
                failure_reason="native_block_disposition_coverage_mismatch",
            )
        )

    for expectation in silver.image_mask_composite_expectations:
        prefix = f"source_ir.image_mask_composite:{expectation.page_number}"
        matching_placements = tuple(
            sorted(
                (
                    node
                    for node in source_ir.nodes
                    if node.kind in {SourceNodeKind.IMAGE, SourceNodeKind.FIGURE}
                    and node.native_locator.page_number == expectation.page_number
                    and node.asset_derivation is not None
                ),
                key=lambda node: node.native_locator.block_index
                if node.native_locator.block_index is not None
                else -1,
            )
        )
        observed_block_indices = tuple(
            node.native_locator.block_index
            if node.native_locator.block_index is not None
            else -1
            for node in matching_placements
        )
        matching_assets = tuple(
            assets_by_id.get(node.asset_ref or "") for node in matching_placements
        )
        observed_content_sha256 = tuple(
            asset.content_sha256 if asset is not None else "missing"
            for asset in matching_assets
        )
        observed_media_types = tuple(
            asset.media_type if asset is not None else "missing"
            for asset in matching_assets
        )
        observed_derivation_kinds = tuple(
            node.asset_derivation.kind.value
            if node.asset_derivation is not None
            else "missing"
            for node in matching_placements
        )
        observed_base_sha256 = tuple(
            node.asset_derivation.base_content_sha256
            if node.asset_derivation is not None
            else "missing"
            for node in matching_placements
        )
        observed_mask_sha256 = tuple(
            node.asset_derivation.mask_content_sha256
            if node.asset_derivation is not None
            else "missing"
            for node in matching_placements
        )
        unique_asset_count = len(
            {
                node.asset_ref
                for node in matching_placements
                if node.asset_ref is not None
            }
        )
        placement_ids = {node.source_node_id for node in matching_placements}
        matching_structure_ids = {
            node.structure_node_id
            for node in structure_tree.nodes
            if any(
                ref.kind is StructureSourceRefKind.SOURCE_NODE
                and ref.source_id in placement_ids
                for ref in node.source_refs
            )
        }
        projected_requirement_count = sum(
            node.semantic_class is SemanticClass.REQUIREMENT
            for node in requirement_view.nodes
            if node.target_structure_node_id in matching_structure_ids
        )
        page_diagnostics = tuple(
            diagnostic
            for diagnostic in source_ir.diagnostics
            if diagnostic.native_locator is not None
            and diagnostic.native_locator.page_number == expectation.page_number
        )
        materialized_diagnostics = tuple(
            item
            for item in page_diagnostics
            if item.code == "pdf_image_mask_materialized"
        )
        diagnostic_grounding_matches = (
            len(materialized_diagnostics) == len(matching_placements)
            and {item.source_node_id for item in materialized_diagnostics}
            == placement_ids
            and all(
                item.source_node_id is not None
                and item.native_locator is not None
                and item.native_locator.page_number == expectation.page_number
                for item in materialized_diagnostics
            )
        )
        expected_count = len(expectation.block_indices)
        expected_unique_asset_count = len(set(expectation.composite_content_sha256))
        expected_media_types = (expectation.media_type,) * expected_count
        expected_derivation_kinds = (
            SourceAssetDerivationKind.ALPHA_MASK_COMPOSITE.value,
        ) * expected_count
        observed_metrics: tuple[
            tuple[
                str,
                PdfShadowSilverCheckCategory,
                SilverCheckValue,
                SilverCheckValue,
                str,
            ],
            ...,
        ] = (
            (
                "source_status",
                "source_ir",
                expectation.expected_source_status,
                source_ir.status.value,
                "image_mask_source_status_mismatch",
            ),
            (
                "placement_count",
                "source_ir",
                expected_count,
                len(matching_placements),
                "image_mask_placement_count_mismatch",
            ),
            (
                "unique_asset_count",
                "source_ir",
                expected_unique_asset_count,
                unique_asset_count,
                "image_mask_asset_count_mismatch",
            ),
            (
                "block_indices",
                "source_ir",
                expectation.block_indices,
                observed_block_indices,
                "image_mask_block_indices_mismatch",
            ),
            (
                "composite_content_sha256",
                "source_ir",
                expectation.composite_content_sha256,
                observed_content_sha256,
                "image_mask_composite_hash_mismatch",
            ),
            (
                "media_types",
                "source_ir",
                expected_media_types,
                observed_media_types,
                "image_mask_media_type_mismatch",
            ),
            (
                "derivation_kinds",
                "source_ir",
                expected_derivation_kinds,
                observed_derivation_kinds,
                "image_mask_derivation_kind_mismatch",
            ),
            (
                "base_content_sha256",
                "source_ir",
                expectation.base_content_sha256,
                observed_base_sha256,
                "image_mask_base_hash_mismatch",
            ),
            (
                "mask_content_sha256",
                "source_ir",
                expectation.mask_content_sha256,
                observed_mask_sha256,
                "image_mask_mask_hash_mismatch",
            ),
            (
                "materialized_diagnostic_count",
                "source_ir",
                expected_count,
                sum(
                    item.code == "pdf_image_mask_materialized"
                    for item in page_diagnostics
                ),
                "image_mask_materialized_diagnostic_count_mismatch",
            ),
            (
                "materialized_diagnostic_grounding",
                "source_ir",
                True,
                diagnostic_grounding_matches,
                "image_mask_materialized_diagnostic_grounding_mismatch",
            ),
            (
                "failure_diagnostic_count",
                "source_ir",
                0,
                sum(
                    item.code.startswith("pdf_image_mask_")
                    and item.code != "pdf_image_mask_materialized"
                    for item in page_diagnostics
                ),
                "image_mask_failure_diagnostic_count_mismatch",
            ),
            (
                "projected_requirement_count",
                "projection",
                expectation.projected_requirement_count,
                projected_requirement_count,
                "image_mask_projection_count_mismatch",
            ),
        )
        for suffix, category, expected, observed, reason in observed_metrics:
            checks.append(
                _check(
                    check_id=f"{prefix}.{suffix}",
                    category=category,
                    expected=expected,
                    observed=observed,
                    matches=observed == expected,
                    failure_reason=reason,
                )
            )

    for expectation in silver.outline_chains:
        target, reason = _unique_section(section_index, expectation.target_label)
        if target is None:
            checks.append(
                _failed_resolution_check(
                    check_id=f"structure.outline_chain:{expectation.target_label}",
                    category="structure",
                    expected=expectation.ancestor_labels,
                    reason=reason,
                    candidate_count=len(
                        section_index.get(expectation.target_label, ())
                    ),
                )
            )
            continue
        observed = _ancestor_labels(target, structure_tree)
        checks.append(
            _check(
                check_id=f"structure.outline_chain:{expectation.target_label}",
                category="structure",
                expected=expectation.ancestor_labels,
                observed=observed,
                matches=observed == expectation.ancestor_labels,
                failure_reason="ancestor_chain_mismatch",
            )
        )

    view_by_target = {
        node.target_structure_node_id: node for node in requirement_view.nodes
    }
    annotations_by_target: dict[str, list[Any]] = {}
    for annotation in semantics.annotations:
        annotations_by_target.setdefault(
            annotation.target_structure_node_id,
            [],
        ).append(annotation)
    structure_nodes_by_id = {
        node.structure_node_id: node for node in structure_tree.nodes
    }
    for expectation in silver.structure_scope_expectations:
        target, reason = _unique_section(section_index, expectation.section_label)
        direct_key = "direct" if expectation.direct_only else "descendant"
        prefix = (
            f"structure.scope:{expectation.section_label}:"
            f"{direct_key}:{expectation.target_kind.value}"
        )
        if target is None:
            checks.append(
                _failed_resolution_check(
                    check_id=f"{prefix}.count",
                    category="structure",
                    expected=expectation.count,
                    reason=reason,
                    candidate_count=len(
                        section_index.get(expectation.section_label, ())
                    ),
                )
            )
            continue
        if expectation.direct_only:
            scoped_nodes = tuple(
                node
                for node in structure_tree.nodes
                if node.parent_structure_node_id == target.structure_node_id
            )
        else:
            descendant_ids = _descendant_ids(
                target.structure_node_id,
                structure_tree,
            ) - {target.structure_node_id}
            scoped_nodes = tuple(
                structure_nodes_by_id[node_id] for node_id in descendant_ids
            )
        observed_count = sum(
            node.kind is expectation.target_kind for node in scoped_nodes
        )
        checks.append(
            _check(
                check_id=f"{prefix}.count",
                category="structure",
                expected=expectation.count,
                observed=observed_count,
                matches=observed_count == expectation.count,
                failure_reason="structure_scope_count_mismatch",
            )
        )

    source_nodes_by_id = {node.source_node_id: node for node in source_ir.nodes}
    for expectation in silver.revision_history_expectations:
        prefix = (
            f"revision_history:{expectation.page_number}:"
            f"{expectation.table_index}:{expectation.row_count}x"
            f"{expectation.column_count}"
        )
        matching_source_tables = tuple(
            node
            for node in source_ir.nodes
            if node.kind is SourceNodeKind.TABLE
            and node.native_locator.page_number == expectation.page_number
            and node.native_locator.table_index == expectation.table_index
            and node.table_payload is not None
            and node.table_payload.row_count == expectation.row_count
            and node.table_payload.column_count == expectation.column_count
        )
        checks.append(
            _check(
                check_id=f"{prefix}.source_table_count",
                category="source_ir",
                expected=1,
                observed=len(matching_source_tables),
                matches=len(matching_source_tables) == 1,
                failure_reason="revision_history_source_table_count_mismatch",
            )
        )

        matching_structure_tables: tuple[StructureNode, ...] = ()
        if len(matching_source_tables) == 1:
            source_table_id = matching_source_tables[0].source_node_id
            matching_structure_tables = tuple(
                node
                for node in structure_tree.nodes
                if node.kind is StructureNodeKind.TABLE
                and any(
                    ref.kind is StructureSourceRefKind.SOURCE_NODE
                    and ref.source_id == source_table_id
                    for ref in node.source_refs
                )
            )

        row_nodes_by_index: dict[int, list[StructureNode]] = {}
        if len(matching_structure_tables) == 1:
            structure_table_id = matching_structure_tables[0].structure_node_id
            for node in structure_tree.nodes:
                if (
                    node.kind is StructureNodeKind.TABLE_ROW
                    and node.parent_structure_node_id == structure_table_id
                    and node.table_row_index is not None
                ):
                    row_nodes_by_index.setdefault(node.table_row_index, []).append(node)

        resolved_row_nodes: dict[int, StructureNode] = {}
        for row_index in expectation.revision_row_indices:
            candidates = row_nodes_by_index.get(row_index, ())
            if len(candidates) == 1:
                resolved_row_nodes[row_index] = candidates[0]
        observed_row_indices: SilverCheckValue = (
            tuple(sorted(resolved_row_nodes))
            if len(matching_structure_tables) == 1
            else None
        )
        checks.append(
            _check(
                check_id=f"{prefix}.structure_row_indices",
                category="structure",
                expected=expectation.revision_row_indices,
                observed=observed_row_indices,
                matches=observed_row_indices == expectation.revision_row_indices,
                failure_reason="revision_history_structure_rows_mismatch",
            )
        )

        expected_classes = tuple(
            f"{row_index}:{SemanticClass.VERSION_METADATA.value}"
            for row_index in expectation.revision_row_indices
        )
        observed_classes: list[str] = []
        for row_index in expectation.revision_row_indices:
            row_node = resolved_row_nodes.get(row_index)
            if row_node is None:
                observed_classes.append(f"{row_index}:MISSING")
                continue
            annotations = annotations_by_target.get(row_node.structure_node_id, ())
            if len(annotations) != 1:
                observed_classes.append(
                    f"{row_index}:{'MISSING' if not annotations else 'AMBIGUOUS'}"
                )
                continue
            annotation = annotations[0]
            observed_class = (
                "REJECTED"
                if annotation.status is SemanticAnnotationStatus.REJECTED
                else annotation.semantic_class.value
            )
            observed_classes.append(f"{row_index}:{observed_class}")
        observed_class_tuple = tuple(observed_classes)
        checks.append(
            _check(
                check_id=f"{prefix}.semantic_classes",
                category="semantic",
                expected=expected_classes,
                observed=observed_class_tuple,
                matches=observed_class_tuple == expected_classes,
                failure_reason="revision_history_semantic_classes_mismatch",
            )
        )

        resolved_row_ids = {
            node.structure_node_id for node in resolved_row_nodes.values()
        }
        observed_projected_count: SilverCheckValue = (
            sum(
                node.semantic_class is SemanticClass.REQUIREMENT
                and node.target_structure_node_id in resolved_row_ids
                for node in requirement_view.nodes
            )
            if len(resolved_row_nodes) == len(expectation.revision_row_indices)
            else None
        )
        checks.append(
            _check(
                check_id=f"{prefix}.projected_requirement_count",
                category="projection",
                expected=expectation.projected_requirement_count,
                observed=observed_projected_count,
                matches=(
                    observed_projected_count == expectation.projected_requirement_count
                ),
                failure_reason="revision_history_projection_count_mismatch",
            )
        )

    for expectation in silver.table_expectations:
        target, reason = _unique_section(section_index, expectation.section_label)
        prefix = (
            f"structure.table:{expectation.section_label}:"
            f"{expectation.page_number}:{expectation.row_count}x"
            f"{expectation.column_count}"
        )
        if target is None:
            checks.append(
                _failed_resolution_check(
                    check_id=f"{prefix}.count",
                    category="structure",
                    expected=expectation.count,
                    reason=reason,
                    candidate_count=len(
                        section_index.get(expectation.section_label, ())
                    ),
                )
            )
            continue
        scoped_ids = _descendant_ids(target.structure_node_id, structure_tree)
        expected_parent_id: str | None = None
        if expectation.parent_list_ordinal is not None:
            ordinal_parents = tuple(
                node
                for node in _direct_numbered_children(target, structure_tree)
                if node.list_ordinal == expectation.parent_list_ordinal
            )
            occurrence_index = (expectation.parent_ordinal_occurrence or 0) - 1
            if 0 <= occurrence_index < len(ordinal_parents):
                expected_parent_id = ordinal_parents[occurrence_index].structure_node_id
        matching_count = 0
        for structure_node in structure_tree.nodes:
            if (
                structure_node.structure_node_id not in scoped_ids
                or structure_node.kind is not StructureNodeKind.TABLE
            ):
                continue
            source_table = next(
                (
                    source_nodes_by_id[ref.source_id]
                    for ref in structure_node.source_refs
                    if ref.kind is StructureSourceRefKind.SOURCE_NODE
                    and ref.source_id in source_nodes_by_id
                    and source_nodes_by_id[ref.source_id].kind is SourceNodeKind.TABLE
                ),
                None,
            )
            table = source_table.table_payload if source_table is not None else None
            if (
                source_table is None
                or table is None
                or source_table.native_locator.page_number != expectation.page_number
                or table.row_count != expectation.row_count
                or table.column_count != expectation.column_count
                or (
                    expectation.parent_list_ordinal is not None
                    and structure_node.parent_structure_node_id != expected_parent_id
                )
            ):
                continue
            matching_count += 1
        checks.append(
            _check(
                check_id=f"{prefix}.count",
                category="structure",
                expected=expectation.count,
                observed=matching_count,
                matches=matching_count == expectation.count,
                failure_reason="table_scope_count_mismatch",
            )
        )

    for expectation in silver.table_note_expectations:
        target, reason = _unique_section(section_index, expectation.section_label)
        prefix = (
            f"structure.table_note:{expectation.section_label}:"
            f"{expectation.page_number}:{expectation.row_count}x"
            f"{expectation.column_count}"
        )
        if target is None:
            for suffix, expected in (
                ("table_count", 1),
                ("applies_to_rows", expectation.applies_to_row_indices),
            ):
                checks.append(
                    _failed_resolution_check(
                        check_id=f"{prefix}.{suffix}",
                        category="structure",
                        expected=expected,
                        reason=reason,
                        candidate_count=len(
                            section_index.get(expectation.section_label, ())
                        ),
                    )
                )
            continue
        matching_tables = _matching_scoped_tables(
            section=target,
            page_number=expectation.page_number,
            row_count=expectation.row_count,
            column_count=expectation.column_count,
            source_ir=source_ir,
            structure_tree=structure_tree,
        )
        checks.append(
            _check(
                check_id=f"{prefix}.table_count",
                category="structure",
                expected=1,
                observed=len(matching_tables),
                matches=len(matching_tables) == 1,
                failure_reason=(
                    "table_note_table_missing"
                    if not matching_tables
                    else "table_note_table_ambiguous"
                ),
            )
        )
        if len(matching_tables) != 1:
            checks.append(
                _check(
                    check_id=f"{prefix}.applies_to_rows",
                    category="structure",
                    expected=expectation.applies_to_row_indices,
                    observed=None,
                    matches=False,
                    failure_reason="table_note_table_unresolved",
                )
            )
            continue
        table_node = matching_tables[0]
        rows_by_index = {
            node.table_row_index: node
            for node in structure_tree.nodes
            if node.parent_structure_node_id == table_node.structure_node_id
            and node.kind is StructureNodeKind.TABLE_ROW
            and node.table_row_index is not None
        }
        note_row = rows_by_index.get(expectation.note_row_index)
        observed_rows: tuple[int, ...] = ()
        if note_row is not None:
            row_index_by_id = {
                node.structure_node_id: row_index
                for row_index, node in rows_by_index.items()
            }
            observed_rows = tuple(
                sorted(
                    row_index_by_id[relation.target_structure_node_id]
                    for relation in structure_tree.relations
                    if relation.kind is StructureRelationKind.APPLIES_TO
                    and relation.source_structure_node_id == note_row.structure_node_id
                    and relation.target_structure_node_id in row_index_by_id
                )
            )
        checks.append(
            _check(
                check_id=f"{prefix}.applies_to_rows",
                category="structure",
                expected=expectation.applies_to_row_indices,
                observed=observed_rows,
                matches=observed_rows == expectation.applies_to_row_indices,
                failure_reason="table_note_scope_mismatch",
            )
        )

    for expectation in silver.figure_expectations:
        target, reason = _unique_section(section_index, expectation.section_label)
        prefix = (
            f"figure:{expectation.section_label}:{expectation.page_number}:"
            f"{expectation.source_node_kind.value}"
        )
        expected_metrics: tuple[
            tuple[str, PdfShadowSilverCheckCategory, SilverCheckValue], ...
        ] = (
            ("asset_count", "source_ir", expectation.asset_count),
            ("placement_count", "source_ir", expectation.placement_count),
            ("structure_count", "structure", expectation.structure_count),
            (
                "reference_relation_count",
                "structure",
                expectation.reference_relation_count,
            ),
            (
                "projected_requirement_count",
                "projection",
                expectation.projected_requirement_count,
            ),
            ("projection_role", "projection", expectation.projection_role.value),
            ("decision_mode", "projection", expectation.decision_mode.value),
        )
        if target is None:
            for suffix, category, expected in expected_metrics:
                checks.append(
                    _failed_resolution_check(
                        check_id=f"{prefix}.{suffix}",
                        category=category,
                        expected=expected,
                        reason=reason,
                        candidate_count=len(
                            section_index.get(expectation.section_label, ())
                        ),
                    )
                )
            continue
        matching_assets = tuple(
            asset
            for asset in source_ir.assets
            if asset.content_sha256 == expectation.content_sha256
            and asset.kind.value == expectation.source_node_kind.value
        )
        matching_asset_ids = {asset.asset_id for asset in matching_assets}
        matching_placements = tuple(
            node
            for node in source_ir.nodes
            if node.kind is expectation.source_node_kind
            and node.native_locator.page_number == expectation.page_number
            and node.asset_ref in matching_asset_ids
            and node.asset_ref in assets_by_id
        )
        placement_ids = {node.source_node_id for node in matching_placements}
        structural_scope = _descendant_ids(
            target.structure_node_id,
            structure_tree,
        )
        matching_structure_nodes = tuple(
            node
            for node in structure_tree.nodes
            if node.structure_node_id in structural_scope
            and any(
                ref.kind is StructureSourceRefKind.SOURCE_NODE
                and ref.source_id in placement_ids
                for ref in node.source_refs
            )
        )
        matching_structure_ids = {
            node.structure_node_id for node in matching_structure_nodes
        }
        reference_count = sum(
            relation.kind is StructureRelationKind.REFERENCES
            and relation.source_structure_node_id in structural_scope
            and relation.target_structure_node_id in matching_structure_ids
            for relation in structure_tree.relations
        )
        matching_views = tuple(
            node
            for node in requirement_view.nodes
            if node.target_structure_node_id in matching_structure_ids
        )
        projected_requirement_count = sum(
            node.semantic_class is SemanticClass.REQUIREMENT for node in matching_views
        )
        observed_roles = tuple(sorted({node.role.value for node in matching_views}))
        observed_modes = tuple(
            sorted({node.decision_mode.value for node in matching_views})
        )
        observed_role: SilverCheckValue = (
            observed_roles[0]
            if len(observed_roles) == 1
            else observed_roles
            if observed_roles
            else None
        )
        observed_mode: SilverCheckValue = (
            observed_modes[0]
            if len(observed_modes) == 1
            else observed_modes
            if observed_modes
            else None
        )
        observed_metrics: tuple[tuple[str, SilverCheckValue], ...] = (
            ("asset_count", len(matching_assets)),
            ("placement_count", len(matching_placements)),
            ("structure_count", len(matching_structure_nodes)),
            ("reference_relation_count", reference_count),
            ("projected_requirement_count", projected_requirement_count),
            ("projection_role", observed_role),
            ("decision_mode", observed_mode),
        )
        for (suffix, category, expected), (_, observed) in zip(
            expected_metrics,
            observed_metrics,
            strict=True,
        ):
            checks.append(
                _check(
                    check_id=f"{prefix}.{suffix}",
                    category=category,
                    expected=expected,
                    observed=observed,
                    matches=observed == expected,
                    failure_reason=f"figure_{suffix}_mismatch",
                )
            )

    source_text_by_ref = _source_text_by_ref(source_ir)
    view_target_ids = {node.target_structure_node_id for node in requirement_view.nodes}
    for expectation in silver.option_state_expectations:
        target, reason = _unique_section(section_index, expectation.section_label)
        prefix = f"option_state:{expectation.section_label}:{expectation.page_number}"
        expected_states = tuple(state.value for state in expectation.states)
        expected_metrics: tuple[
            tuple[str, PdfShadowSilverCheckCategory, SilverCheckValue], ...
        ] = (
            ("source_count", "source_ir", len(expectation.states)),
            ("source_states", "source_ir", expected_states),
            ("relation_count", "structure", len(expectation.states)),
            ("relation_states", "structure", expected_states),
            (
                "projected_checked_count",
                "projection",
                expectation.projected_checked_count,
            ),
            (
                "projected_unchecked_count",
                "projection",
                expectation.projected_unchecked_count,
            ),
            (
                "projected_uncertain_count",
                "projection",
                expectation.projected_uncertain_count,
            ),
        )
        if target is None:
            for suffix, category, expected in expected_metrics:
                checks.append(
                    _failed_resolution_check(
                        check_id=f"{prefix}.{suffix}",
                        category=category,
                        expected=expected,
                        reason=reason,
                        candidate_count=len(
                            section_index.get(expectation.section_label, ())
                        ),
                    )
                )
            continue

        scoped_ids = _descendant_ids(
            target.structure_node_id,
            structure_tree,
        )
        candidates = tuple(
            (node, state)
            for node in sorted(
                structure_tree.nodes,
                key=lambda item: (item.source_order, item.structure_node_id),
            )
            if node.structure_node_id in scoped_ids
            and node.kind in {StructureNodeKind.CONTENT, StructureNodeKind.CLAUSE}
            and page_index.pages_for_structure_node(
                node.structure_node_id,
                structure_tree,
            )
            == (expectation.page_number,)
            and (
                state := _explicit_option_state(
                    _structure_node_text(
                        node,
                        source_text_by_ref=source_text_by_ref,
                    )
                )
            )
            is not None
        )
        candidate_ids = {node.structure_node_id for node, _ in candidates}
        option_relations = tuple(
            relation
            for relation in structure_tree.relations
            if relation.kind is StructureRelationKind.OPTION_STATE
            and relation.target_structure_node_id in scoped_ids
            and page_index.pages_for_structure_node(
                relation.target_structure_node_id,
                structure_tree,
            )
            == (expectation.page_number,)
        )
        relations_by_target: dict[str, list[Any]] = {}
        for relation in option_relations:
            relations_by_target.setdefault(
                relation.target_structure_node_id,
                [],
            ).append(relation)
        observed_relation_states: list[str] = []
        for node, _ in candidates:
            relations = relations_by_target.get(node.structure_node_id, ())
            if len(relations) == 1 and relations[0].option_state is not None:
                observed_relation_states.append(relations[0].option_state.value)
            elif not relations:
                observed_relation_states.append("MISSING")
            else:
                observed_relation_states.append("AMBIGUOUS")

        projected_counts = {
            StructureOptionState.CHECKED: 0,
            StructureOptionState.UNCHECKED: 0,
            StructureOptionState.UNCERTAIN: 0,
        }
        for node, state in candidates:
            if node.structure_node_id in view_target_ids:
                projected_counts[state] += 1
        observed_metrics: tuple[tuple[str, SilverCheckValue], ...] = (
            ("source_count", len(candidates)),
            ("source_states", tuple(state.value for _, state in candidates)),
            ("relation_count", len(option_relations)),
            ("relation_states", tuple(observed_relation_states)),
            (
                "projected_checked_count",
                projected_counts[StructureOptionState.CHECKED],
            ),
            (
                "projected_unchecked_count",
                projected_counts[StructureOptionState.UNCHECKED],
            ),
            (
                "projected_uncertain_count",
                projected_counts[StructureOptionState.UNCERTAIN],
            ),
        )
        for (suffix, category, expected), (_, observed) in zip(
            expected_metrics,
            observed_metrics,
            strict=True,
        ):
            checks.append(
                _check(
                    check_id=f"{prefix}.{suffix}",
                    category=category,
                    expected=expected,
                    observed=observed,
                    matches=(
                        observed == expected
                        and (
                            suffix not in {"relation_count", "relation_states"}
                            or candidate_ids
                            == {
                                relation.target_structure_node_id
                                for relation in option_relations
                            }
                        )
                    ),
                    failure_reason=f"option_state_{suffix}_mismatch",
                )
            )
    for expectation in silver.continuations:
        target, reason = _unique_section(section_index, expectation.section_label)
        prefix = (
            f"structure.continuation:{expectation.section_label}:"
            f"{expectation.from_page}-{expectation.to_page}"
        )
        if target is None:
            unresolved_metrics: list[tuple[str, SilverCheckValue]] = [
                ("relation_count", 1)
            ]
            if expectation.numbered_child_count is not None:
                unresolved_metrics.append(
                    ("numbered_child_count", expectation.numbered_child_count)
                )
            if expectation.pseudo_primary_requirement_count is not None:
                unresolved_metrics.append(
                    (
                        "pseudo_primary_requirement_count",
                        expectation.pseudo_primary_requirement_count,
                    )
                )
            for suffix, expected in unresolved_metrics:
                checks.append(
                    _failed_resolution_check(
                        check_id=f"{prefix}.{suffix}",
                        category="structure",
                        expected=expected,
                        reason=reason,
                        candidate_count=len(
                            section_index.get(expectation.section_label, ())
                        ),
                    )
                )
            continue

        descendants = _descendant_ids(target.structure_node_id, structure_tree)
        continuation_matches = tuple(
            relation
            for relation in structure_tree.relations
            if relation.kind is StructureRelationKind.CONTINUES
            and relation.source_structure_node_id in descendants
            and relation.target_structure_node_id in descendants
            and (
                expectation.node_kind is None
                or (
                    structure_nodes_by_id[relation.source_structure_node_id].kind
                    is expectation.node_kind
                    and structure_nodes_by_id[relation.target_structure_node_id].kind
                    is expectation.node_kind
                )
            )
            and page_index.pages_for_structure_node(
                relation.source_structure_node_id,
                structure_tree,
            )
            == (expectation.from_page,)
            and page_index.pages_for_structure_node(
                relation.target_structure_node_id,
                structure_tree,
            )
            == (expectation.to_page,)
        )
        checks.append(
            _check(
                check_id=f"{prefix}.relation_count",
                category="structure",
                expected=1,
                observed=len(continuation_matches),
                matches=len(continuation_matches) == 1,
                failure_reason=(
                    "continuation_missing"
                    if not continuation_matches
                    else "continuation_ambiguous"
                ),
            )
        )

        if (
            expectation.numbered_child_count is not None
            or expectation.pseudo_primary_requirement_count is not None
        ):
            numbered_children = _direct_numbered_children(target, structure_tree)
            if expectation.numbered_child_count is not None:
                checks.append(
                    _check(
                        check_id=f"{prefix}.numbered_child_count",
                        category="structure",
                        expected=expectation.numbered_child_count,
                        observed=len(numbered_children),
                        matches=(
                            len(numbered_children) == expectation.numbered_child_count
                        ),
                        failure_reason="numbered_child_count_mismatch",
                    )
                )
            if expectation.pseudo_primary_requirement_count is not None:
                numbered_child_ids = {
                    node.structure_node_id for node in numbered_children
                }
                pseudo_primary_count = sum(
                    node.role is RequirementProjectionRole.PRIMARY
                    and node.target_structure_node_id in numbered_child_ids
                    for node in requirement_view.nodes
                )
                checks.append(
                    _check(
                        check_id=f"{prefix}.pseudo_primary_requirement_count",
                        category="structure",
                        expected=expectation.pseudo_primary_requirement_count,
                        observed=pseudo_primary_count,
                        matches=(
                            pseudo_primary_count
                            == expectation.pseudo_primary_requirement_count
                        ),
                        failure_reason="pseudo_primary_requirement_count_mismatch",
                    )
                )

    for expectation in silver.numbered_children:
        target, reason = _unique_section(section_index, expectation.section_label)
        check_id = f"structure.numbered_children:{expectation.section_label}"
        if target is None:
            checks.append(
                _failed_resolution_check(
                    check_id=check_id,
                    category="structure",
                    expected=expectation.ordinals,
                    reason=reason,
                    candidate_count=len(
                        section_index.get(expectation.section_label, ())
                    ),
                )
            )
            continue
        observed = tuple(
            node.list_ordinal
            for node in _direct_numbered_children(target, structure_tree)
            if node.list_ordinal is not None
        )
        checks.append(
            _check(
                check_id=check_id,
                category="structure",
                expected=expectation.ordinals,
                observed=observed,
                matches=observed == expectation.ordinals,
                failure_reason="numbered_child_ordinals_mismatch",
            )
        )

    for expectation in silver.finding_expectations:
        scope_key = expectation.section_label or "document"
        check_id = f"finding:{expectation.code}:{scope_key}.count"
        scoped_structure_ids: set[str] | None = None
        if expectation.section_label is not None:
            target, reason = _unique_section(
                section_index,
                expectation.section_label,
            )
            if target is None:
                checks.append(
                    _failed_resolution_check(
                        check_id=check_id,
                        category="finding",
                        expected=expectation.count,
                        reason=reason,
                        candidate_count=len(
                            section_index.get(expectation.section_label, ())
                        ),
                    )
                )
                continue
            scoped_structure_ids = _descendant_ids(
                target.structure_node_id,
                structure_tree,
            )
        observed_count = sum(
            finding.code == expectation.code
            and (
                scoped_structure_ids is None
                or finding.structure_node_id in scoped_structure_ids
            )
            for finding in structure_tree.findings
        )
        checks.append(
            _check(
                check_id=check_id,
                category="finding",
                expected=expectation.count,
                observed=observed_count,
                matches=observed_count == expectation.count,
                failure_reason="finding_count_mismatch",
            )
        )

    for expectation in silver.semantic_expectations:
        target, reason = _unique_section(section_index, expectation.section_label)
        prefix = f"semantic:{expectation.section_label}"
        expected_subtype = (
            expectation.requirement_subtype.value
            if expectation.requirement_subtype is not None
            else None
        )
        if target is None:
            for suffix, expected in (
                ("class", expectation.semantic_class.value),
                ("subtype", expected_subtype),
            ):
                checks.append(
                    _failed_resolution_check(
                        check_id=f"{prefix}.{suffix}",
                        category="semantic",
                        expected=expected,
                        reason=reason,
                        candidate_count=len(
                            section_index.get(expectation.section_label, ())
                        ),
                    )
                )
            continue
        annotations = annotations_by_target.get(target.structure_node_id, ())
        if len(annotations) != 1:
            reason = (
                "semantic_annotation_missing"
                if not annotations
                else "semantic_annotation_ambiguous"
            )
            for suffix, expected in (
                ("class", expectation.semantic_class.value),
                ("subtype", expected_subtype),
            ):
                checks.append(
                    _failed_resolution_check(
                        check_id=f"{prefix}.{suffix}",
                        category="semantic",
                        expected=expected,
                        reason=reason,
                        candidate_count=len(annotations),
                    )
                )
            continue
        annotation = annotations[0]
        is_active = annotation.status is not SemanticAnnotationStatus.REJECTED
        checks.append(
            _check(
                check_id=f"{prefix}.class",
                category="semantic",
                expected=expectation.semantic_class.value,
                observed=annotation.semantic_class.value,
                matches=(
                    is_active
                    and annotation.semantic_class is expectation.semantic_class
                ),
                failure_reason=(
                    "semantic_annotation_inactive"
                    if not is_active
                    else "semantic_class_mismatch"
                ),
            )
        )
        observed_subtype = (
            annotation.requirement_subtype.value
            if annotation.requirement_subtype is not None
            else None
        )
        checks.append(
            _check(
                check_id=f"{prefix}.subtype",
                category="semantic",
                expected=expected_subtype,
                observed=observed_subtype,
                matches=(
                    is_active
                    and annotation.requirement_subtype
                    is expectation.requirement_subtype
                ),
                failure_reason=(
                    "semantic_annotation_inactive"
                    if not is_active
                    else "semantic_subtype_mismatch"
                ),
            )
        )

    for expectation in silver.projection_expectations:
        target, reason = _unique_section(section_index, expectation.section_label)
        prefix = f"projection:{expectation.section_label}"
        metrics: tuple[tuple[str, SilverCheckValue], ...] = (
            ("primary_count", expectation.primary_count),
            ("content_child_count", expectation.content_child_count),
            (
                "top_level_requirement_count",
                expectation.top_level_requirement_count,
            ),
            ("primary_decision_mode", expectation.primary_decision_mode.value),
        )
        if target is None:
            for suffix, expected in metrics:
                checks.append(
                    _failed_resolution_check(
                        check_id=f"{prefix}.{suffix}",
                        category="projection",
                        expected=expected,
                        reason=reason,
                        candidate_count=len(
                            section_index.get(expectation.section_label, ())
                        ),
                    )
                )
            continue

        primary = view_by_target.get(target.structure_node_id)
        primary_count = int(
            primary is not None and primary.role is RequirementProjectionRole.PRIMARY
        )
        content_child_count = (
            _content_descendant_count(primary, requirement_view)
            if primary_count == 1
            else 0
        )
        structural_scope = _descendant_ids(
            target.structure_node_id,
            structure_tree,
        )
        top_level_count = sum(
            node.role is RequirementProjectionRole.PRIMARY
            and node.semantic_class is SemanticClass.REQUIREMENT
            and node.target_structure_node_id in structural_scope
            for node in requirement_view.nodes
        )
        decision_mode = (
            primary.decision_mode.value
            if primary_count == 1 and primary is not None
            else RequirementDecisionMode.NONE.value
        )
        observed_metrics: tuple[tuple[str, SilverCheckValue], ...] = (
            ("primary_count", primary_count),
            ("content_child_count", content_child_count),
            ("top_level_requirement_count", top_level_count),
            ("primary_decision_mode", decision_mode),
        )
        for (suffix, expected), (_, observed) in zip(
            metrics,
            observed_metrics,
            strict=True,
        ):
            checks.append(
                _check(
                    check_id=f"{prefix}.{suffix}",
                    category="projection",
                    expected=expected,
                    observed=observed,
                    matches=observed == expected,
                    failure_reason=f"{suffix}_mismatch",
                )
            )

    for expectation in silver.projection_scope_expectations:
        role_key = (
            expectation.role.value.lower() if expectation.role is not None else "any"
        )
        kind_key = (
            expectation.target_kind.value
            if expectation.target_kind is not None
            else "any"
        )
        prefix = f"projection.scope:{expectation.section_label}:{role_key}:{kind_key}"
        target, reason = _unique_section(section_index, expectation.section_label)
        if target is None:
            checks.append(
                _failed_resolution_check(
                    check_id=f"{prefix}.count",
                    category="projection",
                    expected=expectation.count,
                    reason=reason,
                    candidate_count=len(
                        section_index.get(expectation.section_label, ())
                    ),
                )
            )
            continue
        structural_scope = _descendant_ids(
            target.structure_node_id,
            structure_tree,
        )
        observed_count = sum(
            node.target_structure_node_id in structural_scope
            and (expectation.role is None or node.role is expectation.role)
            and (
                expectation.target_kind is None
                or structure_nodes_by_id[node.target_structure_node_id].kind
                is expectation.target_kind
            )
            for node in requirement_view.nodes
        )
        checks.append(
            _check(
                check_id=f"{prefix}.count",
                category="projection",
                expected=expectation.count,
                observed=observed_count,
                matches=observed_count == expectation.count,
                failure_reason="projection_scope_count_mismatch",
            )
        )

    return _report(
        registry=registry,
        document_id=document_id,
        source_ir=source_ir,
        checks=checks,
    )


def _validate_bbox_q100(value: tuple[int, int, int, int]) -> None:
    x0, y0, x1, y1 = value
    if x1 <= x0 or y1 <= y0:
        raise ValueError("quantized bounding boxes require positive area")


def _image_cell_assignment_sort_key(
    assignment: PdfShadowSilverImageCellOwnershipAssignment,
) -> tuple[Any, ...]:
    return (
        assignment.image.page_number,
        assignment.image.block_index,
        assignment.image.kind.value,
        assignment.image.content_sha256,
        assignment.image.bbox_q100,
        assignment.table.index,
        assignment.table.bbox_q100,
        assignment.table.shape,
        tuple(
            (
                cell.row,
                cell.column,
                cell.span,
                cell.bbox_q100,
            )
            for cell in assignment.cell_bindings
        ),
        assignment.relation_kind,
    )


def _image_cell_assignment_sha256(
    assignment: PdfShadowSilverImageCellOwnershipAssignment,
) -> str:
    canonical = json.dumps(
        assignment.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _bbox_q100(
    bbox: tuple[float, float, float, float],
) -> tuple[int, int, int, int]:
    return (
        round(bbox[0] * 100.0),
        round(bbox[1] * 100.0),
        round(bbox[2] * 100.0),
        round(bbox[3] * 100.0),
    )


def _bbox_q100_intersects(
    left: tuple[int, int, int, int],
    right: tuple[int, int, int, int],
) -> bool:
    return bool(
        left[0] < right[2]
        and right[0] < left[2]
        and left[1] < right[3]
        and right[1] < left[3]
    )


def _bbox_coverage_for_image_cell_silver(
    subject: tuple[float, float, float, float],
    container: tuple[float, float, float, float],
) -> float:
    subject_x0, subject_y0, subject_x1, subject_y1 = subject
    container_x0, container_y0, container_x1, container_y1 = container
    subject_area = max(0.0, subject_x1 - subject_x0) * max(
        0.0,
        subject_y1 - subject_y0,
    )
    if subject_area <= 0.0:
        return 0.0
    intersection_width = max(
        0.0,
        min(subject_x1, container_x1) - max(subject_x0, container_x0),
    )
    intersection_height = max(
        0.0,
        min(subject_y1, container_y1) - max(subject_y0, container_y0),
    )
    return intersection_width * intersection_height / subject_area


def _is_page_canvas_image_for_silver(node: SourceNode) -> bool:
    bbox = node.native_locator.bbox
    if bbox is None:
        return False
    x0, y0, x1, y1 = bbox
    return x0 <= 2.0 and y0 <= 2.0 and x1 - x0 >= 400.0 and y1 - y0 >= 500.0


def _resolve_pdf_image_cell_geometry(
    source_ir: SourceIRSnapshot,
    *,
    image_node: SourceNode,
) -> tuple[SourceNode | None, TableCellIR | None, int, int]:
    """Recompute the v1 geometry policy directly from SourceIR facts.

    This intentionally duplicates the public Silver policy instead of calling
    a Structure resolver helper, so a resolver regression cannot certify
    itself. Counts are binary resolution outcomes: ambiguity or incomplete
    geometry yields zero and is never resolved by nearest/center heuristics.
    """

    locator = image_node.native_locator
    if (
        image_node.kind not in {SourceNodeKind.IMAGE, SourceNodeKind.FIGURE}
        or locator.format is not NativeDocumentFormat.PDF
        or locator.page_number is None
        or locator.block_index is None
        or locator.bbox is None
        or image_node.parent_source_node_id is not None
        or _is_page_canvas_image_for_silver(image_node)
        or (locator.bbox[2] - locator.bbox[0] < _PDF_IMAGE_CELL_MIN_DIMENSION_POINTS)
        or (locator.bbox[3] - locator.bbox[1] < _PDF_IMAGE_CELL_MIN_DIMENSION_POINTS)
    ):
        return None, None, 0, 0

    page_tables = tuple(
        node
        for node in source_ir.nodes
        if node.kind is SourceNodeKind.TABLE
        and node.native_locator.format is NativeDocumentFormat.PDF
        and node.native_locator.page_number == locator.page_number
        and node.table_payload is not None
    )
    measurable_tables = tuple(
        table_node
        for table_node in page_tables
        if table_node.native_locator.bbox is not None
    )
    table_coverages = {
        table_node.source_node_id: _bbox_coverage_for_image_cell_silver(
            locator.bbox,
            table_node.native_locator.bbox,
        )
        for table_node in measurable_tables
        if table_node.native_locator.bbox is not None
    }
    strong_tables = tuple(
        table_node
        for table_node in measurable_tables
        if table_coverages[table_node.source_node_id] >= _PDF_IMAGE_CELL_MIN_COVERAGE
    )
    competing_tables = tuple(
        table_node
        for table_node in measurable_tables
        if table_coverages[table_node.source_node_id]
        > _PDF_IMAGE_CELL_MAX_COMPETING_COVERAGE
    )
    if (
        len(strong_tables) != 1
        or len(competing_tables) != 1
        or len(measurable_tables) != len(page_tables)
    ):
        return None, None, 0, 0

    table_node = strong_tables[0]
    table = table_node.table_payload
    if table is None or any(cell.native_locator.bbox is None for cell in table.cells):
        return None, None, 0, 0
    cell_coverages = {
        cell.cell_id: _bbox_coverage_for_image_cell_silver(
            locator.bbox,
            cell.native_locator.bbox,
        )
        for cell in table.cells
        if cell.native_locator.bbox is not None
    }
    strong_cells = tuple(
        cell
        for cell in table.cells
        if cell_coverages[cell.cell_id] >= _PDF_IMAGE_CELL_MIN_COVERAGE
    )
    competing_cells = tuple(
        cell
        for cell in table.cells
        if cell_coverages[cell.cell_id] > _PDF_IMAGE_CELL_MAX_COMPETING_COVERAGE
    )
    if len(strong_cells) != 1 or competing_cells != strong_cells:
        return table_node, None, 1, 0
    cell = strong_cells[0]
    return table_node, cell, 1, 1


def _resolve_pdf_image_multi_cell_geometry(
    source_ir: SourceIRSnapshot,
    *,
    image_node: SourceNode,
) -> tuple[SourceNode | None, tuple[TableCellIR, ...], int, int]:
    """Independently recompute exact multi-cell ownership from SourceIR."""

    locator = image_node.native_locator
    if (
        image_node.kind not in {SourceNodeKind.IMAGE, SourceNodeKind.FIGURE}
        or locator.format is not NativeDocumentFormat.PDF
        or locator.page_number is None
        or locator.block_index is None
        or locator.bbox is None
        or image_node.parent_source_node_id is not None
        or _is_page_canvas_image_for_silver(image_node)
        or (
            locator.bbox[2] - locator.bbox[0]
            < _PDF_IMAGE_CELL_MIN_MULTI_DIMENSION_POINTS
        )
        or (
            locator.bbox[3] - locator.bbox[1]
            < _PDF_IMAGE_CELL_MIN_MULTI_DIMENSION_POINTS
        )
    ):
        return None, (), 0, 0
    page_tables = tuple(
        node
        for node in source_ir.nodes
        if node.kind is SourceNodeKind.TABLE
        and node.native_locator.format is NativeDocumentFormat.PDF
        and node.native_locator.page_number == locator.page_number
        and node.table_payload is not None
    )
    measurable_tables = tuple(
        table_node
        for table_node in page_tables
        if table_node.native_locator.bbox is not None
    )
    table_coverages = {
        table_node.source_node_id: _bbox_coverage_for_image_cell_silver(
            locator.bbox,
            table_node.native_locator.bbox,
        )
        for table_node in measurable_tables
        if table_node.native_locator.bbox is not None
    }
    strong_tables = tuple(
        table_node
        for table_node in measurable_tables
        if table_coverages[table_node.source_node_id] >= _PDF_IMAGE_CELL_MIN_COVERAGE
    )
    competing_tables = tuple(
        table_node
        for table_node in measurable_tables
        if table_coverages[table_node.source_node_id]
        > _PDF_IMAGE_CELL_MAX_COMPETING_COVERAGE
    )
    if (
        len(strong_tables) != 1
        or len(competing_tables) != 1
        or len(measurable_tables) != len(page_tables)
    ):
        return None, (), 0, 0
    table_node = strong_tables[0]
    table = table_node.table_payload
    if table is None or any(cell.native_locator.bbox is None for cell in table.cells):
        return table_node, (), 1, 0

    derivation = image_node.asset_derivation
    if derivation is None:
        coverages = {
            cell.cell_id: _bbox_coverage_for_image_cell_silver(
                locator.bbox,
                cell.native_locator.bbox,
            )
            for cell in table.cells
            if cell.native_locator.bbox is not None
        }
        resolved_cells = tuple(
            sorted(
                (
                    cell
                    for cell in table.cells
                    if coverages[cell.cell_id] > _PDF_IMAGE_CELL_MAX_COMPETING_COVERAGE
                ),
                key=lambda cell: (cell.row, cell.column, cell.cell_id),
            )
        )
        if (
            len(resolved_cells) < 2
            or len(resolved_cells) > _PDF_IMAGE_CELL_MAX_MULTI_CELL_COUNT
            or any(
                coverages[cell.cell_id] < _PDF_IMAGE_CELL_MIN_OPAQUE_MULTI_CELL_COVERAGE
                for cell in resolved_cells
            )
            or not (
                _PDF_IMAGE_CELL_MIN_COVERAGE
                <= sum(coverages[cell.cell_id] for cell in resolved_cells)
                <= 1.0 + _PDF_IMAGE_CELL_MAX_COMPETING_COVERAGE
            )
        ):
            return table_node, (), 1, 0
    elif (
        derivation.kind is SourceAssetDerivationKind.ALPHA_MASK_COMPOSITE
        and derivation.alpha_footprint is not None
    ):
        resolved_cells = _alpha_footprint_cells_for_silver(
            image_bbox=locator.bbox,
            cells=table.cells,
            width_px=derivation.alpha_footprint.width_px,
            height_px=derivation.alpha_footprint.height_px,
            runs=tuple(
                (run.y, run.x_start, run.x_end)
                for run in derivation.alpha_footprint.runs
            ),
            expected_nonzero_sample_count=(
                derivation.alpha_footprint.nonzero_sample_count
            ),
        )
        if resolved_cells is None:
            return table_node, (), 1, 0
    else:
        return table_node, (), 1, 0
    if len(resolved_cells) < 2 or not _cells_form_connected_native_region_for_silver(
        resolved_cells
    ):
        return table_node, (), 1, 0
    return table_node, resolved_cells, 1, len(resolved_cells)


def _alpha_footprint_cells_for_silver(
    *,
    image_bbox: tuple[float, float, float, float],
    cells: tuple[TableCellIR, ...],
    width_px: int,
    height_px: int,
    runs: tuple[tuple[int, int, int], ...],
    expected_nonzero_sample_count: int,
) -> tuple[TableCellIR, ...] | None:
    x0, y0, x1, y1 = image_bbox
    pixel_rectangles: list[tuple[TableCellIR, int, int, int, int]] = []
    for cell in cells:
        cell_bbox = cell.native_locator.bbox
        if cell_bbox is None:
            return None
        cell_x0, cell_y0, cell_x1, cell_y1 = cell_bbox
        pixel_x0, pixel_x1 = _pixel_center_interval_for_silver(
            image_start=x0,
            image_end=x1,
            pixel_count=width_px,
            container_start=cell_x0,
            container_end=cell_x1,
        )
        pixel_y0, pixel_y1 = _pixel_center_interval_for_silver(
            image_start=y0,
            image_end=y1,
            pixel_count=height_px,
            container_start=cell_y0,
            container_end=cell_y1,
        )
        if pixel_x1 > pixel_x0 and pixel_y1 > pixel_y0:
            pixel_rectangles.append((cell, pixel_x0, pixel_x1, pixel_y0, pixel_y1))
    sample_counts: dict[str, int] = {}
    observed_sample_count = 0
    for run_y, run_x0, run_x1 in runs:
        intervals = [
            (
                max(run_x0, cell_x0),
                min(run_x1, cell_x1),
                cell,
            )
            for cell, cell_x0, cell_x1, cell_y0, cell_y1 in pixel_rectangles
            if cell_y0 <= run_y < cell_y1
            and min(run_x1, cell_x1) > max(run_x0, cell_x0)
        ]
        intervals.sort(
            key=lambda item: (
                item[0],
                item[1],
                item[2].row,
                item[2].column,
                item[2].cell_id,
            )
        )
        cursor = run_x0
        for interval_x0, interval_x1, cell in intervals:
            if interval_x0 != cursor:
                return None
            sample_count = interval_x1 - interval_x0
            sample_counts[cell.cell_id] = (
                sample_counts.get(cell.cell_id, 0) + sample_count
            )
            observed_sample_count += sample_count
            cursor = interval_x1
        if cursor != run_x1:
            return None
    if observed_sample_count != expected_nonzero_sample_count:
        return None
    cells_by_id = {cell.cell_id: cell for cell in cells}
    return tuple(
        sorted(
            (cells_by_id[cell_id] for cell_id in sample_counts),
            key=lambda cell: (cell.row, cell.column, cell.cell_id),
        )
    )


def _pixel_center_interval_for_silver(
    *,
    image_start: float,
    image_end: float,
    pixel_count: int,
    container_start: float,
    container_end: float,
) -> tuple[int, int]:
    if pixel_count < 1 or image_end <= image_start or container_end <= container_start:
        return (0, 0)
    scale = (image_end - image_start) / pixel_count
    epsilon = 1e-9
    first = math.ceil((container_start - image_start) / scale - 0.5 - epsilon)
    end = math.ceil((container_end - image_start) / scale - 0.5 - epsilon)
    return (
        max(0, min(pixel_count, first)),
        max(0, min(pixel_count, end)),
    )


def _cells_form_connected_native_region_for_silver(
    cells: tuple[TableCellIR, ...],
) -> bool:
    if (
        len(cells) < 2
        or len(cells) > _PDF_IMAGE_CELL_MAX_MULTI_CELL_COUNT
        or any(cell.native_locator.bbox is None for cell in cells)
    ):
        return False
    covered_slots = {
        (row, column)
        for cell in cells
        for row in range(cell.row, cell.row + cell.row_span)
        for column in range(cell.column, cell.column + cell.column_span)
    }
    if len(covered_slots) > _PDF_IMAGE_CELL_MAX_MULTI_CELL_GRID_SLOTS:
        return False
    reachable = {cells[0].cell_id}
    changed = True
    while changed:
        changed = False
        for left in cells:
            if left.cell_id not in reachable:
                continue
            for right in cells:
                if (
                    right.cell_id not in reachable
                    and _cells_share_native_boundary_for_silver(left, right)
                ):
                    reachable.add(right.cell_id)
                    changed = True
    return len(reachable) == len(cells)


def _cells_share_native_boundary_for_silver(
    left: TableCellIR,
    right: TableCellIR,
) -> bool:
    left_slots = {
        (row, column)
        for row in range(left.row, left.row + left.row_span)
        for column in range(left.column, left.column + left.column_span)
    }
    right_slots = {
        (row, column)
        for row in range(right.row, right.row + right.row_span)
        for column in range(right.column, right.column + right.column_span)
    }
    if not any(
        abs(left_row - right_row) + abs(left_column - right_column) == 1
        for left_row, left_column in left_slots
        for right_row, right_column in right_slots
    ):
        return False
    left_bbox = left.native_locator.bbox
    right_bbox = right.native_locator.bbox
    if left_bbox is None or right_bbox is None:
        return False
    lx0, ly0, lx1, ly1 = left_bbox
    rx0, ry0, rx1, ry1 = right_bbox
    return bool(
        (
            min(abs(lx1 - rx0), abs(rx1 - lx0))
            <= _PDF_IMAGE_CELL_NATIVE_BOUNDARY_TOLERANCE_POINTS
            and min(ly1, ry1) - max(ly0, ry0)
            >= _PDF_IMAGE_CELL_MIN_SHARED_BOUNDARY_POINTS
        )
        or (
            min(abs(ly1 - ry0), abs(ry1 - ly0))
            <= _PDF_IMAGE_CELL_NATIVE_BOUNDARY_TOLERANCE_POINTS
            and min(lx1, rx1) - max(lx0, rx0)
            >= _PDF_IMAGE_CELL_MIN_SHARED_BOUNDARY_POINTS
        )
    )


def _image_cell_assignment_from_source(
    *,
    image_node: SourceNode,
    table_node: SourceNode,
    cells: tuple[TableCellIR, ...],
    content_sha256: str,
) -> PdfShadowSilverImageCellOwnershipAssignment | None:
    image_locator = image_node.native_locator
    table_locator = table_node.native_locator
    table = table_node.table_payload
    if (
        image_locator.page_number is None
        or image_locator.block_index is None
        or image_locator.bbox is None
        or table_locator.table_index is None
        or table_locator.bbox is None
        or table is None
        or not cells
        or any(cell.native_locator.bbox is None for cell in cells)
    ):
        return None
    bindings = tuple(
        PdfShadowSilverImageCellCellBinding(
            row=cell.row,
            column=cell.column,
            span=(cell.row_span, cell.column_span),
            bbox_q100=_bbox_q100(cell.native_locator.bbox),
        )
        for cell in cells
        if cell.native_locator.bbox is not None
    )
    is_single_cell = len(bindings) == 1
    return PdfShadowSilverImageCellOwnershipAssignment(
        image=PdfShadowSilverImageCellImageBinding(
            page_number=image_locator.page_number,
            block_index=image_locator.block_index,
            kind=image_node.kind,
            content_sha256=content_sha256,
            bbox_q100=_bbox_q100(image_locator.bbox),
        ),
        table=PdfShadowSilverImageCellTableBinding(
            index=table_locator.table_index,
            bbox_q100=_bbox_q100(table_locator.bbox),
            shape=(table.row_count, table.column_count),
        ),
        cell=bindings[0] if is_single_cell else None,
        cells=() if is_single_cell else bindings,
        relation_kind="placed_in_cell" if is_single_cell else "placed_over_cells",
    )


def _enumerate_pdf_image_cell_assignments(
    source_ir: SourceIRSnapshot,
    *,
    page_number: int,
) -> tuple[PdfShadowSilverImageCellOwnershipAssignment, ...]:
    assets_by_id = {asset.asset_id: asset for asset in source_ir.assets}
    assignments: list[PdfShadowSilverImageCellOwnershipAssignment] = []
    for image_node in source_ir.nodes:
        if (
            image_node.kind not in {SourceNodeKind.IMAGE, SourceNodeKind.FIGURE}
            or image_node.native_locator.page_number != page_number
        ):
            continue
        table_node, cell, _, _ = _resolve_pdf_image_cell_geometry(
            source_ir,
            image_node=image_node,
        )
        cells = (cell,) if cell is not None else ()
        if not cells:
            (
                table_node,
                cells,
                _,
                _,
            ) = _resolve_pdf_image_multi_cell_geometry(
                source_ir,
                image_node=image_node,
            )
        asset = assets_by_id.get(image_node.asset_ref or "")
        if table_node is None or not cells or asset is None:
            continue
        assignment = _image_cell_assignment_from_source(
            image_node=image_node,
            table_node=table_node,
            cells=cells,
            content_sha256=asset.content_sha256,
        )
        if assignment is not None:
            assignments.append(assignment)
    return tuple(sorted(assignments, key=_image_cell_assignment_sort_key))


def _matching_pdf_image_placements(
    source_ir: SourceIRSnapshot,
    *,
    assignment: PdfShadowSilverImageCellOwnershipAssignment,
    assets_by_id: dict[str, Any],
) -> tuple[SourceNode, ...]:
    expected = assignment.image
    return tuple(
        node
        for node in source_ir.nodes
        if node.kind is expected.kind
        and node.native_locator.format is NativeDocumentFormat.PDF
        and node.native_locator.page_number == expected.page_number
        and node.native_locator.block_index == expected.block_index
        and node.native_locator.bbox is not None
        and _bbox_q100(node.native_locator.bbox) == expected.bbox_q100
        and node.asset_ref in assets_by_id
        and assets_by_id[node.asset_ref].content_sha256 == expected.content_sha256
    )


def _source_assignment_matches_expected(
    *,
    assignment: PdfShadowSilverImageCellOwnershipAssignment,
    image_node: SourceNode,
    table_node: SourceNode,
    cells: tuple[TableCellIR, ...],
) -> bool:
    image_locator = image_node.native_locator
    table_locator = table_node.native_locator
    table = table_node.table_payload
    if not (
        image_locator.page_number == assignment.image.page_number
        and image_locator.block_index == assignment.image.block_index
        and image_locator.bbox is not None
        and _bbox_q100(image_locator.bbox) == assignment.image.bbox_q100
        and table_locator.table_index == assignment.table.index
        and table_locator.bbox is not None
        and _bbox_q100(table_locator.bbox) == assignment.table.bbox_q100
        and table is not None
        and (table.row_count, table.column_count) == assignment.table.shape
        and len(cells) == len(assignment.cell_bindings)
    ):
        return False
    return all(
        cell.row == binding.row
        and cell.column == binding.column
        and (cell.row_span, cell.column_span) == binding.span
        and cell.native_locator.bbox is not None
        and _bbox_q100(cell.native_locator.bbox) == binding.bbox_q100
        for cell, binding in zip(
            cells,
            assignment.cell_bindings,
            strict=True,
        )
    )


def _exact_image_cell_relation_count(
    structure_tree: StructureTreeSnapshot,
    *,
    assignment: PdfShadowSilverImageCellOwnershipAssignment,
    image_node: SourceNode | None,
    table_node: SourceNode | None,
    cells: tuple[TableCellIR, ...],
    independent_structure_nodes: tuple[StructureNode, ...],
) -> int:
    if (
        image_node is None
        or table_node is None
        or not cells
        or not _source_assignment_matches_expected(
            assignment=assignment,
            image_node=image_node,
            table_node=table_node,
            cells=cells,
        )
    ):
        return 0
    image_ref = StructureSourceRef(
        kind=StructureSourceRefKind.SOURCE_NODE,
        source_id=image_node.source_node_id,
    )
    table_ref = StructureSourceRef(
        kind=StructureSourceRefKind.SOURCE_NODE,
        source_id=table_node.source_node_id,
    )
    cell_refs = tuple(
        StructureSourceRef(
            kind=StructureSourceRefKind.TABLE_CELL,
            source_id=cell.cell_id,
        )
        for cell in cells
    )
    image_structure_ids = {
        node.structure_node_id for node in independent_structure_nodes
    }
    table_structure_ids = {
        node.structure_node_id
        for node in structure_tree.nodes
        if node.kind is StructureNodeKind.TABLE and table_ref in node.source_refs
    }
    expected_relation_kind = (
        StructureRelationKind.PLACED_IN_CELL
        if assignment.relation_kind == "placed_in_cell"
        else StructureRelationKind.PLACED_OVER_CELLS
    )
    return sum(
        relation.kind is expected_relation_kind
        and relation.source_structure_node_id in image_structure_ids
        and relation.target_structure_node_id in table_structure_ids
        and relation.source_refs == (image_ref, *cell_refs)
        for relation in structure_tree.relations
    )


def _projected_source_ref_count(
    requirement_view: RequirementViewSnapshot,
    *,
    source_node_id: str,
) -> int:
    source_ref = StructureSourceRef(
        kind=StructureSourceRefKind.SOURCE_NODE,
        source_id=source_node_id,
    )
    return sum(source_ref in node.source_refs for node in requirement_view.nodes)


def _matching_scoped_tables(
    *,
    section: StructureNode,
    page_number: int,
    row_count: int,
    column_count: int,
    source_ir: SourceIRSnapshot,
    structure_tree: StructureTreeSnapshot,
) -> tuple[StructureNode, ...]:
    source_nodes_by_id = {node.source_node_id: node for node in source_ir.nodes}
    scoped_ids = _descendant_ids(section.structure_node_id, structure_tree)
    result: list[StructureNode] = []
    for structure_node in structure_tree.nodes:
        if (
            structure_node.structure_node_id not in scoped_ids
            or structure_node.kind is not StructureNodeKind.TABLE
        ):
            continue
        source_tables = tuple(
            source_nodes_by_id[ref.source_id]
            for ref in structure_node.source_refs
            if ref.kind is StructureSourceRefKind.SOURCE_NODE
            and ref.source_id in source_nodes_by_id
            and source_nodes_by_id[ref.source_id].kind is SourceNodeKind.TABLE
        )
        if len(source_tables) != 1:
            continue
        source_table = source_tables[0]
        table = source_table.table_payload
        if (
            table is not None
            and source_table.native_locator.page_number == page_number
            and table.row_count == row_count
            and table.column_count == column_count
        ):
            result.append(structure_node)
    return tuple(result)


def _source_text_by_ref(
    source_ir: SourceIRSnapshot,
) -> dict[StructureSourceRef, str]:
    result = {
        StructureSourceRef(
            kind=StructureSourceRefKind.SOURCE_NODE,
            source_id=node.source_node_id,
        ): node.raw_text
        for node in source_ir.nodes
    }
    result.update(
        {
            StructureSourceRef(
                kind=StructureSourceRefKind.TABLE_CELL,
                source_id=cell.cell_id,
            ): cell.raw_text
            for node in source_ir.nodes
            if node.table_payload is not None
            for cell in node.table_payload.cells
        }
    )
    result.update(
        {
            StructureSourceRef(
                kind=StructureSourceRefKind.VISUAL_TEXT_OBSERVATION,
                source_id=observation.observation_id,
            ): observation.raw_text
            for snapshot in source_ir.visual_text_snapshots
            for observation in snapshot.observations
        }
    )
    return result


def _structure_node_text(
    node: StructureNode,
    *,
    source_text_by_ref: dict[StructureSourceRef, str],
) -> str:
    return "\n".join(
        text
        for source_ref in node.source_refs
        if (text := source_text_by_ref.get(source_ref, "")).strip()
    )


def _explicit_option_state(raw_text: str) -> StructureOptionState | None:
    match = _SQUARE_OPTION_MARKER.search(raw_text)
    if match is None:
        match = _PAREN_OPTION_MARKER.search(raw_text)
    if match is None:
        match = _BOX_OPTION_MARKER.search(raw_text)
    if match is None:
        return None
    mark = re.sub(r"\s+", "", match.group("mark"))
    if mark in _CHECKED_OPTION_MARKS:
        return StructureOptionState.CHECKED
    if mark in _UNCHECKED_OPTION_MARKS:
        return StructureOptionState.UNCHECKED
    if any(character.isalnum() for character in mark):
        return None
    return StructureOptionState.UNCERTAIN


def _large_image_asset_hashes_by_page(
    source_ir: SourceIRSnapshot,
) -> dict[int, tuple[str, ...]]:
    assets_by_id = {asset.asset_id: asset for asset in source_ir.assets}
    candidates: dict[int, list[tuple[float, str]]] = {}
    for node in source_ir.nodes:
        locator = node.native_locator
        bbox = locator.bbox
        if (
            node.kind not in {SourceNodeKind.IMAGE, SourceNodeKind.FIGURE}
            or locator.page_number is None
            or bbox is None
            or node.asset_ref is None
        ):
            continue
        asset = assets_by_id.get(node.asset_ref)
        if asset is None:
            continue
        width = bbox[2] - bbox[0]
        height = bbox[3] - bbox[1]
        if width <= 0.0 or height <= 0.0:
            continue
        candidates.setdefault(locator.page_number, []).append(
            (width * height, asset.content_sha256)
        )

    result: dict[int, tuple[str, ...]] = {}
    for page_number, records in candidates.items():
        maximum_area = max(area for area, _ in records)
        # A scan and its full-page overlay can share the same geometry.  Keep
        # every near-maximum asset and let the source-bound silver hash select
        # exactly one; smaller logos/figures cannot satisfy the binding.
        result[page_number] = tuple(
            sorted(
                content_sha256
                for area, content_sha256 in records
                if area >= maximum_area * 0.95
            )
        )
    return result


class _SourcePageIndex:
    def __init__(self, source_ir: SourceIRSnapshot) -> None:
        self._source_node_pages = {
            node.source_node_id: node.native_locator.page_number
            for node in source_ir.nodes
            if node.native_locator.page_number is not None
        }
        self._source_cell_pages = {
            cell.cell_id: cell.native_locator.page_number
            for node in source_ir.nodes
            if node.table_payload is not None
            for cell in node.table_payload.cells
            if cell.native_locator.page_number is not None
        }
        self._visual_pages = {
            observation.observation_id: observation.page_number
            for snapshot in source_ir.visual_text_snapshots
            for observation in snapshot.observations
        }

    def pages_for_ref(self, source_ref: StructureSourceRef) -> tuple[int, ...]:
        if source_ref.kind is StructureSourceRefKind.SOURCE_NODE:
            page = self._source_node_pages.get(source_ref.source_id)
        elif source_ref.kind is StructureSourceRefKind.TABLE_CELL:
            page = self._source_cell_pages.get(source_ref.source_id)
        else:
            page = self._visual_pages.get(source_ref.source_id)
        return () if page is None else (page,)

    def pages_for_structure_node(
        self,
        structure_node_id: str,
        tree: StructureTreeSnapshot,
    ) -> tuple[int, ...]:
        node = next(
            (
                item
                for item in tree.nodes
                if item.structure_node_id == structure_node_id
            ),
            None,
        )
        if node is None:
            return ()
        return tuple(
            sorted(
                {
                    page
                    for source_ref in node.source_refs
                    for page in self.pages_for_ref(source_ref)
                }
            )
        )


def _load_json_object_strict(path: Path) -> dict[str, Any]:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise PdfShadowSilverRegistryError(
                    "The PDF shadow silver JSON contains a duplicate key."
                )
            result[key] = value
        return result

    payload = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=reject_duplicate_keys,
    )
    if not isinstance(payload, dict):
        raise PdfShadowSilverRegistryError(
            "The PDF shadow silver JSON root must be an object."
        )
    return payload


def _require_unique(values: Any, label: str) -> None:
    observed = tuple(values)
    if len(observed) != len(set(observed)):
        raise ValueError(f"{label} must be unique")


def evaluate_pdf_shadow_silver_subset(
    *,
    document_id: str,
    source_ir: SourceIRSnapshot,
    registry: PdfShadowSilverRegistry,
    selected_page_numbers: tuple[int, ...],
    provider_id: str,
    provider_version: str,
    expected_evaluated_content_types: tuple[VisualContentType, ...],
) -> PdfShadowSilverSubsetReport:
    """Evaluate only explicitly selected pages beneath the full Silver route.

    This contract intentionally does not run document-level structure,
    semantic, projection, or content-fidelity expectations. Unselected
    authorized pages are recorded as ``NOT_EVALUATED`` and never contribute a
    PASS or FAIL check. A PASS therefore means only that the selected capture
    is source/page/provider bound and structurally valid.
    """

    if not isinstance(source_ir, SourceIRSnapshot):
        raise TypeError("source_ir has an invalid type")
    if not isinstance(registry, PdfShadowSilverRegistry):
        raise TypeError("registry has an invalid type")
    if not isinstance(document_id, str) or not _DOCUMENT_ID_PATTERN.fullmatch(
        document_id
    ):
        raise ValueError("document_id must be a machine-readable key")
    silver = registry.documents.get(document_id)
    if silver is None:
        raise ValueError("document_not_registered")

    selected_pages = _normalize_pdf_shadow_subset_page_numbers(selected_page_numbers)
    page_assets_by_number = {item.page_number: item for item in silver.page_assets}
    authorized_pages = tuple(sorted(page_assets_by_number))
    if not set(selected_pages).issubset(page_assets_by_number):
        raise ValueError("subset_page_not_authorized")
    normalized_provider_id = str(provider_id or "").strip()
    normalized_provider_version = str(provider_version or "").strip()
    if (
        not normalized_provider_id
        or len(normalized_provider_id) > 160
        or not normalized_provider_version
        or len(normalized_provider_version) > 160
    ):
        raise ValueError("subset_provider_identity_invalid")
    if normalized_provider_id not in silver.allowed_visual_provider_ids:
        raise ValueError("visual_text_provider_not_allowed")
    expected_content_types = tuple(expected_evaluated_content_types)
    if (
        not expected_content_types
        or expected_content_types
        != tuple(
            item for item in VISUAL_CONTENT_TYPES if item in expected_content_types
        )
        or len(expected_content_types) != len(set(expected_content_types))
    ):
        raise ValueError("subset_evaluated_content_types_invalid")

    checks: list[PdfShadowSilverCheck] = [
        _check(
            check_id="binding.document_id",
            category="binding",
            expected=document_id,
            observed=document_id,
            matches=True,
            failure_reason="document_not_registered",
        ),
        _check(
            check_id="binding.source_sha256",
            category="binding",
            expected=silver.source_sha256,
            observed=source_ir.source_sha256,
            matches=source_ir.source_sha256 == silver.source_sha256,
            failure_reason="source_hash_mismatch",
        ),
    ]

    snapshots_by_page: dict[int, list[VisualTextSnapshot]] = {}
    for snapshot in source_ir.visual_text_snapshots:
        snapshots_by_page.setdefault(snapshot.page_number, []).append(snapshot)
    observed_pages = tuple(sorted(snapshots_by_page))
    checks.append(
        _check(
            check_id="binding.selected_page_scope",
            category="binding",
            expected=selected_pages,
            observed=observed_pages,
            matches=observed_pages == selected_pages
            and all(len(snapshots_by_page[page]) == 1 for page in observed_pages),
            failure_reason="subset_visual_page_scope_mismatch",
        )
    )
    observed_provider_ids = tuple(
        sorted(
            {
                snapshot.provider_id
                for snapshots in snapshots_by_page.values()
                for snapshot in snapshots
            }
        )
    )
    checks.append(
        _check(
            check_id="binding.visual_provider_policy",
            category="binding",
            expected=tuple(silver.allowed_visual_provider_ids),
            observed=observed_provider_ids,
            matches=(
                observed_provider_ids == (normalized_provider_id,)
                and normalized_provider_id in silver.allowed_visual_provider_ids
            ),
            failure_reason="visual_provider_not_allowed",
        )
    )

    source_assets_by_page = _large_image_asset_hashes_by_page(source_ir)
    provider_version_sha256 = hashlib.sha256(
        normalized_provider_version.encode("utf-8")
    ).hexdigest()
    page_evidence: list[PdfShadowSilverSubsetPageEvidence] = []
    for page_number in selected_pages:
        page_asset = page_assets_by_number[page_number]
        page_candidates = snapshots_by_page.get(page_number, ())
        snapshot = page_candidates[0] if len(page_candidates) == 1 else None
        checks.append(
            _check(
                check_id=f"binding.visual_page:{page_number}",
                category="binding",
                expected=1,
                observed=len(page_candidates),
                matches=len(page_candidates) == 1,
                failure_reason=(
                    "visual_page_missing"
                    if not page_candidates
                    else "visual_page_ambiguous"
                ),
            )
        )

        if page_asset.source_asset_sha256 is not None:
            binding_kind = "source_asset_sha256"
            binding_sha256 = page_asset.source_asset_sha256
            observed_binding_values = source_assets_by_page.get(page_number, ())
            matching_binding_count = observed_binding_values.count(binding_sha256)
            binding_observed: SilverCheckValue = (
                binding_sha256
                if matching_binding_count == 1
                else observed_binding_values or None
            )
            binding_matches = matching_binding_count == 1
            binding_failure_reason = (
                "page_asset_missing"
                if not observed_binding_values
                else "page_asset_ambiguous"
                if matching_binding_count > 1
                else "page_asset_hash_mismatch"
            )
        else:
            binding_kind = "page_image_sha256"
            if page_asset.page_image_sha256 is None:
                raise AssertionError("validated page asset lost its hash binding")
            binding_sha256 = page_asset.page_image_sha256
            binding_observed = (
                snapshot.page_image_sha256 if snapshot is not None else None
            )
            binding_matches = binding_observed == binding_sha256
            binding_failure_reason = (
                "page_image_missing"
                if binding_observed is None
                else "page_image_hash_mismatch"
            )
        checks.append(
            _check(
                check_id=f"binding.page_asset:{page_number}",
                category="binding",
                expected=binding_sha256,
                observed=binding_observed,
                matches=binding_matches,
                failure_reason=binding_failure_reason,
            )
        )

        observed_provider_id = snapshot.provider_id if snapshot is not None else None
        observed_provider_version = (
            snapshot.provider_version if snapshot is not None else None
        )
        observed_provider_version_sha256 = (
            hashlib.sha256(observed_provider_version.encode("utf-8")).hexdigest()
            if observed_provider_version is not None
            else None
        )
        checks.extend(
            (
                _check(
                    check_id=f"binding.provider_id:{page_number}",
                    category="binding",
                    expected=normalized_provider_id,
                    observed=observed_provider_id,
                    matches=observed_provider_id == normalized_provider_id,
                    failure_reason="visual_provider_identity_mismatch",
                ),
                _check(
                    check_id=f"binding.provider_version:{page_number}",
                    category="binding",
                    expected=provider_version_sha256,
                    observed=observed_provider_version_sha256,
                    matches=(
                        observed_provider_version_sha256 == provider_version_sha256
                    ),
                    failure_reason="visual_provider_version_mismatch",
                ),
            )
        )
        observed_content_types = (
            tuple(snapshot.evaluated_content_types) if snapshot is not None else ()
        )
        checks.append(
            _check(
                check_id=f"source_ir.visual_page:{page_number}.content_types",
                category="source_ir",
                expected=expected_content_types,
                observed=observed_content_types,
                matches=observed_content_types == expected_content_types,
                failure_reason="visual_content_type_scope_mismatch",
            )
        )
        table_count = (
            sum(
                observation.content_type == "TABLE"
                for observation in snapshot.observations
            )
            if snapshot is not None
            else 0
        )
        page_evidence.append(
            PdfShadowSilverSubsetPageEvidence(
                page_number=page_number,
                input_rotation_cw_degrees=(page_asset.input_rotation_cw_degrees),
                binding_kind=binding_kind,
                binding_sha256=binding_sha256,
                observed_provider_id=observed_provider_id,
                observed_provider_version=observed_provider_version,
                observed_provider_version_sha256=(observed_provider_version_sha256),
                observed_page_image_sha256=(
                    snapshot.page_image_sha256 if snapshot is not None else None
                ),
                evaluated_content_types=observed_content_types,
                observation_count=(
                    len(snapshot.observations) if snapshot is not None else 0
                ),
                table_count=table_count,
            )
        )

    authorized_page_bindings = tuple(
        PdfShadowSilverSubsetAuthorizedPageBinding(
            page_number=page_number,
            input_rotation_cw_degrees=(
                page_assets_by_number[page_number].input_rotation_cw_degrees
            ),
            binding_kind=(
                "source_asset_sha256"
                if page_assets_by_number[page_number].source_asset_sha256 is not None
                else "page_image_sha256"
            ),
            binding_sha256=(
                page_assets_by_number[page_number].source_asset_sha256
                or page_assets_by_number[page_number].page_image_sha256
                or _validated_page_binding_missing()
            ),
        )
        for page_number in authorized_pages
    )
    passed = sum(check.status is PdfShadowSilverCheckStatus.PASS for check in checks)
    failed = sum(check.status is PdfShadowSilverCheckStatus.FAIL for check in checks)
    return PdfShadowSilverSubsetReport(
        registry_sha256=canonical_pdf_shadow_silver_sha256(registry),
        document_id=document_id,
        source_sha256=source_ir.source_sha256,
        source_ir_provenance=PdfShadowSilverSourceIRProvenance(
            source_ir_id=source_ir.source_ir_id,
            adapter_id=source_ir.adapter_id,
            adapter_version=source_ir.adapter_version,
        ),
        authorized_page_numbers=authorized_pages,
        authorized_page_bindings=authorized_page_bindings,
        selected_page_numbers=selected_pages,
        unevaluated_authorized_page_numbers=tuple(
            page for page in authorized_pages if page not in selected_pages
        ),
        provider_id=normalized_provider_id,
        provider_version=normalized_provider_version,
        provider_version_sha256=provider_version_sha256,
        status=(
            PdfShadowSilverCheckStatus.PASS
            if checks and failed == 0
            else PdfShadowSilverCheckStatus.FAIL
        ),
        passed_check_count=passed,
        failed_check_count=failed,
        blocked_check_count=0,
        page_evidence=tuple(page_evidence),
        checks=tuple(checks),
    )


def _validated_page_binding_missing() -> str:
    raise AssertionError("validated page asset lost its hash binding")


def _normalize_pdf_shadow_subset_page_numbers(
    page_numbers: tuple[int, ...],
) -> tuple[int, ...]:
    if not isinstance(page_numbers, tuple) or not page_numbers:
        raise ValueError("subset_page_scope_empty")
    if any(
        isinstance(page, bool) or not isinstance(page, int) or page < 1
        for page in page_numbers
    ):
        raise ValueError("subset_page_scope_invalid")
    if len(page_numbers) != len(set(page_numbers)):
        raise ValueError("subset_page_scope_duplicate")
    return tuple(sorted(page_numbers))


def _require_machine_report_value(value: SilverCheckValue) -> None:
    if value is None or isinstance(value, (bool, int)):
        return
    values = value if isinstance(value, tuple) else (value,)
    for item in values:
        if isinstance(item, bool) or isinstance(item, int):
            continue
        if not isinstance(item, str) or not _MACHINE_REPORT_VALUE_PATTERN.fullmatch(
            item
        ):
            raise ValueError(
                "silver report values must be machine-readable and text-free"
            )


def _require_input_types(
    *,
    source_ir: SourceIRSnapshot,
    structure_tree: StructureTreeSnapshot,
    semantics: SemanticClassificationSnapshot,
    requirement_view: RequirementViewSnapshot,
    registry: PdfShadowSilverRegistry,
) -> None:
    expected = (
        (source_ir, SourceIRSnapshot, "source_ir"),
        (structure_tree, StructureTreeSnapshot, "structure_tree"),
        (semantics, SemanticClassificationSnapshot, "semantics"),
        (requirement_view, RequirementViewSnapshot, "requirement_view"),
        (registry, PdfShadowSilverRegistry, "registry"),
    )
    for value, value_type, field_name in expected:
        if not isinstance(value, value_type):
            raise TypeError(f"{field_name} has an invalid type")


def _validation_check(
    *,
    check_id: str,
    callback: Any,
    failure_reason: str,
) -> PdfShadowSilverCheck:
    try:
        callback()
    except Exception:
        return _check(
            check_id=check_id,
            category="binding",
            expected=True,
            observed=False,
            matches=False,
            failure_reason=failure_reason,
        )
    return _check(
        check_id=check_id,
        category="binding",
        expected=True,
        observed=True,
        matches=True,
        failure_reason=failure_reason,
    )


def canonical_visual_text_raw_text_bundle_sha256(
    observations: tuple[VisualTextObservation, ...],
) -> str:
    """Hash one ordered OCR page without exposing line text in the registry."""

    _require_visual_text_observation_sequence(observations)
    payload = [
        {
            "observation_index": observation.observation_index,
            "raw_text": observation.raw_text,
        }
        for observation in observations
    ]
    return _canonical_sha256(payload)


def canonical_visual_text_layout_bundle_sha256(
    observations: tuple[VisualTextObservation, ...],
) -> str:
    """Hash ordered, quantized OCR geometry separately from recognized text."""

    _require_visual_text_observation_sequence(observations)
    payload = [
        {
            "observation_index": observation.observation_index,
            "bbox_q100": list(_bbox_q100(observation.bbox)),
        }
        for observation in observations
    ]
    return _canonical_sha256(payload)


def _canonical_sha256(payload: object) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _require_visual_text_observation_sequence(
    observations: tuple[VisualTextObservation, ...],
) -> None:
    if not isinstance(observations, tuple) or any(
        not isinstance(observation, VisualTextObservation)
        for observation in observations
    ):
        raise TypeError("observations must be a tuple of VisualTextObservation")
    indexes = tuple(item.observation_index for item in observations)
    if indexes != tuple(sorted(indexes)) or len(indexes) != len(set(indexes)):
        raise ValueError("visual text observations must keep unique source order")


@dataclass(frozen=True, slots=True)
class _VisualTextTokenRecord:
    token_sha256: str
    center_x_q100: int
    center_y_q100: int
    observation_index: int
    token_index: int


def canonical_visual_text_token_sha256(token: str) -> str:
    """Hash one NFKC/case-folded Unicode alphanumeric token."""

    tokens = _normalized_visual_text_tokens(token)
    if len(tokens) != 1:
        raise ValueError("anchor input must normalize to exactly one token")
    return hashlib.sha256(tokens[0].encode("utf-8")).hexdigest()


def _normalized_visual_text_tokens(raw_text: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", raw_text).casefold()
    tokens: list[str] = []
    current: list[str] = []
    for character in normalized:
        if character.isalnum():
            current.append(character)
        elif current:
            tokens.append("".join(current))
            current.clear()
    if current:
        tokens.append("".join(current))
    return tuple(tokens)


def _visual_text_token_records(
    observations: tuple[VisualTextObservation, ...],
) -> tuple[_VisualTextTokenRecord, ...]:
    result: list[_VisualTextTokenRecord] = []
    for observation in observations:
        center_x_q100 = round((observation.bbox[0] + observation.bbox[2]) * 50.0)
        center_y_q100 = round((observation.bbox[1] + observation.bbox[3]) * 50.0)
        for token_index, token in enumerate(
            _normalized_visual_text_tokens(observation.raw_text)
        ):
            result.append(
                _VisualTextTokenRecord(
                    token_sha256=hashlib.sha256(token.encode("utf-8")).hexdigest(),
                    center_x_q100=center_x_q100,
                    center_y_q100=center_y_q100,
                    observation_index=observation.observation_index,
                    token_index=token_index,
                )
            )
    return tuple(result)


def _visual_text_anchor_fidelity_profile_checks(
    *,
    expectation: PdfShadowSilverVisualTextAnchorFidelityExpectation,
    observations: tuple[VisualTextObservation, ...] | None,
    profile_matches: bool,
    prefix: str,
) -> tuple[PdfShadowSilverCheck, ...]:
    checks: list[PdfShadowSilverCheck] = []
    observed_count = len(observations) if observations is not None else None
    observed_character_count = (
        sum(len(item.raw_text) for item in observations)
        if observations is not None
        else None
    )
    range_specs = (
        (
            "observation_count_min",
            expectation.observation_count_min,
            observed_count,
            bool(
                observed_count is not None
                and observed_count >= expectation.observation_count_min
            ),
            "visual_text_observation_count_below_minimum",
        ),
        (
            "observation_count_max",
            expectation.observation_count_max,
            observed_count,
            bool(
                observed_count is not None
                and observed_count <= expectation.observation_count_max
            ),
            "visual_text_observation_count_above_maximum",
        ),
        (
            "character_count_min",
            expectation.character_count_min,
            observed_character_count,
            bool(
                observed_character_count is not None
                and observed_character_count >= expectation.character_count_min
            ),
            "visual_text_character_count_below_minimum",
        ),
        (
            "character_count_max",
            expectation.character_count_max,
            observed_character_count,
            bool(
                observed_character_count is not None
                and observed_character_count <= expectation.character_count_max
            ),
            "visual_text_character_count_above_maximum",
        ),
    )
    for suffix, expected, observed, range_matches, reason in range_specs:
        checks.append(
            _check(
                check_id=f"{prefix}.{suffix}",
                category="source_ir",
                expected=expected,
                observed=observed,
                matches=profile_matches and range_matches,
                failure_reason=(
                    reason
                    if profile_matches
                    else "visual_text_provider_profile_mismatch"
                ),
            )
        )

    if expectation.ordered_raw_text_bundle_sha256 is not None:
        observed_raw_text_sha256 = (
            canonical_visual_text_raw_text_bundle_sha256(observations)
            if observations is not None
            else None
        )
        checks.append(
            _check(
                check_id=f"{prefix}.ordered_raw_text_bundle_sha256",
                category="source_ir",
                expected=expectation.ordered_raw_text_bundle_sha256,
                observed=observed_raw_text_sha256,
                matches=(
                    profile_matches
                    and observed_raw_text_sha256
                    == expectation.ordered_raw_text_bundle_sha256
                ),
                failure_reason=(
                    "visual_text_raw_text_bundle_mismatch"
                    if profile_matches
                    else "visual_text_provider_profile_mismatch"
                ),
            )
        )

    records = _visual_text_token_records(observations or ())
    expected_anchor_hashes = frozenset(
        item.token_sha256 for item in expectation.ordered_anchors
    )
    axis_is_x = expectation.anchor_order_axis == "x"
    observed_anchor_records = tuple(
        sorted(
            (
                record
                for record in records
                if record.token_sha256 in expected_anchor_hashes
            ),
            key=lambda item: (
                item.center_x_q100 if axis_is_x else item.center_y_q100,
                item.center_y_q100 if axis_is_x else item.center_x_q100,
                item.observation_index,
                item.token_index,
                item.token_sha256,
            ),
        )
    )
    expected_sequence = tuple(item.token_sha256 for item in expectation.ordered_anchors)
    observed_sequence = tuple(item.token_sha256 for item in observed_anchor_records)
    checks.append(
        _check(
            check_id=f"{prefix}.ordered_anchor_sequence",
            category="source_ir",
            expected=expected_sequence,
            observed=observed_sequence,
            matches=profile_matches and observed_sequence == expected_sequence,
            failure_reason=(
                "visual_text_anchor_sequence_mismatch"
                if profile_matches
                else "visual_text_provider_profile_mismatch"
            ),
        )
    )

    records_by_hash: dict[str, list[_VisualTextTokenRecord]] = {}
    for record in records:
        records_by_hash.setdefault(record.token_sha256, []).append(record)
    for anchor_index, anchor in enumerate(expectation.ordered_anchors):
        matches = records_by_hash.get(anchor.token_sha256, ())
        anchor_prefix = f"{prefix}.anchor:{anchor_index}"
        checks.append(
            _check(
                check_id=f"{anchor_prefix}.occurrences",
                category="source_ir",
                expected=1,
                observed=len(matches),
                matches=profile_matches and len(matches) == 1,
                failure_reason=(
                    "visual_text_anchor_occurrence_mismatch"
                    if profile_matches
                    else "visual_text_provider_profile_mismatch"
                ),
            )
        )
        observed_axis_center = (
            (matches[0].center_x_q100 if axis_is_x else matches[0].center_y_q100)
            if len(matches) == 1
            else None
        )
        center_matches = bool(
            observed_axis_center is not None
            and abs(observed_axis_center - anchor.axis_center_q100)
            <= anchor.max_axis_delta_q100
        )
        checks.append(
            _check(
                check_id=f"{anchor_prefix}.axis_center",
                category="source_ir",
                expected=anchor.axis_center_q100,
                observed=observed_axis_center,
                matches=profile_matches and center_matches,
                failure_reason=(
                    (
                        "visual_text_anchor_axis_center_mismatch"
                        if len(matches) == 1
                        else "visual_text_anchor_occurrence_mismatch"
                    )
                    if profile_matches
                    else "visual_text_provider_profile_mismatch"
                ),
            )
        )

    for region in expectation.reviewed_token_regions:
        region_records = tuple(
            record
            for record in records
            if _point_in_bbox_q100(
                (record.center_x_q100, record.center_y_q100),
                region.bbox_q100,
            )
        )
        region_prefix = f"{prefix}.region:{region.region_id}"
        for token_index, required_token in enumerate(region.required_tokens):
            observed_occurrences = sum(
                record.token_sha256 == required_token.token_sha256
                for record in region_records
            )
            checks.append(
                _check(
                    check_id=f"{region_prefix}.token:{token_index}.occurrences",
                    category="source_ir",
                    expected=required_token.exact_occurrences,
                    observed=observed_occurrences,
                    matches=(
                        profile_matches
                        and observed_occurrences == required_token.exact_occurrences
                    ),
                    failure_reason=(
                        "visual_text_region_token_occurrence_mismatch"
                        if profile_matches
                        else "visual_text_provider_profile_mismatch"
                    ),
                )
            )
    return tuple(checks)


def _point_in_bbox_q100(
    point: tuple[int, int],
    bbox: tuple[int, int, int, int],
) -> bool:
    return bool(bbox[0] <= point[0] <= bbox[2] and bbox[1] <= point[1] <= bbox[3])


def _visual_text_fidelity_checks(
    *,
    silver: PdfShadowSilverDocument,
    source_ir: SourceIRSnapshot,
    requirement_view: RequirementViewSnapshot,
) -> tuple[PdfShadowSilverCheck, ...]:
    snapshots_by_page: dict[int, list[VisualTextSnapshot]] = {}
    observation_page_by_id: dict[str, int] = {}
    for snapshot in source_ir.visual_text_snapshots:
        snapshots_by_page.setdefault(snapshot.page_number, []).append(snapshot)
        for observation in snapshot.observations:
            observation_page_by_id[observation.observation_id] = snapshot.page_number

    checks: list[PdfShadowSilverCheck] = []
    for expectation in sorted(
        silver.visual_text_fidelity_expectations,
        key=lambda item: (
            item.page_number,
            item.provider_id,
            item.provider_version_sha256,
        ),
    ):
        prefix = (
            f"source_ir.visual_text:{expectation.page_number}:{expectation.provider_id}"
        )
        page_candidates = snapshots_by_page.get(expectation.page_number, ())
        snapshot = page_candidates[0] if len(page_candidates) == 1 else None
        observed_provider_id = snapshot.provider_id if snapshot is not None else None
        observed_provider_version_sha256 = (
            hashlib.sha256(snapshot.provider_version.encode("utf-8")).hexdigest()
            if snapshot is not None
            else None
        )
        profile_matches = bool(
            snapshot is not None
            and observed_provider_id == expectation.provider_id
            and observed_provider_version_sha256 == expectation.provider_version_sha256
        )
        checks.append(
            _check(
                check_id=f"{prefix}.provider_profile",
                category="source_ir",
                expected=expectation.provider_version_sha256,
                observed=observed_provider_version_sha256,
                matches=profile_matches,
                failure_reason="visual_text_provider_profile_mismatch",
            )
        )

        if isinstance(
            expectation,
            PdfShadowSilverVisualTextAnchorFidelityExpectation,
        ):
            observations = snapshot.observations if snapshot is not None else None
            checks.extend(
                _visual_text_anchor_fidelity_profile_checks(
                    expectation=expectation,
                    observations=observations,
                    profile_matches=profile_matches,
                    prefix=prefix,
                )
            )
            projected_on_page = sum(
                1
                for node in requirement_view.nodes
                if expectation.page_number
                in {
                    observation_page_by_id.get(ref.source_id)
                    for ref in node.source_refs
                    if ref.kind is StructureSourceRefKind.VISUAL_TEXT_OBSERVATION
                }
            )
            checks.append(
                _check(
                    check_id=f"{prefix}.projected_requirement_count",
                    category="projection",
                    expected=expectation.projected_requirement_count,
                    observed=projected_on_page,
                    matches=(
                        profile_matches
                        and projected_on_page == expectation.projected_requirement_count
                    ),
                    failure_reason=(
                        "visual_text_projection_count_mismatch"
                        if profile_matches
                        else "visual_text_provider_profile_mismatch"
                    ),
                )
            )
            continue

        observations = snapshot.observations if snapshot is not None else None
        observed_count = len(observations) if observations is not None else None
        observed_character_count = (
            sum(len(item.raw_text) for item in observations)
            if observations is not None
            else None
        )
        observed_raw_text_sha256 = (
            canonical_visual_text_raw_text_bundle_sha256(observations)
            if observations is not None
            else None
        )
        observed_layout_sha256 = (
            canonical_visual_text_layout_bundle_sha256(observations)
            if observations is not None
            else None
        )
        for suffix, expected, observed, reason in (
            (
                "observation_count",
                expectation.observation_count,
                observed_count,
                "visual_text_observation_count_mismatch",
            ),
            (
                "character_count",
                expectation.character_count,
                observed_character_count,
                "visual_text_character_count_mismatch",
            ),
            (
                "raw_text_bundle_sha256",
                expectation.raw_text_bundle_sha256,
                observed_raw_text_sha256,
                "visual_text_raw_text_bundle_mismatch",
            ),
            (
                "layout_bundle_sha256",
                expectation.layout_bundle_sha256,
                observed_layout_sha256,
                "visual_text_layout_bundle_mismatch",
            ),
        ):
            checks.append(
                _check(
                    check_id=f"{prefix}.{suffix}",
                    category="source_ir",
                    expected=expected,
                    observed=observed,
                    matches=profile_matches and observed == expected,
                    failure_reason=(
                        reason
                        if profile_matches
                        else "visual_text_provider_profile_mismatch"
                    ),
                )
            )

        for region in expectation.reviewed_regions:
            region_prefix = f"{prefix}.region:{region.region_id}"
            region_observations = (
                tuple(
                    observation
                    for observation in observations
                    if _bbox_q100_intersects(
                        _bbox_q100(observation.bbox),
                        region.bbox_q100,
                    )
                )
                if observations is not None
                else None
            )
            region_count = (
                len(region_observations) if region_observations is not None else None
            )
            region_character_count = (
                sum(len(item.raw_text) for item in region_observations)
                if region_observations is not None
                else None
            )
            region_text_sha256 = (
                canonical_visual_text_raw_text_bundle_sha256(region_observations)
                if region_observations
                else None
            )
            region_layout_sha256 = (
                canonical_visual_text_layout_bundle_sha256(region_observations)
                if region_observations
                else None
            )
            for suffix, expected, observed, reason in (
                (
                    "observation_count",
                    region.observation_count,
                    region_count,
                    "visual_text_region_observation_count_mismatch",
                ),
                (
                    "character_count",
                    region.character_count,
                    region_character_count,
                    "visual_text_region_character_count_mismatch",
                ),
                (
                    "raw_text_bundle_sha256",
                    region.raw_text_bundle_sha256,
                    region_text_sha256,
                    "visual_text_region_raw_text_bundle_mismatch",
                ),
                (
                    "layout_bundle_sha256",
                    region.layout_bundle_sha256,
                    region_layout_sha256,
                    "visual_text_region_layout_bundle_mismatch",
                ),
            ):
                checks.append(
                    _check(
                        check_id=f"{region_prefix}.{suffix}",
                        category="source_ir",
                        expected=expected,
                        observed=observed,
                        matches=profile_matches and observed == expected,
                        failure_reason=(
                            reason
                            if profile_matches
                            else "visual_text_provider_profile_mismatch"
                        ),
                    )
                )

        projected_on_page = sum(
            1
            for node in requirement_view.nodes
            if expectation.page_number
            in {
                observation_page_by_id.get(ref.source_id)
                for ref in node.source_refs
                if ref.kind is StructureSourceRefKind.VISUAL_TEXT_OBSERVATION
            }
        )
        checks.append(
            _check(
                check_id=f"{prefix}.projected_requirement_count",
                category="projection",
                expected=expectation.projected_requirement_count,
                observed=projected_on_page,
                matches=(
                    profile_matches
                    and projected_on_page == expectation.projected_requirement_count
                ),
                failure_reason=(
                    "visual_text_projection_count_mismatch"
                    if profile_matches
                    else "visual_text_provider_profile_mismatch"
                ),
            )
        )
    return tuple(checks)


def _document_projection_policy_checks(
    *,
    silver: PdfShadowSilverDocument,
    requirement_view: RequirementViewSnapshot,
) -> tuple[PdfShadowSilverCheck, ...]:
    if (
        silver.projection_policy
        is not RequirementProjectionPolicy.NON_REQUIREMENT_ARTIFACT
    ):
        return ()
    decision_count = sum(
        node.decision_mode
        in {RequirementDecisionMode.DIRECT, RequirementDecisionMode.ROLLUP}
        for node in requirement_view.nodes
    )
    return (
        _check(
            check_id="projection.non_requirement_artifact.node_count",
            category="projection",
            expected=0,
            observed=len(requirement_view.nodes),
            matches=not requirement_view.nodes,
            failure_reason="non_requirement_artifact_projection_not_empty",
        ),
        _check(
            check_id="projection.non_requirement_artifact.decision_count",
            category="projection",
            expected=0,
            observed=decision_count,
            matches=decision_count == 0,
            failure_reason="non_requirement_artifact_decision_count_mismatch",
        ),
        _check(
            check_id="projection.non_requirement_artifact.requirement_set_bound",
            category="projection",
            expected=False,
            observed=requirement_view.requirement_set_id is not None,
            matches=requirement_view.requirement_set_id is None,
            failure_reason="non_requirement_artifact_requirement_set_forbidden",
        ),
    )


def _check(
    *,
    check_id: str,
    category: PdfShadowSilverCheckCategory,
    expected: SilverCheckValue,
    observed: SilverCheckValue,
    matches: bool,
    failure_reason: str,
) -> PdfShadowSilverCheck:
    return PdfShadowSilverCheck(
        check_id=check_id,
        category=category,
        status=(
            PdfShadowSilverCheckStatus.PASS
            if matches
            else PdfShadowSilverCheckStatus.FAIL
        ),
        reason_code="matched" if matches else failure_reason,
        expected=expected,
        observed=observed,
    )


def _failed_resolution_check(
    *,
    check_id: str,
    category: Literal[
        "source_ir",
        "structure",
        "finding",
        "semantic",
        "projection",
    ],
    expected: SilverCheckValue,
    reason: str,
    candidate_count: int,
) -> PdfShadowSilverCheck:
    return _check(
        check_id=check_id,
        category=category,
        expected=expected,
        observed=candidate_count,
        matches=False,
        failure_reason=reason,
    )


def _binding_blocked_expectation_checks(
    silver: PdfShadowSilverDocument,
) -> tuple[PdfShadowSilverCheck, ...]:
    specs: list[
        tuple[
            str,
            Literal[
                "source_ir",
                "structure",
                "finding",
                "semantic",
                "projection",
            ],
            SilverCheckValue,
        ]
    ] = []
    specs.extend(
        (
            f"structure.outline_chain:{item.target_label}",
            "structure",
            item.ancestor_labels,
        )
        for item in silver.outline_chains
    )
    for item in silver.continuations:
        prefix = (
            f"structure.continuation:{item.section_label}:"
            f"{item.from_page}-{item.to_page}"
        )
        specs.append((f"{prefix}.relation_count", "structure", 1))
        if item.numbered_child_count is not None:
            specs.append(
                (
                    f"{prefix}.numbered_child_count",
                    "structure",
                    item.numbered_child_count,
                )
            )
        if item.pseudo_primary_requirement_count is not None:
            specs.append(
                (
                    f"{prefix}.pseudo_primary_requirement_count",
                    "structure",
                    item.pseudo_primary_requirement_count,
                )
            )
    specs.extend(
        (
            f"structure.numbered_children:{item.section_label}",
            "structure",
            item.ordinals,
        )
        for item in silver.numbered_children
    )
    for item in silver.semantic_expectations:
        expected_subtype = (
            item.requirement_subtype.value
            if item.requirement_subtype is not None
            else None
        )
        specs.extend(
            (
                (
                    f"semantic:{item.section_label}.class",
                    "semantic",
                    item.semantic_class.value,
                ),
                (
                    f"semantic:{item.section_label}.subtype",
                    "semantic",
                    expected_subtype,
                ),
            )
        )
    for item in silver.projection_expectations:
        prefix = f"projection:{item.section_label}"
        specs.extend(
            (
                (f"{prefix}.primary_count", "projection", item.primary_count),
                (
                    f"{prefix}.content_child_count",
                    "projection",
                    item.content_child_count,
                ),
                (
                    f"{prefix}.top_level_requirement_count",
                    "projection",
                    item.top_level_requirement_count,
                ),
                (
                    f"{prefix}.primary_decision_mode",
                    "projection",
                    item.primary_decision_mode.value,
                ),
            )
        )
    for item in silver.finding_expectations:
        scope_key = item.section_label or "document"
        specs.append(
            (
                f"finding:{item.code}:{scope_key}.count",
                "finding",
                item.count,
            )
        )
    for item in silver.projection_scope_expectations:
        role_key = item.role.value.lower() if item.role is not None else "any"
        kind_key = item.target_kind.value if item.target_kind is not None else "any"
        specs.append(
            (
                (f"projection.scope:{item.section_label}:{role_key}:{kind_key}.count"),
                "projection",
                item.count,
            )
        )
    for item in silver.diagnostic_expectations:
        page_key = item.page_number or "document"
        specs.append(
            (
                f"source_ir.diagnostic:{item.code}:{page_key}.count",
                "source_ir",
                item.count,
            )
        )
    for item in silver.native_block_disposition_expectations:
        prefix = (
            f"source_ir.native_block_disposition:{item.disposition}:"
            f"{item.content_sha256}"
        )
        locator_keys = tuple(
            f"{locator.page_number}:{locator.block_index}" for locator in item.locators
        )
        specs.extend(
            (
                (
                    f"{prefix}.source_status",
                    "source_ir",
                    item.expected_source_status,
                ),
                (
                    f"{prefix}.source_node_count",
                    "source_ir",
                    item.source_node_count,
                ),
                (f"{prefix}.asset_count", "source_ir", item.asset_count),
                (
                    f"{prefix}.diagnostic_count",
                    "source_ir",
                    len(item.locators),
                ),
                (
                    f"{prefix}.diagnostic_locator_keys",
                    "source_ir",
                    locator_keys,
                ),
                (
                    f"{prefix}.diagnostic_grounding",
                    "source_ir",
                    True,
                ),
                (
                    f"{prefix}.content_sha256",
                    "source_ir",
                    item.content_sha256,
                ),
                (
                    f"{prefix}.forbidden_diagnostic_count",
                    "source_ir",
                    0,
                ),
            )
        )
    specs.extend(
        (
            (
                "source_ir.native_block_disposition_coverage:"
                f"{diagnostic_code}.exact_bindings",
                "source_ir",
                expected_bindings,
            )
            for diagnostic_code, expected_bindings in (
                _native_block_disposition_coverage_expectations(silver)
            )
        )
    )
    for item in silver.structure_scope_expectations:
        direct_key = "direct" if item.direct_only else "descendant"
        specs.append(
            (
                (
                    f"structure.scope:{item.section_label}:"
                    f"{direct_key}:{item.target_kind.value}.count"
                ),
                "structure",
                item.count,
            )
        )
    for item in silver.table_expectations:
        specs.append(
            (
                (
                    f"structure.table:{item.section_label}:"
                    f"{item.page_number}:{item.row_count}x"
                    f"{item.column_count}.count"
                ),
                "structure",
                item.count,
            )
        )
    for item in silver.table_note_expectations:
        prefix = (
            f"structure.table_note:{item.section_label}:"
            f"{item.page_number}:{item.row_count}x{item.column_count}"
        )
        specs.extend(
            (
                (f"{prefix}.table_count", "structure", 1),
                (
                    f"{prefix}.applies_to_rows",
                    "structure",
                    item.applies_to_row_indices,
                ),
            )
        )
    for item in silver.revision_history_expectations:
        prefix = (
            f"revision_history:{item.page_number}:{item.table_index}:"
            f"{item.row_count}x{item.column_count}"
        )
        expected_classes = tuple(
            f"{row_index}:{SemanticClass.VERSION_METADATA.value}"
            for row_index in item.revision_row_indices
        )
        specs.extend(
            (
                (f"{prefix}.source_table_count", "source_ir", 1),
                (
                    f"{prefix}.structure_row_indices",
                    "structure",
                    item.revision_row_indices,
                ),
                (f"{prefix}.semantic_classes", "semantic", expected_classes),
                (
                    f"{prefix}.projected_requirement_count",
                    "projection",
                    item.projected_requirement_count,
                ),
            )
        )
    for item in silver.image_mask_composite_expectations:
        prefix = f"source_ir.image_mask_composite:{item.page_number}"
        expected_count = len(item.block_indices)
        expected_unique_asset_count = len(set(item.composite_content_sha256))
        expected_media_types = (item.media_type,) * expected_count
        expected_derivation_kinds = (
            SourceAssetDerivationKind.ALPHA_MASK_COMPOSITE.value,
        ) * expected_count
        specs.extend(
            (
                (
                    f"{prefix}.source_status",
                    "source_ir",
                    item.expected_source_status,
                ),
                (f"{prefix}.placement_count", "source_ir", expected_count),
                (
                    f"{prefix}.unique_asset_count",
                    "source_ir",
                    expected_unique_asset_count,
                ),
                (f"{prefix}.block_indices", "source_ir", item.block_indices),
                (
                    f"{prefix}.composite_content_sha256",
                    "source_ir",
                    item.composite_content_sha256,
                ),
                (f"{prefix}.media_types", "source_ir", expected_media_types),
                (
                    f"{prefix}.derivation_kinds",
                    "source_ir",
                    expected_derivation_kinds,
                ),
                (
                    f"{prefix}.base_content_sha256",
                    "source_ir",
                    item.base_content_sha256,
                ),
                (
                    f"{prefix}.mask_content_sha256",
                    "source_ir",
                    item.mask_content_sha256,
                ),
                (
                    f"{prefix}.materialized_diagnostic_count",
                    "source_ir",
                    expected_count,
                ),
                (
                    f"{prefix}.materialized_diagnostic_grounding",
                    "source_ir",
                    True,
                ),
                (
                    f"{prefix}.failure_diagnostic_count",
                    "source_ir",
                    0,
                ),
                (
                    f"{prefix}.projected_requirement_count",
                    "projection",
                    item.projected_requirement_count,
                ),
            )
        )
    for item in silver.image_cell_ownership_expectations:
        page_prefix = f"image_cell_ownership:{item.page_number}"
        expected_assignment_fingerprints = tuple(
            sorted(
                _image_cell_assignment_sha256(assignment)
                for assignment in item.assignments
            )
        )
        specs.append(
            (
                f"{page_prefix}.exact_assignments",
                "source_ir",
                expected_assignment_fingerprints,
            )
        )
        for assignment_index, _ in enumerate(item.assignments):
            prefix = f"{page_prefix}:{assignment_index}"
            specs.extend(
                (
                    (f"{prefix}.placement_count", "source_ir", 1),
                    (f"{prefix}.unique_table_count", "source_ir", 1),
                    (f"{prefix}.unique_cell_count", "source_ir", 1),
                    (
                        f"{prefix}.independent_structure_count",
                        "structure",
                        1,
                    ),
                    (f"{prefix}.relation_count", "structure", 1),
                    (f"{prefix}.projected_requirement_count", "projection", 0),
                )
            )
    for item in silver.figure_expectations:
        prefix = (
            f"figure:{item.section_label}:{item.page_number}:"
            f"{item.source_node_kind.value}"
        )
        specs.extend(
            (
                (f"{prefix}.asset_count", "source_ir", item.asset_count),
                (
                    f"{prefix}.placement_count",
                    "source_ir",
                    item.placement_count,
                ),
                (
                    f"{prefix}.structure_count",
                    "structure",
                    item.structure_count,
                ),
                (
                    f"{prefix}.reference_relation_count",
                    "structure",
                    item.reference_relation_count,
                ),
                (
                    f"{prefix}.projected_requirement_count",
                    "projection",
                    item.projected_requirement_count,
                ),
                (
                    f"{prefix}.projection_role",
                    "projection",
                    item.projection_role.value,
                ),
                (
                    f"{prefix}.decision_mode",
                    "projection",
                    item.decision_mode.value,
                ),
            )
        )
    for item in silver.option_state_expectations:
        prefix = f"option_state:{item.section_label}:{item.page_number}"
        expected_states = tuple(state.value for state in item.states)
        specs.extend(
            (
                (f"{prefix}.source_count", "source_ir", len(item.states)),
                (f"{prefix}.source_states", "source_ir", expected_states),
                (f"{prefix}.relation_count", "structure", len(item.states)),
                (f"{prefix}.relation_states", "structure", expected_states),
                (
                    f"{prefix}.projected_checked_count",
                    "projection",
                    item.projected_checked_count,
                ),
                (
                    f"{prefix}.projected_unchecked_count",
                    "projection",
                    item.projected_unchecked_count,
                ),
                (
                    f"{prefix}.projected_uncertain_count",
                    "projection",
                    item.projected_uncertain_count,
                ),
            )
        )
    return tuple(
        PdfShadowSilverCheck(
            check_id=check_id,
            category=category,
            status=PdfShadowSilverCheckStatus.BLOCKED,
            reason_code="silver_binding_failed",
            expected=expected,
            observed=None,
        )
        for check_id, category, expected in specs
    )


def _section_index(
    tree: StructureTreeSnapshot,
) -> dict[str, tuple[StructureNode, ...]]:
    grouped: dict[str, list[StructureNode]] = {}
    for node in tree.nodes:
        if node.kind is StructureNodeKind.SECTION and node.explicit_label is not None:
            grouped.setdefault(node.explicit_label, []).append(node)
    return {
        label: tuple(sorted(nodes, key=lambda node: node.source_order))
        for label, nodes in grouped.items()
    }


def _unique_section(
    index: dict[str, tuple[StructureNode, ...]],
    label: str,
) -> tuple[StructureNode | None, str]:
    candidates = index.get(label, ())
    if not candidates:
        return None, "section_not_found"
    if len(candidates) > 1:
        return None, "section_ambiguous"
    return candidates[0], "matched"


def _ancestor_labels(
    target: StructureNode,
    tree: StructureTreeSnapshot,
) -> tuple[str, ...]:
    nodes_by_id = {node.structure_node_id: node for node in tree.nodes}
    labels: list[str] = []
    parent_id = target.parent_structure_node_id
    while parent_id is not None:
        parent = nodes_by_id[parent_id]
        if (
            parent.kind is StructureNodeKind.SECTION
            and parent.explicit_label is not None
        ):
            labels.append(parent.explicit_label)
        parent_id = parent.parent_structure_node_id
    labels.reverse()
    return tuple(labels)


def _descendant_ids(
    target_id: str,
    tree: StructureTreeSnapshot,
) -> set[str]:
    children: dict[str, list[str]] = {}
    for node in tree.nodes:
        if node.parent_structure_node_id is not None:
            children.setdefault(node.parent_structure_node_id, []).append(
                node.structure_node_id
            )
    descendants = {target_id}
    pending = [target_id]
    while pending:
        current = pending.pop()
        for child in children.get(current, ()):
            if child not in descendants:
                descendants.add(child)
                pending.append(child)
    return descendants


def _direct_numbered_children(
    target: StructureNode,
    tree: StructureTreeSnapshot,
) -> tuple[StructureNode, ...]:
    return tuple(
        sorted(
            (
                node
                for node in tree.nodes
                if node.parent_structure_node_id == target.structure_node_id
                and node.kind is StructureNodeKind.CLAUSE
                and node.list_ordinal is not None
            ),
            key=lambda node: (node.source_order, node.structure_node_id),
        )
    )


def _content_descendant_count(
    primary: RequirementViewNode,
    view: RequirementViewSnapshot,
) -> int:
    children: dict[str, list[RequirementViewNode]] = {}
    for node in view.nodes:
        if node.parent_requirement_view_node_id is not None:
            children.setdefault(node.parent_requirement_view_node_id, []).append(node)
    count = 0
    pending = [primary.requirement_view_node_id]
    visited: set[str] = set()
    while pending:
        parent_id = pending.pop()
        if parent_id in visited:
            continue
        visited.add(parent_id)
        for child in children.get(parent_id, ()):
            if child.role is RequirementProjectionRole.CONTENT:
                count += 1
            pending.append(child.requirement_view_node_id)
    return count


def _report(
    *,
    registry: PdfShadowSilverRegistry,
    document_id: str,
    source_ir: SourceIRSnapshot,
    checks: list[PdfShadowSilverCheck],
) -> PdfShadowSilverReport:
    passed = sum(check.status is PdfShadowSilverCheckStatus.PASS for check in checks)
    failed = sum(check.status is PdfShadowSilverCheckStatus.FAIL for check in checks)
    blocked = sum(
        check.status is PdfShadowSilverCheckStatus.BLOCKED for check in checks
    )
    return PdfShadowSilverReport(
        registry_sha256=canonical_pdf_shadow_silver_sha256(registry),
        document_id=document_id,
        source_sha256=source_ir.source_sha256,
        source_ir_provenance=PdfShadowSilverSourceIRProvenance(
            source_ir_id=source_ir.source_ir_id,
            adapter_id=source_ir.adapter_id,
            adapter_version=source_ir.adapter_version,
        ),
        status=(
            PdfShadowSilverCheckStatus.PASS
            if checks and failed == 0 and blocked == 0
            else PdfShadowSilverCheckStatus.FAIL
        ),
        passed_check_count=passed,
        failed_check_count=failed,
        blocked_check_count=blocked,
        checks=tuple(checks),
    )


__all__ = [
    "DEFAULT_PDF_SHADOW_SILVER_PATH",
    "DEFAULT_PDF_SHADOW_SILVER_SCHEMA_PATH",
    "PDF_SHADOW_SILVER_EVALUATION_SCHEMA_VERSION",
    "PDF_SHADOW_SILVER_SCHEMA_VERSION",
    "PDF_SHADOW_SILVER_SUBSET_EVALUATION_SCHEMA_VERSION",
    "PdfShadowSilverCheck",
    "PdfShadowSilverCheckCategory",
    "PdfShadowSilverCheckStatus",
    "PdfShadowSilverImageCellCellBinding",
    "PdfShadowSilverImageCellImageBinding",
    "PdfShadowSilverImageCellOwnershipAssignment",
    "PdfShadowSilverImageCellOwnershipExpectation",
    "PdfShadowSilverImageCellTableBinding",
    "PdfShadowSilverImageMaskCompositeExpectation",
    "PdfShadowSilverOptionStateExpectation",
    "PdfShadowSilverRevisionHistoryExpectation",
    "PdfShadowSilverRegistry",
    "PdfShadowSilverRegistryError",
    "PdfShadowSilverReport",
    "PdfShadowSilverSourceIRProvenance",
    "PdfShadowSilverSubsetAuthorizedPageBinding",
    "PdfShadowSilverSubsetPageEvidence",
    "PdfShadowSilverSubsetReport",
    "PdfShadowSilverVisualTextAnchor",
    "PdfShadowSilverVisualTextAnchorFidelityExpectation",
    "PdfShadowSilverVisualTextFidelityExpectation",
    "PdfShadowSilverVisualTextRequiredToken",
    "PdfShadowSilverVisualTextRegionExpectation",
    "PdfShadowSilverVisualTextTokenRegion",
    "canonical_visual_text_token_sha256",
    "canonical_visual_text_layout_bundle_sha256",
    "canonical_visual_text_raw_text_bundle_sha256",
    "canonical_pdf_shadow_silver_sha256",
    "evaluate_pdf_shadow_silver",
    "evaluate_pdf_shadow_silver_subset",
    "load_pdf_shadow_silver",
]

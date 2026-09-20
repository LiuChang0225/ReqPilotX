from __future__ import annotations

import hashlib
import json
import re
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


STRUCTURE_TREE_SCHEMA_VERSION = "structure-tree/v2"
_SOURCE_NODE_ID_PATTERN = re.compile(r"^node-[0-9a-f]{32}$")
_SOURCE_CELL_ID_PATTERN = re.compile(r"^cell-[0-9a-f]{32}$")
_VISUAL_TEXT_OBSERVATION_ID_PATTERN = re.compile(r"^vto-[0-9a-f]{32}$")
_VISUAL_TABLE_CELL_ID_PATTERN = re.compile(r"^vtc-[0-9a-f]{32}$")
_BILINGUAL_SPAN_ID_PATTERN = re.compile(r"^bilingual-span-[0-9a-f]{32}$")
_STRUCTURE_IDENTITY_PATTERNS = (
    re.compile(r"^document$"),
    re.compile(r"^source:node-[0-9a-f]{32}$"),
    re.compile(
        r"^source:node-[0-9a-f]{32}:span:[0-9]+:[1-9][0-9]*$"
    ),
    re.compile(r"^visual:vto-[0-9a-f]{32}$"),
    re.compile(r"^table:node-[0-9a-f]{32}:row:[0-9]+$"),
    re.compile(
        r"^table:node-[0-9a-f]{32}:row:[0-9]+:"
        r"cell:cell-[0-9a-f]{32}:peer:[1-9][0-9]{0,3}$"
    ),
    re.compile(
        r"^table:node-[0-9a-f]{32}:merged:[0-9]+:[0-9]+:[1-9][0-9]*:[1-9][0-9]*$"
    ),
    re.compile(r"^model:[0-9a-f]{64}$"),
)
_FINDING_IDENTITY_PATTERNS = (
    re.compile(r"^gap:[0-9]+(?:\.[0-9]+)*:[0-9]+(?:\.[0-9]+)*$"),
    re.compile(r"^finding:[0-9a-f]{64}$"),
)
_FINDING_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,119}$")
_MESSAGE_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,159}$")
_EXPLICIT_SECTION_LABEL_PATTERN = re.compile(
    r"^[1-9][0-9]{0,2}(?:\.(?:0|[1-9][0-9]{0,2})){0,7}$"
)


class _StrictFrozenModel(BaseModel):
    """Immutable, provider-neutral value object for derived structure facts."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class StructureSourceRefKind(str, Enum):
    SOURCE_NODE = "source_node"
    TABLE_CELL = "table_cell"
    VISUAL_TEXT_OBSERVATION = "visual_text_observation"


class StructureNodeKind(str, Enum):
    DOCUMENT = "document"
    SECTION = "section"
    CLAUSE = "clause"
    CONTENT = "content"
    TABLE = "table"
    TABLE_ROW = "table_row"
    LIST_GROUP = "list_group"
    GROUP = "group"
    CONTINUATION = "continuation"


class StructureBoundaryBasis(str, Enum):
    DOCUMENT = "document"
    NATIVE_HEADING = "native_heading"
    NATIVE_BLOCK = "native_block"
    TABLE_SPAN = "table_span"
    TABLE_ROW = "table_row"
    MERGED_RANGE = "merged_range"
    EXPLICIT_NUMBER = "explicit_number"
    TOC_BLOCK = "toc_block"
    PAGE_FURNITURE = "page_furniture"
    VISUAL_TEXT = "visual_text"
    MODEL_SUGGESTION = "model_suggestion"


class StructureSourceSpanBoundaryBasis(str, Enum):
    """Why an exact semantic interval is safe to use inside one source fact."""

    EXPLICIT_NUMBER = "explicit_number"


class StructureRelationKind(str, Enum):
    CONTAINS = "contains"
    CONTINUES = "continues"
    APPLIES_TO = "applies_to"
    INHERITS = "inherits"
    REFERENCES = "references"
    TRANSLATION_OF = "translation_of"
    OPTION_STATE = "option_state"
    PLACED_IN_CELL = "placed_in_cell"
    PLACED_OVER_CELLS = "placed_over_cells"


class StructureOptionState(str, Enum):
    CHECKED = "checked"
    UNCHECKED = "unchecked"
    UNCERTAIN = "uncertain"


class StructureFindingSeverity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class BilingualLanguageEvidence(str, Enum):
    EN = "en"
    ZH_CN = "zh-CN"


class BilingualSpanBoundaryBasis(str, Enum):
    DOCX_RUN_BOUNDARY = "DOCX_RUN_BOUNDARY"
    SOURCE_BLOCK_BOUNDARY = "SOURCE_BLOCK_BOUNDARY"


class StructureSourceRef(_StrictFrozenModel):
    """Reference to immutable SourceIR identity; customer text is never copied."""

    kind: StructureSourceRefKind
    source_id: str = Field(min_length=1, max_length=160)

    @model_validator(mode="after")
    def validate_source_id_shape(self) -> StructureSourceRef:
        value = self.source_id
        if not value.strip():
            raise ValueError("source_id must not be blank")
        if self.kind is StructureSourceRefKind.SOURCE_NODE:
            pattern = _SOURCE_NODE_ID_PATTERN
        elif self.kind is StructureSourceRefKind.TABLE_CELL:
            pattern = _SOURCE_CELL_ID_PATTERN
        else:
            pattern = _VISUAL_TEXT_OBSERVATION_ID_PATTERN
        if not pattern.fullmatch(value):
            raise ValueError("source_id must use a stable SourceIR identity")
        return self


class StructureSourceSpan(_StrictFrozenModel):
    """A content-free half-open interval over one immutable source fact."""

    structure_source_span_id: str = Field(
        pattern=r"^structure-source-span-[0-9a-f]{32}$"
    )
    source_ref: StructureSourceRef
    start_offset: int = Field(ge=0)
    end_offset: int = Field(gt=0)
    boundary_basis: StructureSourceSpanBoundaryBasis

    @model_validator(mode="after")
    def validate_span(self) -> StructureSourceSpan:
        if self.source_ref.kind not in {
            StructureSourceRefKind.SOURCE_NODE,
            StructureSourceRefKind.VISUAL_TEXT_OBSERVATION,
        }:
            raise ValueError(
                "structure source spans must bind textual source facts"
            )
        if self.end_offset <= self.start_offset:
            raise ValueError(
                "structure source spans use non-empty half-open intervals"
            )
        expected_id = stable_structure_source_span_id(
            source_ref=self.source_ref,
            start_offset=self.start_offset,
            end_offset=self.end_offset,
            boundary_basis=self.boundary_basis,
        )
        if self.structure_source_span_id != expected_id:
            raise ValueError(
                "structure_source_span_id must bind its exact source interval"
            )
        return self


class BilingualSourceSpan(_StrictFrozenModel):
    """A content-free language-side interval over one immutable SourceIR node."""

    bilingual_span_id: str = Field(pattern=r"^bilingual-span-[0-9a-f]{32}$")
    source_ref: StructureSourceRef
    start_offset: int = Field(ge=0)
    end_offset: int = Field(gt=0)
    language_evidence: BilingualLanguageEvidence
    boundary_basis: BilingualSpanBoundaryBasis

    @model_validator(mode="after")
    def validate_span(self) -> BilingualSourceSpan:
        if self.source_ref.kind is not StructureSourceRefKind.SOURCE_NODE:
            raise ValueError("bilingual spans must bind an immutable SourceNode")
        if self.end_offset <= self.start_offset:
            raise ValueError("bilingual spans use non-empty half-open intervals")
        expected_id = stable_bilingual_source_span_id(
            source_ref=self.source_ref,
            start_offset=self.start_offset,
            end_offset=self.end_offset,
            language_evidence=self.language_evidence,
            boundary_basis=self.boundary_basis,
        )
        if self.bilingual_span_id != expected_id:
            raise ValueError("bilingual_span_id must bind its exact source interval")
        return self


class DocumentLanguagePolicy(_StrictFrozenModel):
    """Auditable bilingual authority policy.

    A declared policy remains bound to its exact declaration spans.  PDF
    counterpart evidence may instead carry an ``UNKNOWN`` policy scoped to the
    immutable SourceIR snapshot; that state deliberately selects no language
    and makes no equal-validity claim.
    """

    document_language_policy_id: str = Field(
        pattern=r"^document-language-policy-[0-9a-f]{32}$"
    )
    authority_basis: Literal["DECLARED_DOCUMENT_POLICY", "UNKNOWN"] = (
        "DECLARED_DOCUMENT_POLICY"
    )
    policy_scope_source_ir_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=160,
        exclude_if=lambda value: value is None,
    )
    equal_validity: bool | None = True
    conflict_precedence: Literal["zh-CN", "UNKNOWN"] = "zh-CN"
    english_declaration_spans: tuple[BilingualSourceSpan, ...] = ()
    chinese_declaration_spans: tuple[BilingualSourceSpan, ...] = ()

    @model_validator(mode="after")
    def validate_policy(self) -> DocumentLanguagePolicy:
        if self.authority_basis == "UNKNOWN":
            if (
                self.policy_scope_source_ir_id is None
                or self.equal_validity is not None
                or self.conflict_precedence != "UNKNOWN"
                or self.english_declaration_spans
                or self.chinese_declaration_spans
            ):
                raise ValueError(
                    "unknown language authority requires only a SourceIR scope"
                )
            expected_id = stable_document_language_policy_id(
                english_declaration_spans=(),
                chinese_declaration_spans=(),
                equal_validity=None,
                conflict_precedence="UNKNOWN",
                authority_basis=self.authority_basis,
                policy_scope_source_ir_id=self.policy_scope_source_ir_id,
            )
            if self.document_language_policy_id != expected_id:
                raise ValueError(
                    "document_language_policy_id must bind its unknown scope"
                )
            return self
        if self.policy_scope_source_ir_id is not None:
            raise ValueError(
                "declared language authority is bound by declarations, not scope"
            )
        if (
            len(self.english_declaration_spans) != 1
            or len(self.chinese_declaration_spans) != 1
        ):
            raise ValueError(
                "the document language policy requires one declaration per language"
            )
        if any(
            span.language_evidence is not BilingualLanguageEvidence.EN
            for span in self.english_declaration_spans
        ) or any(
            span.language_evidence is not BilingualLanguageEvidence.ZH_CN
            for span in self.chinese_declaration_spans
        ):
            raise ValueError("language-policy declarations must bind matching sides")
        all_spans = (
            *self.english_declaration_spans,
            *self.chinese_declaration_spans,
        )
        if len(all_spans) != len({span.bilingual_span_id for span in all_spans}):
            raise ValueError("language-policy declaration spans must be unique")
        expected_id = stable_document_language_policy_id(
            english_declaration_spans=self.english_declaration_spans,
            chinese_declaration_spans=self.chinese_declaration_spans,
            equal_validity=self.equal_validity,
            conflict_precedence=self.conflict_precedence,
            authority_basis=self.authority_basis,
        )
        if self.document_language_policy_id != expected_id:
            raise ValueError(
                "document_language_policy_id must bind its declaration evidence"
            )
        return self


class BilingualCounterpartGroup(_StrictFrozenModel):
    """One requirement identity with one or more source spans on each side."""

    counterpart_group_id: str = Field(pattern=r"^bilingual-group-[0-9a-f]{32}$")
    document_language_policy_id: str = Field(
        pattern=r"^document-language-policy-[0-9a-f]{32}$"
    )
    canonical_structure_node_id: str = Field(min_length=1, max_length=160)
    member_structure_node_ids: tuple[str, ...]
    english_spans: tuple[BilingualSourceSpan, ...]
    chinese_spans: tuple[BilingualSourceSpan, ...]
    relation_kind: Literal[StructureRelationKind.TRANSLATION_OF] = (
        StructureRelationKind.TRANSLATION_OF
    )
    translation_relation_id: str | None = Field(
        default=None,
        pattern=r"^structure-relation-[0-9a-f]{32}$",
    )

    @model_validator(mode="after")
    def validate_group(self) -> BilingualCounterpartGroup:
        if (
            not self.member_structure_node_ids
            or len(self.member_structure_node_ids) > 2
            or len(self.member_structure_node_ids)
            != len(set(self.member_structure_node_ids))
            or self.canonical_structure_node_id not in self.member_structure_node_ids
        ):
            raise ValueError(
                "a bilingual group requires unique members and one canonical member"
            )
        if not self.english_spans or not self.chinese_spans:
            raise ValueError("a bilingual group requires grounding on both sides")
        if any(
            span.language_evidence is not BilingualLanguageEvidence.EN
            for span in self.english_spans
        ) or any(
            span.language_evidence is not BilingualLanguageEvidence.ZH_CN
            for span in self.chinese_spans
        ):
            raise ValueError("bilingual group spans must bind matching language sides")
        all_spans = (*self.english_spans, *self.chinese_spans)
        if len(all_spans) != len({span.bilingual_span_id for span in all_spans}):
            raise ValueError("a bilingual span cannot be assigned twice in one group")
        spans_by_ref: dict[StructureSourceRef, list[BilingualSourceSpan]] = {}
        for span in all_spans:
            spans_by_ref.setdefault(span.source_ref, []).append(span)
        for spans in spans_by_ref.values():
            ordered = sorted(
                spans,
                key=lambda span: (
                    span.start_offset,
                    span.end_offset,
                    span.bilingual_span_id,
                ),
            )
            if ordered[0].start_offset != 0 or any(
                current.end_offset != following.start_offset
                for current, following in zip(ordered, ordered[1:])
            ):
                raise ValueError(
                    "bilingual spans over one source must be gap-free and non-overlapping"
                )
        if (
            len(self.member_structure_node_ids) == 1
            and self.translation_relation_id is not None
        ):
            raise ValueError(
                "a single-owner bilingual group cannot use a self relation"
            )
        if (
            len(self.member_structure_node_ids) > 1
            and self.translation_relation_id is None
        ):
            raise ValueError(
                "a multi-owner bilingual group requires TRANSLATION_OF materialization"
            )
        expected_id = stable_bilingual_counterpart_group_id(
            document_language_policy_id=self.document_language_policy_id,
            canonical_structure_node_id=self.canonical_structure_node_id,
            member_structure_node_ids=self.member_structure_node_ids,
            english_spans=self.english_spans,
            chinese_spans=self.chinese_spans,
        )
        if self.counterpart_group_id != expected_id:
            raise ValueError("counterpart_group_id must bind members and exact spans")
        return self


class BilingualMismatchSignal(_StrictFrozenModel):
    """Review-only mismatch evidence; replacement text is not representable."""

    mismatch_signal_id: str = Field(pattern=r"^bilingual-mismatch-[0-9a-f]{32}$")
    counterpart_group_id: str = Field(pattern=r"^bilingual-group-[0-9a-f]{32}$")
    severity: Literal[StructureFindingSeverity.WARNING] = (
        StructureFindingSeverity.WARNING
    )
    blocking: Literal[False] = False
    english_span_ids: tuple[str, ...]
    chinese_span_ids: tuple[str, ...]
    message_key: Literal["structure.bilingual_counterpart_mismatch_review"] = (
        "structure.bilingual_counterpart_mismatch_review"
    )

    @model_validator(mode="after")
    def validate_signal(self) -> BilingualMismatchSignal:
        if (
            not self.english_span_ids
            or not self.chinese_span_ids
            or any(
                _BILINGUAL_SPAN_ID_PATTERN.fullmatch(span_id) is None
                for span_id in (*self.english_span_ids, *self.chinese_span_ids)
            )
            or len(self.english_span_ids) != len(set(self.english_span_ids))
            or len(self.chinese_span_ids) != len(set(self.chinese_span_ids))
            or set(self.english_span_ids).intersection(self.chinese_span_ids)
        ):
            raise ValueError(
                "a mismatch signal requires unique grounding on both sides"
            )
        expected_id = stable_bilingual_mismatch_signal_id(
            counterpart_group_id=self.counterpart_group_id,
            english_span_ids=self.english_span_ids,
            chinese_span_ids=self.chinese_span_ids,
        )
        if self.mismatch_signal_id != expected_id:
            raise ValueError("mismatch_signal_id must bind both grounded sides")
        return self


class StructureNode(_StrictFrozenModel):
    """One structural boundary grounded only by SourceIR IDs.

    Requirement classification intentionally does not exist in this contract.
    In particular, ``TABLE_ROW`` means a locatable structural row, not an
    independently promoted requirement.
    """

    structure_node_id: str = Field(min_length=1, max_length=160)
    identity_key: str = Field(min_length=1, max_length=500)
    kind: StructureNodeKind
    parent_structure_node_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=160,
    )
    source_order: int = Field(ge=0)
    source_refs: tuple[StructureSourceRef, ...] = ()
    semantic_source_refs: tuple[StructureSourceRef, ...] | None = None
    semantic_source_spans: tuple[StructureSourceSpan, ...] = Field(
        default=(),
        exclude_if=lambda value: not value,
    )
    boundary_basis: StructureBoundaryBasis
    explicit_label: str | None = Field(default=None, min_length=1, max_length=80)
    list_ordinal: int | None = Field(default=None, ge=1, le=10_000)
    table_row_index: int | None = Field(default=None, ge=0)

    @field_validator("structure_node_id", "identity_key")
    @classmethod
    def reject_blank_identity_value(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("identity values must not be blank")
        return value

    @model_validator(mode="after")
    def validate_row_shape(self) -> StructureNode:
        if not any(
            pattern.fullmatch(self.identity_key)
            for pattern in _STRUCTURE_IDENTITY_PATTERNS
        ):
            raise ValueError("identity_key must use a non-content structural identity")
        if self.kind is StructureNodeKind.TABLE_ROW:
            if self.table_row_index is None:
                raise ValueError("table_row nodes require table_row_index")
        elif self.table_row_index is not None:
            raise ValueError("table_row_index is valid only for table_row nodes")
        if self.explicit_label is not None:
            if self.kind is not StructureNodeKind.SECTION:
                raise ValueError("explicit_label is valid only for section nodes")
            if not _EXPLICIT_SECTION_LABEL_PATTERN.fullmatch(self.explicit_label):
                raise ValueError(
                    "explicit_label must be a normalized numeric outline label"
                )
        if self.list_ordinal is not None:
            if self.kind is not StructureNodeKind.CLAUSE:
                raise ValueError("list_ordinal is valid only for clause nodes")
        if self.boundary_basis is StructureBoundaryBasis.EXPLICIT_NUMBER:
            if self.kind is StructureNodeKind.SECTION and self.explicit_label is None:
                raise ValueError("explicit-number sections require explicit_label")
            if self.kind is StructureNodeKind.CLAUSE and self.list_ordinal is None:
                raise ValueError("explicit-number clauses require list_ordinal")
            if self.kind not in {StructureNodeKind.SECTION, StructureNodeKind.CLAUSE}:
                raise ValueError(
                    "explicit-number boundaries are reserved for sections and clauses"
                )
        elif self.list_ordinal is not None:
            raise ValueError("list_ordinal requires an explicit-number boundary")
        if self.parent_structure_node_id == self.structure_node_id:
            raise ValueError("a structure node cannot be its own parent")
        if len(self.source_refs) != len(set(self.source_refs)):
            raise ValueError("source_refs must not contain duplicates")
        if self.semantic_source_refs is not None:
            if len(self.semantic_source_refs) != len(set(self.semantic_source_refs)):
                raise ValueError("semantic_source_refs must not contain duplicates")
            if not set(self.semantic_source_refs).issubset(self.source_refs):
                raise ValueError(
                    "semantic_source_refs must remain inside structural grounding"
                )
        if self.semantic_source_spans:
            span_ids = tuple(
                span.structure_source_span_id
                for span in self.semantic_source_spans
            )
            if len(span_ids) != len(set(span_ids)):
                raise ValueError(
                    "semantic_source_spans must not contain duplicates"
                )
            effective_refs = set(self.effective_semantic_source_refs)
            if any(
                span.source_ref not in effective_refs
                for span in self.semantic_source_spans
            ):
                raise ValueError(
                    "semantic source spans must remain inside semantic grounding"
                )
            spans_by_ref: dict[
                StructureSourceRef, list[StructureSourceSpan]
            ] = {}
            for span in self.semantic_source_spans:
                spans_by_ref.setdefault(span.source_ref, []).append(span)
            for spans in spans_by_ref.values():
                ordered = sorted(
                    spans,
                    key=lambda span: (
                        span.start_offset,
                        span.end_offset,
                        span.structure_source_span_id,
                    ),
                )
                if len(ordered) > 1:
                    raise ValueError(
                        "one structure node may declare only one semantic "
                        "interval per source fact"
                    )
                if any(
                    current.end_offset > following.start_offset
                    for current, following in zip(ordered, ordered[1:])
                ):
                    raise ValueError(
                        "semantic source spans over one fact must not overlap"
                    )
        return self

    @property
    def effective_semantic_source_refs(self) -> tuple[StructureSourceRef, ...]:
        if self.semantic_source_refs is None:
            return self.source_refs
        return self.semantic_source_refs


class StructureRelation(_StrictFrozenModel):
    relation_id: str = Field(min_length=1, max_length=160)
    kind: StructureRelationKind
    source_structure_node_id: str = Field(min_length=1, max_length=160)
    target_structure_node_id: str = Field(min_length=1, max_length=160)
    source_refs: tuple[StructureSourceRef, ...] = ()
    option_state: StructureOptionState | None = None

    @model_validator(mode="after")
    def validate_relation_shape(self) -> StructureRelation:
        if self.source_structure_node_id == self.target_structure_node_id:
            raise ValueError("a structure relation cannot reference itself")
        if len(self.source_refs) != len(set(self.source_refs)):
            raise ValueError("relation source_refs must not contain duplicates")
        if self.kind is StructureRelationKind.OPTION_STATE:
            if self.option_state is None:
                raise ValueError("option_state relations require an explicit state")
        elif self.option_state is not None:
            raise ValueError("option_state is valid only for option_state relations")
        return self


class StructureFinding(_StrictFrozenModel):
    finding_id: str = Field(min_length=1, max_length=160)
    identity_key: str = Field(min_length=1, max_length=500)
    code: str = Field(min_length=1, max_length=120)
    severity: StructureFindingSeverity
    blocking: bool = False
    message_key: str = Field(min_length=1, max_length=160)
    structure_node_id: str | None = Field(default=None, min_length=1, max_length=160)
    source_refs: tuple[StructureSourceRef, ...] = ()

    @field_validator("finding_id", "identity_key", "code", "message_key")
    @classmethod
    def reject_blank_finding_value(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("finding values must not be blank")
        return value

    @model_validator(mode="after")
    def validate_finding_refs(self) -> StructureFinding:
        if not any(
            pattern.fullmatch(self.identity_key)
            for pattern in _FINDING_IDENTITY_PATTERNS
        ):
            raise ValueError("finding identity_key must use a non-content identity")
        if not _FINDING_CODE_PATTERN.fullmatch(self.code):
            raise ValueError("finding code must use a machine-readable key")
        if not _MESSAGE_KEY_PATTERN.fullmatch(self.message_key):
            raise ValueError("message_key must use a localized message key")
        if len(self.source_refs) != len(set(self.source_refs)):
            raise ValueError("finding source_refs must not contain duplicates")
        return self


class VisualTableCellTextBinding(_StrictFrozenModel):
    """Exact-topology visual table text mapped to one native cell anchor."""

    table_structure_node_id: str = Field(min_length=1, max_length=160)
    table_cell_id: str = Field(min_length=1, max_length=160)
    visual_table_cell_id: str = Field(min_length=1, max_length=160)

    @model_validator(mode="after")
    def validate_binding_shape(self) -> VisualTableCellTextBinding:
        if not _SOURCE_CELL_ID_PATTERN.fullmatch(self.table_cell_id):
            raise ValueError("table_cell_id must use a stable SourceIR identity")
        if _VISUAL_TABLE_CELL_ID_PATTERN.fullmatch(self.visual_table_cell_id) is None:
            raise ValueError(
                "visual_table_cell_id must use a stable visual-table identity"
            )
        return self


class StructureTreeSnapshot(_StrictFrozenModel):
    """Validated StructureTree v2 snapshot derived from one SourceIR snapshot."""

    schema_version: Literal["structure-tree/v2"] = STRUCTURE_TREE_SCHEMA_VERSION
    structure_snapshot_id: str = Field(min_length=1, max_length=160)
    source_ir_id: str = Field(min_length=1, max_length=160)
    resolver_id: str = Field(min_length=1, max_length=160)
    resolver_version: str = Field(min_length=1, max_length=160)
    source_node_ids: tuple[str, ...] = ()
    source_cell_ids: tuple[str, ...] = ()
    visual_text_observation_ids: tuple[str, ...] = ()
    visual_table_cell_ids: tuple[str, ...] = ()
    nodes: tuple[StructureNode, ...]
    relations: tuple[StructureRelation, ...] = ()
    document_language_policy: DocumentLanguagePolicy | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    bilingual_counterpart_groups: tuple[BilingualCounterpartGroup, ...] = Field(
        default=(),
        exclude_if=lambda value: not value,
    )
    bilingual_mismatch_signals: tuple[BilingualMismatchSignal, ...] = Field(
        default=(),
        exclude_if=lambda value: not value,
    )
    visual_table_cell_text_bindings: tuple[VisualTableCellTextBinding, ...] = ()
    findings: tuple[StructureFinding, ...] = ()

    @field_validator(
        "structure_snapshot_id",
        "source_ir_id",
        "resolver_id",
        "resolver_version",
    )
    @classmethod
    def reject_blank_snapshot_value(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("snapshot identity values must not be blank")
        return value

    @field_validator("source_node_ids")
    @classmethod
    def validate_source_node_catalog(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not _SOURCE_NODE_ID_PATTERN.fullmatch(source_id) for source_id in value):
            raise ValueError("source node catalog values must use stable SourceIR IDs")
        if len(value) != len(set(value)):
            raise ValueError("source identity catalogs must contain unique values")
        if value != tuple(sorted(value)):
            raise ValueError("source identity catalogs must be sorted")
        return value

    @field_validator("source_cell_ids")
    @classmethod
    def validate_source_cell_catalog(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not _SOURCE_CELL_ID_PATTERN.fullmatch(source_id) for source_id in value):
            raise ValueError("source cell catalog values must use stable SourceIR IDs")
        if len(value) != len(set(value)):
            raise ValueError("source identity catalogs must contain unique values")
        if value != tuple(sorted(value)):
            raise ValueError("source identity catalogs must be sorted")
        return value

    @field_validator("visual_text_observation_ids")
    @classmethod
    def validate_visual_text_observation_catalog(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        if any(
            not _VISUAL_TEXT_OBSERVATION_ID_PATTERN.fullmatch(observation_id)
            for observation_id in value
        ):
            raise ValueError(
                "visual text observation catalog values must use stable identities"
            )
        if len(value) != len(set(value)):
            raise ValueError("source identity catalogs must contain unique values")
        if value != tuple(sorted(value)):
            raise ValueError("source identity catalogs must be sorted")
        return value

    @field_validator("visual_table_cell_ids")
    @classmethod
    def validate_visual_table_cell_catalog(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        if any(
            _VISUAL_TABLE_CELL_ID_PATTERN.fullmatch(cell_id) is None
            for cell_id in value
        ):
            raise ValueError(
                "visual table cell catalog values must use stable identities"
            )
        if len(value) != len(set(value)):
            raise ValueError("source identity catalogs must contain unique values")
        if value != tuple(sorted(value)):
            raise ValueError("source identity catalogs must be sorted")
        return value

    @model_validator(mode="after")
    def validate_snapshot_graph(self) -> StructureTreeSnapshot:
        expected_snapshot_id = stable_structure_snapshot_id(
            source_ir_id=self.source_ir_id,
            resolver_id=self.resolver_id,
            resolver_version=self.resolver_version,
        )
        if self.structure_snapshot_id != expected_snapshot_id:
            raise ValueError(
                "structure_snapshot_id must be derived from SourceIR and resolver identity"
            )

        source_node_id_set = set(self.source_node_ids)
        source_cell_id_set = set(self.source_cell_ids)
        visual_text_observation_id_set = set(self.visual_text_observation_ids)
        visual_table_cell_id_set = set(self.visual_table_cell_ids)

        orders = [node.source_order for node in self.nodes]
        if len(orders) != len(set(orders)):
            raise ValueError("structure source_order must be unique within a snapshot")
        if orders != sorted(orders):
            raise ValueError("structure nodes must be stored in ascending source_order")

        nodes_by_id = {node.structure_node_id: node for node in self.nodes}
        if len(nodes_by_id) != len(self.nodes):
            raise ValueError("structure_node_id must be unique within a snapshot")
        identity_keys = [node.identity_key for node in self.nodes]
        if len(identity_keys) != len(set(identity_keys)):
            raise ValueError("identity_key must be unique within a snapshot")
        for node in self.nodes:
            expected_node_id = stable_structure_node_id(
                source_ir_id=self.source_ir_id,
                resolver_id=self.resolver_id,
                resolver_version=self.resolver_version,
                kind=node.kind,
                identity_key=node.identity_key,
            )
            if node.structure_node_id != expected_node_id:
                raise ValueError(
                    "structure_node_id must be derived from its stable identity"
                )
            if (
                node.parent_structure_node_id is not None
                and node.parent_structure_node_id not in nodes_by_id
            ):
                raise ValueError(
                    "parent_structure_node_id must reference a node in the snapshot"
                )
            self._validate_source_refs(
                node.source_refs,
                source_node_ids=source_node_id_set,
                source_cell_ids=source_cell_id_set,
                visual_text_observation_ids=visual_text_observation_id_set,
            )

        roots = [node for node in self.nodes if node.parent_structure_node_id is None]
        if len(roots) != 1 or roots[0].kind is not StructureNodeKind.DOCUMENT:
            raise ValueError("StructureTree must contain exactly one document root")
        root = roots[0]
        if root.source_refs:
            raise ValueError(
                "the document root must not copy or aggregate SourceIR content"
            )
        if (
            root.identity_key != "document"
            or root.boundary_basis is not StructureBoundaryBasis.DOCUMENT
            or root.source_order != 0
        ):
            raise ValueError("the document root must use the canonical root boundary")
        for node in self.nodes:
            if node is root:
                continue
            if node.kind is StructureNodeKind.DOCUMENT:
                raise ValueError("only the tree root may be a document node")
            if node.parent_structure_node_id is None:
                raise ValueError("non-document structure nodes require one parent")
            if not node.source_refs:
                raise ValueError(
                    "non-document structure nodes require SourceIR grounding"
                )
            if node.boundary_basis is StructureBoundaryBasis.DOCUMENT:
                raise ValueError("document boundary basis is reserved for the root")
            if (
                node.kind is StructureNodeKind.TABLE_ROW
                and nodes_by_id[node.parent_structure_node_id].kind
                is not StructureNodeKind.TABLE
            ):
                raise ValueError("table_row nodes must be direct children of a table")

        _require_acyclic_parent_graph(nodes_by_id)
        for node in self.nodes:
            if node.parent_structure_node_id is None:
                continue
            if (
                nodes_by_id[node.parent_structure_node_id].source_order
                >= node.source_order
            ):
                raise ValueError(
                    "a parent structure node must precede each of its children"
                )

        binding_keys: list[tuple[str, str]] = []
        grounded_visual_table_cell_ids: set[str] = set()
        table_cell_ids_by_structure_id: dict[str, set[str]] = {}
        for node in self.nodes:
            if node.kind is not StructureNodeKind.TABLE_ROW:
                continue
            assert node.parent_structure_node_id is not None
            table_cell_ids_by_structure_id.setdefault(
                node.parent_structure_node_id,
                set(),
            ).update(
                ref.source_id
                for ref in node.source_refs
                if ref.kind is StructureSourceRefKind.TABLE_CELL
            )
        for binding in self.visual_table_cell_text_bindings:
            table_node = nodes_by_id.get(binding.table_structure_node_id)
            if table_node is None or table_node.kind is not StructureNodeKind.TABLE:
                raise ValueError(
                    "visual table-cell binding must reference a table node"
                )
            if binding.table_cell_id not in source_cell_id_set:
                raise ValueError(
                    "visual table-cell binding cell is not present in SourceIR"
                )
            if binding.table_cell_id not in table_cell_ids_by_structure_id.get(
                binding.table_structure_node_id,
                set(),
            ):
                raise ValueError(
                    "visual table-cell binding cell must belong to its table"
                )
            if binding.visual_table_cell_id not in visual_table_cell_id_set:
                raise ValueError(
                    "visual table-cell binding fact is not present in SourceIR"
                )
            if binding.visual_table_cell_id in grounded_visual_table_cell_ids:
                raise ValueError(
                    "a visual table cell can ground at most one native table cell"
                )
            grounded_visual_table_cell_ids.add(binding.visual_table_cell_id)
            binding_keys.append(
                (
                    binding.table_structure_node_id,
                    binding.table_cell_id,
                )
            )
        if len(binding_keys) != len(set(binding_keys)):
            raise ValueError("a table cell can have at most one visual text binding")
        if binding_keys != sorted(binding_keys):
            raise ValueError("visual table-cell bindings must be stored stably")

        relation_ids: set[str] = set()
        relations_by_id: dict[str, StructureRelation] = {}
        contains_by_target: dict[str, list[StructureRelation]] = {}
        option_state_by_target: dict[str, list[StructureRelation]] = {}
        cell_placement_by_source: dict[str, list[StructureRelation]] = {}
        for relation in self.relations:
            if relation.relation_id in relation_ids:
                raise ValueError("relation_id must be unique within a snapshot")
            relation_ids.add(relation.relation_id)
            relations_by_id[relation.relation_id] = relation
            if relation.source_structure_node_id not in nodes_by_id:
                raise ValueError("relation source must reference a structure node")
            if relation.target_structure_node_id not in nodes_by_id:
                raise ValueError("relation target must reference a structure node")
            self._validate_source_refs(
                relation.source_refs,
                source_node_ids=source_node_id_set,
                source_cell_ids=source_cell_id_set,
                visual_text_observation_ids=visual_text_observation_id_set,
            )
            expected_relation_id = stable_structure_relation_id(
                source_ir_id=self.source_ir_id,
                resolver_id=self.resolver_id,
                resolver_version=self.resolver_version,
                kind=relation.kind,
                source_structure_node_id=relation.source_structure_node_id,
                target_structure_node_id=relation.target_structure_node_id,
                source_refs=relation.source_refs,
                option_state=relation.option_state,
            )
            if relation.relation_id != expected_relation_id:
                raise ValueError("relation_id must be derived from its stable identity")
            if relation.kind is StructureRelationKind.CONTAINS:
                contains_by_target.setdefault(
                    relation.target_structure_node_id,
                    [],
                ).append(relation)
            elif relation.kind is StructureRelationKind.CONTINUES:
                source_node = nodes_by_id[relation.source_structure_node_id]
                target_node = nodes_by_id[relation.target_structure_node_id]
                if source_node.source_order >= target_node.source_order:
                    raise ValueError(
                        "continues relations must move forward in structure order"
                    )
                if not relation.source_refs:
                    raise ValueError("continues relations require target grounding")
                if relation.source_refs != target_node.source_refs:
                    raise ValueError(
                        "continues relation grounding must match the continuation target"
                    )
            elif relation.kind is StructureRelationKind.OPTION_STATE:
                source_node = nodes_by_id[relation.source_structure_node_id]
                target_node = nodes_by_id[relation.target_structure_node_id]
                if (
                    target_node.parent_structure_node_id
                    != source_node.structure_node_id
                ):
                    raise ValueError(
                        "option_state relations must bind a direct child to its parent"
                    )
                if target_node.kind not in {
                    StructureNodeKind.CONTENT,
                    StructureNodeKind.CLAUSE,
                }:
                    raise ValueError(
                        "option_state targets must be content or clause nodes"
                    )
                if (
                    not relation.source_refs
                    or relation.source_refs != target_node.source_refs
                ):
                    raise ValueError(
                        "option_state grounding must match the option target"
                    )
                option_state_by_target.setdefault(
                    relation.target_structure_node_id,
                    [],
                ).append(relation)
            elif relation.kind in {
                StructureRelationKind.PLACED_IN_CELL,
                StructureRelationKind.PLACED_OVER_CELLS,
            }:
                source_node = nodes_by_id[relation.source_structure_node_id]
                target_node = nodes_by_id[relation.target_structure_node_id]
                if target_node.kind is not StructureNodeKind.TABLE:
                    raise ValueError(
                        "image-cell placement relations must target a table node"
                    )
                expected_cell_ref_count = (
                    1 if relation.kind is StructureRelationKind.PLACED_IN_CELL else None
                )
                cell_refs = relation.source_refs[1:]
                if (
                    not relation.source_refs
                    or relation.source_refs[0].kind
                    is not StructureSourceRefKind.SOURCE_NODE
                    or any(
                        ref.kind is not StructureSourceRefKind.TABLE_CELL
                        for ref in cell_refs
                    )
                    or (
                        expected_cell_ref_count is not None
                        and len(cell_refs) != expected_cell_ref_count
                    )
                    or (
                        relation.kind is StructureRelationKind.PLACED_OVER_CELLS
                        and len(cell_refs) < 2
                    )
                ):
                    raise ValueError(
                        "image-cell placement relations require one image node "
                        "followed by the exact table-cell grounding references"
                    )
                if relation.source_refs[0] not in source_node.source_refs:
                    raise ValueError(
                        "image-cell placement grounding must belong to its source node"
                    )
                target_cell_ids = table_cell_ids_by_structure_id.get(
                    relation.target_structure_node_id,
                    set(),
                )
                if any(ref.source_id not in target_cell_ids for ref in cell_refs):
                    raise ValueError(
                        "image-cell placement references must belong to the target table"
                    )
                cell_placement_by_source.setdefault(
                    relation.source_structure_node_id,
                    [],
                ).append(relation)

        if self.document_language_policy is None:
            if self.bilingual_counterpart_groups or self.bilingual_mismatch_signals:
                raise ValueError(
                    "bilingual groups and mismatch signals require a language policy"
                )
        else:
            if (
                self.document_language_policy.authority_basis == "UNKNOWN"
                and self.document_language_policy.policy_scope_source_ir_id
                != self.source_ir_id
            ):
                raise ValueError(
                    "unknown language authority must bind this SourceIR snapshot"
                )
            policy_spans = (
                *self.document_language_policy.english_declaration_spans,
                *self.document_language_policy.chinese_declaration_spans,
            )
            for span in policy_spans:
                self._validate_source_refs(
                    (span.source_ref,),
                    source_node_ids=source_node_id_set,
                    source_cell_ids=source_cell_id_set,
                    visual_text_observation_ids=visual_text_observation_id_set,
                )

        group_ids = [
            group.counterpart_group_id for group in self.bilingual_counterpart_groups
        ]
        if len(group_ids) != len(set(group_ids)) or group_ids != sorted(group_ids):
            raise ValueError("bilingual counterpart groups must be unique and sorted")
        assigned_member_ids: set[str] = set()
        assigned_span_ids: set[str] = set()
        groups_by_id: dict[str, BilingualCounterpartGroup] = {}
        direct_content_refs_by_parent: dict[
            str,
            set[StructureSourceRef],
        ] = {}
        for node in self.nodes:
            parent_id = node.parent_structure_node_id
            if node.kind is not StructureNodeKind.CONTENT or parent_id is None:
                continue
            direct_content_refs_by_parent.setdefault(parent_id, set()).update(
                node.source_refs
            )
        for group in self.bilingual_counterpart_groups:
            groups_by_id[group.counterpart_group_id] = group
            if (
                self.document_language_policy is None
                or group.document_language_policy_id
                != self.document_language_policy.document_language_policy_id
            ):
                raise ValueError(
                    "a bilingual group must bind the snapshot language policy"
                )
            if any(
                member_id not in nodes_by_id
                for member_id in group.member_structure_node_ids
            ):
                raise ValueError("bilingual group members must exist in the snapshot")
            if assigned_member_ids.intersection(group.member_structure_node_ids):
                raise ValueError(
                    "a structure owner can belong to at most one bilingual group"
                )
            assigned_member_ids.update(group.member_structure_node_ids)
            member_source_refs = {
                source_ref
                for member_id in group.member_structure_node_ids
                for source_ref in nodes_by_id[member_id].source_refs
            }
            # A numbered owner and its continuous direct CONTENT zone are one
            # structural ownership boundary.  This lets a single-owner group
            # retain language spans split into immutable PDF blocks without
            # pretending that each physical continuation is a separate
            # requirement owner.
            member_source_refs.update(
                source_ref
                for member_id in group.member_structure_node_ids
                for source_ref in direct_content_refs_by_parent.get(
                    member_id,
                    (),
                )
            )
            group_spans = (*group.english_spans, *group.chinese_spans)
            for span in group_spans:
                self._validate_source_refs(
                    (span.source_ref,),
                    source_node_ids=source_node_id_set,
                    source_cell_ids=source_cell_id_set,
                    visual_text_observation_ids=visual_text_observation_id_set,
                )
                if span.source_ref not in member_source_refs:
                    raise ValueError(
                        "bilingual spans must stay inside their declared structure owners"
                    )
                if span.bilingual_span_id in assigned_span_ids:
                    raise ValueError(
                        "a bilingual source interval can belong to at most one group"
                    )
                assigned_span_ids.add(span.bilingual_span_id)
            if len(group.member_structure_node_ids) == 2:
                assert group.translation_relation_id is not None
                relation = relations_by_id.get(group.translation_relation_id)
                other_member = next(
                    member_id
                    for member_id in group.member_structure_node_ids
                    if member_id != group.canonical_structure_node_id
                )
                expected_refs = tuple(
                    dict.fromkeys(span.source_ref for span in group_spans)
                )
                if (
                    relation is None
                    or relation.kind is not StructureRelationKind.TRANSLATION_OF
                    or relation.source_structure_node_id
                    != group.canonical_structure_node_id
                    or relation.target_structure_node_id != other_member
                    or relation.source_refs != expected_refs
                ):
                    raise ValueError(
                        "a multi-owner bilingual group requires its exact "
                        "TRANSLATION_OF relation"
                    )

        mismatch_ids = [
            signal.mismatch_signal_id for signal in self.bilingual_mismatch_signals
        ]
        if len(mismatch_ids) != len(set(mismatch_ids)) or mismatch_ids != sorted(
            mismatch_ids
        ):
            raise ValueError("bilingual mismatch signals must be unique and sorted")
        for signal in self.bilingual_mismatch_signals:
            group = groups_by_id.get(signal.counterpart_group_id)
            if group is None:
                raise ValueError("a mismatch signal must reference one bilingual group")
            if not set(signal.english_span_ids).issubset(
                span.bilingual_span_id for span in group.english_spans
            ) or not set(signal.chinese_span_ids).issubset(
                span.bilingual_span_id for span in group.chinese_spans
            ):
                raise ValueError(
                    "a mismatch signal must be grounded on both counterpart sides"
                )

        if any(len(relations) != 1 for relations in option_state_by_target.values()):
            raise ValueError(
                "an option target requires exactly one option_state relation"
            )
        if any(len(relations) != 1 for relations in cell_placement_by_source.values()):
            raise ValueError(
                "an image placement can have at most one exact table-cell owner set"
            )

        if root.structure_node_id in contains_by_target:
            raise ValueError("the document root cannot be contained by another node")
        for node in self.nodes:
            if node is root:
                continue
            parent_relations = contains_by_target.get(node.structure_node_id, [])
            if len(parent_relations) != 1:
                raise ValueError(
                    "every non-document node requires exactly one contains relation"
                )
            if (
                parent_relations[0].source_structure_node_id
                != node.parent_structure_node_id
            ):
                raise ValueError("contains relation must match the node parent")

        finding_ids: set[str] = set()
        for finding in self.findings:
            if finding.finding_id in finding_ids:
                raise ValueError("finding_id must be unique within a snapshot")
            finding_ids.add(finding.finding_id)
            if (
                finding.structure_node_id is not None
                and finding.structure_node_id not in nodes_by_id
            ):
                raise ValueError(
                    "finding must reference a structure node in the snapshot"
                )
            self._validate_source_refs(
                finding.source_refs,
                source_node_ids=source_node_id_set,
                source_cell_ids=source_cell_id_set,
                visual_text_observation_ids=visual_text_observation_id_set,
            )
            expected_finding_id = stable_structure_finding_id(
                source_ir_id=self.source_ir_id,
                resolver_id=self.resolver_id,
                resolver_version=self.resolver_version,
                code=finding.code,
                identity_key=finding.identity_key,
            )
            if finding.finding_id != expected_finding_id:
                raise ValueError("finding_id must be derived from its stable identity")
        return self

    @staticmethod
    def _validate_source_refs(
        refs: tuple[StructureSourceRef, ...],
        *,
        source_node_ids: set[str],
        source_cell_ids: set[str],
        visual_text_observation_ids: set[str],
    ) -> None:
        for source_ref in refs:
            if source_ref.kind is StructureSourceRefKind.SOURCE_NODE:
                if source_ref.source_id not in source_node_ids:
                    raise ValueError("source node reference is not present in SourceIR")
            elif source_ref.kind is StructureSourceRefKind.TABLE_CELL:
                if source_ref.source_id not in source_cell_ids:
                    raise ValueError("table cell reference is not present in SourceIR")
            elif source_ref.source_id not in visual_text_observation_ids:
                raise ValueError(
                    "visual text observation reference is not present in SourceIR"
                )


def stable_structure_snapshot_id(
    *,
    source_ir_id: str,
    resolver_id: str,
    resolver_version: str,
) -> str:
    _require_non_blank(source_ir_id, "source_ir_id")
    _require_non_blank(resolver_id, "resolver_id")
    _require_non_blank(resolver_version, "resolver_version")
    return _stable_id("structure", source_ir_id, resolver_id, resolver_version)


def stable_structure_source_span_id(
    *,
    source_ref: StructureSourceRef,
    start_offset: int,
    end_offset: int,
    boundary_basis: StructureSourceSpanBoundaryBasis,
) -> str:
    if not isinstance(source_ref, StructureSourceRef):
        raise TypeError("source_ref must be a StructureSourceRef")
    if (
        not isinstance(start_offset, int)
        or isinstance(start_offset, bool)
        or not isinstance(end_offset, int)
        or isinstance(end_offset, bool)
        or start_offset < 0
        or end_offset <= start_offset
    ):
        raise ValueError(
            "structure source span offsets must form a non-empty interval"
        )
    if not isinstance(boundary_basis, StructureSourceSpanBoundaryBasis):
        raise TypeError(
            "boundary_basis must be a StructureSourceSpanBoundaryBasis"
        )
    return _stable_id(
        "structure-source-span",
        source_ref.model_dump(mode="json"),
        start_offset,
        end_offset,
        boundary_basis.value,
    )


def stable_bilingual_source_span_id(
    *,
    source_ref: StructureSourceRef,
    start_offset: int,
    end_offset: int,
    language_evidence: BilingualLanguageEvidence,
    boundary_basis: BilingualSpanBoundaryBasis,
) -> str:
    if not isinstance(source_ref, StructureSourceRef):
        raise TypeError("source_ref must be a StructureSourceRef")
    if (
        not isinstance(start_offset, int)
        or isinstance(start_offset, bool)
        or not isinstance(end_offset, int)
        or isinstance(end_offset, bool)
        or start_offset < 0
        or end_offset <= start_offset
    ):
        raise ValueError("bilingual span offsets must form a non-empty interval")
    if not isinstance(language_evidence, BilingualLanguageEvidence):
        raise TypeError("language_evidence must be a BilingualLanguageEvidence")
    if not isinstance(boundary_basis, BilingualSpanBoundaryBasis):
        raise TypeError("boundary_basis must be a BilingualSpanBoundaryBasis")
    return _stable_id(
        "bilingual-span",
        source_ref.model_dump(mode="json"),
        start_offset,
        end_offset,
        language_evidence.value,
        boundary_basis.value,
    )


def stable_document_language_policy_id(
    *,
    english_declaration_spans: tuple[BilingualSourceSpan, ...],
    chinese_declaration_spans: tuple[BilingualSourceSpan, ...],
    equal_validity: bool | None,
    conflict_precedence: str,
    authority_basis: str = "DECLARED_DOCUMENT_POLICY",
    policy_scope_source_ir_id: str | None = None,
) -> str:
    if authority_basis == "UNKNOWN":
        if (
            english_declaration_spans
            or chinese_declaration_spans
            or equal_validity is not None
            or conflict_precedence != "UNKNOWN"
            or not isinstance(policy_scope_source_ir_id, str)
            or not policy_scope_source_ir_id.strip()
        ):
            raise ValueError(
                "unknown language policy identity requires only a SourceIR scope"
            )
        return _stable_id(
            "document-language-policy",
            authority_basis,
            policy_scope_source_ir_id,
        )
    if authority_basis != "DECLARED_DOCUMENT_POLICY":
        raise ValueError("unsupported document language authority basis")
    if policy_scope_source_ir_id is not None:
        raise ValueError("declared language policy identity cannot use a scope")
    if not english_declaration_spans or not chinese_declaration_spans:
        raise ValueError("language policy identity requires both declaration sides")
    if equal_validity is not True or conflict_precedence != "zh-CN":
        raise ValueError("only equal validity with zh-CN precedence is supported")
    return _stable_id(
        "document-language-policy",
        [span.model_dump(mode="json") for span in english_declaration_spans],
        [span.model_dump(mode="json") for span in chinese_declaration_spans],
        equal_validity,
        conflict_precedence,
    )


def stable_bilingual_counterpart_group_id(
    *,
    document_language_policy_id: str,
    canonical_structure_node_id: str,
    member_structure_node_ids: tuple[str, ...],
    english_spans: tuple[BilingualSourceSpan, ...],
    chinese_spans: tuple[BilingualSourceSpan, ...],
) -> str:
    _require_non_blank(
        document_language_policy_id,
        "document_language_policy_id",
    )
    _require_non_blank(
        canonical_structure_node_id,
        "canonical_structure_node_id",
    )
    if not member_structure_node_ids or not english_spans or not chinese_spans:
        raise ValueError("bilingual group identity requires members and both sides")
    return _stable_id(
        "bilingual-group",
        document_language_policy_id,
        canonical_structure_node_id,
        member_structure_node_ids,
        [span.model_dump(mode="json") for span in english_spans],
        [span.model_dump(mode="json") for span in chinese_spans],
    )


def stable_bilingual_mismatch_signal_id(
    *,
    counterpart_group_id: str,
    english_span_ids: tuple[str, ...],
    chinese_span_ids: tuple[str, ...],
) -> str:
    _require_non_blank(counterpart_group_id, "counterpart_group_id")
    if not english_span_ids or not chinese_span_ids:
        raise ValueError("mismatch identity requires grounding on both sides")
    return _stable_id(
        "bilingual-mismatch",
        counterpart_group_id,
        english_span_ids,
        chinese_span_ids,
    )


def stable_structure_node_id(
    *,
    source_ir_id: str,
    resolver_id: str,
    resolver_version: str,
    kind: StructureNodeKind,
    identity_key: str,
) -> str:
    _require_non_blank(source_ir_id, "source_ir_id")
    _require_non_blank(resolver_id, "resolver_id")
    _require_non_blank(resolver_version, "resolver_version")
    _require_non_blank(identity_key, "identity_key")
    if not isinstance(kind, StructureNodeKind):
        raise TypeError("kind must be a StructureNodeKind value")
    return _stable_id(
        "structure-node",
        source_ir_id,
        resolver_id,
        resolver_version,
        kind.value,
        identity_key,
    )


def stable_structure_relation_id(
    *,
    source_ir_id: str,
    resolver_id: str,
    resolver_version: str,
    kind: StructureRelationKind,
    source_structure_node_id: str,
    target_structure_node_id: str,
    source_refs: tuple[StructureSourceRef, ...] = (),
    option_state: StructureOptionState | None = None,
) -> str:
    _require_non_blank(source_ir_id, "source_ir_id")
    _require_non_blank(resolver_id, "resolver_id")
    _require_non_blank(resolver_version, "resolver_version")
    _require_non_blank(source_structure_node_id, "source_structure_node_id")
    _require_non_blank(target_structure_node_id, "target_structure_node_id")
    if not isinstance(kind, StructureRelationKind):
        raise TypeError("kind must be a StructureRelationKind value")
    if kind is StructureRelationKind.OPTION_STATE:
        if not isinstance(option_state, StructureOptionState):
            raise TypeError(
                "option_state relations require a StructureOptionState value"
            )
        identity_parts: tuple[object, ...] = (
            kind.value,
            source_structure_node_id,
            target_structure_node_id,
            [source_ref.model_dump(mode="json") for source_ref in source_refs],
            option_state.value,
        )
    else:
        if option_state is not None:
            raise TypeError("option_state is valid only for option_state relations")
        identity_parts = (
            kind.value,
            source_structure_node_id,
            target_structure_node_id,
            [source_ref.model_dump(mode="json") for source_ref in source_refs],
        )
    return _stable_id(
        "structure-relation",
        source_ir_id,
        resolver_id,
        resolver_version,
        *identity_parts,
    )


def stable_structure_finding_id(
    *,
    source_ir_id: str,
    resolver_id: str,
    resolver_version: str,
    code: str,
    identity_key: str,
) -> str:
    _require_non_blank(source_ir_id, "source_ir_id")
    _require_non_blank(resolver_id, "resolver_id")
    _require_non_blank(resolver_version, "resolver_version")
    _require_non_blank(code, "code")
    _require_non_blank(identity_key, "identity_key")
    return _stable_id(
        "structure-finding",
        source_ir_id,
        resolver_id,
        resolver_version,
        code,
        identity_key,
    )


def _require_acyclic_parent_graph(nodes_by_id: dict[str, StructureNode]) -> None:
    completed: set[str] = set()
    for start_id in nodes_by_id:
        if start_id in completed:
            continue
        path: set[str] = set()
        node_id: str | None = start_id
        while node_id is not None and node_id not in completed:
            if node_id in path:
                raise ValueError("StructureTree parent graph must be acyclic")
            path.add(node_id)
            node_id = nodes_by_id[node_id].parent_structure_node_id
        completed.update(path)


def _stable_id(prefix: str, *parts: object) -> str:
    payload = json.dumps(
        parts,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f"{prefix}-{digest}"


def _require_non_blank(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must not be blank")


__all__ = [
    "STRUCTURE_TREE_SCHEMA_VERSION",
    "BilingualCounterpartGroup",
    "BilingualLanguageEvidence",
    "BilingualMismatchSignal",
    "BilingualSourceSpan",
    "BilingualSpanBoundaryBasis",
    "DocumentLanguagePolicy",
    "StructureBoundaryBasis",
    "StructureFinding",
    "StructureFindingSeverity",
    "StructureNode",
    "StructureNodeKind",
    "StructureOptionState",
    "StructureRelation",
    "StructureRelationKind",
    "StructureSourceRef",
    "StructureSourceRefKind",
    "StructureSourceSpan",
    "StructureSourceSpanBoundaryBasis",
    "StructureTreeSnapshot",
    "VisualTableCellTextBinding",
    "stable_bilingual_counterpart_group_id",
    "stable_bilingual_mismatch_signal_id",
    "stable_bilingual_source_span_id",
    "stable_document_language_policy_id",
    "stable_structure_finding_id",
    "stable_structure_node_id",
    "stable_structure_relation_id",
    "stable_structure_source_span_id",
    "stable_structure_snapshot_id",
]

from __future__ import annotations

import hashlib
import json
import re
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from reqpilotx_server.domain.extraction.llm_requirement_review import LlmReviewSourceSpan


TABLE_EVIDENCE_BINDING_SCHEMA_VERSION = "table-evidence-binding/v1"
REQUIREMENT_EVIDENCE_BINDING_SCHEMA_VERSION = "requirement-evidence-binding/v1"
REQUIREMENT_EVIDENCE_BUNDLE_SCHEMA_VERSION = "requirement-evidence-bundle/v1"

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_TABLE_BINDING_ID_PATTERN = re.compile(r"^table-evidence-binding-[0-9a-f]{32}$")
_REQUIREMENT_BINDING_ID_PATTERN = re.compile(
    r"^requirement-evidence-binding-[0-9a-f]{32}$"
)


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class EvidenceReferenceKind(str, Enum):
    """Kinds of small, resolvable evidence references kept outside SourceIR."""

    TABLE = "TABLE"
    CONTENT_BLOCK = "CONTENT_BLOCK"
    HEADER = "HEADER"
    SHARED_VALUE = "SHARED_VALUE"
    NOTE = "NOTE"
    IMAGE = "IMAGE"


class EvidenceReferenceV1(_StrictFrozenModel):
    """A content-addressed pointer, not a copy of the referenced source object."""

    kind: EvidenceReferenceKind
    reference_id: str = Field(min_length=1, max_length=260)
    resolver_key: str = Field(min_length=1, max_length=1000)
    content_sha256: str
    page_number: int | None = Field(default=None, ge=1)
    row_start: int | None = Field(default=None, ge=0)
    row_end: int | None = Field(default=None, ge=0)
    column_start: int | None = Field(default=None, ge=0)
    column_end: int | None = Field(default=None, ge=0)

    @field_validator("reference_id", "resolver_key")
    @classmethod
    def reject_blank_reference_identity(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("evidence reference identities cannot be blank")
        return value

    @field_validator("content_sha256")
    @classmethod
    def validate_content_sha256(cls, value: str) -> str:
        if not _SHA256_PATTERN.fullmatch(value):
            raise ValueError("evidence references require a lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def validate_ranges(self) -> EvidenceReferenceV1:
        if bool(self.row_start is None) != bool(self.row_end is None):
            raise ValueError("evidence row range must record both endpoints")
        if bool(self.column_start is None) != bool(self.column_end is None):
            raise ValueError("evidence column range must record both endpoints")
        if (
            self.row_start is not None
            and self.row_end is not None
            and self.row_end < self.row_start
        ):
            raise ValueError("evidence row_end cannot precede row_start")
        if (
            self.column_start is not None
            and self.column_end is not None
            and self.column_end < self.column_start
        ):
            raise ValueError("evidence column_end cannot precede column_start")
        return self


class RequirementEvidenceSourceRefV1(_StrictFrozenModel):
    """Content-free pointer back to an immutable source fact.

    Requirement publication supports both the semantic-unit spans produced by
    the LLM workflow and the direct SourceIR references used by the local
    structure projection.  This small contract preserves the latter without
    copying customer text into the Requirement Set snapshot.
    """

    kind: str = Field(min_length=1, max_length=120)
    source_id: str = Field(min_length=1, max_length=260)
    owner_structure_node_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=260,
    )
    start_offset: int | None = Field(default=None, ge=0)
    end_offset: int | None = Field(default=None, ge=0)
    page_start: int | None = Field(default=None, ge=1)
    page_end: int | None = Field(default=None, ge=1)

    @field_validator("kind", "source_id", "owner_structure_node_id")
    @classmethod
    def reject_blank_identity(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError(
                "requirement source reference identities cannot be blank"
            )
        return value

    @model_validator(mode="after")
    def validate_ranges(self) -> RequirementEvidenceSourceRefV1:
        if bool(self.start_offset is None) != bool(self.end_offset is None):
            raise ValueError("requirement source range requires both offsets")
        if (
            self.start_offset is not None
            and self.end_offset is not None
            and self.end_offset < self.start_offset
        ):
            raise ValueError(
                "requirement source end_offset cannot precede start_offset"
            )
        if bool(self.page_start is None) != bool(self.page_end is None):
            raise ValueError(
                "requirement source page range requires both endpoints"
            )
        if (
            self.page_start is not None
            and self.page_end is not None
            and self.page_end < self.page_start
        ):
            raise ValueError(
                "requirement source page_end cannot precede page_start"
            )
        return self


class TableEvidenceBindingV1(_StrictFrozenModel):
    """Frozen evidence for one whole table or one derived table record.

    ``record_id=None`` means the immutable whole-table projection. A record is
    evidence only: it intentionally carries no requirement or atom identity.
    """

    schema_version: Literal["table-evidence-binding/v1"] = (
        TABLE_EVIDENCE_BINDING_SCHEMA_VERSION
    )
    binding_id: str
    logical_table_id: str = Field(min_length=1, max_length=260)
    source_table_ids: tuple[str, ...]
    projection_id: str = Field(min_length=1, max_length=260)
    projection_revision: int = Field(ge=0)
    projection_sha256: str
    record_id: str | None = Field(default=None, min_length=1, max_length=260)
    source_spans: tuple[LlmReviewSourceSpan, ...] = ()
    table_refs: tuple[EvidenceReferenceV1, ...]
    content_block_refs: tuple[EvidenceReferenceV1, ...] = ()
    header_refs: tuple[EvidenceReferenceV1, ...] = ()
    shared_value_refs: tuple[EvidenceReferenceV1, ...] = ()
    note_refs: tuple[EvidenceReferenceV1, ...] = ()
    image_refs: tuple[EvidenceReferenceV1, ...] = ()
    evidence_sha256: str

    @classmethod
    def create(
        cls,
        *,
        logical_table_id: str,
        source_table_ids: tuple[str, ...],
        projection_id: str,
        projection_revision: int,
        projection_sha256: str,
        record_id: str | None = None,
        source_spans: tuple[LlmReviewSourceSpan, ...] = (),
        table_refs: tuple[EvidenceReferenceV1, ...],
        content_block_refs: tuple[EvidenceReferenceV1, ...] = (),
        header_refs: tuple[EvidenceReferenceV1, ...] = (),
        shared_value_refs: tuple[EvidenceReferenceV1, ...] = (),
        note_refs: tuple[EvidenceReferenceV1, ...] = (),
        image_refs: tuple[EvidenceReferenceV1, ...] = (),
    ) -> TableEvidenceBindingV1:
        evidence_sha256 = stable_table_evidence_sha256(
            logical_table_id=logical_table_id,
            source_table_ids=source_table_ids,
            projection_id=projection_id,
            projection_revision=projection_revision,
            projection_sha256=projection_sha256,
            record_id=record_id,
            source_spans=source_spans,
            table_refs=table_refs,
            content_block_refs=content_block_refs,
            header_refs=header_refs,
            shared_value_refs=shared_value_refs,
            note_refs=note_refs,
            image_refs=image_refs,
        )
        return cls(
            binding_id=stable_table_evidence_binding_id(evidence_sha256),
            logical_table_id=logical_table_id,
            source_table_ids=source_table_ids,
            projection_id=projection_id,
            projection_revision=projection_revision,
            projection_sha256=projection_sha256,
            record_id=record_id,
            source_spans=source_spans,
            table_refs=table_refs,
            content_block_refs=content_block_refs,
            header_refs=header_refs,
            shared_value_refs=shared_value_refs,
            note_refs=note_refs,
            image_refs=image_refs,
            evidence_sha256=evidence_sha256,
        )

    @field_validator("binding_id")
    @classmethod
    def validate_binding_id(cls, value: str) -> str:
        if not _TABLE_BINDING_ID_PATTERN.fullmatch(value):
            raise ValueError("table evidence binding identity is invalid")
        return value

    @field_validator("projection_sha256", "evidence_sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        if not _SHA256_PATTERN.fullmatch(value):
            raise ValueError("table evidence hashes must be lowercase SHA-256")
        return value

    @field_validator("logical_table_id", "projection_id", "record_id")
    @classmethod
    def reject_blank_identity(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("table evidence identities cannot be blank")
        return value

    @model_validator(mode="after")
    def validate_binding(self) -> TableEvidenceBindingV1:
        if not self.source_table_ids:
            raise ValueError("table evidence requires at least one source table")
        if len(self.source_table_ids) != len(set(self.source_table_ids)):
            raise ValueError("table evidence source table identities must be unique")
        if not self.table_refs:
            raise ValueError("table evidence requires a resolvable table reference")
        _require_reference_kinds(
            self.table_refs,
            {EvidenceReferenceKind.TABLE},
            "table_refs",
        )
        _require_reference_kinds(
            self.content_block_refs,
            {EvidenceReferenceKind.CONTENT_BLOCK},
            "content_block_refs",
        )
        _require_reference_kinds(
            self.header_refs,
            {EvidenceReferenceKind.HEADER},
            "header_refs",
        )
        _require_reference_kinds(
            self.shared_value_refs,
            {EvidenceReferenceKind.SHARED_VALUE},
            "shared_value_refs",
        )
        _require_reference_kinds(
            self.note_refs,
            {EvidenceReferenceKind.NOTE},
            "note_refs",
        )
        _require_reference_kinds(
            self.image_refs,
            {EvidenceReferenceKind.IMAGE},
            "image_refs",
        )
        for name, refs in (
            ("table_refs", self.table_refs),
            ("content_block_refs", self.content_block_refs),
            ("header_refs", self.header_refs),
            ("shared_value_refs", self.shared_value_refs),
            ("note_refs", self.note_refs),
            ("image_refs", self.image_refs),
        ):
            _require_unique_references(refs, name)
        if len(self.source_spans) != len(set(self.source_spans)):
            raise ValueError("table evidence source spans must be unique")

        expected_sha256 = stable_table_evidence_sha256(
            logical_table_id=self.logical_table_id,
            source_table_ids=self.source_table_ids,
            projection_id=self.projection_id,
            projection_revision=self.projection_revision,
            projection_sha256=self.projection_sha256,
            record_id=self.record_id,
            source_spans=self.source_spans,
            table_refs=self.table_refs,
            content_block_refs=self.content_block_refs,
            header_refs=self.header_refs,
            shared_value_refs=self.shared_value_refs,
            note_refs=self.note_refs,
            image_refs=self.image_refs,
        )
        if self.evidence_sha256 != expected_sha256:
            raise ValueError(
                "table evidence SHA-256 does not match its immutable references"
            )
        if self.binding_id != stable_table_evidence_binding_id(expected_sha256):
            raise ValueError("table evidence binding ID does not match its evidence")
        return self


class RequirementEvidenceBindingV1(_StrictFrozenModel):
    """Source evidence for one requirement entry or one atom.

    A table-backed requirement points to a separate table evidence binding.
    The table record never embeds requirement identities, which permits one
    record to yield zero, one, or many atoms without changing its identity.
    """

    schema_version: Literal["requirement-evidence-binding/v1"] = (
        REQUIREMENT_EVIDENCE_BINDING_SCHEMA_VERSION
    )
    binding_id: str
    requirement_entry_id: str = Field(min_length=1, max_length=260)
    atomic_requirement_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=260,
    )
    source_spans: tuple[LlmReviewSourceSpan, ...] = ()
    source_refs: tuple[RequirementEvidenceSourceRefV1, ...] = ()
    table_evidence_binding_id: str | None = Field(default=None, max_length=260)
    evidence_sha256: str

    @classmethod
    def create(
        cls,
        *,
        requirement_entry_id: str,
        atomic_requirement_id: str | None = None,
        source_spans: tuple[LlmReviewSourceSpan, ...] = (),
        source_refs: tuple[RequirementEvidenceSourceRefV1, ...] = (),
        table_evidence_binding_id: str | None = None,
    ) -> RequirementEvidenceBindingV1:
        evidence_sha256 = stable_requirement_evidence_sha256(
            requirement_entry_id=requirement_entry_id,
            atomic_requirement_id=atomic_requirement_id,
            source_spans=source_spans,
            source_refs=source_refs,
            table_evidence_binding_id=table_evidence_binding_id,
        )
        return cls(
            binding_id=stable_requirement_evidence_binding_id(evidence_sha256),
            requirement_entry_id=requirement_entry_id,
            atomic_requirement_id=atomic_requirement_id,
            source_spans=source_spans,
            source_refs=source_refs,
            table_evidence_binding_id=table_evidence_binding_id,
            evidence_sha256=evidence_sha256,
        )

    @field_validator("binding_id")
    @classmethod
    def validate_binding_id(cls, value: str) -> str:
        if not _REQUIREMENT_BINDING_ID_PATTERN.fullmatch(value):
            raise ValueError("requirement evidence binding identity is invalid")
        return value

    @field_validator("evidence_sha256")
    @classmethod
    def validate_evidence_sha256(cls, value: str) -> str:
        if not _SHA256_PATTERN.fullmatch(value):
            raise ValueError("requirement evidence requires a lowercase SHA-256")
        return value

    @field_validator(
        "requirement_entry_id",
        "atomic_requirement_id",
        "table_evidence_binding_id",
    )
    @classmethod
    def reject_blank_identity(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("requirement evidence identities cannot be blank")
        return value

    @model_validator(mode="after")
    def validate_binding(self) -> RequirementEvidenceBindingV1:
        if (
            not self.source_spans
            and not self.source_refs
            and self.table_evidence_binding_id is None
        ):
            raise ValueError(
                "requirement evidence requires source spans, source refs, "
                "or a table binding"
            )
        if len(self.source_spans) != len(set(self.source_spans)):
            raise ValueError("requirement evidence source spans must be unique")
        source_ref_keys = tuple(
            json.dumps(
                item.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            for item in self.source_refs
        )
        if len(source_ref_keys) != len(set(source_ref_keys)):
            raise ValueError("requirement evidence source refs must be unique")
        if (
            self.table_evidence_binding_id is not None
            and not _TABLE_BINDING_ID_PATTERN.fullmatch(
                self.table_evidence_binding_id
            )
        ):
            raise ValueError(
                "requirement evidence table pointer must use a table binding ID"
            )

        expected_sha256 = stable_requirement_evidence_sha256(
            requirement_entry_id=self.requirement_entry_id,
            atomic_requirement_id=self.atomic_requirement_id,
            source_spans=self.source_spans,
            source_refs=self.source_refs,
            table_evidence_binding_id=self.table_evidence_binding_id,
        )
        if self.evidence_sha256 != expected_sha256:
            raise ValueError(
                "requirement evidence SHA-256 does not match its immutable binding"
            )
        if self.binding_id != stable_requirement_evidence_binding_id(expected_sha256):
            raise ValueError(
                "requirement evidence binding ID does not match its evidence"
            )
        return self


class RequirementEvidenceBundleV1(_StrictFrozenModel):
    """Requirement Set evidence with tables and requirements stored separately."""

    schema_version: Literal["requirement-evidence-bundle/v1"] = (
        REQUIREMENT_EVIDENCE_BUNDLE_SCHEMA_VERSION
    )
    source_ir_id: str = Field(min_length=1, max_length=260)
    source_sha256: str
    structure_snapshot_id: str = Field(min_length=1, max_length=260)
    table_bindings: tuple[TableEvidenceBindingV1, ...] = ()
    requirement_bindings: tuple[RequirementEvidenceBindingV1, ...] = ()
    evidence_sha256: str

    @classmethod
    def create(
        cls,
        *,
        source_ir_id: str,
        source_sha256: str,
        structure_snapshot_id: str,
        table_bindings: tuple[TableEvidenceBindingV1, ...] = (),
        requirement_bindings: tuple[RequirementEvidenceBindingV1, ...] = (),
    ) -> RequirementEvidenceBundleV1:
        evidence_sha256 = stable_requirement_evidence_bundle_sha256(
            source_ir_id=source_ir_id,
            source_sha256=source_sha256,
            structure_snapshot_id=structure_snapshot_id,
            table_bindings=table_bindings,
            requirement_bindings=requirement_bindings,
        )
        return cls(
            source_ir_id=source_ir_id,
            source_sha256=source_sha256,
            structure_snapshot_id=structure_snapshot_id,
            table_bindings=table_bindings,
            requirement_bindings=requirement_bindings,
            evidence_sha256=evidence_sha256,
        )

    @field_validator("source_sha256", "evidence_sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        if not _SHA256_PATTERN.fullmatch(value):
            raise ValueError("evidence bundle hashes must be lowercase SHA-256")
        return value

    @field_validator("source_ir_id", "structure_snapshot_id")
    @classmethod
    def reject_blank_identity(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("evidence bundle identities cannot be blank")
        return value

    @model_validator(mode="after")
    def validate_bundle(self) -> RequirementEvidenceBundleV1:
        table_by_id = {item.binding_id: item for item in self.table_bindings}
        if len(table_by_id) != len(self.table_bindings):
            raise ValueError("evidence bundle table binding IDs must be unique")
        requirement_by_id = {
            item.binding_id: item for item in self.requirement_bindings
        }
        if len(requirement_by_id) != len(self.requirement_bindings):
            raise ValueError("evidence bundle requirement binding IDs must be unique")

        table_record_keys = {
            (
                item.logical_table_id,
                item.projection_sha256,
                item.record_id,
            )
            for item in self.table_bindings
        }
        if len(table_record_keys) != len(self.table_bindings):
            raise ValueError(
                "a table projection record can have only one evidence binding"
            )
        requirement_targets = {
            (item.requirement_entry_id, item.atomic_requirement_id)
            for item in self.requirement_bindings
        }
        if len(requirement_targets) != len(self.requirement_bindings):
            raise ValueError(
                "a requirement or atom can have only one evidence binding"
            )
        for requirement in self.requirement_bindings:
            table_binding_id = requirement.table_evidence_binding_id
            if table_binding_id is not None and table_binding_id not in table_by_id:
                raise ValueError(
                    "requirement evidence points to an unknown table binding"
                )

        expected_sha256 = stable_requirement_evidence_bundle_sha256(
            source_ir_id=self.source_ir_id,
            source_sha256=self.source_sha256,
            structure_snapshot_id=self.structure_snapshot_id,
            table_bindings=self.table_bindings,
            requirement_bindings=self.requirement_bindings,
        )
        if self.evidence_sha256 != expected_sha256:
            raise ValueError(
                "evidence bundle SHA-256 does not match its immutable bindings"
            )
        return self


def stable_table_evidence_sha256(
    *,
    logical_table_id: str,
    source_table_ids: tuple[str, ...],
    projection_id: str,
    projection_revision: int,
    projection_sha256: str,
    record_id: str | None,
    source_spans: tuple[LlmReviewSourceSpan, ...],
    table_refs: tuple[EvidenceReferenceV1, ...],
    content_block_refs: tuple[EvidenceReferenceV1, ...],
    header_refs: tuple[EvidenceReferenceV1, ...],
    shared_value_refs: tuple[EvidenceReferenceV1, ...],
    note_refs: tuple[EvidenceReferenceV1, ...],
    image_refs: tuple[EvidenceReferenceV1, ...],
) -> str:
    return _sha256(
        {
            "content_block_refs": _dump_models(content_block_refs),
            "header_refs": _dump_models(header_refs),
            "image_refs": _dump_models(image_refs),
            "logical_table_id": logical_table_id,
            "note_refs": _dump_models(note_refs),
            "projection_id": projection_id,
            "projection_revision": projection_revision,
            "projection_sha256": projection_sha256,
            "record_id": record_id,
            "shared_value_refs": _dump_models(shared_value_refs),
            "source_spans": _dump_models(source_spans),
            "source_table_ids": list(source_table_ids),
            "table_refs": _dump_models(table_refs),
        }
    )


def stable_table_evidence_binding_id(evidence_sha256: str) -> str:
    if not _SHA256_PATTERN.fullmatch(evidence_sha256):
        raise ValueError("table evidence identity requires a lowercase SHA-256")
    return f"table-evidence-binding-{evidence_sha256[:32]}"


def stable_requirement_evidence_sha256(
    *,
    requirement_entry_id: str,
    atomic_requirement_id: str | None,
    source_spans: tuple[LlmReviewSourceSpan, ...],
    source_refs: tuple[RequirementEvidenceSourceRefV1, ...] = (),
    table_evidence_binding_id: str | None,
) -> str:
    return _sha256(
        {
            "atomic_requirement_id": atomic_requirement_id,
            "requirement_entry_id": requirement_entry_id,
            "source_refs": _dump_models(source_refs),
            "source_spans": _dump_models(source_spans),
            "table_evidence_binding_id": table_evidence_binding_id,
        }
    )


def stable_requirement_evidence_binding_id(evidence_sha256: str) -> str:
    if not _SHA256_PATTERN.fullmatch(evidence_sha256):
        raise ValueError("requirement evidence identity requires a lowercase SHA-256")
    return f"requirement-evidence-binding-{evidence_sha256[:32]}"


def stable_requirement_evidence_bundle_sha256(
    *,
    source_ir_id: str,
    source_sha256: str,
    structure_snapshot_id: str,
    table_bindings: tuple[TableEvidenceBindingV1, ...],
    requirement_bindings: tuple[RequirementEvidenceBindingV1, ...],
) -> str:
    return _sha256(
        {
            "requirement_bindings": _dump_models(requirement_bindings),
            "source_ir_id": source_ir_id,
            "source_sha256": source_sha256,
            "structure_snapshot_id": structure_snapshot_id,
            "table_bindings": _dump_models(table_bindings),
        }
    )


def canonical_requirement_evidence_bundle_bytes(
    bundle: RequirementEvidenceBundleV1,
) -> bytes:
    return _canonical_json_bytes(bundle.model_dump(mode="json"))


def canonical_requirement_evidence_bundle_sha256(
    bundle: RequirementEvidenceBundleV1,
) -> str:
    return hashlib.sha256(canonical_requirement_evidence_bundle_bytes(bundle)).hexdigest()


def _require_reference_kinds(
    refs: tuple[EvidenceReferenceV1, ...],
    expected: set[EvidenceReferenceKind],
    field_name: str,
) -> None:
    if any(item.kind not in expected for item in refs):
        expected_values = ", ".join(sorted(item.value for item in expected))
        raise ValueError(f"{field_name} accepts only {expected_values} references")


def _require_unique_references(
    refs: tuple[EvidenceReferenceV1, ...],
    field_name: str,
) -> None:
    identities = tuple((item.kind, item.reference_id) for item in refs)
    if len(identities) != len(set(identities)):
        raise ValueError(f"{field_name} references must be unique")


def _dump_models(models: tuple[BaseModel, ...]) -> list[object]:
    return [item.model_dump(mode="json") for item in models]


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


__all__ = [
    "REQUIREMENT_EVIDENCE_BINDING_SCHEMA_VERSION",
    "REQUIREMENT_EVIDENCE_BUNDLE_SCHEMA_VERSION",
    "TABLE_EVIDENCE_BINDING_SCHEMA_VERSION",
    "EvidenceReferenceKind",
    "EvidenceReferenceV1",
    "RequirementEvidenceBindingV1",
    "RequirementEvidenceBundleV1",
    "RequirementEvidenceSourceRefV1",
    "TableEvidenceBindingV1",
    "canonical_requirement_evidence_bundle_bytes",
    "canonical_requirement_evidence_bundle_sha256",
    "stable_requirement_evidence_binding_id",
    "stable_requirement_evidence_bundle_sha256",
    "stable_requirement_evidence_sha256",
    "stable_table_evidence_binding_id",
    "stable_table_evidence_sha256",
]

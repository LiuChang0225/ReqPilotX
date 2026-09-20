from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

_SEMANTIC_ACTIVATION_SCHEMA_VERSION = (
    "requirement-set-semantic-activation-request/v1"
)
_LOCK_REQUEST_SCHEMA_VERSION = "requirement-set-lock-request/v1"


class ActivateRequirementSetRequest(BaseModel):
    """Optional strict CAS preconditions for semantic W4 activation.

    An empty body keeps the historical activate/restore API behavior. Once any
    semantic precondition is supplied, the complete versioned tuple is
    required so a partial request can never silently degrade to legacy
    activation.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: str = Field(default="", max_length=96)
    expected_active_requirement_set_id: str = Field(default="", max_length=200)
    expected_enrichment_publication_binding_id: str = Field(
        default="", max_length=200
    )
    expected_enrichment_publication_binding_sha256: str = Field(
        default="", max_length=64
    )

    @model_validator(mode="after")
    def validate_semantic_preconditions(self) -> Self:
        expected_field_names = {
            "expected_active_requirement_set_id",
            "expected_enrichment_publication_binding_id",
            "expected_enrichment_publication_binding_sha256",
        }
        expected_values = (
            self.expected_active_requirement_set_id,
            self.expected_enrichment_publication_binding_id,
            self.expected_enrichment_publication_binding_sha256,
        )
        supplied_expected_fields = expected_field_names.intersection(
            self.model_fields_set
        )
        if not self.schema_version and not supplied_expected_fields:
            return self
        if self.schema_version != _SEMANTIC_ACTIVATION_SCHEMA_VERSION:
            raise ValueError("unsupported Requirement Set activation schema")
        if not expected_field_names.issubset(self.model_fields_set):
            raise ValueError(
                "semantic Requirement Set activation preconditions are incomplete"
            )
        if (
            not self.expected_enrichment_publication_binding_id
            or not self.expected_enrichment_publication_binding_sha256
        ):
            raise ValueError(
                "semantic Requirement Set activation binding is incomplete"
            )
        if any(value != value.strip() for value in expected_values):
            raise ValueError(
                "semantic Requirement Set activation identities must be canonical"
            )
        digest = self.expected_enrichment_publication_binding_sha256
        if len(digest) != 64 or any(
            character not in "0123456789abcdef" for character in digest
        ):
            raise ValueError(
                "expected enrichment publication binding SHA-256 is invalid"
            )
        return self


class LockExtractionRequest(BaseModel):
    """Lock one reviewed Matrix, optionally with a strict readiness CAS."""

    waiver_reason: str = Field(default="", max_length=2000)
    requirement_set_id: str = Field(min_length=1, max_length=200)
    schema_version: str = Field(default="", max_length=96)
    expected_readiness_sha256: str = Field(default="", max_length=64)

    @model_validator(mode="after")
    def validate_readiness_precondition(self) -> Self:
        strict_fields = {"schema_version", "expected_readiness_sha256"}
        if not strict_fields.intersection(self.model_fields_set):
            return self
        if self.schema_version != _LOCK_REQUEST_SCHEMA_VERSION:
            raise ValueError("unsupported Requirement Set lock schema")
        if "expected_readiness_sha256" not in self.model_fields_set:
            raise ValueError("strict Requirement Set lock readiness is incomplete")
        digest = self.expected_readiness_sha256
        if (
            digest != digest.strip()
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError("expected lock readiness SHA-256 is invalid")
        return self


class ReviewedRequirementEntry(BaseModel):
    requirement_set_id: str = Field(min_length=1, max_length=200)
    entry_id: str | None = None
    req_code: str | None = None
    entry_kind: str | None = None
    section_code: str | None = None
    section_title: str | None = None
    section_label: str | None = None
    section_key: str | None = None
    section_instance_key: str | None = None
    source_page: int | None = None
    source_page_end: int | None = None
    source_anchor: str | None = None
    requirement_text: str | None = None
    display_text: str | None = None
    raw_text: str | None = None
    requirement_type: str | None = None
    priority: str | None = None
    text_relation: str | None = None
    is_deduped: bool | None = None
    heading_text: str | None = None
    parent_section_code: str | None = None
    parent_section_title: str | None = None
    parent_section_label: str | None = None
    owner_section_code: str | None = None
    owner_section_title: str | None = None
    owner_section_label: str | None = None
    structural_parent_section_code: str | None = None
    structural_parent_section_key: str | None = None
    structural_parent_section_title: str | None = None
    structural_parent_section_label: str | None = None
    merged_from_req_codes: list[str] | None = None
    merged_from_entry_ids: list[str] | None = None
    dedupe_version: str | None = None
    table_refs: list[dict] | None = None
    content_blocks: list[dict] | None = None
    has_table_asset: bool | None = None
    table_render_mode: str | None = None
    complex_table_mode: bool | None = None
    complex_zone_id: str | None = None
    complex_family: str | None = None
    complex_role: str | None = None
    complex_group_label: str | None = None
    complex_split_level: str | None = None
    complex_warnings: list[str] | None = None
    complex_row_policy: str | None = None
    keep: bool = True


class ApplyReviewedExtractionRequest(BaseModel):
    entries: list[ReviewedRequirementEntry]


__all__ = [
    "ActivateRequirementSetRequest",
    "ApplyReviewedExtractionRequest",
    "LockExtractionRequest",
    "ReviewedRequirementEntry",
]

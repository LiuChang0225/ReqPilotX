from __future__ import annotations

from enum import StrEnum
import hashlib
import json
import re
from typing import Annotated, Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from reqpilotx_server.application.agent_workflow.extraction_runtime import AgentKitIdentityV1


DOCUMENT_MAP_SURVEY_CANDIDATE_SCHEMA_VERSION = (
    "document-map-survey-candidate/v1"
)
DOCUMENT_MAP_VISUAL_COVERAGE_RECEIPT_SCHEMA_VERSION = (
    "document-map-visual-coverage-receipt/v1"
)
DOCUMENT_MAP_SURVEY_COMPILATION_SCHEMA_VERSION = (
    "document-map-survey-compilation/v1"
)
DocumentMapPageEvidenceRef = Annotated[
    str,
    Field(
        pattern=r"^page:[1-9][0-9]{0,2}$",
        description="Exact PDF page reference using page:N, for example page:14.",
    ),
]


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class DocumentMapSurveyStatus(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"


class DocumentMapSurveyRangeKind(StrEnum):
    FRONT_MATTER = "FRONT_MATTER"
    MAIN_DOCUMENT = "MAIN_DOCUMENT"
    ATTACHMENT = "ATTACHMENT"
    TABLE_PACKAGE = "TABLE_PACKAGE"
    REFERENCE_MATERIAL = "REFERENCE_MATERIAL"
    UNRESOLVED = "UNRESOLVED"


class DocumentMapVisualCoverageMode(StrEnum):
    REFERENCE_FULL_PAGE = "REFERENCE_FULL_PAGE"
    PRODUCT_ADAPTIVE = "PRODUCT_ADAPTIVE"


class DocumentMapSurveyFindingV1(_StrictFrozenModel):
    finding_code: str = Field(
        min_length=1,
        max_length=120,
        description="Content-free snake_case observation code.",
    )
    page_numbers: tuple[int, ...] = Field(min_length=1, max_length=250)
    evidence_refs: tuple[DocumentMapPageEvidenceRef, ...] = Field(
        min_length=1,
        max_length=512,
    )
    public_summary: str = Field(min_length=1, max_length=1000)

    @field_validator("page_numbers")
    @classmethod
    def validate_pages(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if value != tuple(sorted(set(value))) or any(page < 1 for page in value):
            raise ValueError("survey finding pages must be positive, unique, and sorted")
        return value

    @field_validator("finding_code", mode="before")
    @classmethod
    def normalize_finding_code(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("survey finding code must be text")
        normalized = value.strip().casefold()
        if re.fullmatch(r"[a-z][a-z0-9_]{0,119}", normalized) is None:
            raise ValueError("survey finding code must be snake_case")
        return normalized

    @field_validator("evidence_refs")
    @classmethod
    def validate_evidence_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        pages = tuple(_page_ref_number(item) for item in value)
        if any(page is None for page in pages) or pages != tuple(
            sorted(set(pages))
        ):
            raise ValueError(
                "survey finding evidence refs must use unique numeric page order"
            )
        return value

    @field_validator("public_summary")
    @classmethod
    def normalize_public_summary(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or "\x00" in normalized:
            raise ValueError("survey finding summary must be plain text")
        return normalized


class DocumentMapSurveyRangeCandidateV1(_StrictFrozenModel):
    proposal_key: str = Field(pattern=r"^[a-z][a-z0-9._:-]{0,119}$")
    kind: DocumentMapSurveyRangeKind
    label: str = Field(min_length=1, max_length=300)
    page_numbers: tuple[int, ...] = Field(min_length=1, max_length=250)
    evidence_refs: tuple[DocumentMapPageEvidenceRef, ...] = Field(
        min_length=1,
        max_length=2048,
    )
    public_summary: str = Field(min_length=1, max_length=1000)

    @field_validator("page_numbers")
    @classmethod
    def validate_pages(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if value != tuple(range(value[0], value[-1] + 1)):
            raise ValueError("survey ranges must use one contiguous page interval")
        return value

    @field_validator("evidence_refs")
    @classmethod
    def validate_evidence_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        pages = tuple(_page_ref_number(item) for item in value)
        if any(page is None for page in pages) or pages != tuple(
            sorted(set(pages))
        ):
            raise ValueError(
                "survey range evidence refs must use unique numeric page order"
            )
        return value

    @field_validator("label", "public_summary")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or "\x00" in normalized:
            raise ValueError("survey range text must be non-empty plain text")
        return normalized

    @model_validator(mode="after")
    def validate_evidence_pages(self) -> DocumentMapSurveyRangeCandidateV1:
        referenced_pages = {
            page
            for item in self.evidence_refs
            if (page := _page_ref_number(item)) is not None
        }
        if not referenced_pages.issubset(set(self.page_numbers)):
            raise ValueError("survey range evidence ref leaves its page range")
        return self


class DocumentMapSurveyCandidateV1(_StrictFrozenModel):
    schema_version: Literal["document-map-survey-candidate/v1"] = (
        DOCUMENT_MAP_SURVEY_CANDIDATE_SCHEMA_VERSION
    )
    run_id: str = Field(pattern=r"^agent-run-[0-9a-f]{32}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    kit_identity: AgentKitIdentityV1
    page_count: int = Field(ge=1, le=250)
    status: DocumentMapSurveyStatus
    document_summary: str = Field(min_length=1, max_length=3000)
    ranges: tuple[DocumentMapSurveyRangeCandidateV1, ...] = Field(
        min_length=1,
        max_length=250,
    )
    findings: tuple[DocumentMapSurveyFindingV1, ...] = Field(
        default=(),
        max_length=256,
    )

    @field_validator("document_summary")
    @classmethod
    def normalize_summary(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or "\x00" in normalized:
            raise ValueError("document summary must be non-empty plain text")
        return normalized

    @model_validator(mode="after")
    def validate_partition(self) -> DocumentMapSurveyCandidateV1:
        keys = tuple(item.proposal_key for item in self.ranges)
        if len(keys) != len(set(keys)):
            raise ValueError("survey proposal keys must be unique")
        observed_pages = tuple(
            page for item in self.ranges for page in item.page_numbers
        )
        expected_pages = tuple(range(1, self.page_count + 1))
        if observed_pages != expected_pages:
            raise ValueError(
                "survey ranges must partition every page exactly once in source order"
            )
        unresolved = any(
            item.kind is DocumentMapSurveyRangeKind.UNRESOLVED
            for item in self.ranges
        )
        if (self.status is DocumentMapSurveyStatus.PARTIAL) != unresolved:
            raise ValueError("survey status must disclose unresolved page ranges")
        if any(
            page > self.page_count
            for finding in self.findings
            for page in finding.page_numbers
        ):
            raise ValueError("survey finding page exceeds the document")
        for finding in self.findings:
            if {
                page
                for item in finding.evidence_refs
                if (page := _page_ref_number(item)) is not None
            } != set(finding.page_numbers):
                raise ValueError(
                    "survey finding evidence refs must match its declared pages"
                )
        finding_codes = tuple(item.finding_code for item in self.findings)
        if len(finding_codes) != len(set(finding_codes)):
            raise ValueError("survey finding codes must be unique")
        return self

    @property
    def candidate_sha256(self) -> str:
        return _canonical_sha256(self.model_dump(mode="json"))


class DocumentMapVisualCoverageReceiptV1(_StrictFrozenModel):
    schema_version: Literal["document-map-visual-coverage-receipt/v1"] = (
        DOCUMENT_MAP_VISUAL_COVERAGE_RECEIPT_SCHEMA_VERSION
    )
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_count: int = Field(ge=1, le=250)
    mode: DocumentMapVisualCoverageMode
    contact_sheet_opened: bool
    high_resolution_page_numbers: tuple[int, ...] = Field(
        default=(),
        max_length=250,
    )
    other_image_view_count: int = Field(ge=0)
    event_summary_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("high_resolution_page_numbers")
    @classmethod
    def validate_pages(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if value != tuple(sorted(set(value))) or any(page < 1 for page in value):
            raise ValueError("visual coverage pages must be positive, unique, and sorted")
        return value

    @model_validator(mode="after")
    def validate_receipt(self) -> DocumentMapVisualCoverageReceiptV1:
        if any(page > self.page_count for page in self.high_resolution_page_numbers):
            raise ValueError("visual coverage page exceeds the document")
        if not self.contact_sheet_opened:
            raise ValueError("document survey requires an opened contact sheet")
        if self.mode is DocumentMapVisualCoverageMode.REFERENCE_FULL_PAGE and (
            self.high_resolution_page_numbers
            != tuple(range(1, self.page_count + 1))
        ):
            raise ValueError(
                "reference parity requires the contact sheet and every high-resolution page"
            )
        expected = _canonical_sha256(
            self.model_dump(mode="json", exclude={"receipt_sha256"})
        )
        if self.receipt_sha256 != expected:
            raise ValueError("visual coverage receipt SHA drifted")
        return self


class DocumentMapSurveyScopeV1(_StrictFrozenModel):
    scope_id: str = Field(pattern=r"^document-map-scope-[0-9a-f]{32}$")
    scope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    proposal_key: str = Field(pattern=r"^[a-z][a-z0-9._:-]{0,119}$")
    kind: DocumentMapSurveyRangeKind
    label: str = Field(min_length=1, max_length=300)
    page_numbers: tuple[int, ...] = Field(min_length=1, max_length=250)
    evidence_refs: tuple[DocumentMapPageEvidenceRef, ...] = Field(
        min_length=1,
        max_length=2048,
    )
    public_summary: str = Field(min_length=1, max_length=1000)


class DocumentMapSurveyCompilationV1(_StrictFrozenModel):
    schema_version: Literal["document-map-survey-compilation/v1"] = (
        DOCUMENT_MAP_SURVEY_COMPILATION_SCHEMA_VERSION
    )
    run_id: str = Field(pattern=r"^agent-run-[0-9a-f]{32}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    kit_identity: AgentKitIdentityV1
    page_count: int = Field(ge=1, le=250)
    status: DocumentMapSurveyStatus
    document_summary: str = Field(min_length=1, max_length=3000)
    candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    visual_coverage_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scopes: tuple[DocumentMapSurveyScopeV1, ...] = Field(
        min_length=1,
        max_length=250,
    )
    unresolved_scope_ids: tuple[str, ...] = Field(default=(), max_length=250)
    findings: tuple[DocumentMapSurveyFindingV1, ...] = Field(
        default=(),
        max_length=256,
    )
    compilation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_compilation(self) -> DocumentMapSurveyCompilationV1:
        expected_unresolved = tuple(
            item.scope_id
            for item in self.scopes
            if item.kind is DocumentMapSurveyRangeKind.UNRESOLVED
        )
        if self.unresolved_scope_ids != expected_unresolved:
            raise ValueError("survey unresolved scope identities drifted")
        expected = _canonical_sha256(
            self.model_dump(mode="json", exclude={"compilation_sha256"})
        )
        if self.compilation_sha256 != expected:
            raise ValueError("survey compilation SHA drifted")
        return self


def build_document_map_visual_coverage_receipt(
    *,
    source_sha256: str,
    page_count: int,
    mode: DocumentMapVisualCoverageMode,
    event_summary: Mapping[str, Any],
) -> DocumentMapVisualCoverageReceiptV1:
    visual = event_summary.get("visual_inspection")
    if not isinstance(visual, Mapping):
        raise ValueError("canonical Agent activity has no visual inspection receipt")
    pages = visual.get("high_resolution_page_numbers")
    if not isinstance(pages, (list, tuple)) or any(
        isinstance(page, bool) or not isinstance(page, int) for page in pages
    ):
        raise ValueError("canonical visual page receipt is invalid")
    values = {
        "source_sha256": source_sha256,
        "page_count": page_count,
        "mode": mode,
        "contact_sheet_opened": visual.get("contact_sheet_opened") is True,
        "high_resolution_page_numbers": tuple(pages),
        "other_image_view_count": int(visual.get("other_image_view_count") or 0),
        "event_summary_sha256": _canonical_sha256(event_summary),
    }
    draft = DocumentMapVisualCoverageReceiptV1.model_construct(
        **values,
        receipt_sha256="0" * 64,
    )
    return DocumentMapVisualCoverageReceiptV1(
        **values,
        receipt_sha256=_canonical_sha256(
            draft.model_dump(mode="json", exclude={"receipt_sha256"})
        ),
    )


def compile_document_map_survey(
    candidate: DocumentMapSurveyCandidateV1,
    *,
    visual_coverage: DocumentMapVisualCoverageReceiptV1,
    expected_run_id: str,
    expected_source_sha256: str,
    expected_kit_identity: AgentKitIdentityV1,
) -> DocumentMapSurveyCompilationV1:
    candidate = DocumentMapSurveyCandidateV1.model_validate(
        candidate.model_dump(mode="json")
    )
    visual = DocumentMapVisualCoverageReceiptV1.model_validate(
        visual_coverage.model_dump(mode="json")
    )
    kit = AgentKitIdentityV1.model_validate(
        expected_kit_identity.model_dump(mode="json")
    )
    if (
        candidate.run_id != expected_run_id
        or candidate.source_sha256 != expected_source_sha256
        or candidate.kit_identity != kit
        or visual.source_sha256 != candidate.source_sha256
        or visual.page_count != candidate.page_count
    ):
        raise ValueError("document-map survey lineage drifted")
    if visual.mode is DocumentMapVisualCoverageMode.PRODUCT_ADAPTIVE and any(
        not set(item.page_numbers).intersection(visual.high_resolution_page_numbers)
        for item in candidate.ranges
    ):
        raise ValueError(
            "adaptive survey requires a high-resolution observation in every range"
        )
    scopes = tuple(
        _compile_scope(candidate.source_sha256, item) for item in candidate.ranges
    )
    values = {
        "run_id": candidate.run_id,
        "source_sha256": candidate.source_sha256,
        "kit_identity": candidate.kit_identity,
        "page_count": candidate.page_count,
        "status": candidate.status,
        "document_summary": candidate.document_summary,
        "candidate_sha256": candidate.candidate_sha256,
        "visual_coverage_receipt_sha256": visual.receipt_sha256,
        "scopes": scopes,
        "unresolved_scope_ids": tuple(
            item.scope_id
            for item in scopes
            if item.kind is DocumentMapSurveyRangeKind.UNRESOLVED
        ),
        "findings": tuple(
            sorted(
                candidate.findings,
                key=lambda item: (
                    item.page_numbers[0],
                    item.page_numbers[-1],
                    item.finding_code,
                ),
            )
        ),
    }
    draft = DocumentMapSurveyCompilationV1.model_construct(
        **values,
        compilation_sha256="0" * 64,
    )
    return DocumentMapSurveyCompilationV1(
        **values,
        compilation_sha256=_canonical_sha256(
            draft.model_dump(mode="json", exclude={"compilation_sha256"})
        ),
    )


def _compile_scope(
    source_sha256: str,
    candidate: DocumentMapSurveyRangeCandidateV1,
) -> DocumentMapSurveyScopeV1:
    identity = {
        "source_sha256": source_sha256,
        "kind": candidate.kind.value,
        "page_numbers": list(candidate.page_numbers),
    }
    scope_id = "document-map-scope-" + _canonical_sha256(identity)[:32]
    values = {
        "scope_id": scope_id,
        "proposal_key": candidate.proposal_key,
        "kind": candidate.kind,
        "label": candidate.label,
        "page_numbers": candidate.page_numbers,
        "evidence_refs": candidate.evidence_refs,
        "public_summary": candidate.public_summary,
    }
    return DocumentMapSurveyScopeV1(
        **values,
        scope_sha256=_canonical_sha256(values),
    )


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=_json_default,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _json_default(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    raise TypeError(f"Unsupported canonical JSON value: {type(value)!r}")


def _page_ref_number(value: str) -> int | None:
    if not isinstance(value, str) or not value.startswith("page:"):
        return None
    suffix = value[5:]
    if not suffix.isdigit() or suffix.startswith("0"):
        return None
    page = int(suffix)
    return page if 1 <= page <= 250 else None


__all__ = [
    "DOCUMENT_MAP_SURVEY_CANDIDATE_SCHEMA_VERSION",
    "DOCUMENT_MAP_SURVEY_COMPILATION_SCHEMA_VERSION",
    "DOCUMENT_MAP_VISUAL_COVERAGE_RECEIPT_SCHEMA_VERSION",
    "DocumentMapSurveyCandidateV1",
    "DocumentMapSurveyCompilationV1",
    "DocumentMapSurveyFindingV1",
    "DocumentMapSurveyRangeCandidateV1",
    "DocumentMapSurveyRangeKind",
    "DocumentMapSurveyScopeV1",
    "DocumentMapSurveyStatus",
    "DocumentMapPageEvidenceRef",
    "DocumentMapVisualCoverageMode",
    "DocumentMapVisualCoverageReceiptV1",
    "build_document_map_visual_coverage_receipt",
    "compile_document_map_survey",
]

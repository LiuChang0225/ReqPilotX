"""Host-frozen direct-text boundaries and resumable A/B/F windows.

This module deliberately keeps semantic boundary selection separate from
classification.  Boundary turns work on one immutable text scope at a time;
the Host validates Unicode exact-cover, freezes stable record keys, and only
then creates physical classification windows containing whole records.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    ValidationError,
    field_validator,
    model_validator,
)

from app.adapters.agents.codex_app_server.native_source_item_text_reference import (
    SourceItemTextBatchCandidateV1,
)
from app.adapters.agents.codex_app_server.native_source_requirement_windowed_reference import (
    SourceRequirementClassificationDecisionV1,
)
from app.application.agent_workflow.runner_output_schema import (
    compile_strict_runner_output_schema,
)
from app.application.agent_workflow.source_requirement_baseline import (
    DocumentMapArtifactV1,
    SourceItemStructureOnlySpanV1,
    SourceItemTextBoundaryCandidateV1,
    SourceItemTextScopeRequestV1,
    SourceItemTextSliceCandidateV1,
    SourceItemTextSpanV1,
    SourceRecordForm,
    SourceRequirementBaselineError,
    _is_structure_only_title,
    canonical_json_sha256,
    validate_source_item_text_slice_candidate,
)


NATIVE_SOURCE_ITEM_TEXT_WINDOWED_RUNNER_VERSION = "1.1.0"
BOUNDARY_AGENT_SCHEMA_VERSION = "source-item-text-boundary-agent-candidate/v1"
BOUNDARY_PLAN_SCHEMA_VERSION = "source-item-text-boundary-plan/v1"
CLASSIFICATION_WINDOW_SCHEMA_VERSION = "source-item-text-classification-window/v1"
CLASSIFICATION_INPUT_SCHEMA_VERSION = (
    "source-item-text-classification-window-input/v1"
)
CLASSIFICATION_AGENT_SCHEMA_VERSION = (
    "source-item-text-classification-window-candidate/v1"
)
WINDOWED_CHECKPOINT_SCHEMA_VERSION = (
    "native-source-item-text-windowed-checkpoint/v1"
)
MAX_CLASSIFICATION_WINDOWS = 128


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


def source_item_text_request_set_sha256(
    requests: tuple[SourceItemTextScopeRequestV1, ...],
    *,
    execution_context: Mapping[str, object] | None = None,
) -> str:
    """Bind optional private revision guidance without changing ordinary runs."""

    request_payload = [item.model_dump(mode="json") for item in requests]
    if execution_context is None:
        return canonical_json_sha256(request_payload)
    return canonical_json_sha256(
        {
            "schema_version": "source-item-text-execution-binding/v1",
            "requests": request_payload,
            "execution_context": dict(execution_context),
        }
    )


def _machine_codes(
    values: tuple[str, ...],
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if not values and allow_empty:
        return values
    if not values or values != tuple(sorted(set(values))):
        raise ValueError("machine codes must be non-empty, unique, and sorted")
    if any(
        not value
        or len(value) > 160
        or not value[0].islower()
        or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_" for character in value)
        for value in values
    ):
        raise ValueError("machine codes must be lowercase snake_case")
    return values


class SourceItemTextBoundaryAgentRecordV1(_StrictFrozenModel):
    """One semantic record proposed without a machine key or A/B/F."""

    label: str = Field(min_length=1, max_length=1000)
    source_form: SourceRecordForm
    text_spans: tuple[SourceItemTextSpanV1, ...] = Field(
        min_length=1,
        max_length=4096,
    )
    shared_source_content_ids: tuple[str, ...] = Field(default=(), max_length=256)

    @model_validator(mode="after")
    def validate_text_form(self) -> SourceItemTextBoundaryAgentRecordV1:
        if self.source_form not in {
            SourceRecordForm.NUMBERED_CLAUSE,
            SourceRecordForm.NATIVE_LIST,
            SourceRecordForm.PARAGRAPH,
        }:
            raise ValueError("direct-text boundaries require a textual source form")
        if len(self.shared_source_content_ids) != len(
            set(self.shared_source_content_ids)
        ):
            raise ValueError("shared source content IDs must be unique")
        return self


class SourceItemTextBoundaryAgentStructureOnlySpanV1(_StrictFrozenModel):
    """A title span; the Host supplies the validated disposition reason."""

    text_span: SourceItemTextSpanV1


class SourceItemTextBoundaryAgentCandidateV1(_StrictFrozenModel):
    """Agent output for exactly one direct-text scope; intentionally no A/B/F."""

    schema_version: Literal[
        "source-item-text-boundary-agent-candidate/v1"
    ] = BOUNDARY_AGENT_SCHEMA_VERSION
    scope_id: str = Field(pattern=r"^text:[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
    records: tuple[SourceItemTextBoundaryAgentRecordV1, ...] = Field(
        default=(),
        max_length=8192,
    )
    structure_only_spans: tuple[
        SourceItemTextBoundaryAgentStructureOnlySpanV1, ...
    ] = Field(
        default=(),
        max_length=4096,
    )

    @model_validator(mode="after")
    def validate_disposition(self) -> SourceItemTextBoundaryAgentCandidateV1:
        if not self.records and not self.structure_only_spans:
            raise ValueError("text boundary candidate must disposition its scope")
        return self


class SourceItemTextBoundaryPlanRecordV1(_StrictFrozenModel):
    record_key: str = Field(pattern=r"^text-record-[0-9a-f]{32}$")
    label: str = Field(min_length=1, max_length=1000)
    source_form: SourceRecordForm
    text_spans: tuple[SourceItemTextSpanV1, ...] = Field(
        min_length=1,
        max_length=4096,
    )
    shared_source_content_ids: tuple[str, ...] = Field(default=(), max_length=256)


class SourceItemTextBoundaryPlanV1(_StrictFrozenModel):
    """Host-validated, immutable semantic boundary for one text scope."""

    schema_version: Literal["source-item-text-boundary-plan/v1"] = (
        BOUNDARY_PLAN_SCHEMA_VERSION
    )
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    document_map_artifact_id: str = Field(pattern=r"^document-map-[0-9a-f]{32}$")
    document_map_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope_id: str = Field(pattern=r"^text:[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
    records: tuple[SourceItemTextBoundaryPlanRecordV1, ...] = Field(
        default=(),
        max_length=8192,
    )
    structure_only_spans: tuple[SourceItemStructureOnlySpanV1, ...] = Field(
        default=(),
        max_length=4096,
    )
    boundary_identity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_plan(self) -> SourceItemTextBoundaryPlanV1:
        keys = tuple(item.record_key for item in self.records)
        if len(keys) != len(set(keys)):
            raise ValueError("text boundary-plan record keys must be unique")
        if not self.records and not self.structure_only_spans:
            raise ValueError("text boundary plan must disposition its scope")
        expected = canonical_json_sha256(
            self.model_dump(
                mode="json",
                exclude={"boundary_identity_sha256", "plan_sha256"},
            )
        )
        if self.plan_sha256 != expected:
            raise ValueError("text boundary-plan SHA drifted")
        if self.boundary_identity_sha256 != _boundary_identity_sha256(self):
            raise ValueError("text boundary semantic identity drifted")
        return self


class SourceItemTextClassificationAgentCandidateV1(_StrictFrozenModel):
    """Text-window wrapper reusing the canonical source-requirement A/B/F decision."""

    schema_version: Literal[
        "source-item-text-classification-window-candidate/v1"
    ] = CLASSIFICATION_AGENT_SCHEMA_VERSION
    decisions: tuple[SourceRequirementClassificationDecisionV1, ...] = Field(
        min_length=1,
        max_length=8192,
    )
    diagnostic_codes: tuple[str, ...] = Field(default=(), max_length=64)

    @field_validator("diagnostic_codes")
    @classmethod
    def validate_diagnostics(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _machine_codes(value, allow_empty=True)

    @model_validator(mode="after")
    def validate_candidate(self) -> SourceItemTextClassificationAgentCandidateV1:
        keys = tuple(item.record_key for item in self.decisions)
        if len(keys) != len(set(keys)):
            raise ValueError("text classification record keys must be unique")
        return self


class SourceItemTextClassificationWindowV1(_StrictFrozenModel):
    schema_version: Literal["source-item-text-classification-window/v1"] = (
        CLASSIFICATION_WINDOW_SCHEMA_VERSION
    )
    window_id: str = Field(pattern=r"^text-window-[0-9]{2,}$")
    boundary_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    record_keys: tuple[str, ...] = Field(min_length=1, max_length=8192)
    scope_ids: tuple[str, ...] = Field(min_length=1, max_length=128)
    estimated_character_count: StrictInt = Field(ge=1)
    estimated_weight: StrictInt = Field(ge=1)
    risk_signals: tuple[str, ...] = Field(default=(), max_length=64)

    @field_validator("risk_signals")
    @classmethod
    def validate_risks(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _machine_codes(value, allow_empty=True)

    @model_validator(mode="after")
    def validate_window(self) -> SourceItemTextClassificationWindowV1:
        if len(self.record_keys) != len(set(self.record_keys)):
            raise ValueError("text classification window keys must be unique")
        if len(self.scope_ids) != len(set(self.scope_ids)):
            raise ValueError("text classification window scopes must be unique")
        return self


@dataclass(frozen=True, slots=True)
class TextBoundaryRunResult:
    plan: SourceItemTextBoundaryPlanV1
    candidate: SourceItemTextBoundaryAgentCandidateV1
    audit_summaries: tuple[str, ...]
    attempt_efforts: tuple[str, ...]
    raw_candidate_bytes: int
    elapsed_milliseconds: int


@dataclass(frozen=True, slots=True)
class TextClassificationWindowRunResult:
    window: SourceItemTextClassificationWindowV1
    decisions: tuple[SourceRequirementClassificationDecisionV1, ...]
    audit_summaries: tuple[str, ...]
    attempt_efforts: tuple[str, ...]
    raw_candidate_bytes: int
    elapsed_milliseconds: int
    input_payload: dict[str, object]
    diagnostic_codes: tuple[str, ...]


class NativeSourceItemTextBoundaryScopeCheckpointV1(_StrictFrozenModel):
    schema_version: Literal[
        "native-source-item-text-boundary-scope-checkpoint/v1"
    ] = "native-source-item-text-boundary-scope-checkpoint/v1"
    plan: SourceItemTextBoundaryPlanV1
    candidate: SourceItemTextBoundaryAgentCandidateV1
    audit_summaries: tuple[str, ...] = Field(default=(), max_length=2)
    attempt_efforts: tuple[str, ...] = Field(min_length=1, max_length=2)
    raw_candidate_bytes: StrictInt = Field(ge=1)
    elapsed_milliseconds: StrictInt = Field(ge=0)
    checkpoint_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_checkpoint(
        self,
    ) -> NativeSourceItemTextBoundaryScopeCheckpointV1:
        if self.candidate.scope_id != self.plan.scope_id:
            raise ValueError("text boundary checkpoint scope drifted")
        expected = canonical_json_sha256(
            self.model_dump(mode="json", exclude={"checkpoint_sha256"})
        )
        if self.checkpoint_sha256 != expected:
            raise ValueError("text boundary checkpoint SHA drifted")
        return self


class NativeSourceItemTextWindowResultCheckpointV1(_StrictFrozenModel):
    schema_version: Literal[
        "native-source-item-text-window-result-checkpoint/v1"
    ] = "native-source-item-text-window-result-checkpoint/v1"
    window: SourceItemTextClassificationWindowV1
    decisions: tuple[SourceRequirementClassificationDecisionV1, ...] = Field(
        min_length=1,
        max_length=8192,
    )
    audit_summaries: tuple[str, ...] = Field(default=(), max_length=2)
    attempt_efforts: tuple[str, ...] = Field(min_length=1, max_length=2)
    raw_candidate_bytes: StrictInt = Field(ge=1)
    elapsed_milliseconds: StrictInt = Field(ge=0)
    input_payload: dict[str, object]
    diagnostic_codes: tuple[str, ...] = Field(default=(), max_length=64)
    checkpoint_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_checkpoint(
        self,
    ) -> NativeSourceItemTextWindowResultCheckpointV1:
        if tuple(item.record_key for item in self.decisions) != self.window.record_keys:
            raise ValueError("text window checkpoint record order drifted")
        expected = canonical_json_sha256(
            self.model_dump(mode="json", exclude={"checkpoint_sha256"})
        )
        if self.checkpoint_sha256 != expected:
            raise ValueError("text window checkpoint SHA drifted")
        return self


class NativeSourceItemTextWindowedCheckpointV1(_StrictFrozenModel):
    """Cumulative private state for all text boundaries and classification windows."""

    schema_version: Literal["native-source-item-text-windowed-checkpoint/v1"] = (
        WINDOWED_CHECKPOINT_SCHEMA_VERSION
    )
    content_bearing: Literal[True] = True
    private_object_store_only: Literal[True] = True
    runner_version: str = Field(min_length=1, max_length=80)
    executor_id: str = Field(min_length=1, max_length=120)
    executor_version: str = Field(min_length=1, max_length=80)
    transport_kind: str = Field(min_length=1, max_length=120)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    document_map_artifact_id: str = Field(pattern=r"^document-map-[0-9a-f]{32}$")
    document_map_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope_ids: tuple[str, ...] = Field(min_length=1, max_length=128)
    model_id: str = Field(min_length=1, max_length=200)
    requested_reasoning_effort: str = Field(min_length=1, max_length=20)
    codex_version: str = Field(min_length=1, max_length=200)
    kit_version: str = Field(min_length=1, max_length=80)
    kit_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    visual_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    visual_page_numbers: tuple[StrictInt, ...] = Field(default=(), max_length=4096)
    accepted_boundaries: tuple[
        NativeSourceItemTextBoundaryScopeCheckpointV1, ...
    ] = Field(default=(), max_length=128)
    classification_windows_planned: bool = False
    classification_windows: tuple[SourceItemTextClassificationWindowV1, ...] = Field(
        default=(),
        max_length=128,
    )
    accepted_windows: tuple[NativeSourceItemTextWindowResultCheckpointV1, ...] = (
        Field(default=(), max_length=128)
    )
    boundary_elapsed_milliseconds: StrictInt = Field(ge=0)
    classification_elapsed_milliseconds: StrictInt = Field(ge=0)
    checkpoint_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_checkpoint(self) -> NativeSourceItemTextWindowedCheckpointV1:
        if len(self.scope_ids) != len(set(self.scope_ids)):
            raise ValueError("text checkpoint scope IDs must be unique")
        if self.visual_page_numbers != tuple(sorted(set(self.visual_page_numbers))):
            raise ValueError("text checkpoint visual pages must be unique and sorted")
        boundary_ids = tuple(item.plan.scope_id for item in self.accepted_boundaries)
        if (
            len(boundary_ids) != len(set(boundary_ids))
            or boundary_ids
            != tuple(item for item in self.scope_ids if item in set(boundary_ids))
        ):
            raise ValueError("text checkpoint boundary scopes drifted")
        if any(
            item.plan.source_sha256 != self.source_sha256
            or item.plan.document_map_artifact_id != self.document_map_artifact_id
            or item.plan.document_map_artifact_sha256
            != self.document_map_artifact_sha256
            for item in self.accepted_boundaries
        ):
            raise ValueError("text checkpoint boundary lineage drifted")
        window_ids = tuple(item.window_id for item in self.classification_windows)
        if len(window_ids) != len(set(window_ids)):
            raise ValueError("text checkpoint window IDs must be unique")
        accepted_window_ids = tuple(
            item.window.window_id for item in self.accepted_windows
        )
        expected_windows = {
            item.window_id: item for item in self.classification_windows
        }
        if (
            len(accepted_window_ids) != len(set(accepted_window_ids))
            or accepted_window_ids
            != tuple(
                item for item in window_ids if item in set(accepted_window_ids)
            )
            or any(
                expected_windows.get(item.window.window_id) != item.window
                for item in self.accepted_windows
            )
        ):
            raise ValueError("text checkpoint accepted windows drifted")
        if not self.classification_windows_planned and (
            self.classification_windows or self.accepted_windows
        ):
            raise ValueError("unplanned text classification windows are invalid")
        if self.classification_windows_planned and set(boundary_ids) != set(
            self.scope_ids
        ):
            raise ValueError("text classification planning requires all boundaries")
        expected = canonical_json_sha256(
            self.model_dump(mode="json", exclude={"checkpoint_sha256"})
        )
        if self.checkpoint_sha256 != expected:
            raise ValueError("text windowed checkpoint SHA drifted")
        return self


def compile_source_item_text_boundary_plan(
    *,
    request: SourceItemTextScopeRequestV1,
    candidate: SourceItemTextBoundaryAgentCandidateV1,
) -> SourceItemTextBoundaryPlanV1:
    """Normalize exact spans, sign stable keys, and validate Host exact-cover."""

    if candidate.scope_id != request.scope_id:
        raise SourceRequirementBaselineError("SOURCE_ITEM_TEXT_BOUNDARY_SCOPE_DRIFT")
    content_order = {
        item.content_id: index for index, item in enumerate(request.source_content)
    }
    available_content_ids = set(content_order)
    records: list[SourceItemTextBoundaryPlanRecordV1] = []
    for item in candidate.records:
        spans = _canonical_spans(item.text_spans, content_order=content_order)
        if not set(item.shared_source_content_ids).issubset(available_content_ids):
            raise SourceRequirementBaselineError("SOURCE_ITEM_SHARED_CONTENT_UNKNOWN")
        owned_content_ids = {span.content_id for span in spans}
        if owned_content_ids.intersection(item.shared_source_content_ids):
            raise SourceRequirementBaselineError(
                "SOURCE_ITEM_TEXT_SHARED_CONTENT_OVERLAPS_OWNED_SPAN"
            )
        record_key = _host_record_key(
            scope_id=request.scope_id,
            source_form=item.source_form,
            spans=spans,
        )
        records.append(
            SourceItemTextBoundaryPlanRecordV1(
                record_key=record_key,
                label=item.label,
                source_form=item.source_form,
                text_spans=spans,
                shared_source_content_ids=tuple(item.shared_source_content_ids),
            )
        )
    records.sort(
        key=lambda item: (
            content_order.get(item.text_spans[0].content_id, len(content_order)),
            item.text_spans[0].start_offset,
            item.text_spans[0].end_offset,
            item.record_key,
        )
    )
    structure_only_spans = tuple(
        sorted(
            (
                SourceItemStructureOnlySpanV1(
                    text_span=item.text_span,
                    reason_codes=("visible_structure_title",),
                )
                for item in candidate.structure_only_spans
            ),
            key=lambda item: (
                content_order.get(
                    item.text_span.content_id,
                    len(content_order),
                ),
                item.text_span.start_offset,
                item.text_span.end_offset,
            ),
        )
    )
    values = {
        "source_sha256": request.source_sha256,
        "document_map_artifact_id": request.document_map_artifact_id,
        "document_map_artifact_sha256": request.document_map_artifact_sha256,
        "request_sha256": request.request_sha256,
        "scope_id": request.scope_id,
        "records": tuple(records),
        "structure_only_spans": structure_only_spans,
    }
    draft = SourceItemTextBoundaryPlanV1.model_construct(
        **values,
        boundary_identity_sha256="0" * 64,
        plan_sha256="0" * 64,
    )
    plan = SourceItemTextBoundaryPlanV1(
        **values,
        boundary_identity_sha256=_boundary_identity_sha256(draft),
        plan_sha256=canonical_json_sha256(
            draft.model_dump(
                mode="json",
                exclude={"boundary_identity_sha256", "plan_sha256"},
            )
        ),
    )
    validate_source_item_text_boundary_plan(request=request, plan=plan)
    return plan


def validate_source_item_text_boundary_plan(
    *,
    request: SourceItemTextScopeRequestV1,
    plan: SourceItemTextBoundaryPlanV1,
) -> None:
    """Validate Unicode code-point exact-cover and title disposition without A/B/F."""

    if (
        plan.source_sha256 != request.source_sha256
        or plan.document_map_artifact_id != request.document_map_artifact_id
        or plan.document_map_artifact_sha256
        != request.document_map_artifact_sha256
        or plan.request_sha256 != request.request_sha256
        or plan.scope_id != request.scope_id
    ):
        raise SourceRequirementBaselineError("SOURCE_ITEM_TEXT_BOUNDARY_LINEAGE_DRIFT")
    content_by_id = {item.content_id: item for item in request.source_content}
    content_order = {
        item.content_id: index for index, item in enumerate(request.source_content)
    }
    assignments: dict[tuple[str, int], str] = {}

    def assign(
        span: SourceItemTextSpanV1,
        owner: str,
        *,
        structure_only: bool,
    ) -> None:
        content = content_by_id.get(span.content_id)
        if content is None:
            raise SourceRequirementBaselineError("SOURCE_ITEM_TEXT_CONTENT_UNKNOWN")
        if span.end_offset > len(content.text):
            raise SourceRequirementBaselineError("SOURCE_ITEM_TEXT_SPAN_OUT_OF_RANGE")
        selected = content.text[span.start_offset : span.end_offset]
        if not selected.strip():
            raise SourceRequirementBaselineError("SOURCE_ITEM_TEXT_SPAN_EMPTY")
        is_title = _is_structure_only_title(
            request=request,
            content=content,
            selected=selected,
        )
        if structure_only and not is_title:
            raise SourceRequirementBaselineError(
                "SOURCE_ITEM_STRUCTURE_ONLY_SPAN_NOT_TITLE"
            )
        if not structure_only and is_title:
            raise SourceRequirementBaselineError(
                "SOURCE_ITEM_TEXT_TITLE_MUST_BE_STRUCTURE_ONLY"
            )
        for offset in range(span.start_offset, span.end_offset):
            if content.text[offset].isspace():
                continue
            key = (span.content_id, offset)
            if key in assignments:
                raise SourceRequirementBaselineError("SOURCE_ITEM_TEXT_SPAN_OVERLAP")
            assignments[key] = owner

    for record in plan.records:
        positions = tuple(
            (content_order.get(span.content_id, len(content_order)), span.start_offset)
            for span in record.text_spans
        )
        if positions != tuple(sorted(positions)):
            raise SourceRequirementBaselineError("SOURCE_ITEM_TEXT_SPAN_ORDER_INVALID")
        if not set(record.shared_source_content_ids).issubset(content_by_id):
            raise SourceRequirementBaselineError("SOURCE_ITEM_SHARED_CONTENT_UNKNOWN")
        owned_content_ids = {span.content_id for span in record.text_spans}
        if owned_content_ids.intersection(record.shared_source_content_ids):
            raise SourceRequirementBaselineError(
                "SOURCE_ITEM_TEXT_SHARED_CONTENT_OVERLAPS_OWNED_SPAN"
            )
        for span in record.text_spans:
            assign(span, f"record:{record.record_key}", structure_only=False)
    for index, item in enumerate(plan.structure_only_spans):
        assign(item.text_span, f"structure:{index}", structure_only=True)
    expected = {
        (content.content_id, offset)
        for content in request.source_content
        for offset, character in enumerate(content.text)
        if not character.isspace()
    }
    if set(assignments) != expected:
        raise SourceRequirementBaselineError("SOURCE_ITEM_TEXT_COVERAGE_INCOMPLETE")


def build_source_item_text_classification_windows(
    *,
    document_map: DocumentMapArtifactV1,
    requests: tuple[SourceItemTextScopeRequestV1, ...],
    plans: tuple[SourceItemTextBoundaryPlanV1, ...],
    max_concurrency: int = 2,
    max_window_characters: int = 12_000,
    max_window_records: int = 48,
) -> tuple[SourceItemTextClassificationWindowV1, ...]:
    """Create physical windows after every semantic scope boundary is frozen."""

    if max_concurrency < 1 or max_concurrency > 2:
        raise ValueError("source-item text classification concurrency must be 1..2")
    if max_window_characters < 1000 or max_window_records < 1:
        raise ValueError("source-item text classification window budget is invalid")
    request_by_scope = {item.scope_id: item for item in requests}
    plan_by_scope = {item.scope_id: item for item in plans}
    if tuple(plan_by_scope) != tuple(request_by_scope):
        raise ValueError("source-item text boundary set is incomplete or reordered")
    flattened: list[
        tuple[
            SourceItemTextBoundaryPlanRecordV1,
            SourceItemTextScopeRequestV1,
            int,
            int,
            tuple[str, ...],
        ]
    ] = []
    for request in requests:
        plan = plan_by_scope[request.scope_id]
        validate_source_item_text_boundary_plan(request=request, plan=plan)
        for record in plan.records:
            characters = _record_character_count(record, request=request)
            risks = _record_risk_signals(
                record=record,
                request=request,
                document_map=document_map,
            )
            weight = max(1, characters + 1000 * len(risks))
            flattened.append((record, request, characters, weight, risks))
    if not flattened:
        return ()
    weights = tuple(item[3] for item in flattened)
    groups: list[list[int]] = []
    current: list[int] = []
    current_characters = 0
    for record_index, item in enumerate(flattened):
        record_characters = item[2]
        if current and (
            len(current) >= max_window_records
            or current_characters + record_characters > max_window_characters
        ):
            groups.append(current)
            current = []
            current_characters = 0
        if record_characters > max_window_characters:
            # A semantic record is indivisible. It may exceed the physical
            # character budget only when isolated in its own window.
            groups.append([record_index])
            continue
        current.append(record_index)
        current_characters += record_characters
    if current:
        groups.append(current)
    if len(groups) > MAX_CLASSIFICATION_WINDOWS:
        raise SourceRequirementBaselineError(
            "SOURCE_ITEM_TEXT_CLASSIFICATION_WINDOW_CAPACITY_EXCEEDED"
        )
    if len(groups) == 1 and min(max_concurrency, len(flattened)) == 2:
        group = groups[0]
        target = sum(weights[item] for item in group) / 2
        split_at = 0
        accumulated = 0
        while split_at < len(group) - 1:
            accumulated += weights[group[split_at]]
            split_at += 1
            if accumulated >= target:
                break
        groups = [group[:split_at], group[split_at:]]
    for group in groups:
        character_count = sum(flattened[item][2] for item in group)
        if len(group) > max_window_records or (
            character_count > max_window_characters
            and not (
                len(group) == 1
                and flattened[group[0]][2] > max_window_characters
            )
        ):
            raise RuntimeError("source-item text window capacity invariant failed")
    boundary_set_sha256 = source_item_text_boundary_set_sha256(plans)
    windows: list[SourceItemTextClassificationWindowV1] = []
    for index, indices in enumerate(groups, start=1):
        selected = tuple(flattened[item] for item in indices)
        scope_ids = tuple(dict.fromkeys(item[1].scope_id for item in selected))
        windows.append(
            SourceItemTextClassificationWindowV1(
                window_id=f"text-window-{index:02d}",
                boundary_set_sha256=boundary_set_sha256,
                record_keys=tuple(item[0].record_key for item in selected),
                scope_ids=scope_ids,
                estimated_character_count=sum(item[2] for item in selected),
                estimated_weight=sum(item[3] for item in selected),
                risk_signals=tuple(
                    sorted({signal for item in selected for signal in item[4]})
                ),
            )
        )
    return tuple(windows)


def build_source_item_text_classification_window_input(
    *,
    window: SourceItemTextClassificationWindowV1,
    requests: tuple[SourceItemTextScopeRequestV1, ...],
    plans: tuple[SourceItemTextBoundaryPlanV1, ...],
) -> dict[str, object]:
    """Build the private, content-bearing input for one whole-record window."""

    request_by_scope = {item.scope_id: item for item in requests}
    plan_by_scope = {item.scope_id: item for item in plans}
    records: dict[
        str,
        tuple[SourceItemTextBoundaryPlanRecordV1, SourceItemTextScopeRequestV1],
    ] = {}
    for scope_id, plan in plan_by_scope.items():
        request = request_by_scope[scope_id]
        for record in plan.records:
            records[record.record_key] = (record, request)
    if set(window.record_keys) - set(records):
        raise ValueError("source-item text classification input key drifted")
    values: list[dict[str, object]] = []
    for key in window.record_keys:
        record, request = records[key]
        content_by_id = {item.content_id: item for item in request.source_content}
        spans = []
        for span in record.text_spans:
            content = content_by_id[span.content_id]
            spans.append(
                {
                    "content_id": span.content_id,
                    "page": content.page,
                    "start_offset": span.start_offset,
                    "end_offset": span.end_offset,
                    "text": content.text[span.start_offset : span.end_offset],
                    "precision": content.precision,
                    "is_title": content.is_title,
                }
            )
        shared = [
            {
                "content_id": content_id,
                "page": content_by_id[content_id].page,
                "text": content_by_id[content_id].text,
                "precision": content_by_id[content_id].precision,
                "is_title": content_by_id[content_id].is_title,
            }
            for content_id in record.shared_source_content_ids
        ]
        values.append(
            {
                "record_key": record.record_key,
                "scope_id": request.scope_id,
                "node_id": request.node_id,
                "node_label": request.node_label,
                "node_title": request.node_title,
                "ancestor_path": request.ancestor_path,
                "label": record.label,
                "source_form": record.source_form.value,
                "text_spans": spans,
                "shared_source_content": shared,
            }
        )
    return {
        "schema_version": CLASSIFICATION_INPUT_SCHEMA_VERSION,
        "window": window.model_dump(mode="json"),
        "records": values,
        "instruction": (
            "Classify each immutable whole record with the canonical A/B/F "
            "decision schema and classification V1: FUNCTIONAL, QUALITY and CONSTRAINT "
            "are product/system kinds; PROJECT and PROCESS are independent kinds. "
            "Topics and verification relations do not create main categories. Standards "
            "and methods are not automatically constraints. Do not merge, split, relabel, "
            "reorder, or omit records."
        ),
    }


def aggregate_source_item_text_batch(
    *,
    document_map: DocumentMapArtifactV1,
    requests: tuple[SourceItemTextScopeRequestV1, ...],
    plans: tuple[SourceItemTextBoundaryPlanV1, ...],
    window_results: Mapping[str, TextClassificationWindowRunResult],
) -> SourceItemTextBatchCandidateV1:
    decisions = {
        item.record_key: item
        for result in window_results.values()
        for item in result.decisions
    }
    expected = {record.record_key for plan in plans for record in plan.records}
    if set(decisions) != expected:
        raise ValueError("SOURCE_ITEM_TEXT_CLASSIFICATION_AGGREGATE_KEY_SET_INVALID")
    diagnostics_by_scope: dict[str, set[str]] = {
        plan.scope_id: set() for plan in plans
    }
    record_scope = {
        record.record_key: plan.scope_id
        for plan in plans
        for record in plan.records
    }
    for result in window_results.values():
        for decision in result.decisions:
            diagnostics_by_scope[record_scope[decision.record_key]].update(
                result.diagnostic_codes
            )
    plan_by_scope = {item.scope_id: item for item in plans}
    candidates: list[SourceItemTextSliceCandidateV1] = []
    for request in requests:
        plan = plan_by_scope[request.scope_id]
        items = tuple(
            SourceItemTextBoundaryCandidateV1(
                item_key=record.record_key,
                label=record.label,
                source_form=record.source_form,
                text_spans=record.text_spans,
                shared_source_content_ids=record.shared_source_content_ids,
                requirementness=decisions[record.record_key].requirementness,
                requirement_kinds=decisions[record.record_key].requirement_kinds,
                non_requirement_role=decisions[record.record_key].non_requirement_role,
                confidence=decisions[record.record_key].confidence,
                decision_summary=decisions[record.record_key].decision_summary,
                reason_codes=decisions[record.record_key].reason_codes,
            )
            for record in plan.records
        )
        candidate = SourceItemTextSliceCandidateV1(
            source_sha256=request.source_sha256,
            document_map_artifact_id=request.document_map_artifact_id,
            document_map_artifact_sha256=request.document_map_artifact_sha256,
            request_sha256=request.request_sha256,
            scope_id=request.scope_id,
            items=items,
            structure_only_spans=plan.structure_only_spans,
            diagnostic_codes=tuple(sorted(diagnostics_by_scope[request.scope_id])),
        )
        validate_source_item_text_slice_candidate(request, candidate)
        candidates.append(candidate)
    return SourceItemTextBatchCandidateV1(
        source_sha256=document_map.source_sha256,
        document_map_artifact_id=document_map.artifact_id,
        document_map_artifact_sha256=document_map.artifact_sha256,
        candidates=tuple(candidates),
    )


def build_source_item_text_windowed_checkpoint(
    *,
    requests: tuple[SourceItemTextScopeRequestV1, ...],
    runner_version: str,
    executor_id: str,
    executor_version: str,
    transport_kind: str,
    model_id: str,
    reasoning_effort: str,
    codex_version: str,
    kit_version: str,
    kit_bundle_sha256: str,
    visual_source_sha256: str,
    visual_page_numbers: tuple[int, ...],
    boundary_results: Mapping[str, TextBoundaryRunResult],
    classification_windows_planned: bool,
    classification_windows: tuple[SourceItemTextClassificationWindowV1, ...],
    window_results: Mapping[str, TextClassificationWindowRunResult],
    boundary_elapsed_milliseconds: int,
    classification_elapsed_milliseconds: int,
    request_set_sha256: str | None = None,
) -> NativeSourceItemTextWindowedCheckpointV1:
    boundary_checkpoints = tuple(
        _boundary_result_checkpoint(boundary_results[request.scope_id])
        for request in requests
        if request.scope_id in boundary_results
    )
    window_checkpoints = tuple(
        _window_result_checkpoint(window_results[window.window_id])
        for window in classification_windows
        if window.window_id in window_results
    )
    values = {
        "runner_version": runner_version,
        "executor_id": executor_id,
        "executor_version": executor_version,
        "transport_kind": transport_kind,
        "source_sha256": requests[0].source_sha256,
        "document_map_artifact_id": requests[0].document_map_artifact_id,
        "document_map_artifact_sha256": requests[0].document_map_artifact_sha256,
        "request_set_sha256": request_set_sha256
        or source_item_text_request_set_sha256(requests),
        "scope_ids": tuple(item.scope_id for item in requests),
        "model_id": model_id,
        "requested_reasoning_effort": reasoning_effort,
        "codex_version": codex_version,
        "kit_version": kit_version,
        "kit_bundle_sha256": kit_bundle_sha256,
        "visual_source_sha256": visual_source_sha256,
        "visual_page_numbers": visual_page_numbers,
        "accepted_boundaries": boundary_checkpoints,
        "classification_windows_planned": classification_windows_planned,
        "classification_windows": classification_windows,
        "accepted_windows": window_checkpoints,
        "boundary_elapsed_milliseconds": boundary_elapsed_milliseconds,
        "classification_elapsed_milliseconds": classification_elapsed_milliseconds,
    }
    draft = NativeSourceItemTextWindowedCheckpointV1.model_construct(
        **values,
        checkpoint_sha256="0" * 64,
    )
    return NativeSourceItemTextWindowedCheckpointV1(
        **values,
        checkpoint_sha256=canonical_json_sha256(
            draft.model_dump(mode="json", exclude={"checkpoint_sha256"})
        ),
    )


def validate_source_item_text_resume_checkpoint_identity(
    checkpoint: NativeSourceItemTextWindowedCheckpointV1,
    *,
    document_map: DocumentMapArtifactV1,
    requests: tuple[SourceItemTextScopeRequestV1, ...],
    runner_version: str,
    executor_id: str,
    executor_version: str,
    transport_kind: str,
    model_id: str,
    reasoning_effort: str,
    codex_version: str,
    kit_version: str,
    kit_bundle_sha256: str,
    visual_source_sha256: str,
    visual_page_numbers: tuple[int, ...],
    max_concurrency: int,
    request_set_sha256: str | None = None,
) -> None:
    expected_request_set_sha256 = (
        request_set_sha256 or source_item_text_request_set_sha256(requests)
    )
    if (
        checkpoint.runner_version != runner_version
        or checkpoint.executor_id != executor_id
        or checkpoint.executor_version != executor_version
        or checkpoint.transport_kind != transport_kind
        or checkpoint.source_sha256 != document_map.source_sha256
        or checkpoint.document_map_artifact_id != document_map.artifact_id
        or checkpoint.document_map_artifact_sha256 != document_map.artifact_sha256
        or checkpoint.request_set_sha256 != expected_request_set_sha256
        or checkpoint.scope_ids != tuple(item.scope_id for item in requests)
        or checkpoint.model_id != model_id
        or checkpoint.requested_reasoning_effort != reasoning_effort
        or checkpoint.codex_version != codex_version
        or checkpoint.kit_version != kit_version
        or checkpoint.kit_bundle_sha256 != kit_bundle_sha256
        or checkpoint.visual_source_sha256 != visual_source_sha256
        or checkpoint.visual_page_numbers != visual_page_numbers
    ):
        raise ValueError("source-item text checkpoint execution identity drifted")
    request_by_scope = {item.scope_id: item for item in requests}
    for boundary in checkpoint.accepted_boundaries:
        validate_source_item_text_boundary_plan(
            request=request_by_scope[boundary.plan.scope_id],
            plan=boundary.plan,
        )
    if checkpoint.classification_windows_planned:
        plans = tuple(item.plan for item in checkpoint.accepted_boundaries)
        expected_windows = build_source_item_text_classification_windows(
            document_map=document_map,
            requests=requests,
            plans=plans,
            max_concurrency=max_concurrency,
        )
        if checkpoint.classification_windows != expected_windows:
            raise ValueError("source-item text checkpoint window plan drifted")


def boundary_result_from_checkpoint(
    checkpoint: NativeSourceItemTextBoundaryScopeCheckpointV1,
) -> TextBoundaryRunResult:
    return TextBoundaryRunResult(
        plan=checkpoint.plan,
        candidate=checkpoint.candidate,
        audit_summaries=checkpoint.audit_summaries,
        attempt_efforts=checkpoint.attempt_efforts,
        raw_candidate_bytes=checkpoint.raw_candidate_bytes,
        elapsed_milliseconds=checkpoint.elapsed_milliseconds,
    )


def window_result_from_checkpoint(
    checkpoint: NativeSourceItemTextWindowResultCheckpointV1,
) -> TextClassificationWindowRunResult:
    return TextClassificationWindowRunResult(
        window=checkpoint.window,
        decisions=checkpoint.decisions,
        audit_summaries=checkpoint.audit_summaries,
        attempt_efforts=checkpoint.attempt_efforts,
        raw_candidate_bytes=checkpoint.raw_candidate_bytes,
        elapsed_milliseconds=checkpoint.elapsed_milliseconds,
        input_payload=checkpoint.input_payload,
        diagnostic_codes=checkpoint.diagnostic_codes,
    )


def boundary_output_schema() -> dict[str, object]:
    return compile_strict_runner_output_schema(
        SourceItemTextBoundaryAgentCandidateV1
    )


def classification_output_schema() -> dict[str, object]:
    return compile_strict_runner_output_schema(
        SourceItemTextClassificationAgentCandidateV1
    )


def normalize_boundary_payload(payload: object) -> object:
    if not isinstance(payload, dict):
        return payload
    normalized = dict(payload)
    return normalized


def normalize_classification_payload(payload: object) -> object:
    if not isinstance(payload, dict):
        return payload
    normalized = dict(payload)
    diagnostics = normalized.get("diagnostic_codes")
    if isinstance(diagnostics, list):
        normalized["diagnostic_codes"] = sorted(
            {item for item in diagnostics if isinstance(item, str)}
        )
    decisions = normalized.get("decisions")
    if isinstance(decisions, list):
        normalized["decisions"] = [
            _normalize_reason_container(item) for item in decisions
        ]
    return normalized


def boundary_validation_feedback(
    exc: Exception,
    *,
    request: SourceItemTextScopeRequestV1,
) -> dict[str, object]:
    failure_code = safe_validation_error(exc)
    correction = ""
    if failure_code == "SOURCE_ITEM_TEXT_SHARED_CONTENT_OVERLAPS_OWNED_SPAN":
        correction = (
            " A content ID owned by this record's text_spans must not also appear "
            "in its shared_source_content_ids; keep the owned span and remove only "
            "the duplicate shared reference."
        )
    return {
        "schema_version": "source-item-text-boundary-validation-feedback/v1",
        "scope_id": request.scope_id,
        "failure_code": failure_code,
        "content_lengths": {
            item.content_id: len(item.text) for item in request.source_content
        },
        "instruction": (
            "Return one complete replacement boundary for this immutable scope. "
            "Use exact Unicode half-open spans, title-only dispositions, and no A/B/F."
            + correction
        ),
    }


def safe_validation_error(exc: Exception | None) -> str:
    if exc is None:
        return "SOURCE_ITEM_TEXT_WINDOWED_CANDIDATE_INVALID"
    if isinstance(exc, SourceRequirementBaselineError):
        value = str(exc).strip()
        if value:
            return value[:160]
    if isinstance(exc, ValidationError):
        return "SOURCE_ITEM_TEXT_WINDOWED_SCHEMA_INVALID"
    value = str(exc).strip()
    if value and value.replace("_", "").isalnum() and value.upper() == value:
        return value[:160]
    return "SOURCE_ITEM_TEXT_WINDOWED_CANDIDATE_INVALID"


def _boundary_result_checkpoint(
    result: TextBoundaryRunResult,
) -> NativeSourceItemTextBoundaryScopeCheckpointV1:
    values = {
        "plan": result.plan,
        "candidate": result.candidate,
        "audit_summaries": result.audit_summaries,
        "attempt_efforts": result.attempt_efforts,
        "raw_candidate_bytes": result.raw_candidate_bytes,
        "elapsed_milliseconds": result.elapsed_milliseconds,
    }
    draft = NativeSourceItemTextBoundaryScopeCheckpointV1.model_construct(
        **values,
        checkpoint_sha256="0" * 64,
    )
    return NativeSourceItemTextBoundaryScopeCheckpointV1(
        **values,
        checkpoint_sha256=canonical_json_sha256(
            draft.model_dump(mode="json", exclude={"checkpoint_sha256"})
        ),
    )


def _window_result_checkpoint(
    result: TextClassificationWindowRunResult,
) -> NativeSourceItemTextWindowResultCheckpointV1:
    values = {
        "window": result.window,
        "decisions": result.decisions,
        "audit_summaries": result.audit_summaries,
        "attempt_efforts": result.attempt_efforts,
        "raw_candidate_bytes": result.raw_candidate_bytes,
        "elapsed_milliseconds": result.elapsed_milliseconds,
        "input_payload": result.input_payload,
        "diagnostic_codes": result.diagnostic_codes,
    }
    draft = NativeSourceItemTextWindowResultCheckpointV1.model_construct(
        **values,
        checkpoint_sha256="0" * 64,
    )
    return NativeSourceItemTextWindowResultCheckpointV1(
        **values,
        checkpoint_sha256=canonical_json_sha256(
            draft.model_dump(mode="json", exclude={"checkpoint_sha256"})
        ),
    )


def _canonical_spans(
    spans: tuple[SourceItemTextSpanV1, ...],
    *,
    content_order: Mapping[str, int],
) -> tuple[SourceItemTextSpanV1, ...]:
    ordered = sorted(
        spans,
        key=lambda item: (
            content_order.get(item.content_id, len(content_order)),
            item.start_offset,
            item.end_offset,
        ),
    )
    merged: list[SourceItemTextSpanV1] = []
    for span in ordered:
        if (
            merged
            and merged[-1].content_id == span.content_id
            and merged[-1].end_offset == span.start_offset
        ):
            previous = merged.pop()
            merged.append(
                SourceItemTextSpanV1(
                    content_id=span.content_id,
                    start_offset=previous.start_offset,
                    end_offset=span.end_offset,
                )
            )
        else:
            merged.append(span)
    return tuple(merged)


def _host_record_key(
    *,
    scope_id: str,
    source_form: SourceRecordForm,
    spans: tuple[SourceItemTextSpanV1, ...],
) -> str:
    digest = canonical_json_sha256(
        {
            "scope_id": scope_id,
            "source_form": source_form.value,
            "text_spans": [item.model_dump(mode="json") for item in spans],
        }
    )
    return f"text-record-{digest[:32]}"


def _boundary_identity_sha256(plan: SourceItemTextBoundaryPlanV1) -> str:
    return canonical_json_sha256(
        {
            "source_sha256": plan.source_sha256,
            "document_map_artifact_id": plan.document_map_artifact_id,
            "document_map_artifact_sha256": plan.document_map_artifact_sha256,
            "request_sha256": plan.request_sha256,
            "scope_id": plan.scope_id,
            "records": [
                {
                    "record_key": item.record_key,
                    "source_form": item.source_form.value,
                    "text_spans": [
                        span.model_dump(mode="json") for span in item.text_spans
                    ],
                    "shared_source_content_ids": item.shared_source_content_ids,
                }
                for item in plan.records
            ],
            "structure_only_spans": [
                item.text_span.model_dump(mode="json")
                for item in plan.structure_only_spans
            ],
        }
    )


def source_item_text_boundary_set_sha256(
    plans: tuple[SourceItemTextBoundaryPlanV1, ...],
) -> str:
    return canonical_json_sha256(
        [
            {
                "scope_id": item.scope_id,
                "boundary_identity_sha256": item.boundary_identity_sha256,
            }
            for item in plans
        ]
    )


def _record_character_count(
    record: SourceItemTextBoundaryPlanRecordV1,
    *,
    request: SourceItemTextScopeRequestV1,
) -> int:
    content_by_id = {item.content_id: item for item in request.source_content}
    return max(
        1,
        sum(
            len(content_by_id[span.content_id].text[span.start_offset : span.end_offset])
            for span in record.text_spans
        )
        + sum(
            len(content_by_id[content_id].text)
            for content_id in record.shared_source_content_ids
        ),
    )


def _record_risk_signals(
    *,
    record: SourceItemTextBoundaryPlanRecordV1,
    request: SourceItemTextScopeRequestV1,
    document_map: DocumentMapArtifactV1,
) -> tuple[str, ...]:
    content_by_id = {item.content_id: item for item in request.source_content}
    selected = [content_by_id[item.content_id] for item in record.text_spans]
    signals: set[str] = set()
    if document_map.evidence_state != "SUFFICIENT":
        signals.add("document_evidence_not_sufficient")
    if len(record.text_spans) > 1:
        signals.add("multi_span_record")
    if len({item.content_id for item in record.text_spans}) > 1:
        signals.add("multi_content_record")
    if len({item.page for item in selected}) > 1:
        signals.add("multi_page_record")
    if record.shared_source_content_ids:
        signals.add("shared_source_context")
    if any(not item.precision.startswith("EXACT") for item in selected):
        signals.add("non_exact_source_content")
    if _record_character_count(record, request=request) > 4000:
        signals.add("large_record")
    node = next((item for item in document_map.nodes if item.node_id == request.node_id), None)
    if node is not None and node.finding_codes:
        signals.add("source_structure_finding")
    return tuple(sorted(signals))


def _normalize_reason_container(payload: object) -> object:
    if not isinstance(payload, dict):
        return payload
    normalized = dict(payload)
    for key in ("reason_codes", "requirement_kinds"):
        values = normalized.get(key)
        if isinstance(values, list):
            normalized[key] = sorted(
                {item for item in values if isinstance(item, str)}
            )
    return normalized


__all__ = [
    "BOUNDARY_AGENT_SCHEMA_VERSION",
    "BOUNDARY_PLAN_SCHEMA_VERSION",
    "CLASSIFICATION_AGENT_SCHEMA_VERSION",
    "CLASSIFICATION_INPUT_SCHEMA_VERSION",
    "CLASSIFICATION_WINDOW_SCHEMA_VERSION",
    "MAX_CLASSIFICATION_WINDOWS",
    "NATIVE_SOURCE_ITEM_TEXT_WINDOWED_RUNNER_VERSION",
    "NativeSourceItemTextBoundaryScopeCheckpointV1",
    "NativeSourceItemTextWindowResultCheckpointV1",
    "NativeSourceItemTextWindowedCheckpointV1",
    "SourceItemTextBoundaryAgentCandidateV1",
    "SourceItemTextBoundaryAgentRecordV1",
    "SourceItemTextBoundaryAgentStructureOnlySpanV1",
    "SourceItemTextBoundaryPlanRecordV1",
    "SourceItemTextBoundaryPlanV1",
    "SourceItemTextClassificationAgentCandidateV1",
    "SourceItemTextClassificationWindowV1",
    "TextBoundaryRunResult",
    "TextClassificationWindowRunResult",
    "aggregate_source_item_text_batch",
    "boundary_output_schema",
    "boundary_result_from_checkpoint",
    "boundary_validation_feedback",
    "build_source_item_text_classification_window_input",
    "build_source_item_text_classification_windows",
    "build_source_item_text_windowed_checkpoint",
    "classification_output_schema",
    "compile_source_item_text_boundary_plan",
    "normalize_boundary_payload",
    "normalize_classification_payload",
    "safe_validation_error",
    "source_item_text_request_set_sha256",
    "source_item_text_boundary_set_sha256",
    "validate_source_item_text_boundary_plan",
    "validate_source_item_text_resume_checkpoint_identity",
    "window_result_from_checkpoint",
]

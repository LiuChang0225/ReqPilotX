from __future__ import annotations

import hashlib
import json
from enum import Enum
import re
from typing import Any
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)


CODEX_EXTRACTION_OBSERVATION_SCHEMA_VERSION = "codex-extraction-observation/v1"
CODEX_EXTRACTION_PARITY_REPORT_SCHEMA_VERSION = "codex-extraction-parity-report/v1"
NATIVE_CODEX_REFERENCE_PAYLOAD_SCHEMA_VERSION = "native-codex-reference-payload/v1"


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class CodexExtractionLane(str, Enum):
    NATIVE_CODEX_REFERENCE = "NATIVE_CODEX_REFERENCE"
    CODEX_CANDIDATE_DRAFT = "CODEX_CANDIDATE_DRAFT"
    REQPILOTX = "REQPILOTX"


class CodexExtractionProvenance(str, Enum):
    NATIVE_CODEX_REFERENCE = "NATIVE_CODEX_REFERENCE"
    AI_REVIEWED_CANDIDATE_DRAFT = "AI_REVIEWED_CANDIDATE_DRAFT"
    REQPILOTX_RUNTIME_OBSERVATION = "REQPILOTX_RUNTIME_OBSERVATION"


class CodexExtractionStage(str, Enum):
    PAGE_UNDERSTANDING = "PAGE_UNDERSTANDING"
    STRUCTURE = "STRUCTURE"
    REQUIREMENT_RECOGNITION = "REQUIREMENT_RECOGNITION"
    REQUIREMENT_DECOMPOSITION = "REQUIREMENT_DECOMPOSITION"


_STAGE_ORDER = (
    CodexExtractionStage.PAGE_UNDERSTANDING,
    CodexExtractionStage.STRUCTURE,
    CodexExtractionStage.REQUIREMENT_RECOGNITION,
    CodexExtractionStage.REQUIREMENT_DECOMPOSITION,
)

_NATIVE_REFERENCE_METRIC_NAMES = {
    CodexExtractionStage.PAGE_UNDERSTANDING: (
        "page_count",
        "pages_observed",
        "pages_unresolved",
    ),
    CodexExtractionStage.STRUCTURE: (
        "native_reference_figures",
        "native_reference_logical_tables",
        "native_reference_numbered_sections",
    ),
    CodexExtractionStage.REQUIREMENT_RECOGNITION: (
        "native_reference_requirement_parents",
        "native_reference_semantic_unresolved",
    ),
    CodexExtractionStage.REQUIREMENT_DECOMPOSITION: (
        "native_reference_requirement_children",
        "native_reference_requirement_parents",
        "native_reference_unresolved_spans",
    ),
}

_NATIVE_REFERENCE_CAPABILITIES = {
    "figure_inspection",
    "page_render",
    "parent_child_decomposition",
    "pdf_native_text",
    "semantic_reasoning",
    "source_grounding",
    "structure_reconstruction",
    "table_reconstruction",
    "visual_inspection",
}


class CodexExtractionStageStatus(str, Enum):
    OBSERVED = "OBSERVED"
    PARTIAL = "PARTIAL"
    BLOCKED = "BLOCKED"
    NOT_RUN = "NOT_RUN"


class CodexExtractionParityStatus(str, Enum):
    PARITY = "PARITY"
    DIFFERENT = "DIFFERENT"


class CodexExtractionMetricV1(_StrictFrozenModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,119}$")
    value: StrictInt = Field(ge=0)


class CodexExtractionStageObservationV1(_StrictFrozenModel):
    stage: CodexExtractionStage
    status: CodexExtractionStageStatus
    metrics: tuple[CodexExtractionMetricV1, ...] = ()
    decision_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    grounding_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    decision_fingerprint_kind: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_.-]{0,119}$",
    )
    grounding_fingerprint_kind: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_.-]{0,119}$",
    )
    capabilities_used: tuple[str, ...] = ()
    diagnostic_codes: tuple[str, ...] = ()

    @field_validator("metrics")
    @classmethod
    def validate_metrics(
        cls,
        value: tuple[CodexExtractionMetricV1, ...],
    ) -> tuple[CodexExtractionMetricV1, ...]:
        names = tuple(item.name for item in value)
        if names != tuple(sorted(set(names))):
            raise ValueError("extraction metrics must be unique and sorted by name")
        return value

    @field_validator("capabilities_used", "diagnostic_codes")
    @classmethod
    def validate_sorted_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("extraction codes must be unique and sorted")
        if any(not re.fullmatch(r"[a-z][a-z0-9_]{0,159}", item) for item in value):
            raise ValueError(
                "extraction codes must be content-free snake_case identifiers"
            )
        return value

    @model_validator(mode="after")
    def validate_fingerprint_kinds(self) -> CodexExtractionStageObservationV1:
        if bool(self.decision_sha256) != bool(self.decision_fingerprint_kind):
            raise ValueError(
                "decision fingerprint SHA and kind must be supplied together"
            )
        if bool(self.grounding_sha256) != bool(self.grounding_fingerprint_kind):
            raise ValueError(
                "grounding fingerprint SHA and kind must be supplied together"
            )
        return self


class CodexExtractionObservationV1(_StrictFrozenModel):
    schema_version: Literal["codex-extraction-observation/v1"] = (
        CODEX_EXTRACTION_OBSERVATION_SCHEMA_VERSION
    )
    observation_id: str = Field(pattern=r"^codex-extraction-observation-[0-9a-f]{32}$")
    lane: CodexExtractionLane
    provenance: CodexExtractionProvenance
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_count: StrictInt = Field(ge=1)
    model_id: str = Field(min_length=1, max_length=160)
    skill_ids: tuple[str, ...] = ()
    stages: tuple[CodexExtractionStageObservationV1, ...]
    is_human_gold: bool = False
    is_serving_activation_gate: bool = False

    @field_validator("skill_ids")
    @classmethod
    def validate_skill_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("skill IDs must be unique and sorted")
        if any(not item or len(item) > 160 for item in value):
            raise ValueError("skill IDs must be non-empty and bounded")
        return value

    @model_validator(mode="after")
    def validate_observation(self) -> CodexExtractionObservationV1:
        if tuple(item.stage for item in self.stages) != _STAGE_ORDER:
            raise ValueError("extraction stages must be complete and ordered")
        if self.is_human_gold or self.is_serving_activation_gate:
            raise ValueError(
                "Codex parity observations cannot be Gold or serving gates"
            )
        expected_provenance = {
            CodexExtractionLane.NATIVE_CODEX_REFERENCE: (
                CodexExtractionProvenance.NATIVE_CODEX_REFERENCE
            ),
            CodexExtractionLane.CODEX_CANDIDATE_DRAFT: (
                CodexExtractionProvenance.AI_REVIEWED_CANDIDATE_DRAFT
            ),
            CodexExtractionLane.REQPILOTX: (
                CodexExtractionProvenance.REQPILOTX_RUNTIME_OBSERVATION
            ),
        }[self.lane]
        if self.provenance is not expected_provenance:
            raise ValueError("observation provenance does not match its lane")
        return self


class NativeCodexReferencePayloadV1(_StrictFrozenModel):
    schema_version: Literal["native-codex-reference-payload/v1"] = (
        NATIVE_CODEX_REFERENCE_PAYLOAD_SCHEMA_VERSION
    )
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_count: StrictInt = Field(ge=1)
    model_id: str = Field(min_length=1, max_length=160)
    skill_ids: tuple[str, ...]
    stages: tuple[CodexExtractionStageObservationV1, ...]
    is_human_gold: bool = False
    is_serving_activation_gate: bool = False

    @field_validator("skill_ids")
    @classmethod
    def validate_skill_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))) or not value:
            raise ValueError("native reference skill IDs must be non-empty and sorted")
        return value

    @model_validator(mode="after")
    def validate_payload(self) -> NativeCodexReferencePayloadV1:
        if tuple(item.stage for item in self.stages) != _STAGE_ORDER:
            raise ValueError("native reference stages must be complete and ordered")
        if self.is_human_gold or self.is_serving_activation_gate:
            raise ValueError(
                "native reference payloads cannot be Gold or serving gates"
            )
        required_capability = {
            CodexExtractionStage.PAGE_UNDERSTANDING: "page_render",
            CodexExtractionStage.STRUCTURE: "structure_reconstruction",
            CodexExtractionStage.REQUIREMENT_RECOGNITION: "semantic_reasoning",
            CodexExtractionStage.REQUIREMENT_DECOMPOSITION: (
                "parent_child_decomposition"
            ),
        }
        for stage in self.stages:
            metric_names = tuple(item.name for item in stage.metrics)
            if metric_names != _NATIVE_REFERENCE_METRIC_NAMES[stage.stage]:
                raise ValueError(
                    "native reference stage metrics do not match the contract"
                )
            if not set(stage.capabilities_used).issubset(
                _NATIVE_REFERENCE_CAPABILITIES
            ):
                raise ValueError("native reference declared an unsupported capability")
            if (
                stage.status
                in {
                    CodexExtractionStageStatus.OBSERVED,
                    CodexExtractionStageStatus.PARTIAL,
                }
                and required_capability[stage.stage] not in stage.capabilities_used
            ):
                raise ValueError(
                    "native reference stage omitted its required capability"
                )
            if any(
                value is not None
                for value in (
                    stage.decision_sha256,
                    stage.grounding_sha256,
                    stage.decision_fingerprint_kind,
                    stage.grounding_fingerprint_kind,
                )
            ):
                raise ValueError(
                    "native reference v1 fingerprints must be host-generated"
                )
            if stage.stage in {
                CodexExtractionStage.REQUIREMENT_RECOGNITION,
                CodexExtractionStage.REQUIREMENT_DECOMPOSITION,
            }:
                if stage.status is not CodexExtractionStageStatus.NOT_RUN:
                    raise ValueError(
                        "native reference v1 semantic stages must remain NOT_RUN"
                    )
                if stage.capabilities_used:
                    raise ValueError(
                        "native reference v1 deferred stages cannot claim capabilities"
                    )
                if any(item.value for item in stage.metrics):
                    raise ValueError(
                        "native reference v1 deferred stage metrics must be zero"
                    )
                if stage.diagnostic_codes != ("source_bound_stage_deferred",):
                    raise ValueError(
                        "native reference v1 semantic stages require the deferred diagnostic"
                    )
        return self


class CodexExtractionMetricDeltaV1(_StrictFrozenModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,119}$")
    reference_value: StrictInt = Field(ge=0)
    reqpilotx_value: StrictInt = Field(ge=0)
    delta: int

    @model_validator(mode="after")
    def validate_delta(self) -> CodexExtractionMetricDeltaV1:
        if self.delta != self.reqpilotx_value - self.reference_value:
            raise ValueError("metric delta does not match its values")
        return self


class CodexExtractionStageComparisonV1(_StrictFrozenModel):
    stage: CodexExtractionStage
    reference_status: CodexExtractionStageStatus
    reqpilotx_status: CodexExtractionStageStatus
    status_match: bool
    decision_match: bool | None
    grounding_match: bool | None
    reference_metrics: tuple[CodexExtractionMetricV1, ...]
    reqpilotx_metrics: tuple[CodexExtractionMetricV1, ...]
    metric_deltas: tuple[CodexExtractionMetricDeltaV1, ...]
    reference_only_capabilities: tuple[str, ...]
    reqpilotx_only_capabilities: tuple[str, ...]
    gap_codes: tuple[str, ...]
    reference_diagnostic_codes: tuple[str, ...]
    reqpilotx_diagnostic_codes: tuple[str, ...]

    @model_validator(mode="after")
    def validate_comparison(self) -> CodexExtractionStageComparisonV1:
        expected_status_match = self.reference_status is self.reqpilotx_status
        if self.status_match is not expected_status_match:
            raise ValueError("stage status_match does not match statuses")
        metric_names = tuple(item.name for item in self.metric_deltas)
        if metric_names != tuple(sorted(set(metric_names))):
            raise ValueError("metric deltas must be unique and sorted by name")
        for metrics in (self.reference_metrics, self.reqpilotx_metrics):
            names = tuple(item.name for item in metrics)
            if names != tuple(sorted(set(names))):
                raise ValueError("comparison metrics must be unique and sorted by name")
        for values in (
            self.reference_only_capabilities,
            self.reqpilotx_only_capabilities,
            self.gap_codes,
            self.reference_diagnostic_codes,
            self.reqpilotx_diagnostic_codes,
        ):
            if tuple(values) != tuple(sorted(set(values))):
                raise ValueError(
                    "stage comparison collections must be unique and sorted"
                )
        return self


class CodexExtractionParityReportV1(_StrictFrozenModel):
    schema_version: Literal["codex-extraction-parity-report/v1"] = (
        CODEX_EXTRACTION_PARITY_REPORT_SCHEMA_VERSION
    )
    status: CodexExtractionParityStatus
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_count: StrictInt = Field(ge=1)
    reference_observation_id: str = Field(
        pattern=r"^codex-extraction-observation-[0-9a-f]{32}$"
    )
    reqpilotx_observation_id: str = Field(
        pattern=r"^codex-extraction-observation-[0-9a-f]{32}$"
    )
    reference_provenance: CodexExtractionProvenance
    first_gap_stage: CodexExtractionStage | None
    stages: tuple[CodexExtractionStageComparisonV1, ...]
    limitations: tuple[str, ...]
    is_human_gold: bool = False
    is_serving_activation_gate: bool = False

    @model_validator(mode="after")
    def validate_report(self) -> CodexExtractionParityReportV1:
        if tuple(item.stage for item in self.stages) != _STAGE_ORDER:
            raise ValueError("parity report stages must be complete and ordered")
        expected_first_gap = next(
            (item.stage for item in self.stages if item.gap_codes),
            None,
        )
        if self.first_gap_stage is not expected_first_gap:
            raise ValueError("first_gap_stage does not match stage gaps")
        expected_status = (
            CodexExtractionParityStatus.PARITY
            if expected_first_gap is None
            else CodexExtractionParityStatus.DIFFERENT
        )
        if self.status is not expected_status:
            raise ValueError("parity status does not match stage gaps")
        if self.limitations != tuple(sorted(set(self.limitations))):
            raise ValueError("parity limitations must be unique and sorted")
        if self.is_human_gold or self.is_serving_activation_gate:
            raise ValueError("parity reports cannot be Gold or serving gates")
        return self


def compare_codex_extraction_observations(
    reference: CodexExtractionObservationV1,
    reqpilotx: CodexExtractionObservationV1,
) -> CodexExtractionParityReportV1:
    if reference.lane not in {
        CodexExtractionLane.NATIVE_CODEX_REFERENCE,
        CodexExtractionLane.CODEX_CANDIDATE_DRAFT,
    }:
        raise ValueError("reference observation must come from a Codex reference lane")
    if reqpilotx.lane is not CodexExtractionLane.REQPILOTX:
        raise ValueError("candidate observation must come from the ReqPilotX lane")
    if reference.source_sha256 != reqpilotx.source_sha256:
        raise ValueError("Codex parity observations require the same source SHA-256")
    if reference.page_count != reqpilotx.page_count:
        raise ValueError("Codex parity observations require the same page count")

    stage_comparisons = tuple(
        _compare_stage(reference_stage, reqpilotx_stage)
        for reference_stage, reqpilotx_stage in zip(
            reference.stages,
            reqpilotx.stages,
            strict=True,
        )
    )
    first_gap_stage = next(
        (item.stage for item in stage_comparisons if item.gap_codes),
        None,
    )
    limitations = ["reference_is_not_human_gold"]
    if reference.lane is CodexExtractionLane.CODEX_CANDIDATE_DRAFT:
        limitations.append("reference_is_rule_assisted_candidate_draft")
    if any(
        "source_bound_stage_deferred" in stage.diagnostic_codes
        for stage in reference.stages
    ):
        limitations.append("native_reference_semantics_are_deferred")
    return CodexExtractionParityReportV1(
        status=(
            CodexExtractionParityStatus.PARITY
            if first_gap_stage is None
            else CodexExtractionParityStatus.DIFFERENT
        ),
        source_sha256=reference.source_sha256,
        page_count=reference.page_count,
        reference_observation_id=reference.observation_id,
        reqpilotx_observation_id=reqpilotx.observation_id,
        reference_provenance=reference.provenance,
        first_gap_stage=first_gap_stage,
        stages=stage_comparisons,
        limitations=tuple(sorted(limitations)),
    )


def build_native_codex_reference_observation(
    payload: NativeCodexReferencePayloadV1,
) -> CodexExtractionObservationV1:
    canonical_payload = json.dumps(
        payload.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    digest = hashlib.sha256(canonical_payload).hexdigest()
    return CodexExtractionObservationV1(
        observation_id=f"codex-extraction-observation-{digest[:32]}",
        lane=CodexExtractionLane.NATIVE_CODEX_REFERENCE,
        provenance=CodexExtractionProvenance.NATIVE_CODEX_REFERENCE,
        source_sha256=payload.source_sha256,
        page_count=payload.page_count,
        model_id=payload.model_id,
        skill_ids=payload.skill_ids,
        stages=payload.stages,
    )


def native_codex_reference_output_schema() -> dict[str, Any]:
    return strict_structured_output_schema(NativeCodexReferencePayloadV1)


def strict_structured_output_schema(model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema()

    def normalize(value: object) -> None:
        if isinstance(value, list):
            for item in value:
                normalize(item)
            return
        if not isinstance(value, dict):
            return
        value.pop("default", None)
        properties = value.get("properties")
        if isinstance(properties, dict):
            value["required"] = list(properties)
            value["additionalProperties"] = False
        for item in value.values():
            normalize(item)

    normalize(schema)
    return schema


def _compare_stage(
    reference: CodexExtractionStageObservationV1,
    reqpilotx: CodexExtractionStageObservationV1,
) -> CodexExtractionStageComparisonV1:
    if reference.stage is not reqpilotx.stage:
        raise ValueError("Codex parity stage observations are misaligned")
    reference_metrics = {item.name: item.value for item in reference.metrics}
    reqpilotx_metrics = {item.name: item.value for item in reqpilotx.metrics}
    shared_metric_names = sorted(set(reference_metrics).intersection(reqpilotx_metrics))
    metric_deltas = tuple(
        CodexExtractionMetricDeltaV1(
            name=name,
            reference_value=reference_metrics[name],
            reqpilotx_value=reqpilotx_metrics[name],
            delta=reqpilotx_metrics[name] - reference_metrics[name],
        )
        for name in shared_metric_names
        if reference_metrics[name] != reqpilotx_metrics[name]
    )
    decision_match = _optional_digest_match(
        reference.decision_sha256,
        reqpilotx.decision_sha256,
        reference.decision_fingerprint_kind,
        reqpilotx.decision_fingerprint_kind,
    )
    grounding_match = _optional_digest_match(
        reference.grounding_sha256,
        reqpilotx.grounding_sha256,
        reference.grounding_fingerprint_kind,
        reqpilotx.grounding_fingerprint_kind,
    )
    reference_capabilities = set(reference.capabilities_used)
    reqpilotx_capabilities = set(reqpilotx.capabilities_used)
    reference_only_capabilities = tuple(
        sorted(reference_capabilities - reqpilotx_capabilities)
    )
    reqpilotx_only_capabilities = tuple(
        sorted(reqpilotx_capabilities - reference_capabilities)
    )
    gap_codes: list[str] = []
    if reference.status is not reqpilotx.status:
        gap_codes.append("stage_status_mismatch")
    if metric_deltas:
        gap_codes.append("shared_metric_mismatch")
    if decision_match is False:
        gap_codes.append("decision_fingerprint_mismatch")
    if grounding_match is False:
        gap_codes.append("grounding_fingerprint_mismatch")
    if reference_only_capabilities:
        gap_codes.append("reference_capability_missing_in_reqpilotx")
    return CodexExtractionStageComparisonV1(
        stage=reference.stage,
        reference_status=reference.status,
        reqpilotx_status=reqpilotx.status,
        status_match=reference.status is reqpilotx.status,
        decision_match=decision_match,
        grounding_match=grounding_match,
        reference_metrics=reference.metrics,
        reqpilotx_metrics=reqpilotx.metrics,
        metric_deltas=metric_deltas,
        reference_only_capabilities=reference_only_capabilities,
        reqpilotx_only_capabilities=reqpilotx_only_capabilities,
        gap_codes=tuple(sorted(gap_codes)),
        reference_diagnostic_codes=reference.diagnostic_codes,
        reqpilotx_diagnostic_codes=reqpilotx.diagnostic_codes,
    )


def _optional_digest_match(
    left: str | None,
    right: str | None,
    left_kind: str | None,
    right_kind: str | None,
) -> bool | None:
    if left is None or right is None or left_kind != right_kind:
        return None
    return left == right


__all__ = [
    "CODEX_EXTRACTION_OBSERVATION_SCHEMA_VERSION",
    "CODEX_EXTRACTION_PARITY_REPORT_SCHEMA_VERSION",
    "NATIVE_CODEX_REFERENCE_PAYLOAD_SCHEMA_VERSION",
    "CodexExtractionLane",
    "CodexExtractionMetricDeltaV1",
    "CodexExtractionMetricV1",
    "CodexExtractionObservationV1",
    "CodexExtractionParityReportV1",
    "CodexExtractionParityStatus",
    "CodexExtractionProvenance",
    "CodexExtractionStage",
    "CodexExtractionStageComparisonV1",
    "CodexExtractionStageObservationV1",
    "CodexExtractionStageStatus",
    "NativeCodexReferencePayloadV1",
    "build_native_codex_reference_observation",
    "compare_codex_extraction_observations",
    "native_codex_reference_output_schema",
    "strict_structured_output_schema",
]

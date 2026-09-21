from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

REQUIREMENT_MATRIX_SCHEMA_VERSION = "requirement-matrix-snapshot/v1"


class MatrixSnapshotState(str, Enum):
    READY = "READY"
    REQUIRES_REEXTRACTION = "REQUIRES_REEXTRACTION"


class MatrixNodeKind(str, Enum):
    STRUCTURE = "STRUCTURE"
    CLAUSE = "CLAUSE"
    TABLE = "TABLE"
    TABLE_RECORD = "TABLE_RECORD"
    ATOMIC_REQUIREMENT = "ATOMIC_REQUIREMENT"
    CONTEXT = "CONTEXT"


class MatrixNodeVisibility(str, Enum):
    VISIBLE = "VISIBLE"
    SYSTEM_NON_BUSINESS = "SYSTEM_NON_BUSINESS"
    HUMAN_EXCLUDED = "HUMAN_EXCLUDED"


class MatrixNodeDisposition(str, Enum):
    ANALYZE = "ANALYZE"
    RETAIN = "RETAIN"
    EXCLUDE = "EXCLUDE"


class MatrixAnalysisStatus(str, Enum):
    NOT_APPLICABLE = "NOT_APPLICABLE"
    NOT_STARTED = "NOT_STARTED"
    ASSESSED = "ASSESSED"
    DECIDED = "DECIDED"


class MatrixAnalysisResultState(str, Enum):
    NONE = "NONE"
    CURRENT = "CURRENT"
    STALE_REFERENCE = "STALE_REFERENCE"


class MatrixAnalysisEligibility(str, Enum):
    ELIGIBLE = "ELIGIBLE"
    NOT_ELIGIBLE = "NOT_ELIGIBLE"


class MatrixConclusionSource(str, Enum):
    NONE = "NONE"
    AI_DEFAULT = "AI_DEFAULT"
    HUMAN_OVERRIDE = "HUMAN_OVERRIDE"
    HUMAN_NATIVE = "HUMAN_NATIVE"
    STALE_REFERENCE = "STALE_REFERENCE"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class MatrixAiAssessmentV1(_FrozenModel):
    decision: str = Field(default="", max_length=120)
    compliance_status: str = Field(default="", max_length=120)
    reason: str = Field(default="", max_length=20_000)
    analysis_basis: str = Field(default="", max_length=20_000)
    clarification_question: str = Field(default="", max_length=20_000)
    confidence: float | None = Field(default=None, ge=0, le=1)
    assessment_mode: str = Field(default="", max_length=120)


class MatrixManualDecisionV1(_FrozenModel):
    decision: str = Field(min_length=1, max_length=120)
    reason: str = Field(default="", max_length=20_000)
    decided_at: str = Field(default="", max_length=100)


class MatrixConclusionContractV1(_FrozenModel):
    analysis_target: bool
    effective_conclusion: str = Field(default="", max_length=120)
    conclusion_source: MatrixConclusionSource = MatrixConclusionSource.NONE
    overrides_ai: bool = False


class MatrixConclusionSummaryV1(_FrozenModel):
    analysis_target_count: int = Field(default=0, ge=0)
    effective_conclusion_count: int = Field(default=0, ge=0)
    missing_effective_conclusion_count: int = Field(default=0, ge=0)
    effective_conclusion_coverage: float = Field(default=1.0, ge=0, le=1)
    ai_default_count: int = Field(default=0, ge=0)
    human_override_count: int = Field(default=0, ge=0)
    human_native_count: int = Field(default=0, ge=0)


class MatrixNodeStateV1(_FrozenModel):
    node_id: str = Field(min_length=1, max_length=260)
    parent_id: str | None = Field(default=None, max_length=260)
    child_ids: tuple[str, ...] = ()
    depth: int = Field(ge=0)
    source_order: int = Field(ge=0)
    display_role: MatrixNodeKind
    source_kind: str = Field(default="", max_length=80)
    source_number: str = Field(default="", max_length=160)
    display_title: str = Field(default="", max_length=500_000)
    display_text: str = Field(default="", max_length=500_000)
    label: str = Field(default="", max_length=500_000)
    summary: str = Field(default="", max_length=500_000)
    content_blocks: tuple[dict[str, Any], ...] = ()
    table_refs: tuple[dict[str, Any], ...] = ()
    figure_refs: tuple[dict[str, Any], ...] = ()
    source_refs: tuple[dict[str, Any], ...] = ()
    page_start: int | None = Field(default=None, ge=1)
    page_end: int | None = Field(default=None, ge=1)
    structure_node_id: str | None = Field(default=None, max_length=260)
    requirement_entry_id: str | None = Field(default=None, max_length=260)
    requirement_id: str | None = Field(default=None, max_length=260)
    requirement_code: str = Field(default="", max_length=260)
    requirement_text: str = Field(default="", max_length=500_000)
    table_record_id: str | None = Field(default=None, max_length=260)
    logical_table_ids: tuple[str, ...] = ()
    matrix_visibility: MatrixNodeVisibility
    disposition: MatrixNodeDisposition
    disposition_reason: str = Field(default="", max_length=20_000)
    analysis_eligibility: MatrixAnalysisEligibility
    analysis_status: MatrixAnalysisStatus
    analysis_result_state: MatrixAnalysisResultState = (
        MatrixAnalysisResultState.NONE
    )
    ai_assessment: MatrixAiAssessmentV1 | None = None
    manual_decision: MatrixManualDecisionV1 | None = None
    stale_ai_assessment: MatrixAiAssessmentV1 | None = None
    stale_manual_decision: MatrixManualDecisionV1 | None = None
    compliance_status: str = Field(default="", max_length=120)
    save_revision: int = Field(default=0, ge=0)
    updated_at: str = Field(default="", max_length=100)
    # SourceItem identities and facets survive app/plugin delivery without
    # changing the customer's numbering or creating new hierarchy nodes.
    source_item: dict[str, Any] | None = None
    decomposition: dict[str, Any] | None = None
    source_excerpts: tuple[dict[str, Any], ...] = ()
    classification_paths: tuple[str, ...] = ()
    topic_tags: tuple[str, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def infer_legacy_analysis_result_state(cls, value: Any) -> Any:
        """Keep v1 snapshots readable while making result currency explicit."""

        if not isinstance(value, dict) or "analysis_result_state" in value:
            return value
        payload = dict(value)
        if payload.get("stale_ai_assessment") is not None or payload.get(
            "stale_manual_decision"
        ) is not None:
            payload["analysis_result_state"] = (
                MatrixAnalysisResultState.STALE_REFERENCE
            )
        elif (
            payload.get("ai_assessment") is not None
            or payload.get("manual_decision") is not None
            or str(payload.get("compliance_status") or "").strip()
        ):
            payload["analysis_result_state"] = MatrixAnalysisResultState.CURRENT
        else:
            payload["analysis_result_state"] = MatrixAnalysisResultState.NONE
        return payload

    @model_validator(mode="after")
    def validate_matrix_node(self) -> MatrixNodeStateV1:
        if len(self.child_ids) != len(set(self.child_ids)):
            raise ValueError("matrix node child IDs must be unique")
        if len(self.logical_table_ids) != len(set(self.logical_table_ids)):
            raise ValueError("matrix node logical table IDs must be unique")
        if (
            self.page_start is not None
            and self.page_end is not None
            and self.page_end < self.page_start
        ):
            raise ValueError("matrix node page_end cannot precede page_start")
        if bool(self.requirement_id) != bool(self.requirement_entry_id):
            raise ValueError(
                "matrix requirement identity must bind both tree and stored entry"
            )
        if (
            self.requirement_id is not None
            and self.requirement_entry_id != self.requirement_id
        ):
            raise ValueError("matrix tree and stored requirement identities must match")
        if self.matrix_visibility is MatrixNodeVisibility.SYSTEM_NON_BUSINESS:
            if self.disposition is not MatrixNodeDisposition.RETAIN:
                raise ValueError("system non-business nodes can only be retained")
            if self.analysis_eligibility is MatrixAnalysisEligibility.ELIGIBLE:
                raise ValueError("system non-business nodes cannot be analyzed")
        if self.matrix_visibility is MatrixNodeVisibility.HUMAN_EXCLUDED:
            if self.disposition is not MatrixNodeDisposition.EXCLUDE:
                raise ValueError("human-excluded nodes require EXCLUDE disposition")
        if self.disposition is MatrixNodeDisposition.EXCLUDE and (
            self.matrix_visibility is not MatrixNodeVisibility.HUMAN_EXCLUDED
        ):
            raise ValueError("EXCLUDE disposition must be visibly human excluded")
        if self.analysis_eligibility is MatrixAnalysisEligibility.ELIGIBLE:
            if not self.requirement_id:
                raise ValueError(
                    "analysis eligibility requires a frozen stored entry"
                )
        if self.disposition is MatrixNodeDisposition.ANALYZE:
            if (
                self.matrix_visibility is not MatrixNodeVisibility.VISIBLE
                or self.analysis_eligibility
                is not MatrixAnalysisEligibility.ELIGIBLE
            ):
                raise ValueError(
                    "ANALYZE disposition requires a visible eligible node"
                )
            if self.analysis_status is MatrixAnalysisStatus.NOT_APPLICABLE:
                raise ValueError("analyzed nodes cannot be marked NOT_APPLICABLE")
        elif self.analysis_status is not MatrixAnalysisStatus.NOT_APPLICABLE:
            raise ValueError(
                "nodes outside the active analysis target must be NOT_APPLICABLE"
            )
        if (
            self.analysis_status is MatrixAnalysisStatus.DECIDED
            and self.manual_decision is None
        ):
            raise ValueError("DECIDED matrix nodes require a final decision")
        if self.ai_assessment is not None and (
            self.ai_assessment.assessment_mode.strip().lower() == "manual"
        ):
            raise ValueError("manual assessments cannot masquerade as AI advice")
        if self.analysis_result_state is MatrixAnalysisResultState.STALE_REFERENCE:
            if self.ai_assessment is not None or self.manual_decision is not None:
                raise ValueError(
                    "stale analysis references cannot masquerade as current results"
                )
            if self.compliance_status.strip():
                raise ValueError(
                    "stale analysis references cannot expose current compliance status"
                )
            if self.analysis_status in {
                MatrixAnalysisStatus.ASSESSED,
                MatrixAnalysisStatus.DECIDED,
            }:
                raise ValueError(
                    "stale analysis references cannot be assessed or decided"
                )
        elif self.analysis_result_state is MatrixAnalysisResultState.CURRENT:
            if (
                self.stale_ai_assessment is not None
                or self.stale_manual_decision is not None
            ):
                raise ValueError(
                    "current analysis results cannot also expose stale references"
                )
            if (
                self.ai_assessment is None
                and self.manual_decision is None
                and not self.compliance_status.strip()
            ):
                raise ValueError("current analysis result state requires a result")
        elif (
            self.ai_assessment is not None
            or self.manual_decision is not None
            or self.stale_ai_assessment is not None
            or self.stale_manual_decision is not None
            or self.compliance_status.strip()
        ):
            raise ValueError("NONE analysis result state cannot expose a result")
        return self


def _matrix_contract_value(
    node: MatrixNodeStateV1 | Mapping[str, Any],
    key: str,
) -> Any:
    if isinstance(node, Mapping):
        return node.get(key)
    return getattr(node, key, None)


def _matrix_contract_text(value: Any) -> str:
    enum_value = getattr(value, "value", value)
    return str(enum_value or "").strip()


def _matrix_payload_text(payload: Any, key: str) -> str:
    if isinstance(payload, Mapping):
        return _matrix_contract_text(payload.get(key))
    return _matrix_contract_text(getattr(payload, key, None))


def _matrix_contract_result_state(
    node: MatrixNodeStateV1 | Mapping[str, Any],
) -> str:
    explicit_state = _matrix_contract_text(
        _matrix_contract_value(node, "analysis_result_state")
    ).upper()
    if explicit_state:
        return explicit_state
    if (
        _matrix_contract_value(node, "stale_ai_assessment") is not None
        or _matrix_contract_value(node, "stale_manual_decision") is not None
    ):
        return MatrixAnalysisResultState.STALE_REFERENCE.value
    if (
        _matrix_contract_value(node, "ai_assessment") is not None
        or _matrix_contract_value(node, "manual_decision") is not None
        or _matrix_contract_text(_matrix_contract_value(node, "compliance_status"))
    ):
        return MatrixAnalysisResultState.CURRENT.value
    return MatrixAnalysisResultState.NONE.value


def _matrix_contract_allows_current_conclusion(
    node: MatrixNodeStateV1 | Mapping[str, Any],
) -> bool:
    if isinstance(node, Mapping) and "analysis_status" not in node:
        return True
    status = _matrix_contract_text(
        _matrix_contract_value(node, "analysis_status")
    ).upper()
    return status in {
        MatrixAnalysisStatus.ASSESSED.value,
        MatrixAnalysisStatus.DECIDED.value,
    }


def is_analysis_target(node: MatrixNodeStateV1 | Mapping[str, Any]) -> bool:
    return (
        _matrix_contract_text(
            _matrix_contract_value(node, "matrix_visibility")
        ).upper()
        == MatrixNodeVisibility.VISIBLE.value
        and _matrix_contract_text(
            _matrix_contract_value(node, "disposition")
        ).upper()
        == MatrixNodeDisposition.ANALYZE.value
        and _matrix_contract_text(
            _matrix_contract_value(node, "analysis_eligibility")
        ).upper()
        == MatrixAnalysisEligibility.ELIGIBLE.value
    )


def count_missing_source_business_nodes(
    nodes: Iterable[MatrixNodeStateV1 | Mapping[str, Any]],
) -> int:
    return sum(
        1
        for node in nodes
        if _matrix_contract_text(
            _matrix_contract_value(node, "matrix_visibility")
        ).upper()
        != MatrixNodeVisibility.SYSTEM_NON_BUSINESS.value
        and not _matrix_contract_value(node, "page_start")
        and not _matrix_contract_value(node, "source_refs")
    )


def effective_current_conclusion(
    node: MatrixNodeStateV1 | Mapping[str, Any],
) -> str:
    result_state = _matrix_contract_result_state(node)
    if (
        result_state != MatrixAnalysisResultState.CURRENT.value
        or not _matrix_contract_allows_current_conclusion(node)
    ):
        return ""
    manual_value = _matrix_payload_text(
        _matrix_contract_value(node, "manual_decision"),
        "decision",
    )
    pending = {"UNKNOWN", "PENDING", "PENDING_EVIDENCE", "NEEDS_EVIDENCE", "NEEDS_CLARIFICATION", "NOT_ASSESSED", "UNASSESSED", "UNDETERMINED"}
    if manual_value:
        return "" if manual_value.upper() in pending else manual_value
    ai_assessment = _matrix_contract_value(node, "ai_assessment")
    value = _matrix_payload_text(ai_assessment, "decision") or _matrix_payload_text(
        ai_assessment,
        "compliance_status",
    )
    return "" if value.upper() in pending else value


def has_incomplete_terminal_conclusion(
    node: MatrixNodeStateV1 | Mapping[str, Any],
) -> bool:
    return (
        is_analysis_target(node)
        and _matrix_contract_result_state(node)
        == MatrixAnalysisResultState.CURRENT.value
        and _matrix_contract_allows_current_conclusion(node)
        and not effective_current_conclusion(node)
    )


def conclusion_source(
    node: MatrixNodeStateV1 | Mapping[str, Any],
) -> MatrixConclusionSource:
    result_state = _matrix_contract_result_state(node)
    if result_state == MatrixAnalysisResultState.STALE_REFERENCE.value:
        return MatrixConclusionSource.STALE_REFERENCE
    if (
        result_state != MatrixAnalysisResultState.CURRENT.value
        or not _matrix_contract_allows_current_conclusion(node)
        or not effective_current_conclusion(node)
    ):
        return MatrixConclusionSource.NONE
    manual_value = _matrix_payload_text(
        _matrix_contract_value(node, "manual_decision"),
        "decision",
    )
    ai_assessment = _matrix_contract_value(node, "ai_assessment")
    ai_value = _matrix_payload_text(
        ai_assessment,
        "decision",
    ) or _matrix_payload_text(ai_assessment, "compliance_status")
    if manual_value:
        if ai_value:
            return MatrixConclusionSource.HUMAN_OVERRIDE
        return MatrixConclusionSource.HUMAN_NATIVE
    if ai_value:
        return MatrixConclusionSource.AI_DEFAULT
    return MatrixConclusionSource.NONE


def matrix_conclusion_contract(
    node: MatrixNodeStateV1 | Mapping[str, Any],
) -> MatrixConclusionContractV1:
    source = conclusion_source(node)
    return MatrixConclusionContractV1(
        analysis_target=is_analysis_target(node),
        effective_conclusion=effective_current_conclusion(node),
        conclusion_source=source,
        overrides_ai=source is MatrixConclusionSource.HUMAN_OVERRIDE,
    )


def summarize_effective_conclusions(
    nodes: Iterable[MatrixNodeStateV1 | Mapping[str, Any]],
) -> MatrixConclusionSummaryV1:
    contracts = tuple(
        matrix_conclusion_contract(node)
        for node in nodes
        if is_analysis_target(node)
    )
    effective = tuple(
        contract for contract in contracts if contract.effective_conclusion
    )
    target_count = len(contracts)
    effective_count = len(effective)
    return MatrixConclusionSummaryV1(
        analysis_target_count=target_count,
        effective_conclusion_count=effective_count,
        missing_effective_conclusion_count=target_count - effective_count,
        effective_conclusion_coverage=(
            effective_count / target_count if target_count else 1.0
        ),
        ai_default_count=sum(
            contract.conclusion_source is MatrixConclusionSource.AI_DEFAULT
            for contract in effective
        ),
        human_override_count=sum(
            contract.conclusion_source is MatrixConclusionSource.HUMAN_OVERRIDE
            for contract in effective
        ),
        human_native_count=sum(
            contract.conclusion_source is MatrixConclusionSource.HUMAN_NATIVE
            for contract in effective
        ),
    )


class MatrixLogicalTableV1(_FrozenModel):
    logical_table_id: str = Field(min_length=1, max_length=260)
    projection_id: str = Field(min_length=1, max_length=260)
    projection_mode: str = Field(min_length=1, max_length=80)
    record_count: int = Field(ge=0)
    physical_table_ids: tuple[str, ...]
    node_ids: tuple[str, ...] = ()
    evidence_binding_ids: tuple[str, ...] = ()
    projection: dict[str, Any]
    evidence_bindings: tuple[dict[str, Any], ...] = ()
    title: str = Field(default="", exclude_if=lambda value: not value)
    structured_segments: tuple[dict[str, Any], ...] = Field(default=(), exclude_if=lambda value: not value)

    @model_validator(mode="after")
    def validate_logical_table(self) -> MatrixLogicalTableV1:
        for values, label in (
            (self.physical_table_ids, "physical table IDs"),
            (self.node_ids, "node IDs"),
            (self.evidence_binding_ids, "evidence binding IDs"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"matrix logical table {label} must be unique")
        if not self.physical_table_ids:
            raise ValueError("matrix logical table requires physical table identities")
        return self


class MatrixCountsV1(_FrozenModel):
    total_nodes: int = Field(default=0, ge=0)
    visible_nodes: int = Field(default=0, ge=0)
    system_non_business_nodes: int = Field(default=0, ge=0)
    human_excluded_nodes: int = Field(default=0, ge=0)
    analyze_nodes: int = Field(default=0, ge=0)
    retained_nodes: int = Field(default=0, ge=0)
    excluded_nodes: int = Field(default=0, ge=0)
    analysis_eligible_nodes: int = Field(default=0, ge=0)
    assessed_nodes: int = Field(default=0, ge=0)
    decided_nodes: int = Field(default=0, ge=0)
    stale_result_nodes: int = Field(default=0, ge=0)
    logical_tables: int = Field(default=0, ge=0)


class RequirementMatrixSnapshotV1(_FrozenModel):
    schema_version: Literal["requirement-matrix-snapshot/v1"] = (
        REQUIREMENT_MATRIX_SCHEMA_VERSION
    )
    state: MatrixSnapshotState
    project_id: str = Field(min_length=1, max_length=160)
    document_id: str = Field(min_length=1, max_length=160)
    requirement_set_id: str = Field(min_length=1, max_length=200)
    requirement_set_status: str = Field(default="", max_length=80)
    tree_sha256: str = Field(default="", max_length=64)
    root_node_ids: tuple[str, ...] = ()
    nodes: tuple[MatrixNodeStateV1, ...] = ()
    logical_tables: tuple[MatrixLogicalTableV1, ...] = ()
    counts: MatrixCountsV1 = MatrixCountsV1()
    source_item_baseline: dict[str, Any] | None = None
    delivery_sha256: str = ""

    @model_validator(mode="after")
    def validate_matrix(self) -> RequirementMatrixSnapshotV1:
        node_by_id = {node.node_id: node for node in self.nodes}
        if len(node_by_id) != len(self.nodes):
            raise ValueError("matrix node IDs must be unique")
        if len(self.root_node_ids) != len(set(self.root_node_ids)):
            raise ValueError("matrix root node IDs must be unique")
        if any(node_id not in node_by_id for node_id in self.root_node_ids):
            raise ValueError("matrix roots must reference existing nodes")
        expected_roots = {
            node.node_id for node in self.nodes if node.parent_id is None
        }
        if expected_roots != set(self.root_node_ids):
            raise ValueError("matrix roots must match all parentless nodes")
        requirement_ids: set[str] = set()
        for node in self.nodes:
            if node.parent_id is not None and node.parent_id not in node_by_id:
                raise ValueError("matrix node parent does not exist")
            if node.parent_id is not None:
                parent = node_by_id[node.parent_id]
                if node.node_id not in parent.child_ids:
                    raise ValueError("matrix parent must reference every child")
            for child_id in node.child_ids:
                child = node_by_id.get(child_id)
                if child is None or child.parent_id != node.node_id:
                    raise ValueError("matrix node child relation is inconsistent")
            if node.requirement_id:
                if node.requirement_id in requirement_ids:
                    raise ValueError(
                        "a frozen requirement can appear only once in the matrix"
                    )
                requirement_ids.add(node.requirement_id)

        visited: set[str] = set()
        pending = list(self.root_node_ids)
        while pending:
            node_id = pending.pop()
            if node_id in visited:
                raise ValueError("matrix hierarchy contains a cycle")
            visited.add(node_id)
            pending.extend(node_by_id[node_id].child_ids)
        if visited != set(node_by_id):
            raise ValueError("matrix hierarchy contains disconnected nodes")
        table_ids = tuple(table.logical_table_id for table in self.logical_tables)
        if len(table_ids) != len(set(table_ids)):
            raise ValueError("matrix logical table IDs must be unique")
        known_table_ids = set(table_ids)
        table_by_id = {
            table.logical_table_id: table for table in self.logical_tables
        }
        for node in self.nodes:
            if any(
                logical_table_id not in known_table_ids
                for logical_table_id in node.logical_table_ids
            ):
                raise ValueError(
                    "matrix node references a logical table outside logical_tables"
                )
        record_table_ids: dict[str, set[str]] = {}
        for table in self.logical_tables:
            if any(node_id not in node_by_id for node_id in table.node_ids):
                raise ValueError("matrix logical table references an unknown node")
            expected_node_ids = {
                node.node_id
                for node in self.nodes
                if table.logical_table_id in node.logical_table_ids
            }
            if set(table.node_ids) != expected_node_ids:
                raise ValueError(
                    "matrix logical table node IDs must exactly match node references"
                )

            projection = table.projection
            projection_id = str(projection.get("projection_id") or "").strip()
            if projection_id != table.projection_id:
                raise ValueError(
                    "matrix logical table projection_id must match projection"
                )
            projection_mode = str(
                getattr(projection.get("mode"), "value", projection.get("mode"))
                or ""
            ).strip()
            if projection_mode != table.projection_mode:
                raise ValueError(
                    "matrix logical table projection_mode must match projection"
                )
            if projection_mode not in {"RECORDS", "WHOLE_TABLE"}:
                raise ValueError("matrix logical table projection mode is invalid")

            table_fact = projection.get("table_fact")
            if not isinstance(table_fact, dict):
                raise ValueError(
                    "matrix logical table projection requires a table_fact"
                )
            if (
                str(table_fact.get("logical_table_id") or "").strip()
                != table.logical_table_id
            ):
                raise ValueError(
                    "matrix logical table ID must match projection table_fact"
                )
            table_parts = table_fact.get("table_parts")
            if not isinstance(table_parts, (list, tuple)):
                raise ValueError(
                    "matrix logical table projection requires table parts"
                )
            projected_physical_ids: list[str] = []
            for part in table_parts:
                if not isinstance(part, dict):
                    raise ValueError(
                        "matrix logical table projection table parts are invalid"
                    )
                table_id = str(part.get("table_id") or "").strip()
                if not table_id:
                    raise ValueError(
                        "matrix logical table projection table part ID is blank"
                    )
                projected_physical_ids.append(table_id)
            if tuple(projected_physical_ids) != table.physical_table_ids:
                raise ValueError(
                    "matrix logical table physical table IDs must match projection"
                )

            records = projection.get("records")
            if not isinstance(records, (list, tuple)):
                raise ValueError(
                    "matrix logical table projection requires a records collection"
                )
            if len(records) != table.record_count:
                raise ValueError(
                    "matrix logical table record_count must match projection"
                )
            record_ids: list[str] = []
            for record in records:
                if not isinstance(record, dict):
                    raise ValueError(
                        "matrix logical table projection records are invalid"
                    )
                record_id = str(record.get("record_id") or "").strip()
                if not record_id:
                    raise ValueError(
                        "matrix logical table projection record ID is blank"
                    )
                record_ids.append(record_id)
            if len(record_ids) != len(set(record_ids)):
                raise ValueError(
                    "matrix logical table projection record IDs must be unique"
                )
            if projection_mode == "WHOLE_TABLE" and record_ids:
                raise ValueError(
                    "WHOLE_TABLE matrix logical tables cannot contain records"
                )
            if projection_mode == "RECORDS" and not record_ids:
                raise ValueError(
                    "RECORDS matrix logical tables require projected records"
                )
            if projection_mode == "RECORDS":
                for record_id in record_ids:
                    record_table_ids.setdefault(record_id, set()).add(
                        table.logical_table_id
                    )

        record_bound_roles = {
            MatrixNodeKind.TABLE_RECORD,
            MatrixNodeKind.ATOMIC_REQUIREMENT,
        }
        for node in self.nodes:
            if (
                node.display_role not in record_bound_roles
                or not node.table_record_id
            ):
                continue
            if len(node.logical_table_ids) != 1:
                raise ValueError(
                    "record-bound matrix nodes must reference exactly one logical table"
                )
            logical_table_id = node.logical_table_ids[0]
            logical_table = table_by_id[logical_table_id]
            if logical_table.projection_mode != "RECORDS":
                raise ValueError(
                    "record-bound matrix nodes require a RECORDS logical table"
                )
            if record_table_ids.get(node.table_record_id, set()) != {
                logical_table_id
            }:
                raise ValueError(
                    "matrix table record must belong to exactly one referenced RECORDS table"
                )
        if self.state is MatrixSnapshotState.REQUIRES_REEXTRACTION:
            if self.nodes or self.root_node_ids or self.logical_tables:
                raise ValueError("re-extraction matrix snapshots cannot expose data")
        elif not self.nodes:
            raise ValueError("ready matrix snapshots require nodes")
        if self.counts.total_nodes != len(self.nodes):
            raise ValueError("matrix counts must match the node collection")
        if self.counts.logical_tables != len(self.logical_tables):
            raise ValueError("matrix counts must match logical tables")
        expected_counts = MatrixCountsV1(
            total_nodes=len(self.nodes),
            visible_nodes=sum(
                node.matrix_visibility is MatrixNodeVisibility.VISIBLE
                for node in self.nodes
            ),
            system_non_business_nodes=sum(
                node.matrix_visibility is MatrixNodeVisibility.SYSTEM_NON_BUSINESS
                for node in self.nodes
            ),
            human_excluded_nodes=sum(
                node.matrix_visibility is MatrixNodeVisibility.HUMAN_EXCLUDED
                for node in self.nodes
            ),
            analyze_nodes=sum(
                node.disposition is MatrixNodeDisposition.ANALYZE
                for node in self.nodes
            ),
            retained_nodes=sum(
                node.disposition is MatrixNodeDisposition.RETAIN
                for node in self.nodes
            ),
            excluded_nodes=sum(
                node.disposition is MatrixNodeDisposition.EXCLUDE
                for node in self.nodes
            ),
            analysis_eligible_nodes=sum(
                node.analysis_eligibility is MatrixAnalysisEligibility.ELIGIBLE
                for node in self.nodes
            ),
            assessed_nodes=sum(
                node.analysis_status
                in {MatrixAnalysisStatus.ASSESSED, MatrixAnalysisStatus.DECIDED}
                for node in self.nodes
            ),
            decided_nodes=sum(
                node.analysis_status is MatrixAnalysisStatus.DECIDED
                for node in self.nodes
            ),
            stale_result_nodes=sum(
                node.analysis_result_state
                is MatrixAnalysisResultState.STALE_REFERENCE
                for node in self.nodes
            ),
            logical_tables=len(self.logical_tables),
        )
        if self.counts != expected_counts:
            raise ValueError("matrix counts do not match node states")
        return self


__all__ = [
    "MatrixAiAssessmentV1",
    "MatrixAnalysisEligibility",
    "MatrixAnalysisResultState",
    "MatrixAnalysisStatus",
    "MatrixConclusionContractV1",
    "MatrixConclusionSource",
    "MatrixConclusionSummaryV1",
    "MatrixCountsV1",
    "MatrixLogicalTableV1",
    "MatrixManualDecisionV1",
    "MatrixNodeDisposition",
    "MatrixNodeKind",
    "MatrixNodeStateV1",
    "MatrixNodeVisibility",
    "MatrixSnapshotState",
    "REQUIREMENT_MATRIX_SCHEMA_VERSION",
    "RequirementMatrixSnapshotV1",
    "conclusion_source",
    "count_missing_source_business_nodes",
    "effective_current_conclusion",
    "has_incomplete_terminal_conclusion",
    "is_analysis_target",
    "matrix_conclusion_contract",
    "summarize_effective_conclusions",
]

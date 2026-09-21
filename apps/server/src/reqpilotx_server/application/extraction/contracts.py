from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from reqpilotx_server.domain.extraction.llm_requirement_review import (
    LlmReviewedRequirementNode,
)
from reqpilotx_server.domain.extraction.processing_profile import (
    RunProfileOverrideV1,
)
from reqpilotx_server.domain.extraction.structure_revision import (
    BoundaryResegmentStructureOperationV1,
    BoundarySplitStructureOperationV1,
    StructurePatchOperationV1,
)


class LlmDocumentExtractionStartRequest(BaseModel):
    """Select an explicit visual strategy for one durable extraction run."""

    model_config = {"extra": "forbid"}

    visual_provider: Literal[
        "off",
        "bailian",
        "bailian-qwen3.7-plus-layout",
        "bailian-qwen3.6-flash-layout",
        "azure-document-intelligence",
    ] = "off"


class DocumentExtractionStartRequest(BaseModel):
    """Freeze the project default plus one permitted per-run override."""

    model_config = {"extra": "forbid"}

    schema_version: Literal["document-extraction-start-request/v1"] = (
        "document-extraction-start-request/v1"
    )
    run_profile_override: RunProfileOverrideV1 | None = None


class DocumentExtractionCancelRequest(BaseModel):
    """Cancel exactly the active extraction attempt the user can see."""

    model_config = {"extra": "forbid"}

    schema_version: Literal["document-extraction-cancel-request/v1"] = (
        "document-extraction-cancel-request/v1"
    )
    expected_job_id: str = Field(pattern=r"^(?:job|llm-extraction)-[A-Za-z0-9]+$")


class DocumentExtractionVisualRepairRequest(BaseModel):
    """Retry only the failed visual pages from one exact completed result."""

    model_config = {"extra": "forbid"}

    schema_version: Literal["document-extraction-visual-repair-request/v1"] = (
        "document-extraction-visual-repair-request/v1"
    )
    expected_base_job_id: str = Field(pattern=r"^job-[A-Za-z0-9]+$")
    expected_base_job_generation: int = Field(ge=0)
    expected_base_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class StructureMutationBindingRequest(BaseModel):
    """Optimistic source/runtime/effective identity shared by patch and undo."""

    model_config = {"extra": "forbid"}

    source_ir_id: str = Field(min_length=1, max_length=160)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_ir_adapter_version: str = Field(min_length=1, max_length=160)
    base_structure_snapshot_id: str = Field(min_length=1, max_length=160)
    base_structure_canonical_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    structure_resolver_version: str = Field(min_length=1, max_length=160)
    expected_revision_number: int = Field(ge=0)
    expected_effective_snapshot_id: str = Field(
        pattern=r"^effective-structure-[0-9a-f]{32}$"
    )


class StructurePatchRequest(StructureMutationBindingRequest):
    schema_version: Literal["structure-patch-request/v1"] = (
        "structure-patch-request/v1"
    )
    operations: tuple[StructurePatchOperationV1, ...] = Field(
        min_length=1,
        max_length=64,
    )


class BoundaryCalibrationPreviewRequest(StructureMutationBindingRequest):
    """One exact-line visual boundary edit prepared for side-effect-free preview."""

    schema_version: Literal["boundary-calibration-preview-request/v1"] = (
        "boundary-calibration-preview-request/v1"
    )
    page_number: int = Field(ge=1)
    geometry_index_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    geometry_index_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    operations: tuple[
        BoundarySplitStructureOperationV1
        | BoundaryResegmentStructureOperationV1,
        ...,
    ] = Field(min_length=1, max_length=1)


class StructureUndoRequest(StructureMutationBindingRequest):
    schema_version: Literal["structure-undo-request/v1"] = (
        "structure-undo-request/v1"
    )


class TableRecordProjectionRowCoordinate(BaseModel):
    """One immutable physical row inside a logical table continuation chain."""

    model_config = {"extra": "forbid", "frozen": True}

    table_id: str = Field(min_length=1, max_length=160)
    row_index: int = Field(ge=0)


class TableRecordProjectionRowGroup(BaseModel):
    """One user-confirmed record made from consecutive source table rows."""

    model_config = {"extra": "forbid", "frozen": True}

    rows: tuple[TableRecordProjectionRowCoordinate, ...] = Field(
        min_length=1,
        max_length=10_000,
    )

    @field_validator("rows")
    @classmethod
    def require_unique_rows(
        cls,
        rows: tuple[TableRecordProjectionRowCoordinate, ...],
    ) -> tuple[TableRecordProjectionRowCoordinate, ...]:
        coordinates = tuple((row.table_id, row.row_index) for row in rows)
        if len(coordinates) != len(set(coordinates)):
            raise ValueError("a row group cannot repeat a source row")
        return rows


class TableRecordProjectionRevisionRequest(BaseModel):
    """One explicit correction over a derived table-record projection."""

    model_config = {"extra": "forbid"}

    schema_version: Literal[
        "table-record-projection-revision-request/v1",
        "table-record-projection-revision-request/v2",
    ] = "table-record-projection-revision-request/v1"
    action: Literal[
        "SPLIT_ROWS",
        "MERGE_PREVIOUS",
        "MERGE_NEXT",
        "KEEP_WHOLE",
        "RESTORE_AUTO",
    ]
    expected_projection_revision: int = Field(ge=0)
    expected_source_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    expected_structure_snapshot_id: str | None = Field(
        default=None,
        pattern=r"^effective-structure-[0-9a-f]{32}$",
    )
    record_id: str | None = Field(default=None, min_length=1, max_length=200)
    row_groups: tuple[TableRecordProjectionRowGroup, ...] | None = Field(
        default=None,
        min_length=1,
        max_length=10_000,
    )

    @model_validator(mode="after")
    def validate_action_payload(
        self,
    ) -> TableRecordProjectionRevisionRequest:
        if self.action in {"MERGE_PREVIOUS", "MERGE_NEXT"} and not self.record_id:
            raise ValueError("directional table record merge requires record_id")
        if self.schema_version.endswith("/v1") and self.row_groups is not None:
            raise ValueError("explicit row_groups require request schema v2")
        if self.schema_version.endswith("/v1") and (
            self.expected_source_sha256 is not None
            or self.expected_structure_snapshot_id is not None
        ):
            raise ValueError("explicit source bindings require request schema v2")
        if self.row_groups is not None and self.action != "SPLIT_ROWS":
            raise ValueError("row_groups are valid only for SPLIT_ROWS")
        if (
            self.schema_version.endswith("/v2")
            and self.action == "SPLIT_ROWS"
            and (
                self.row_groups is None
                or self.expected_source_sha256 is None
                or self.expected_structure_snapshot_id is None
            )
        ):
            raise ValueError(
                "schema v2 SPLIT_ROWS requires explicit row_groups and "
                "source/structure bindings"
            )
        return self


class StructureOptimizationStartRequest(BaseModel):
    model_config = {"extra": "forbid"}

    schema_version: Literal["structure-optimization-start-request/v1"] = (
        "structure-optimization-start-request/v1"
    )
    llm_provider_override: Literal["aliyun", "azure_openai"] | None = None


class LlmRequirementReviewUpdateRequest(BaseModel):
    """One reviewer revision over the model's single frozen answer."""

    model_config = {"extra": "forbid"}

    expected_revision: int = Field(ge=0)
    status: Literal["DRAFT", "HUMAN_REVIEWED"] = "DRAFT"
    reviewer_label: str = Field(default="", max_length=120)
    confirmed_review_node_ids: tuple[str, ...] | None = None
    nodes: tuple[LlmReviewedRequirementNode, ...] = Field(min_length=1)


class LlmRequirementConfirmationRequest(BaseModel):
    """A compact confirmation command over one immutable review node."""

    model_config = {"extra": "forbid"}

    expected_revision: int = Field(ge=0)
    review_node_id: str = Field(min_length=1, max_length=200)
    confirmed: bool


class RequirementAtomizationRunRequest(BaseModel):
    model_config = {"extra": "forbid"}

    schema_version: Literal["requirement-atomization-run-request/v1"] = (
        "requirement-atomization-run-request/v1"
    )
    expected_review_revision: int = Field(ge=0)


class RequirementAtomizationScopeRunRequest(BaseModel):
    """One exact, optimistic and explicitly triggered atomization scope."""

    model_config = {"extra": "forbid"}

    schema_version: Literal["requirement-atomization-run-request/v2"] = (
        "requirement-atomization-run-request/v2"
    )
    command_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
    operation: Literal["APPLY", "UNDO"]
    scope: Literal["CURRENT", "SELECTED", "BRANCH", "ALL"]
    expected_review_revision: int = Field(ge=0)
    expected_scope_revision: int = Field(ge=0)
    expected_entry_classification_snapshot_id: str = Field(
        min_length=1,
        max_length=200,
    )
    expected_entry_classification_revision: int = Field(ge=0)
    expected_entry_classification_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_working_tree_id: str = Field(min_length=1, max_length=200)
    expected_working_tree_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_effective_structure_id: str = Field(min_length=1, max_length=200)
    anchor_tree_node_id: str | None = Field(default=None, max_length=260)
    target_review_node_ids: tuple[str, ...] = Field(
        default=(),
        max_length=50_000,
    )
    target_scope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    unselected_policy: Literal["SOURCE_BOUND_PASSTHROUGH"] = "SOURCE_BOUND_PASSTHROUGH"

    @field_validator("target_review_node_ids")
    @classmethod
    def validate_exact_target_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("target_review_node_ids must be unique")
        if any(
            not item
            or len(item) > 260
            or any(ord(character) < 32 for character in item)
            for item in value
        ):
            raise ValueError("target_review_node_ids contain an invalid ID")
        return value

    @field_validator("anchor_tree_node_id")
    @classmethod
    def validate_anchor_tree_node_id(cls, value: str | None) -> str | None:
        if value is not None and (
            not value or any(ord(character) < 32 for character in value)
        ):
            raise ValueError("anchor_tree_node_id is invalid")
        return value

    @model_validator(mode="after")
    def validate_operation_targets(self) -> RequirementAtomizationScopeRunRequest:
        if self.operation == "UNDO":
            if self.scope != "CURRENT":
                raise ValueError("UNDO requires CURRENT scope")
            if self.target_review_node_ids or self.anchor_tree_node_id is not None:
                raise ValueError("UNDO does not accept targets or an anchor")
            return self
        if not self.target_review_node_ids:
            raise ValueError("APPLY requires at least one exact target")
        if self.scope == "CURRENT":
            if (
                len(self.target_review_node_ids) != 1
                or self.anchor_tree_node_id is None
            ):
                raise ValueError("CURRENT requires one exact target and a tree anchor")
        elif self.scope == "BRANCH":
            if self.anchor_tree_node_id is None:
                raise ValueError("BRANCH requires an exact tree anchor")
        elif self.anchor_tree_node_id is not None:
            raise ValueError("SELECTED and ALL do not accept an anchor")
        return self


class RequirementRefinementSuggestionRequest(BaseModel):
    model_config = {"extra": "forbid"}

    schema_version: Literal["requirement-refinement-suggestion-request/v1"] = (
        "requirement-refinement-suggestion-request/v1"
    )
    expected_review_revision: int = Field(ge=0)
    atomization_run_id: str = Field(pattern=r"^atomization-run-[0-9a-f]{32}$")
    atomic_node_id: str = Field(pattern=r"^atomic-obligation-[0-9a-f]{32}$")


class RequirementRefinementDecisionRequest(BaseModel):
    model_config = {"extra": "forbid"}

    schema_version: Literal["requirement-refinement-decision-request/v1"] = (
        "requirement-refinement-decision-request/v1"
    )
    expected_refinement_revision: int = Field(ge=0)
    operation: Literal["REVISE", "ACCEPT", "REJECT"]
    suggested_statement: str | None = Field(
        default=None,
        min_length=1,
        max_length=500_000,
    )
    rationale: str | None = Field(default=None, max_length=20_000)
    decision_note: str | None = Field(default=None, max_length=20_000)


class RequirementPublicationPreviewRequest(BaseModel):
    model_config = {"extra": "forbid"}

    schema_version: Literal[
        "requirement-enrichment-publication-preview-request/v1"
    ] = "requirement-enrichment-publication-preview-request/v1"
    expected_review_revision: int = Field(ge=0)
    atomization_run_id: str = Field(pattern=r"^atomization-run-[0-9a-f]{32}$")


class RequirementPublicationPreviewRequestV2(BaseModel):
    """Bind one exact accepted W3 classification to W4 publication semantics."""

    model_config = {"extra": "forbid"}

    schema_version: Literal[
        "requirement-enrichment-publication-preview-request/v2"
    ] = "requirement-enrichment-publication-preview-request/v2"
    expected_review_revision: int = Field(ge=0)
    accepted_entry_classification_snapshot_id: str = Field(
        min_length=1,
        max_length=200,
    )
    accepted_entry_classification_revision: int = Field(ge=1)
    accepted_entry_classification_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    atomization_state: Literal["NOT_RUN", "APPLIED"]
    atomization_scope_receipt_id: str = Field(default="", max_length=80)
    atomization_scope_receipt_sha256: str = Field(default="", max_length=64)
    atomization_scope_revision: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_atomization_binding(self) -> RequirementPublicationPreviewRequestV2:
        if self.atomization_state == "NOT_RUN":
            if any(
                (
                    self.atomization_scope_receipt_id,
                    self.atomization_scope_receipt_sha256,
                    self.atomization_scope_revision,
                )
            ):
                raise ValueError("NOT_RUN publication cannot bind atomization scope")
            return self
        if (
            not re.fullmatch(
                r"atomization-scope-receipt-[0-9a-f]{32}",
                self.atomization_scope_receipt_id,
            )
            or not re.fullmatch(
                r"[0-9a-f]{64}",
                self.atomization_scope_receipt_sha256,
            )
            or self.atomization_scope_revision < 1
        ):
            raise ValueError("APPLIED publication requires an exact scope receipt")
        return self


class RequirementPublishDraftRequest(BaseModel):
    model_config = {"extra": "forbid"}

    schema_version: Literal[
        "requirement-enrichment-publish-draft-request/v1"
    ] = "requirement-enrichment-publish-draft-request/v1"
    expected_review_revision: int = Field(ge=0)
    atomization_run_id: str = Field(pattern=r"^atomization-run-[0-9a-f]{32}$")
    preview_id: str = Field(
        pattern=r"^requirement-publication-preview-[0-9a-f]{32}$"
    )
    publication_key: str = Field(pattern=r"^[0-9a-f]{64}$")


class RequirementPublishDraftRequestV2(BaseModel):
    model_config = {"extra": "forbid"}

    schema_version: Literal[
        "requirement-enrichment-publish-draft-request/v2"
    ] = "requirement-enrichment-publish-draft-request/v2"
    expected_review_revision: int = Field(ge=0)
    binding_preview_id: str = Field(
        pattern=r"^requirement-enrichment-binding-preview-[0-9a-f]{32}$"
    )
    binding_preview_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class RestorationRunRequest(BaseModel):
    """Start or reuse one exact-route, page-addressable restoration run."""

    model_config = {"extra": "forbid"}

    visual_provider: Literal[
        "off",
        "bailian",
        "bailian-qwen3.7-plus-layout",
        "bailian-qwen3.6-flash-layout",
        "azure-document-intelligence",
    ] = "off"
    initial_page: int = Field(default=1, ge=1)
    force: bool = False


__all__ = [
    "BoundaryCalibrationPreviewRequest",
    "DocumentExtractionStartRequest",
    "DocumentExtractionVisualRepairRequest",
    "LlmDocumentExtractionStartRequest",
    "LlmRequirementConfirmationRequest",
    "LlmRequirementReviewUpdateRequest",
    "RequirementAtomizationRunRequest",
    "RequirementAtomizationScopeRunRequest",
    "RequirementPublicationPreviewRequest",
    "RequirementPublicationPreviewRequestV2",
    "RequirementPublishDraftRequest",
    "RequirementPublishDraftRequestV2",
    "RequirementRefinementDecisionRequest",
    "RequirementRefinementSuggestionRequest",
    "RestorationRunRequest",
    "StructureMutationBindingRequest",
    "StructureOptimizationStartRequest",
    "StructurePatchRequest",
    "TableRecordProjectionRowCoordinate",
    "TableRecordProjectionRowGroup",
    "TableRecordProjectionRevisionRequest",
    "StructureUndoRequest",
]

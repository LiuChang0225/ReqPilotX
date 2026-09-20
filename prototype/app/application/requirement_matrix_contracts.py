from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from app.domain.requirement_matrix import MatrixNodeDisposition


class MatrixDispositionPatchV1(BaseModel):
    schema_version: str = Field(
        default="requirement-matrix-disposition-patch/v1",
        pattern=r"^requirement-matrix-disposition-patch/v1$",
    )
    disposition: MatrixNodeDisposition
    reason: str = Field(default="", max_length=4000)
    expected_revision: int = Field(ge=0)


class MatrixDispositionBatchItemV1(BaseModel):
    node_id: str = Field(min_length=1, max_length=260)
    disposition: MatrixNodeDisposition
    reason: str = Field(default="", max_length=4000)
    expected_revision: int = Field(ge=0)


class MatrixDispositionBatchPatchV1(BaseModel):
    schema_version: str = Field(
        default="requirement-matrix-disposition-batch-patch/v1",
        pattern=r"^requirement-matrix-disposition-batch-patch/v1$",
    )
    expected_tree_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    items: tuple[MatrixDispositionBatchItemV1, ...] = Field(
        min_length=1,
        max_length=2000,
    )

    @model_validator(mode="after")
    def validate_unique_nodes(self) -> "MatrixDispositionBatchPatchV1":
        node_ids = tuple(item.node_id for item in self.items)
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("matrix disposition batch node IDs must be unique")
        return self


class MatrixDecisionRequestV1(BaseModel):
    schema_version: str = Field(
        default="requirement-matrix-decision-request/v1",
        pattern=r"^requirement-matrix-decision-request/v1$",
    )
    final_decision: str = Field(
        pattern="^(ACCEPT|REJECT|DEVIATION_ACCEPT|NEED_CLARIFICATION)$"
    )
    engineer_reason: str = Field(default="", max_length=4000)
    expected_revision: int = Field(ge=0)


__all__ = [
    "MatrixDecisionRequestV1",
    "MatrixDispositionBatchItemV1",
    "MatrixDispositionBatchPatchV1",
    "MatrixDispositionPatchV1",
]

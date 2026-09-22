"""Strict, identity-only catalog DTOs for the reading workspace."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr


_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class _StrictFrozenCatalogModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class ReadingProjectCatalogItemV1(_StrictFrozenCatalogModel):
    id: StrictStr = Field(min_length=1, max_length=160)
    name: StrictStr = Field(min_length=1, max_length=500)
    customer_name: StrictStr = Field(max_length=500)


class ReadingProjectCatalogV1(_StrictFrozenCatalogModel):
    schema_version: Literal["reading-project-catalog/v1"] = (
        "reading-project-catalog/v1"
    )
    items: tuple[ReadingProjectCatalogItemV1, ...] = ()


class ReadingDocumentCatalogItemV1(_StrictFrozenCatalogModel):
    id: StrictStr = Field(min_length=1, max_length=160)
    project_id: StrictStr = Field(min_length=1, max_length=160)
    file_name: StrictStr = Field(min_length=1, max_length=1_000)
    file_type: StrictStr = Field(max_length=64)
    detected_format: StrictStr = Field(max_length=64)
    source_sha256: StrictStr | None = Field(
        default=None,
        pattern=_SHA256_PATTERN,
    )
    source_size: StrictInt = Field(ge=0)
    created_at: StrictStr = Field(min_length=1, max_length=80)


class ReadingDocumentCatalogV1(_StrictFrozenCatalogModel):
    schema_version: Literal["reading-document-catalog/v1"] = (
        "reading-document-catalog/v1"
    )
    project_id: StrictStr = Field(min_length=1, max_length=160)
    items: tuple[ReadingDocumentCatalogItemV1, ...] = ()


__all__ = [
    "ReadingDocumentCatalogItemV1",
    "ReadingDocumentCatalogV1",
    "ReadingProjectCatalogItemV1",
    "ReadingProjectCatalogV1",
]

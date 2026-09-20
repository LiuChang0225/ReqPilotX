from pydantic import BaseModel, Field


class ExportRequest(BaseModel):
    version_label: str | None = Field(default=None, max_length=64)
    compare_export_id: str | None = Field(default=None, max_length=64)
    document_id: str | None = None
    requirement_set_id: str = Field(min_length=1, max_length=200)
    mode: str = Field(default="FORMAL", pattern="^(DRAFT|FORMAL)$")


__all__ = ["ExportRequest"]

from pydantic import BaseModel, Field


class CreateProjectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    customer_name: str = Field(min_length=1, max_length=255)
    command_id: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$"
    )


__all__ = ["CreateProjectRequest"]

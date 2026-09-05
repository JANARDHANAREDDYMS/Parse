from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CatalogSkuInput(BaseModel):
    """Validated shape of one record in the supplied SKU catalog."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: UUID
    sku_code: str = Field(min_length=1, max_length=255)
    name: str = Field(min_length=1, max_length=512)
    description: str | None = None
    parsing_instructions: str | None = None
    usage_count: int = Field(ge=0)

    @field_validator("sku_code")
    @classmethod
    def validate_sku_code(cls, value: str) -> str:
        if not value.replace("_", "").isalnum():
            raise ValueError("sku_code must contain only letters, numbers, and underscores")
        return value


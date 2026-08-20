from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class ProviderManifest(BaseModel):
    id: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9._-]+$")
    name: str = Field(min_length=1, max_length=256)
    version: str = Field(min_length=1, max_length=64)
    protocol_version: str = "1"
    transport: str = "stdio-jsonl"
    command: list[str] = Field(min_length=1)
    capabilities: list[str] = Field(min_length=1)
    metadata: dict[str, object] = Field(default_factory=dict)

    @field_validator("transport")
    @classmethod
    def supported_transport(cls, value: str) -> str:
        if value != "stdio-jsonl":
            raise ValueError("only stdio-jsonl is supported in protocol v1")
        return value


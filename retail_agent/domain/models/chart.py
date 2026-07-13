from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

type ChartFormat = Literal["png", "svg"]


class ChartRequest(BaseModel):
    code: str
    data: list[dict[str, Any]]
    output_format: ChartFormat = "png"


class ChartArtifact(BaseModel):
    path: str
    output_format: ChartFormat
    size_bytes: int = Field(ge=1)
    code_digest: str

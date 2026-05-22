from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, HttpUrl


class Param(BaseModel):
    """A path or query parameter, typed against the vertical taxonomy."""

    name: str
    taxi_type: str
    location: str = "query"  # "path" | "query"
    required: bool = False
    description: str | None = None


class ResponseField(BaseModel):
    """An explicit, curated mapping of a response field to a taxonomy concept."""

    name: str  # Taxi-safe field name
    taxi_type: str
    json_path: str  # accessor into the response JSON, e.g. "$.location.latitude"
    description: str | None = None


class Endpoint(BaseModel):
    path: str
    method: str = "GET"
    description: str | None = None
    model_name: str | None = None  # name of the generated response model
    collection: bool = False  # operation returns multiple records
    items_path: str | None = None  # jsonPath to the array when the response is wrapped
    params: list[Param] = Field(default_factory=list)
    response_fields: list[ResponseField] = Field(default_factory=list)
    response_schema: dict[str, Any] | None = None
    sample_fields: list[str] = Field(default_factory=list)

    @property
    def callable(self) -> bool:
        """True when this endpoint has enough metadata to generate a Taxi HTTP operation."""
        return bool(self.params or self.response_fields)


class ApiRecord(BaseModel):
    id: str
    name: str
    vertical: str
    provider: str
    base_url: HttpUrl | str
    description: str | None = None
    docs_url: HttpUrl | str | None = None
    openapi_url: HttpUrl | str | None = None
    auth: str = "unknown"
    licence: str | None = None
    tags: list[str] = Field(default_factory=list)
    endpoints: list[Endpoint] = Field(default_factory=list)
    source: str = "curated"

    @property
    def all_fields(self) -> list[str]:
        seen: set[str] = set()
        fields: list[str] = []
        for endpoint in self.endpoints:
            for field in endpoint.sample_fields:
                if field not in seen:
                    fields.append(field)
                    seen.add(field)
        return fields


class ApiCatalogue(BaseModel):
    vertical: str
    records: list[ApiRecord] = Field(default_factory=list)

    def save_yaml(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = self.model_dump(mode="json", exclude_none=True)
        path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")

    @classmethod
    def load_yaml(cls, path: Path) -> "ApiCatalogue":
        return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))

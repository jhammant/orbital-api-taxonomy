from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, HttpUrl


class Endpoint(BaseModel):
    path: str
    method: str = "GET"
    description: str | None = None
    response_schema: dict[str, Any] | None = None
    sample_fields: list[str] = Field(default_factory=list)


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

from __future__ import annotations

from collections.abc import Iterator
from typing import Any


def flatten_schema_fields(schema: dict[str, Any], prefix: str = "") -> list[str]:
    """Extract dotted field paths from a small subset of JSON Schema/OpenAPI schemas."""
    return list(_walk(schema, prefix))


def _walk(schema: dict[str, Any], prefix: str) -> Iterator[str]:
    if "$ref" in schema:
        return
    schema_type = schema.get("type")
    if schema_type == "array" and isinstance(schema.get("items"), dict):
        yield from _walk(schema["items"], prefix)
        return
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for name, child in properties.items():
            path = f"{prefix}.{name}" if prefix else name
            yield path
            if isinstance(child, dict):
                yield from _walk(child, path)

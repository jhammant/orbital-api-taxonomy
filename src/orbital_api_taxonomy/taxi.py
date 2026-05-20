from __future__ import annotations

from collections import defaultdict

from .catalog import ApiCatalogue, ApiRecord
from .mapper import FieldMapping
from .taxonomies import VerticalTaxonomy

BASE_TYPE_OVERRIDES = {
    "Latitude": "Decimal",
    "Longitude": "Decimal",
    "ObservationValue": "Decimal",
    "ObservationTime": "Instant",
    "PublicationDate": "Date",
}


def render_taxonomy(taxonomy: VerticalTaxonomy) -> str:
    lines = [f"namespace {taxonomy.namespace}", ""]
    lines.append(f"// {taxonomy.description}")
    for concept in taxonomy.concepts:
        base = BASE_TYPE_OVERRIDES.get(concept.taxi_type, "String")
        lines.append(f"type {concept.taxi_type} inherits {base} // {concept.description}")
    lines.append("")
    return "\n".join(lines)


def render_api_models(api: ApiRecord, mappings: list[FieldMapping], namespace: str) -> str:
    safe_name = _pascal(api.id)
    by_endpoint: dict[str, list[FieldMapping]] = defaultdict(list)
    for mapping in mappings:
        by_endpoint[mapping.endpoint_path].append(mapping)

    lines = [f"namespace {namespace}.apis.{_safe_namespace(api.id)}", ""]
    lines.append(f"// {api.name} — {api.base_url}")
    if api.docs_url:
        lines.append(f"// Docs: {api.docs_url}")
    lines.append("")

    for index, endpoint in enumerate(api.endpoints, start=1):
        endpoint_mappings = by_endpoint.get(endpoint.path, [])
        if not endpoint_mappings:
            continue
        model_name = f"{safe_name}Response{index}"
        lines.append(f"model {model_name} {{")
        for mapping in endpoint_mappings:
            field_name = _field_name(mapping.source_field)
            lines.append(f"  {field_name}: {namespace}.{mapping.taxi_type}")
        lines.append("}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_orbital_services(catalogue: ApiCatalogue, mappings_by_api: dict[str, list[FieldMapping]], namespace: str) -> str:
    lines = [f"namespace {namespace}.services", ""]
    for api in catalogue.records:
        mappings = mappings_by_api.get(api.id, [])
        if not mappings:
            continue
        service_name = f"{_pascal(api.id)}Service"
        lines.append(f"service {service_name} {{")
        lines.append(f"  // Base URL: {api.base_url}")
        lines.append(f"  // Auth: {api.auth}")
        for index, endpoint in enumerate(api.endpoints, start=1):
            if not any(m.endpoint_path == endpoint.path for m in mappings):
                continue
            operation_name = _operation_name(endpoint.path, index)
            model_name = f"{namespace}.apis.{_safe_namespace(api.id)}.{_pascal(api.id)}Response{index}"
            lines.append(f"  operation {operation_name}(): {model_name}")
        lines.append("}")
        lines.append("")
    return "\n".join(lines)


def _safe_namespace(value: str) -> str:
    return value.replace("-", "_").replace(".", "_")


def _pascal(value: str) -> str:
    return "".join(part.capitalize() for part in re_split(value))


def _field_name(value: str) -> str:
    parts = re_split(value)
    if not parts:
        return "field"
    return parts[0].lower() + "".join(part.capitalize() for part in parts[1:])


def _operation_name(path: str, index: int) -> str:
    parts = re_split(path.strip("/{}") or f"endpoint_{index}")
    return "get" + "".join(part.capitalize() for part in parts[:5])


def re_split(value: str) -> list[str]:
    import re

    return [part for part in re.split(r"[^A-Za-z0-9]+", value) if part]

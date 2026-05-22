from __future__ import annotations

import re
from collections import defaultdict

from .catalog import ApiCatalogue, ApiRecord, Endpoint
from .mapper import FieldMapping
from .taxonomies import VerticalTaxonomy

# Taxi base types for concepts that are not plain strings.
#
# Date/time concepts deliberately inherit String, not Date/Instant: real gov APIs
# emit a wide range of date formats and Orbital fails the whole record when a
# strict temporal type cannot parse a value. The semantic meaning lives in the
# concept name; keeping the wire type permissive keeps live queries working.
BASE_TYPE_OVERRIDES = {
    "Latitude": "Decimal",
    "Longitude": "Decimal",
    "ObservationValue": "Decimal",
}

# taxi.http annotations Orbital ships with — imported into every service file.
HTTP_IMPORTS = (
    "import taxi.http.HttpService",
    "import taxi.http.HttpOperation",
    "import taxi.http.PathVariable",
    "import taxi.http.QueryVariable",
)


def render_taxonomy(taxonomy: VerticalTaxonomy) -> str:
    lines = [f"namespace {taxonomy.namespace}", ""]
    lines.append(f"// {taxonomy.description}")
    for concept in taxonomy.concepts:
        base = BASE_TYPE_OVERRIDES.get(concept.taxi_type, "String")
        lines.append(f"type {concept.taxi_type} inherits {base} // {concept.description}")
    lines.append("")
    return "\n".join(lines)


def _endpoint_fields(
    endpoint: Endpoint, mappings: list[FieldMapping]
) -> list[tuple[str, str, str]]:
    """Return (field_name, taxi_type, json_path) triples for an endpoint's response model.

    Curated endpoints carry explicit ``response_fields``; discovered endpoints fall
    back to the fuzzy field mappings derived from ``sample_fields``.
    """
    if endpoint.response_fields:
        return [(f.name, f.taxi_type, f.json_path) for f in endpoint.response_fields]
    triples: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for mapping in mappings:
        field_name = _field_name(mapping.source_field)
        if field_name in seen:
            continue
        seen.add(field_name)
        triples.append((field_name, mapping.taxi_type, "$." + mapping.source_field))
    return triples


def render_api_models(api: ApiRecord, mappings: list[FieldMapping], namespace: str) -> str:
    """Render the response models for one API into its own namespace."""
    by_endpoint: dict[str, list[FieldMapping]] = defaultdict(list)
    for mapping in mappings:
        by_endpoint[mapping.endpoint_path].append(mapping)

    api_ns = f"{namespace}.apis.{_safe_namespace(api.id)}"
    lines = [f"namespace {api_ns}", ""]
    lines.append(f"// {api.name} — {api.base_url}")
    if api.docs_url:
        lines.append(f"// Docs: {api.docs_url}")
    lines.append("")

    for index, endpoint in enumerate(api.endpoints, start=1):
        fields = _endpoint_fields(endpoint, by_endpoint.get(endpoint.path, []))
        if not fields:
            continue
        model_name = endpoint.model_name or f"{_pascal(api.id)}Response{index}"
        if endpoint.description:
            lines.append(f"// {endpoint.description}")
        lines.append(f"model {model_name} {{")
        for field_name, taxi_type, json_path in fields:
            lines.append(f'  {field_name} : {namespace}.{taxi_type} by jsonPath("{json_path}")')
        lines.append("}")
        lines.append("")
        # Wrapped collections: an envelope model exposes the inner array.
        # The field is bound by attribute name (matching the JSON wrapper key)
        # rather than jsonPath — Orbital does not deserialise a model-level
        # jsonPath accessor onto a collection-typed field.
        if endpoint.items_path:
            wrapper_key = endpoint.items_path.split(".")[-1]
            lines.append(f"model {model_name}List {{")
            lines.append(f"  {wrapper_key} : {model_name}[]")
            lines.append("}")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _return_type(endpoint: Endpoint, api_ns: str, model_name: str) -> str:
    if endpoint.items_path:
        return f"{api_ns}.{model_name}List"
    if endpoint.collection:
        return f"{api_ns}.{model_name}[]"
    return f"{api_ns}.{model_name}"


def render_orbital_services(
    catalogue: ApiCatalogue, mappings_by_api: dict[str, list[FieldMapping]], namespace: str
) -> str:
    """Render Taxi services. Curated endpoints become callable @HttpOperations."""
    any_http = any(
        endpoint.callable for api in catalogue.records for endpoint in api.endpoints
    )

    lines: list[str] = []
    if any_http:
        lines.extend(HTTP_IMPORTS)
        lines.append("")
    lines.extend([f"namespace {namespace}.services", ""])

    for api in catalogue.records:
        mappings = mappings_by_api.get(api.id, [])
        api_ns = f"{namespace}.apis.{_safe_namespace(api.id)}"
        rendered = _render_service(api, mappings, namespace, api_ns)
        if rendered:
            lines.extend(rendered)
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _render_service(
    api: ApiRecord, mappings: list[FieldMapping], namespace: str, api_ns: str
) -> list[str] | None:
    by_endpoint: dict[str, list[FieldMapping]] = defaultdict(list)
    for mapping in mappings:
        by_endpoint[mapping.endpoint_path].append(mapping)

    operations: list[str] = []
    for index, endpoint in enumerate(api.endpoints, start=1):
        fields = _endpoint_fields(endpoint, by_endpoint.get(endpoint.path, []))
        if not fields and not endpoint.callable:
            continue
        model_name = endpoint.model_name or f"{_pascal(api.id)}Response{index}"
        return_type = _return_type(endpoint, api_ns, model_name)

        if endpoint.callable:
            operations.extend(_render_operation(api, endpoint, namespace, return_type))
        else:
            # Discovered API: data model is known, but the call is not wired.
            operations.append(f"  // {endpoint.method} {endpoint.path} (not callable)")
            operations.append(f"  operation {_operation_name(endpoint.path, index)}() : {return_type}")

    if not operations:
        return None

    has_http = any(endpoint.callable for endpoint in api.endpoints)
    header: list[str] = [f"// {api.name} — auth: {api.auth}"]
    if has_http:
        header.append(f'@HttpService(baseUrl = "{api.base_url}")')
    header.append(f"service {_pascal(api.id)}Service {{")
    return header + operations + ["}"]


def _render_operation(
    api: ApiRecord, endpoint: Endpoint, namespace: str, return_type: str
) -> list[str]:
    # url is relative to @HttpService(baseUrl) — Orbital joins the two.
    url = endpoint.path
    params: list[str] = []
    for param in endpoint.params:
        annotation = "PathVariable" if param.location == "path" else "QueryVariable"
        # Optional query params are nullable so Orbital still binds the operation
        # when the caller has no value for them.
        optional = "?" if (param.location == "query" and not param.required) else ""
        params.append(
            f'@{annotation}("{param.name}") {param.name} : {namespace}.{param.taxi_type}{optional}'
        )
    signature = ", ".join(params)
    operation_name = f"get{endpoint.model_name}" if endpoint.model_name else _operation_name(endpoint.path, 1)
    return [
        f'  @HttpOperation(method = "{endpoint.method}", url = "{url}")',
        f"  operation {operation_name}( {signature} ) : {return_type}",
    ]


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
    return [part for part in re.split(r"[^A-Za-z0-9]+", value) if part]

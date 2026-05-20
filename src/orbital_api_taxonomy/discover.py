from __future__ import annotations

import re
from html import unescape
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
import yaml

from .catalog import ApiCatalogue, ApiRecord, Endpoint


DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "catalogs"
DEFAULT_TIMEOUT = httpx.Timeout(20.0, connect=10.0)

VERTICAL_TERMS = {
    "gov-uk": [
        "api",
        "government api",
        "council api",
        "transport api",
        "planning api",
        "public register api",
        "statistics api",
        "environment api",
    ],
    "healthcare": ["health api", "fhir", "nhs api", "clinical api", "medicine api"],
    "finance": ["finance api", "open banking", "payments api", "market data api"],
}

APIS_GURU_CATEGORIES = {
    # For gov-uk, category-only matching is too broad in APIs.guru (eg. generic AWS APIs).
    # Use text/domain matching instead.
    "gov-uk": set(),
    "healthcare": {"open_data"},
    "finance": {"financial"},
}


def load_seed_catalogue(vertical: str) -> ApiCatalogue:
    path = DATA_DIR / f"{vertical}.seed.yaml"
    if not path.exists():
        raise FileNotFoundError(f"No seed catalogue for vertical '{vertical}' at {path}")
    return ApiCatalogue.load_yaml(path)


def discover_catalogue(vertical: str, max_records: int = 250) -> ApiCatalogue:
    """Discover public API candidates from supported public registries.

    This intentionally favours high-recall candidate discovery over perfect precision.
    Downstream mapping/provenance review decides what becomes trusted taxonomy input.
    """
    catalogues = [load_seed_catalogue(vertical)]
    if vertical == "gov-uk":
        catalogues.append(discover_api_gov_uk(max_records=max_records))
        catalogues.append(discover_data_gov_uk(max_records=max_records))
    catalogues.append(discover_apis_guru(vertical=vertical, max_records=max_records))
    return enrich_openapi_endpoints(merge_catalogues(*catalogues))


def enrich_openapi_endpoints(catalogue: ApiCatalogue, max_operations_per_api: int = 8) -> ApiCatalogue:
    """Fetch OpenAPI specs and extract response fields for taxonomy mapping."""
    enriched: list[ApiRecord] = []
    with httpx.Client(timeout=DEFAULT_TIMEOUT, follow_redirects=True) as client:
        for record in catalogue.records:
            if not record.openapi_url:
                enriched.append(record)
                continue
            try:
                spec = _fetch_openapi_spec(client, str(record.openapi_url))
                endpoints = _endpoints_from_openapi(spec, max_operations=max_operations_per_api)
            except Exception:
                endpoints = []
            if endpoints:
                record = record.model_copy(update={"endpoints": endpoints})
            enriched.append(record)
    return ApiCatalogue(vertical=catalogue.vertical, records=enriched)


def discover_api_gov_uk(max_records: int = 500) -> ApiCatalogue:
    """Discover APIs listed in the UK government's public API Catalogue.

    api.gov.uk is the highest-signal source for UK public-sector APIs.  The A-Z
    page includes duplicates in navigation and page body, so we dedupe by path
    before fetching detail pages.
    """
    records: dict[str, ApiRecord] = {}
    base = "https://www.api.gov.uk"
    with httpx.Client(timeout=DEFAULT_TIMEOUT, follow_redirects=True) as client:
        index_html = client.get(f"{base}/index/").text
        for href, title in _api_gov_uk_index_links(index_html):
            if len(records) >= max_records:
                break
            page_url = urljoin(base, href)
            try:
                page_html = client.get(page_url).text
            except httpx.HTTPError:
                page_html = ""
            record = _record_from_api_gov_uk_page(href, title, page_url, page_html)
            records[record.id] = record
    return ApiCatalogue(vertical="gov-uk", records=list(records.values()))


def discover_data_gov_uk(max_records: int = 250) -> ApiCatalogue:
    records: dict[str, ApiRecord] = {}
    with httpx.Client(timeout=DEFAULT_TIMEOUT, follow_redirects=True) as client:
        for term in VERTICAL_TERMS["gov-uk"]:
            if len(records) >= max_records:
                break
            response = client.get(
                "https://data.gov.uk/api/3/action/package_search",
                params={"q": term, "rows": min(100, max_records), "start": 0},
            )
            response.raise_for_status()
            for package in response.json().get("result", {}).get("results", []):
                record = _record_from_ckan_package(package)
                if record:
                    records.setdefault(record.id, record)
                if len(records) >= max_records:
                    break
    return ApiCatalogue(vertical="gov-uk", records=list(records.values()))


def discover_apis_guru(vertical: str, max_records: int = 250) -> ApiCatalogue:
    categories = APIS_GURU_CATEGORIES.get(vertical, set())
    records: dict[str, ApiRecord] = {}
    with httpx.Client(timeout=DEFAULT_TIMEOUT, follow_redirects=True) as client:
        response = client.get("https://api.apis.guru/v2/list.json")
        response.raise_for_status()
        listing = response.json()
    for provider_name, provider in listing.items():
        preferred = provider.get("preferred")
        version = provider.get("versions", {}).get(preferred, {}) if preferred else {}
        info = version.get("info", {})
        api_categories = set(info.get("x-apisguru-categories", []) or [])
        text = " ".join(
            str(part)
            for part in [
                provider_name,
                info.get("title", ""),
                info.get("description", ""),
                " ".join(api_categories),
            ]
        ).lower()
        if not _matches_vertical(vertical, text, api_categories, categories):
            continue
        base_url = _base_url_from_swagger(version.get("swaggerUrl") or info.get("x-origin", [{}])[0].get("url"))
        record = ApiRecord(
            id="apisguru-" + slugify(provider_name),
            name=info.get("title") or provider_name,
            vertical=vertical,
            provider=provider_name,
            base_url=base_url or "https://api.apis.guru",
            description=info.get("description"),
            docs_url=info.get("contact", {}).get("url"),
            openapi_url=version.get("swaggerUrl") or version.get("swaggerYamlUrl"),
            auth="unknown",
            tags=sorted(api_categories) or ["apis-guru"],
            source="apis.guru",
            endpoints=[],
        )
        records[record.id] = record
        if len(records) >= max_records:
            break
    return ApiCatalogue(vertical=vertical, records=list(records.values()))


def merge_catalogues(*catalogues: ApiCatalogue) -> ApiCatalogue:
    if not catalogues:
        raise ValueError("At least one catalogue is required")
    vertical = catalogues[0].vertical
    records = {}
    for catalogue in catalogues:
        if catalogue.vertical != vertical:
            raise ValueError("Cannot merge catalogues from different verticals")
        for record in catalogue.records:
            records[record.id] = record
    return ApiCatalogue(vertical=vertical, records=sorted(records.values(), key=lambda record: record.id))


def _api_gov_uk_index_links(html: str) -> list[tuple[str, str]]:
    seen: dict[str, str] = {}
    for href, title in re.findall(r'<a[^>]+href=["\'](/[^"\']+/[^"\']+/)["\'][^>]*>(.*?)</a>', html, flags=re.S):
        parts = [part for part in href.split("/") if part]
        if len(parts) != 2:
            continue
        clean_title = unescape(re.sub(r"<.*?>", "", title, flags=re.S)).strip()
        if clean_title:
            seen[href] = clean_title
    return sorted(seen.items(), key=lambda item: item[0])


def _record_from_api_gov_uk_page(path: str, title: str, page_url: str, html: str) -> ApiRecord:
    org_slug = [part for part in path.split("/") if part][0]
    text = _html_to_text(html)
    endpoint = _extract_label_value(text, "Endpoint")
    documentation_url = _extract_documentation_url(html) or page_url
    urls = _extract_urls(html)
    openapi_url = next((url for url in urls if _looks_like_openapi_url(url) or "swagger" in url.lower()), None)
    endpoint_url = endpoint or openapi_url or next((url for url in urls if _looks_like_service_url(url)), page_url)
    description = _extract_overview(text)
    return ApiRecord(
        id="apigovuk-" + slugify(path),
        name=title,
        vertical="gov-uk",
        provider=org_slug,
        base_url=_base_url_from_swagger(endpoint_url) or "https://www.api.gov.uk",
        description=description,
        docs_url=documentation_url,
        openapi_url=openapi_url,
        auth="unknown",
        licence=_extract_label_value(text, "Usage licence"),
        tags=["api.gov.uk", org_slug],
        endpoints=[Endpoint(path=endpoint_url, description=title)] if endpoint_url else [],
        source="api.gov.uk",
    )


def _fetch_openapi_spec(client: httpx.Client, url: str) -> dict:
    response = client.get(url, headers={"Accept": "application/json, application/yaml, text/yaml, */*"})
    response.raise_for_status()
    text = response.text
    if url.lower().endswith(".json") or text.lstrip().startswith("{"):
        return response.json()
    loaded = yaml.safe_load(text)
    return loaded if isinstance(loaded, dict) else {}


def _endpoints_from_openapi(spec: dict, max_operations: int = 8) -> list[Endpoint]:
    endpoints: list[Endpoint] = []
    paths = spec.get("paths") or {}
    if not isinstance(paths, dict):
        return endpoints
    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        for method in ["get", "post", "put", "patch"]:
            operation = path_item.get(method)
            if not isinstance(operation, dict):
                continue
            fields = _response_fields_from_operation(operation, spec)
            if fields:
                endpoints.append(
                    Endpoint(
                        path=path,
                        method=method.upper(),
                        description=operation.get("summary") or operation.get("description"),
                        sample_fields=fields[:80],
                    )
                )
            if len(endpoints) >= max_operations:
                return endpoints
    return endpoints


def _response_fields_from_operation(operation: dict, spec: dict) -> list[str]:
    responses = operation.get("responses") or {}
    for status in ["200", "201", "default"] + sorted(str(key) for key in responses):
        response = responses.get(status)
        if not isinstance(response, dict):
            continue
        schema = _schema_from_response(response)
        if schema:
            return _flatten_openapi_schema(schema, spec)
    return []


def _schema_from_response(response: dict) -> dict | None:
    content = response.get("content") or {}
    if isinstance(content, dict):
        for media_type in ["application/json", "application/ld+json", "*/*"] + list(content):
            media = content.get(media_type)
            if isinstance(media, dict) and isinstance(media.get("schema"), dict):
                return media["schema"]
    schema = response.get("schema")
    return schema if isinstance(schema, dict) else None


def _flatten_openapi_schema(schema: dict, spec: dict, prefix: str = "", seen: frozenset[str] = frozenset()) -> list[str]:
    ref = schema.get("$ref")
    if isinstance(ref, str):
        if ref in seen:
            return []
        resolved = _resolve_ref(spec, ref)
        return _flatten_openapi_schema(resolved, spec, prefix, seen | {ref}) if resolved else []
    for combiner in ["allOf", "oneOf", "anyOf"]:
        if isinstance(schema.get(combiner), list):
            fields: list[str] = []
            for child in schema[combiner]:
                if isinstance(child, dict):
                    fields.extend(_flatten_openapi_schema(child, spec, prefix, seen))
            return _dedupe(fields)
    if schema.get("type") == "array" and isinstance(schema.get("items"), dict):
        return _flatten_openapi_schema(schema["items"], spec, prefix, seen)
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return []
    fields: list[str] = []
    for name, child in properties.items():
        path = f"{prefix}.{name}" if prefix else name
        fields.append(path)
        if isinstance(child, dict):
            fields.extend(_flatten_openapi_schema(child, spec, path, seen))
    return _dedupe(fields)


def _resolve_ref(spec: dict, ref: str) -> dict | None:
    if not ref.startswith("#/"):
        return None
    current: object = spec
    for part in ref[2:].split("/"):
        if not isinstance(current, dict):
            return None
        current = current.get(part.replace("~1", "/").replace("~0", "~"))
    return current if isinstance(current, dict) else None


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value not in seen:
            out.append(value)
            seen.add(value)
    return out


def _record_from_ckan_package(package: dict) -> ApiRecord | None:
    resources = package.get("resources", []) or []
    api_resources = [resource for resource in resources if _looks_like_api_resource(resource)]
    if not api_resources and not _looks_like_api_text(package.get("notes", "") + " " + package.get("title", "")):
        return None

    org = package.get("organization") or {}
    provider = org.get("title") or org.get("name") or "data.gov.uk"
    resource = api_resources[0] if api_resources else (resources[0] if resources else {})
    url = resource.get("url") or package.get("url") or f"https://www.data.gov.uk/dataset/{package.get('name')}"
    openapi_url = url if _looks_like_openapi_url(url) else None
    endpoints = []
    if url and not openapi_url:
        endpoints.append(Endpoint(path=url, description=resource.get("description") or package.get("title")))
    return ApiRecord(
        id="datagovuk-" + slugify(package.get("name") or package.get("title") or package.get("id")),
        name=package.get("title") or package.get("name") or "data.gov.uk API candidate",
        vertical="gov-uk",
        provider=provider,
        base_url=_base_url_from_swagger(url) or "https://data.gov.uk",
        description=package.get("notes"),
        docs_url=f"https://www.data.gov.uk/dataset/{package.get('name')}",
        openapi_url=openapi_url,
        auth="unknown",
        licence=package.get("license_title"),
        tags=[tag.get("name") for tag in package.get("tags", []) if tag.get("name")][:10],
        endpoints=endpoints,
        source="data.gov.uk",
    )


def _html_to_text(html: str) -> str:
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _extract_urls(html: str) -> list[str]:
    urls: list[str] = []
    for match in re.finditer(r"https?://[^\s<>\"']+", unescape(html)):
        url = match.group(0).rstrip("),.;]")
        if url not in urls and not any(skip in url for skip in ["images/govuk", "issues/new"]):
            urls.append(url)
    return urls


def _extract_label_value(text: str, label: str) -> str | None:
    match = re.search(rf"\b{re.escape(label)}\b\s+(.+?)(?=\s+(Overview|Usage licence|Documentation|Endpoint|Contact|Geographic area|Report a problem)\b|$)", text)
    if not match:
        return None
    value = match.group(1).strip()
    return value or None


def _extract_overview(text: str) -> str | None:
    match = re.search(r"\bOverview\b\s+(.+?)(?=\s+(Usage licence|Documentation|Endpoint|Contact|Geographic area|Report a problem)\b|$)", text)
    if not match:
        return None
    value = match.group(1).strip()
    return value[:1000] or None


def _extract_documentation_url(html: str) -> str | None:
    # The detail page usually renders a Documentation heading followed by a
    # single link. Keep the parser deliberately simple and dependency-free.
    match = re.search(r"Documentation.*?<a[^>]+href=[\"']([^\"']+)[\"']", html, flags=re.S | re.I)
    if not match:
        return None
    href = unescape(match.group(1))
    return urljoin("https://www.api.gov.uk", href)


def _looks_like_api_resource(resource: dict) -> bool:
    text = " ".join(str(resource.get(key, "")) for key in ["name", "description", "format", "url"]).lower()
    return _looks_like_api_text(text)


def _looks_like_api_text(text: str) -> bool:
    text = text.lower()
    return any(token in text for token in [" api", "api ", "swagger", "openapi", "json endpoint", "rest"])


def _looks_like_openapi_url(url: str | None) -> bool:
    if not url:
        return False
    return bool(re.search(r"(openapi|swagger).*(json|ya?ml)$", url, flags=re.I))


def _looks_like_service_url(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc) and "api.gov.uk" not in parsed.netloc


def _matches_vertical(vertical: str, text: str, api_categories: set[str], target_categories: set[str]) -> bool:
    if api_categories & target_categories:
        return True
    if vertical == "finance":
        return any(term in text for term in ["finance", "financial", "bank", "payment", "trading"])
    if vertical == "healthcare":
        return any(term in text for term in ["health", "medical", "fhir", "clinical", "medicine"])
    if vertical == "gov-uk":
        return any(
            term in text
            for term in [
                "government",
                "parliament",
                "police",
                "transport for london",
                "data.gov",
                ".gov.uk",
                "api.gov.uk",
                "nhs",
                "ordnance survey",
                "companies house",
                "office for national statistics",
            ]
        )
    return False


def _base_url_from_swagger(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def slugify(value: str | None) -> str:
    value = value or "unknown"
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value.lower()).strip("-")
    return value[:80] or "unknown"

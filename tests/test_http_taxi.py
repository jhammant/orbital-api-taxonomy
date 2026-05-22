from orbital_api_taxonomy.discover import load_seed_catalogue
from orbital_api_taxonomy.mapper import TaxonomyMapper
from orbital_api_taxonomy.taxi import render_api_models, render_orbital_services
from orbital_api_taxonomy.taxonomies import GOV_UK


def _build():
    catalogue = load_seed_catalogue("gov-uk")
    mapper = TaxonomyMapper(GOV_UK)
    mappings = {record.id: mapper.map_api(record) for record in catalogue.records}
    return catalogue, mappings


def test_services_are_callable_http_operations():
    catalogue, mappings = _build()
    services = render_orbital_services(catalogue, mappings, GOV_UK.namespace)
    assert "import taxi.http.HttpOperation" in services
    assert "@HttpService(baseUrl =" in services
    assert '@HttpOperation(method = "GET"' in services


def test_query_param_is_typed_and_url_is_relative():
    catalogue, mappings = _build()
    services = render_orbital_services(catalogue, mappings, GOV_UK.namespace)
    assert '@QueryVariable("lat") lat : uk.gov.Latitude' in services
    # optional query params are nullable so Orbital still binds the operation
    assert "date : uk.gov.CrimeMonth?" in services
    # url must be relative to @HttpService(baseUrl) — Orbital joins the two
    assert 'url = "/crimes-street/all-crime"' in services


def test_path_variable_is_typed():
    catalogue, mappings = _build()
    services = render_orbital_services(catalogue, mappings, GOV_UK.namespace)
    assert (
        '@PathVariable("companyNumber") companyNumber : uk.gov.CompanyRegistrationNumber'
        in services
    )


def test_response_model_binds_via_jsonpath():
    catalogue, mappings = _build()
    police = next(r for r in catalogue.records if r.id == "police-uk")
    models = render_api_models(police, mappings["police-uk"], GOV_UK.namespace)
    assert "model StreetCrime {" in models
    assert 'category : uk.gov.CrimeCategory by jsonPath("$.category")' in models
    assert 'latitude : uk.gov.Latitude by jsonPath("$.location.latitude")' in models


def test_wrapped_collection_gets_natural_bound_envelope():
    catalogue, mappings = _build()
    flood = next(r for r in catalogue.records if r.id == "environment-agency-flood")
    models = render_api_models(flood, mappings["environment-agency-flood"], GOV_UK.namespace)
    # the envelope exposes the wrapper key by attribute name, not jsonPath
    assert "model FloodReadingList {" in models
    assert "items : FloodReading[]" in models
    assert "by jsonPath" not in models.split("FloodReadingList")[1]


def test_curated_mappings_are_exact():
    _, mappings = _build()
    companies_house = mappings["companies-house"]
    assert companies_house, "expected curated mappings for companies-house"
    assert all(m.confidence == 1.0 for m in companies_house)
    assert all(m.reason == "curated mapping" for m in companies_house)
    assert {m.taxi_type for m in companies_house} >= {"CompanyRegistrationNumber", "Postcode"}

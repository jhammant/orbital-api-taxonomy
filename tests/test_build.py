from orbital_api_taxonomy.discover import load_seed_catalogue
from orbital_api_taxonomy.mapper import TaxonomyMapper
from orbital_api_taxonomy.taxonomies import GOV_UK


def test_seed_catalogue_has_high_signal_apis():
    catalogue = load_seed_catalogue("gov-uk")
    ids = {record.id for record in catalogue.records}
    assert "companies-house" in ids
    assert "tfl" in ids
    assert "ons" in ids


def test_catalogue_maps_fields():
    catalogue = load_seed_catalogue("gov-uk")
    mapper = TaxonomyMapper(GOV_UK)
    mappings = [m for record in catalogue.records for m in mapper.map_api(record)]
    assert len(mappings) >= 20
    assert {m.taxi_type for m in mappings} >= {"CompanyRegistrationNumber", "Postcode", "Latitude", "Longitude"}

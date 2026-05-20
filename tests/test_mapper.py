from orbital_api_taxonomy.mapper import TaxonomyMapper
from orbital_api_taxonomy.taxonomies import GOV_UK


def test_exact_company_number_mapping():
    mapping = TaxonomyMapper(GOV_UK).map_field("company_number")
    assert mapping is not None
    assert mapping.taxi_type == "CompanyRegistrationNumber"
    assert mapping.confidence == 1.0


def test_nested_postcode_mapping():
    mapping = TaxonomyMapper(GOV_UK).map_field("registered_office_address.postal_code")
    assert mapping is not None
    assert mapping.taxi_type == "Postcode"


def test_lat_lon_mapping():
    mapper = TaxonomyMapper(GOV_UK)
    assert mapper.map_field("location.latitude").taxi_type == "Latitude"
    assert mapper.map_field("location.longitude").taxi_type == "Longitude"


def test_generic_description_does_not_map_to_disruption():
    assert TaxonomyMapper(GOV_UK).map_field("description") is None


def test_generic_common_name_does_not_map_to_company_name():
    assert TaxonomyMapper(GOV_UK).map_field("commonName") is None

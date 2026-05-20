from orbital_api_taxonomy.schemas import flatten_schema_fields


def test_flatten_schema_fields():
    schema = {
        "type": "object",
        "properties": {
            "company_number": {"type": "string"},
            "address": {
                "type": "object",
                "properties": {"postal_code": {"type": "string"}},
            },
        },
    }
    assert flatten_schema_fields(schema) == ["company_number", "address", "address.postal_code"]

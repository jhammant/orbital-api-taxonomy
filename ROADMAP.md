# Roadmap

## MVP 0.1 — shipped in scaffold

- [x] UK gov seed catalogue
- [x] Canonical civic taxonomy
- [x] Field-to-concept mapper with confidence scores
- [x] Taxi taxonomy/model/service generation
- [x] Orbital-style example queries
- [x] Unit tests and smoke build

## Next

1. **Live discovery adapters**
   - APIs.guru OpenAPI index
   - data.gov.uk CKAN package search
   - Socrata catalogues
   - GitHub code search for `openapi.yaml` / `swagger.json`

2. **Schema acquisition**
   - Fetch OpenAPI specs where declared
   - Resolve `$ref`s
   - Sample public JSON endpoints where safe/no auth
   - Store provenance and freshness

3. **Better semantic mapping**
   - Add LLM-assisted candidate mappings behind a review file
   - Keep deterministic tests for accepted mappings
   - Add negative concept examples to avoid false positives

4. **Orbital integration**
   - Generate fully annotated Taxi HTTP operations
   - Add an Orbital docker-compose demo
   - Example semantic queries by vertical

5. **Additional verticals**
   - Healthcare: FHIR, NHS dm+d, ICD-10/SNOMED bridge
   - Finance: Open Banking, ISO 20022, FIBO

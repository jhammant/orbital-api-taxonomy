# Orbital API Taxonomy

Discover public APIs in a vertical and map their fields onto a standard semantic taxonomy expressed in [Taxi](https://taxilang.org/) for use with [Orbital](https://orbitalhq.com/).

MVP vertical: **UK government / civic APIs**.

## What this does

1. Discovers candidate public APIs from machine-readable catalogues and curated registries.
2. Normalises API metadata into a local catalogue.
3. Extracts OpenAPI schemas where available.
4. Maps API fields to canonical vertical concepts.
5. Generates Taxi models and service stubs that Orbital can reason over.

## Quick start

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'

# Build bundled UK gov catalogue and Taxi output
orbital-api-taxonomy build --vertical gov-uk --out build/gov-uk

# Run tests
pytest
```

## Current MVP scope

The repo ships with a curated seed catalogue for high-signal UK public APIs:

- Companies House
- Transport for London
- GOV.UK Content API
- NHS public APIs / FHIR-style resources
- Police.uk
- Environment Agency flood monitoring
- Open Gazette API
- UK Parliament APIs
- ONS API

The first taxonomy focuses on common civic/public-sector entities:

- Organisation / company
- Address / location
- Transport stop / route / disruption
- Public document / publication
- Health organisation
- Parliamentary member / constituency
- Measurement / observation

## Repo layout

```text
src/orbital_api_taxonomy/   Python package
  cli.py                    CLI entrypoints
  catalog.py                API catalogue model/load/save
  discover.py               Discovery providers
  mapper.py                 Field -> taxonomy mapper
  taxi.py                   Taxi renderer
  taxonomies.py             Built-in vertical taxonomies
  schemas.py                OpenAPI/schema extraction helpers

data/catalogs/             Seed API catalogues
taxonomies/                 Hand-authored Taxi base taxonomies
examples/orbital/           Orbital query/service examples
tests/                      Unit tests
```

## Vision

Eventually this should become a vertical API atlas:

```text
public API → OpenAPI/schema/sample payload → semantic concepts → Taxi → Orbital discovery/query
```

So instead of searching for endpoints by name, you ask:

> Which public APIs can provide `CompanyRegistrationNumber`, `RegisteredAddress`, `DirectorName`, and `FilingDate`?

…and Orbital can discover viable data providers semantically.

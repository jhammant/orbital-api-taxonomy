# Orbital API Taxonomy

Discover public APIs in a vertical and map their fields onto a standard semantic taxonomy expressed in [Taxi](https://taxilang.org/) for use with [Orbital](https://orbitalhq.com/).

MVP vertical: **UK government / civic APIs**.

## What this does

1. Discovers candidate public APIs from machine-readable catalogues and curated registries.
2. Normalises API metadata into a local catalogue.
3. Maps every API's request **parameters** and response **fields** onto one shared
   semantic taxonomy (`uk.gov`).
4. Generates **callable** Taxi — `@HttpOperation` services with typed path/query
   parameters and `jsonPath`-bound response models.
5. Orbital loads that schema and can run live queries against the real gov APIs —
   including queries that **chain multiple APIs together** because they share the
   taxonomy.

The curated catalogue covers 9 flagship UK gov APIs as fully callable operations.
Discovery (`--discover`) adds hundreds more APIs as data-model stubs.

## Quick start

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'

# Build the curated UK gov catalogue → callable Taxi
orbital-api-taxonomy build --vertical gov-uk --out build/gov-uk

# High-recall discovery across api.gov.uk, data.gov.uk and APIs.guru
orbital-api-taxonomy build --vertical gov-uk --discover --max-records 500 --out build/gov-uk-discovered

# Copy the curated callable Taxi into the local Orbital workspace
scripts/sync-orbital-workspace.sh build/gov-uk/taxi

# Run tests
pytest
```

## Orbital local testing

An Orbital Docker Compose stack is checked in under `orbital-dev/`.

```bash
cd orbital-dev
cp .env.example .env # edit UID/GID if your user is not 1000:1000
docker compose up -d
open http://localhost:9022
```

The generated Taxi project lives in `orbital-dev/workspace/`:

- `taxi.conf` points Orbital at `workspace/src/`
- `workspace.conf` registers the local project with Orbital
- `workspace/src/*.taxi` is refreshed by `scripts/sync-orbital-workspace.sh`
  (defaults to the curated callable build, `build/gov-uk/taxi/`)

Orbital watches the workspace and recompiles within a few seconds of a sync.

## Querying live gov APIs

Once the curated Taxi is synced, Orbital can run live queries. A query supplies
known facts (`given`) and asks for a taxonomy type (`find`); Orbital works out
which API(s) to call.

```bash
# street crime near a coordinate — one live call to data.police.uk
scripts/demo-query.sh examples/orbital/street-crime.taxi

# crime near a TfL stop — Orbital chains TfL -> Police via shared Latitude/Longitude
scripts/demo-query.sh examples/orbital/crime-near-tfl-stop.taxi
```

Or hit the API directly:

```bash
curl -s -X POST http://localhost:9022/api/taxiql -H 'Content-Type: text/plain' \
  --data 'given { lat : uk.gov.Latitude = 51.5072, lng : uk.gov.Longitude = -0.1276 }
          find { uk.gov.apis.police_uk.StreetCrime[] }'
```

7 of the 9 curated APIs are open and queryable live with no credentials (Police.uk,
TfL, GOV.UK Content, Environment Agency, ONS, Parliament). Companies House and NHS
Service Search generate correct callable Taxi but need an API key configured in
Orbital. See `examples/orbital/` for runnable queries.

Discovery snapshot (`--discover`, varies as registries change):

- ~640 catalogue records across `api.gov.uk`, `data.gov.uk` and APIs.guru
- 9 curated seed APIs, exposed as fully callable `@HttpOperation` services
- ~1,300 field-to-taxonomy mappings

## Current MVP scope

The repo ships with a curated seed catalogue for high-signal UK public APIs, then expands it using public catalogues:

- Companies House
- Transport for London
- GOV.UK Content API
- NHS public APIs / FHIR-style resources
- Police.uk
- Environment Agency flood monitoring
- Open Gazette API
- UK Parliament APIs
- ONS API

Discovery providers currently implemented:

- `api.gov.uk` A-Z catalogue scraper with detail-page endpoint/OpenAPI extraction
- `data.gov.uk` CKAN package search
- APIs.guru OpenAPI registry filtering

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

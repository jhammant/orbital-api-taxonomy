# Example Orbital queries

These are runnable [TaxiQL](https://orbitalhq.com/docs/querying/writing-queries)
queries against the generated `uk.gov` schema. Each one drives **live calls to
real UK government APIs** through Orbital.

Run one against the local Orbital stack (see the repo README for startup):

```bash
curl -s -X POST http://localhost:9022/api/taxiql \
  -H 'Content-Type: text/plain' \
  --data @examples/orbital/street-crime.taxi
```

Or use the helper:

```bash
scripts/demo-query.sh examples/orbital/crime-near-tfl-stop.taxi
```

| File | What it shows | Auth |
|------|---------------|------|
| `street-crime.taxi` | A single API call, typed against the taxonomy | none |
| `flood-readings.taxi` | A wrapped collection response | none |
| `crime-near-tfl-stop.taxi` | **Cross-service chaining** — Orbital joins TfL → Police via the shared `Latitude`/`Longitude` concepts | none |

The third query is the point of the whole project: Orbital is never told that
TfL and Police.uk are related. It works it out because both speak the same
`uk.gov` taxonomy.

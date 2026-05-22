#!/usr/bin/env bash
# Run a TaxiQL query against the local Orbital stack and pretty-print the result.
#
# Usage:
#   scripts/demo-query.sh examples/orbital/street-crime.taxi
#   scripts/demo-query.sh                       # runs the cross-service demo
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ORBITAL_URL="${ORBITAL_URL:-http://localhost:9022}"
QUERY_FILE="${1:-$ROOT/examples/orbital/crime-near-tfl-stop.taxi}"

if [ ! -f "$QUERY_FILE" ]; then
  echo "Query file not found: $QUERY_FILE" >&2
  exit 1
fi

if ! curl -s -o /dev/null --max-time 5 "$ORBITAL_URL/"; then
  echo "Orbital is not reachable at $ORBITAL_URL — start it with:" >&2
  echo "  cd orbital-dev && docker compose up -d" >&2
  exit 1
fi

echo "Query ($QUERY_FILE):"
sed 's/^/  /' "$QUERY_FILE"
echo
echo "Result:"
curl -s -X POST "$ORBITAL_URL/api/taxiql" \
  -H 'Content-Type: text/plain' \
  --data-binary @"$QUERY_FILE" \
  | python3 -m json.tool 2>/dev/null \
  || echo "(query failed — is the schema synced? run scripts/sync-orbital-workspace.sh)"

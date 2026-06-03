#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="${1:-$ROOT/build/gov-uk-discovered/taxi}"
DEST="$ROOT/orbital-dev/workspace/src"
if [ ! -d "$BUILD_DIR" ]; then
  echo "Taxi build directory not found: $BUILD_DIR" >&2
  echo "Run: orbital-api-taxonomy build --vertical gov-uk --discover --max-records 500 --out build/gov-uk-discovered" >&2
  exit 1
fi
mkdir -p "$DEST"
# ADDITIVE sync: copy/overwrite only OUR taxi files; never wipe the dir — a second session
# contributes its own typed sources (deeptech/energy/realestate) to this same workspace.
cp "$BUILD_DIR"/*.taxi "$DEST"/
echo "Synced $(find "$DEST" -type f -name '*.taxi' | wc -l) Taxi files to $DEST"

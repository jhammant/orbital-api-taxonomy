from __future__ import annotations

import argparse
import json
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .catalog import ApiCatalogue
from .discover import discover_catalogue, load_seed_catalogue
from .mapper import TaxonomyMapper
from .taxi import render_api_models, render_orbital_services, render_taxonomy
from .taxonomies import TAXONOMIES

console = Console()


def main() -> None:
    parser = argparse.ArgumentParser(prog="orbital-api-taxonomy")
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="Build catalogue mappings and Taxi output")
    build.add_argument("--vertical", default="gov-uk", choices=sorted(TAXONOMIES))
    build.add_argument("--out", type=Path, default=Path("build/gov-uk"))
    build.add_argument("--threshold", type=float, default=0.8)
    build.add_argument("--discover", action="store_true", help="Merge live registry discovery")
    build.add_argument("--max-records", type=int, default=250)

    discover = sub.add_parser("discover", help="Discover public API candidates")
    discover.add_argument("--vertical", default="gov-uk", choices=sorted(TAXONOMIES))
    discover.add_argument("--out", type=Path, default=Path("build/gov-uk/catalogue.discovered.yaml"))
    discover.add_argument("--max-records", type=int, default=250)

    list_cmd = sub.add_parser("list", help="List APIs in a catalogue")
    list_cmd.add_argument("--vertical", default="gov-uk", choices=sorted(TAXONOMIES))
    list_cmd.add_argument("--discover", action="store_true", help="Include live registry discovery")
    list_cmd.add_argument("--max-records", type=int, default=250)

    args = parser.parse_args()
    if args.command == "build":
        build_vertical(args.vertical, args.out, args.threshold, args.discover, args.max_records)
    elif args.command == "discover":
        discover_vertical(args.vertical, args.out, args.max_records)
    elif args.command == "list":
        list_vertical(args.vertical, args.discover, args.max_records)


def get_catalogue(vertical: str, include_discovery: bool, max_records: int) -> ApiCatalogue:
    if include_discovery:
        return discover_catalogue(vertical, max_records=max_records)
    return load_seed_catalogue(vertical)


def list_vertical(vertical: str, include_discovery: bool = False, max_records: int = 250) -> None:
    catalogue = get_catalogue(vertical, include_discovery, max_records)
    table = Table(title=f"{vertical} API catalogue ({len(catalogue.records)} records)")
    table.add_column("ID")
    table.add_column("Name")
    table.add_column("Auth")
    table.add_column("Source")
    table.add_column("Tags")
    for record in catalogue.records:
        table.add_row(record.id, record.name, record.auth, record.source, ", ".join(record.tags))
    console.print(table)


def discover_vertical(vertical: str, out: Path, max_records: int) -> None:
    catalogue = discover_catalogue(vertical, max_records=max_records)
    catalogue.save_yaml(out)
    console.print(f"Discovered {len(catalogue.records)} candidate APIs into {out}")


def build_vertical(
    vertical: str, out: Path, threshold: float, include_discovery: bool = False, max_records: int = 250
) -> None:
    taxonomy = TAXONOMIES[vertical]
    catalogue = get_catalogue(vertical, include_discovery, max_records)
    mapper = TaxonomyMapper(taxonomy, threshold=threshold)
    out.mkdir(parents=True, exist_ok=True)

    mappings_by_api = {record.id: mapper.map_api(record) for record in catalogue.records}
    mapping_rows = [mapping.__dict__ for mappings in mappings_by_api.values() for mapping in mappings]

    catalogue.save_yaml(out / "catalogue.yaml")
    (out / "field-mappings.json").write_text(json.dumps(mapping_rows, indent=2), encoding="utf-8")

    taxi_dir = out / "taxi"
    taxi_dir.mkdir(exist_ok=True)
    (taxi_dir / "taxonomy.taxi").write_text(render_taxonomy(taxonomy), encoding="utf-8")
    for record in catalogue.records:
        mappings = mappings_by_api[record.id]
        if mappings:
            (taxi_dir / f"{record.id}.taxi").write_text(
                render_api_models(record, mappings, taxonomy.namespace), encoding="utf-8"
            )
    (taxi_dir / "services.taxi").write_text(
        render_orbital_services(catalogue, mappings_by_api, taxonomy.namespace), encoding="utf-8"
    )

    console.print(
        f"Built {len(catalogue.records)} APIs and {len(mapping_rows)} field mappings into {out}"
    )


if __name__ == "__main__":
    main()

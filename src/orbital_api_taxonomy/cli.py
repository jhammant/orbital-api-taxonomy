from __future__ import annotations

import argparse
import json
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .discover import load_seed_catalogue
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

    list_cmd = sub.add_parser("list", help="List APIs in the seed catalogue")
    list_cmd.add_argument("--vertical", default="gov-uk", choices=sorted(TAXONOMIES))

    args = parser.parse_args()
    if args.command == "build":
        build_vertical(args.vertical, args.out, args.threshold)
    elif args.command == "list":
        list_vertical(args.vertical)


def list_vertical(vertical: str) -> None:
    catalogue = load_seed_catalogue(vertical)
    table = Table(title=f"{vertical} API catalogue")
    table.add_column("ID")
    table.add_column("Name")
    table.add_column("Auth")
    table.add_column("Tags")
    for record in catalogue.records:
        table.add_row(record.id, record.name, record.auth, ", ".join(record.tags))
    console.print(table)


def build_vertical(vertical: str, out: Path, threshold: float) -> None:
    taxonomy = TAXONOMIES[vertical]
    catalogue = load_seed_catalogue(vertical)
    mapper = TaxonomyMapper(taxonomy, threshold=threshold)
    out.mkdir(parents=True, exist_ok=True)

    mappings_by_api = {record.id: mapper.map_api(record) for record in catalogue.records}
    mapping_rows = [mapping.__dict__ for mappings in mappings_by_api.values() for mapping in mappings]

    (out / "catalogue.yaml").write_text("", encoding="utf-8")
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

    console.print(f"Built {len(catalogue.records)} APIs and {len(mapping_rows)} field mappings into {out}")


if __name__ == "__main__":
    main()

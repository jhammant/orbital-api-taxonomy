from __future__ import annotations

from pathlib import Path

from .catalog import ApiCatalogue


DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "catalogs"


def load_seed_catalogue(vertical: str) -> ApiCatalogue:
    path = DATA_DIR / f"{vertical}.seed.yaml"
    if not path.exists():
        raise FileNotFoundError(f"No seed catalogue for vertical '{vertical}' at {path}")
    return ApiCatalogue.load_yaml(path)


def merge_catalogues(*catalogues: ApiCatalogue) -> ApiCatalogue:
    if not catalogues:
        raise ValueError("At least one catalogue is required")
    vertical = catalogues[0].vertical
    records = {}
    for catalogue in catalogues:
        if catalogue.vertical != vertical:
            raise ValueError("Cannot merge catalogues from different verticals")
        for record in catalogue.records:
            records[record.id] = record
    return ApiCatalogue(vertical=vertical, records=list(records.values()))

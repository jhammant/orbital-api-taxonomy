from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from .catalog import ApiRecord
from .taxonomies import Concept, VerticalTaxonomy

_TOKEN_RE = re.compile(r"[A-Za-z][a-z]*|[A-Z]+(?=[A-Z]|$)|\d+")


def normalise(name: str) -> str:
    tokens = _TOKEN_RE.findall(name.replace("-", "_").replace(".", "_"))
    if not tokens:
        tokens = re.split(r"[_\s/.-]+", name)
    return "_".join(token.lower() for token in tokens if token)


@dataclass(frozen=True)
class FieldMapping:
    api_id: str
    endpoint_path: str
    source_field: str
    normalised_field: str
    taxi_type: str
    concept_name: str
    confidence: float
    reason: str


class TaxonomyMapper:
    def __init__(self, taxonomy: VerticalTaxonomy, threshold: float = 0.8):
        self.taxonomy = taxonomy
        self.threshold = threshold
        self._alias_index: list[tuple[str, Concept]] = []
        for concept in taxonomy.concepts:
            aliases = set(concept.aliases) | {concept.name, concept.taxi_type}
            for alias in aliases:
                self._alias_index.append((normalise(alias), concept))

    def map_field(self, field: str) -> FieldMapping | None:
        field_norm = normalise(field)
        best: tuple[float, Concept, str] | None = None
        for alias_norm, concept in self._alias_index:
            score = self._score(field_norm, alias_norm)
            if best is None or score > best[0]:
                best = (score, concept, alias_norm)
        if best is None or best[0] < self.threshold:
            return None
        score, concept, alias = best
        return FieldMapping(
            api_id="",
            endpoint_path="",
            source_field=field,
            normalised_field=field_norm,
            taxi_type=concept.taxi_type,
            concept_name=concept.name,
            confidence=round(score, 3),
            reason=f"matched alias '{alias}'",
        )

    def map_api(self, api: ApiRecord) -> list[FieldMapping]:
        """Map an API's response fields onto the taxonomy.

        Curated endpoints carry explicit, hand-reviewed ``response_fields`` — these
        are trusted as exact mappings. Discovered endpoints only have raw
        ``sample_fields``, which are matched fuzzily against concept aliases.
        """
        concepts = self.taxonomy.concept_by_type()
        mappings: list[FieldMapping] = []
        for endpoint in api.endpoints:
            if endpoint.response_fields:
                for response_field in endpoint.response_fields:
                    concept = concepts.get(response_field.taxi_type)
                    if concept is None:
                        raise ValueError(
                            f"{api.id}{endpoint.path}: response field "
                            f"'{response_field.name}' references unknown taxonomy type "
                            f"'{response_field.taxi_type}'"
                        )
                    mappings.append(
                        FieldMapping(
                            api_id=api.id,
                            endpoint_path=endpoint.path,
                            source_field=response_field.json_path,
                            normalised_field=normalise(response_field.name),
                            taxi_type=concept.taxi_type,
                            concept_name=concept.name,
                            confidence=1.0,
                            reason="curated mapping",
                        )
                    )
                continue
            for field in endpoint.sample_fields:
                mapping = self.map_field(field)
                if mapping:
                    mappings.append(
                        FieldMapping(
                            api_id=api.id,
                            endpoint_path=endpoint.path,
                            source_field=mapping.source_field,
                            normalised_field=mapping.normalised_field,
                            taxi_type=mapping.taxi_type,
                            concept_name=mapping.concept_name,
                            confidence=mapping.confidence,
                            reason=mapping.reason,
                        )
                    )
        return mappings

    @staticmethod
    def _score(field: str, alias: str) -> float:
        if field == alias:
            return 1.0
        if field.endswith("_" + alias) or field.startswith(alias + "_"):
            return 0.94
        field_parts = set(field.split("_"))
        alias_parts = set(alias.split("_"))
        if alias_parts and alias_parts.issubset(field_parts):
            return 0.88
        return SequenceMatcher(None, field, alias).ratio()

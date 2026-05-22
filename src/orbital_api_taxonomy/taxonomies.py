from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Concept:
    name: str
    taxi_type: str
    description: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class VerticalTaxonomy:
    id: str
    namespace: str
    description: str
    concepts: tuple[Concept, ...] = field(default_factory=tuple)

    def concept_by_type(self) -> dict[str, Concept]:
        return {concept.taxi_type: concept for concept in self.concepts}


GOV_UK = VerticalTaxonomy(
    id="gov-uk",
    namespace="uk.gov",
    description="UK civic and public-sector API semantic taxonomy",
    concepts=(
        Concept("Company name", "CompanyName", "Registered or trading organisation name", ("company_name", "organisation_name", "organisationName", "companyName", "registered_name", "trading_name")),
        Concept("Company registration number", "CompanyRegistrationNumber", "Companies House company number", ("company_number", "companyRegistrationNumber", "company_no", "registration_number")),
        Concept("Company status", "CompanyStatus", "Registered company lifecycle/status", ("company_status", "status")),
        Concept("SIC code", "StandardIndustrialClassificationCode", "UK SIC industry classification", ("sic_codes", "sic_code")),
        Concept("Address line", "AddressLine", "Human-readable address line", ("address_line_1", "address_line_2", "line1", "street_address")),
        Concept("Postcode", "Postcode", "UK postal code", ("postcode", "postal_code", "post_code")),
        Concept("Latitude", "Latitude", "WGS84 latitude", ("lat", "latitude")),
        Concept("Longitude", "Longitude", "WGS84 longitude", ("lon", "lng", "longitude")),
        Concept("Transport stop id", "TransportStopId", "Identifier for a station, stop, pier, or platform", ("naptan_id", "station_id", "stop_id", "icsCode")),
        Concept("Transport route id", "TransportRouteId", "Identifier for a public transport line or route", ("line_id", "route_id", "service_id")),
        Concept("Disruption description", "DisruptionDescription", "Transport/service disruption text", ("disruption", "disruption_description", "disruption_reason", "reason")),
        Concept("Publication title", "PublicationTitle", "Title for a government document/content item", ("title", "document_title", "publication_title")),
        Concept("Publication date", "PublicationDate", "Date a public document was published", ("published_at", "publication_date", "date_published")),
        Concept("Content body", "ContentBody", "Main body/content text", ("body", "details", "content", "details_body")),
        Concept("NHS organisation code", "NhsOrganisationCode", "NHS ODS / organisation identifier", ("ods_code", "organisation_code", "org_code")),
        Concept("Parliament member id", "ParliamentMemberId", "UK Parliament member identifier", ("member_id", "person_id")),
        Concept("Constituency name", "ConstituencyName", "UK parliamentary constituency", ("constituency", "constituency_name")),
        Concept("Observation value", "ObservationValue", "Measured public/environmental data value", ("value", "reading")),
        Concept("Observation time", "ObservationTime", "Timestamp for an observation", ("dateTime", "timestamp", "observed_at", "time")),
        Concept("Content path", "ContentPath", "GOV.UK content item base path/slug", ("base_path", "content_path", "slug")),
        Concept("Crime month", "CrimeMonth", "Year-month a crime dataset covers (YYYY-MM)", ("month", "crime_month")),
        Concept("Crime category", "CrimeCategory", "Police.uk crime category", ("crime_category", "crime_type")),
        Concept("Street name", "StreetName", "Street or thoroughfare name", ("street_name", "thoroughfare")),
        Concept("Monitoring station id", "MonitoringStationId", "Environmental monitoring station identifier", ("station", "station_reference")),
        Concept("Measure name", "MeasureName", "Name of a measured environmental parameter", ("measure", "parameter")),
        Concept("Search query", "SearchQuery", "Free-text search term passed to an API", ("search_text", "q", "query")),
        Concept("Dataset id", "DatasetId", "Statistical dataset identifier", ("dataset_id",)),
        Concept("Dataset edition", "DatasetEdition", "Statistical dataset edition label", ("edition",)),
        Concept("Dataset version", "DatasetVersion", "Statistical dataset version", ("version",)),
        Concept("Notice code", "NoticeCode", "Gazette notice classification code", ("notice_code",)),
    ),
)

TAXONOMIES = {GOV_UK.id: GOV_UK}

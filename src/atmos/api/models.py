"""Response shapes for the public API.

Two rules run through all of these.

**A measurement is never converted to a float.** `value` is the harmonised
number and `raw_value` is the string exactly as the source published it, kept
beside it. Anyone who needs certainty reads `raw_value`, and nothing we serve
has been rounded or re-scaled without saying so.

**Provenance travels with the data.** A reading names the station, the station
names its operator and its publisher, and the publisher carries its licence and
required attribution. Nothing here can be quoted without knowing where it came
from.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class Model(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Source(Model):
    slug: str
    name: str
    operator: str | None = None
    tier: str = Field(description="reference, low_cost, satellite and so on")
    base_url: str | None = None
    attribution: str = Field(description="Credit this source requires when quoted")
    licence: str | None = Field(default=None, description="Licence of the source data")
    licence_url: str | None = None
    timezone: str = Field(description="What the source publishes its timestamps in")
    is_primary: bool = Field(
        description="False for aggregators, which are never a source of record"
    )
    stations: int | None = None
    observations: int | None = None


class Parameter(Model):
    code: str
    name: str | None = None
    canonical_unit: str
    allows_negative: bool = Field(
        description="False where a negative value is physically impossible"
    )


class Station(Model):
    id: int
    source: str
    source_station_id: str
    name: str
    operator: str | None = Field(
        default=None,
        description="The body running the instrument, often not the publisher",
    )
    latitude: float | None = None
    longitude: float | None = None
    elevation_m: Decimal | None = None
    station_type: str = Field(description="background, traffic, industrial or unknown")
    area_type: str = Field(description="urban, suburban, rural or unknown")
    is_indoor: bool = False
    is_mobile: bool = False
    location_precise: bool = Field(
        default=True,
        description="False where the position is rounded or inferred, not surveyed",
    )
    canonical_station_id: int | None = Field(
        default=None,
        description="Set when another publisher carries this same instrument",
    )
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None


class Observation(Model):
    station_id: int
    parameter: str
    phenomenon_start: datetime
    phenomenon_end: datetime
    value: Decimal | None
    unit: str
    raw_value: str | None = Field(
        default=None, description="Exactly as published, before unit harmonisation"
    )
    raw_unit: str | None = None
    revision: int = Field(
        description="1 is the first value published, and stays canonical"
    )
    revision_kind: str | None = Field(
        default=None, description="value_change, withdrawal or reinstatement"
    )
    previous_value: Decimal | None = None
    confirmations: int = Field(
        description="How many times the source republished this same value"
    )
    quality_flags: list[str] = []


class ConsensusSet(Model):
    anchor_station_id: int
    bucket_start: datetime
    bucket_end: datetime
    n: int = Field(description="Instruments in the set, each counted once")
    median: Decimal | None = None
    mad: Decimal | None = Field(
        default=None, description="Median absolute deviation. Null when n is 1"
    )
    mean: Decimal | None = None
    stddev: Decimal | None = None
    min_value: Decimal | None = None
    max_value: Decimal | None = None
    q1: Decimal | None = None
    q3: Decimal | None = None
    basis: str = Field(
        description=(
            "What these numbers can carry. unconfirmed means one instrument, "
            "two_sources means a difference is measurable but not attributable, "
            "exact_agreement means the spread is zero, robust means three or more"
        )
    )
    aggregate: str = Field(description="How sub-bucket readings were reduced")


class Divergence(Model):
    station_id: int
    bucket_start: datetime
    bucket_end: datetime
    value: Decimal | None = None
    readings: int = Field(description="Raw readings behind this bucket value")
    n: int
    median: Decimal | None = None
    mad: Decimal | None = None
    deviation: Decimal | None = None
    modified_z: Decimal | None = Field(
        default=None,
        description="Only where three or more instruments and a non zero spread",
    )
    proportional: Decimal | None = Field(
        default=None, description="Used instead of a z-score when the spread is zero"
    )
    basis: str


class StationHealth(Model):
    station_id: int
    source: str
    station: str
    parameter: str
    readings: int
    zeros: int = Field(description="Readings of exactly zero")
    flagged: int
    last_reading: datetime | None = None


class Meta(Model):
    """What the project is, and what quoting it requires."""

    name: str = "Atmos"
    description: str = (
        "Open archive of air quality and weather measurements for "
        "Bosnia and Herzegovina"
    )
    data_licence: str = "CC BY 4.0"
    code_licence: str = "AGPL-3.0-or-later"
    display_timezone: str = Field(
        default="Europe/Sarajevo",
        description=(
            "Timestamps come back at this offset. Each source also reports the "
            "timezone it publishes in, so the original is never lost."
        ),
    )
    note: str = (
        "Values are stored and served exactly as published. Flags mark a reading "
        "as questionable, never as removed, and source selection is the reader's."
    )
    sources: list[Source] = []


class IndexScale(Model):
    """Which scale a figure was computed on, and where its numbers came from."""

    code: str
    name: str
    revision: str
    citation: str
    verified_on: date


class AirQuality(Model):
    """The index for one station and hour.

    Absent rather than approximate. Where neither PM2.5 nor PM10 was measured
    there is no figure at all, because particulates drive the index nearly
    everywhere here and one without them would carry almost nothing.
    """

    station_id: int
    band: int = Field(description="1 good to 6 extremely poor")
    band_code: str = Field(description="Stable key: good, fair, moderate and so on")
    band_name: str = Field(description="The publishing body's own wording")
    driver: str = Field(description="The pollutant that set the band")
    driver_value: Decimal
    driver_unit: str
    observed_at: datetime
    pollutants_used: int
    pollutants_total: int
    missing: list[str] = Field(
        description="Scale pollutants this station did not report that hour"
    )
    complete: bool
    scale: str = "eaqi"
    basis: str = Field(
        description=(
            "complete when every pollutant of the scale reported. floor when "
            "some are missing: the index takes the worst pollutant, so an "
            "absent one can only make the true value worse, never better"
        )
    )


class CurrentConditions(Model):
    """A station's latest reading and index, for a list or a map."""

    station_id: int
    station: str
    source: str
    station_type: str
    area_type: str
    latitude: float | None = None
    longitude: float | None = None
    observed_at: datetime | None = None
    air_quality: AirQuality | None = Field(
        default=None, description="Absent when the station published no particulates"
    )
    values: dict[str, Decimal] = Field(
        default_factory=dict, description="Every pollutant that hour, index or not"
    )
    units: dict[str, str] = {}

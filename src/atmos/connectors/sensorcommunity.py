"""Sensor.Community connector.

Community run low cost sensors, mostly SDS011 particulate units with a BME280 or
DHT22 alongside for weather. Around 15 locations in Bosnia and Herzegovina.

Two things make this source different from the institutional ones:

- **Timestamps are UTC**, not local. Checked: the newest reading in the feed was
  1.2 minutes old read as UTC and 121 minutes old read as local.
- **Readings are instantaneous**, roughly every two and a half minutes, not
  hourly averages. Start and end are equal, which the schema treats as an
  instant rather than a period.

Data licence is the Database Contents Licence 1.0.

Tier is low_cost. These sensors drift, react to humidity, and a fair number are
plainly broken: temperatures of -144 C and humidity of 1 percent both appear in
the live feed. They are collected because they cover places no reference station
does, and they earn their place only after calibration against nearby reference
stations. Nothing here should be compared with a regulatory measurement raw.

The live API returns only the last few minutes, so polling it captures a sample
rather than the full record. The daily archive at archive.sensor.community holds
complete per sensor files back to 2015 and is handled as backfill, separately.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from atmos.connectors.base import (
    Connector,
    FetchTarget,
    ParsedObservation,
    ParsedStation,
    ParseError,
    SourceMetadata,
)

API = "https://data.sensor.community/airrohr/v1/filter/area"

# Circles covering Bosnia and Herzegovina. The API takes lat,lon,radius_km and
# has no country filter, so results are filtered on the country field instead.
AREAS: dict[str, str] = {
    "sarajevo": "43.85,18.41,80",
    "banjaluka": "44.77,17.19,80",
    "tuzla": "44.54,18.68,60",
    "mostar": "43.34,17.81,80",
    "bihac": "44.81,15.87,60",
    "trebinje": "42.90,18.35,70",
}

COUNTRY = "BA"

# Feed value_type -> (our code, published unit).
# pressure arrives in Pascals, not hectopascals. Converted downstream.
PARAMETERS: dict[str, tuple[str, str]] = {
    "P0": ("pm1", "ug/m3"),
    "P1": ("pm10", "ug/m3"),
    "P2": ("pm25", "ug/m3"),
    "temperature": ("temp", "degC"),
    "humidity": ("rh", "pct"),
    "pressure": ("press", "Pa"),
}

# pressure_at_sealevel is computed by the source from pressure and altitude.
# We keep the measurement and derive that ourselves if it is ever wanted.
IGNORED = {"pressure_at_sealevel"}


def _to_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


class SensorCommunityConnector:
    slug = "sensorcommunity"
    parser_version = "sensorcommunity-1"

    def targets(self) -> list[FetchTarget]:
        return [
            FetchTarget(id=name, url=f"{API}={coords}", station_hint=name)
            for name, coords in AREAS.items()
        ]

    def metadata(self) -> SourceMetadata:
        return SourceMetadata(
            slug=self.slug,
            name="Sensor.Community",
            operator="Sensor.Community contributors",
            tier="low_cost",
            base_url="https://sensor.community/",
            attribution="Sensor.Community contributors",
            is_primary=True,
            timezone="UTC",
            license="DbCL-1.0",
            license_url="https://opendatacommons.org/licenses/dbcl/1-0/",
            archive_mode="bytes",
            notes=(
                "Community low cost sensors, mostly SDS011. Timestamps are UTC and "
                "readings are instantaneous, not hourly means. Many locations are "
                "reported to reduced precision for privacy. Requires calibration "
                "against reference stations before comparison with regulatory data."
            ),
        )

    def stations(self, raw: bytes, target: FetchTarget) -> list[ParsedStation]:
        seen: dict[str, ParsedStation] = {}
        for rec in self._records(raw, target):
            loc = rec.get("location") or {}
            if loc.get("country") != COUNTRY:
                continue
            sid = str(loc.get("id"))
            if sid in seen:
                continue
            lat, lon = _to_decimal(loc.get("latitude")), _to_decimal(loc.get("longitude"))
            alt = _to_decimal(loc.get("altitude"))
            declared = tuple(
                dict.fromkeys(
                    PARAMETERS[v["value_type"]][0]
                    for v in rec.get("sensordatavalues", [])
                    if v.get("value_type") in PARAMETERS
                )
            )
            seen[sid] = ParsedStation(
                source_station_id=sid,
                name=f"Sensor.Community {sid}",
                latitude=float(lat) if lat is not None else None,
                longitude=float(lon) if lon is not None else None,
                elevation_m=float(alt) if alt is not None else None,
                is_indoor=bool(loc.get("indoor")),
                # exact_location 0 means coordinates are rounded for privacy,
                # which matters when matching stations by radius.
                location_precise=bool(loc.get("exact_location")),
                declared_parameters=declared,
            )
        return list(seen.values())

    def parse(self, raw: bytes, target: FetchTarget) -> list[ParsedObservation]:
        out: list[ParsedObservation] = []
        for rec in self._records(raw, target):
            loc = rec.get("location") or {}
            if loc.get("country") != COUNTRY:
                continue

            when = self._timestamp(rec.get("timestamp"))
            if when is None:
                continue
            station = str(loc.get("id"))

            for item in rec.get("sensordatavalues", []):
                vtype = item.get("value_type")
                if vtype in IGNORED or vtype not in PARAMETERS:
                    continue
                code, unit = PARAMETERS[vtype]
                value = _to_decimal(item.get("value"))
                if value is None:
                    continue
                out.append(
                    ParsedObservation(
                        source_station_id=station,
                        parameter_code=code,
                        # Instantaneous, so the window has no duration.
                        phenomenon_start=when,
                        phenomenon_end=when,
                        value=value,
                        unit=unit,
                        raw_value=str(item.get("value")).strip(),
                        raw_unit=unit,
                    )
                )
        return out

    @staticmethod
    def _records(raw: bytes, target: FetchTarget) -> list[dict]:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ParseError(f"not valid JSON: {e}", target_id=target.id,
                             snippet=raw[:500]) from e
        if not isinstance(payload, list):
            raise ParseError("expected a JSON array", target_id=target.id, snippet=raw[:500])
        return payload

    @staticmethod
    def _timestamp(value: object) -> datetime | None:
        """Feed stamps read YYYY-MM-DD HH:MM:SS and are UTC."""
        if not value:
            return None
        try:
            naive = datetime.strptime(str(value).strip(), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
        return naive.replace(tzinfo=UTC)


_: Connector = SensorCommunityConnector()

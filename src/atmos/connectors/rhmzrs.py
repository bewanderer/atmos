"""RHMZ Republike Srpske connector.

Republicki hidrometeoroloski zavod publishes a small JSON feed behind its air
quality map: /data/feeds/EkoPodaci.json. Six stations, coordinates included,
which is more station metadata than FHMZ gives on its pages.

The feed holds one hour per station, the current one. There is no series and no
archive, so an hour not fetched is lost. That is why this connector is polled
hourly while the HTML sources run every three.

The daily PDF reports are summaries, daily and eight hour averages, not hourly
values. Useful later for checking our own aggregates, useless for filling gaps.

CO is published in mg/m3, SO2, NO2, O3, PM10 and PM2.5 in ug/m3. Confirmed from
the operator's own daily report. FHMZ uses ug/m3 for CO, so the two disagree.

The feed also carries an `indeks` block with the operator's own index values.
We ignore it and compute indices ourselves from concentrations.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from atmos.connectors.base import (
    Connector,
    FetchTarget,
    ParsedObservation,
    ParsedStation,
    ParseError,
    SourceMetadata,
)

# RHMZ RS publishes in local time.
SOURCE_TZ = ZoneInfo("Europe/Sarajevo")

FEED = "https://www.rhmzrs.com/data/feeds/EkoPodaci.json"

# Feed key -> station name. Cyrillic names are in the payload but the keys are
# stable Latin, so the keys are the identifier.
STATIONS: dict[str, str] = {
    "Banjaluka": "Banja Luka",
    "Brod": "Brod",
    "Gacko": "Gacko",
    "Doboj": "Doboj",
    "Prijedor": "Prijedor",
    "Trebinje": "Trebinje",
}

# Feed field -> (our code, published unit).
PARAMETERS: dict[str, tuple[str, str]] = {
    "SO2": ("so2", "ug/m3"),
    "NO": ("no", "ug/m3"),
    "NO2": ("no2", "ug/m3"),
    "NOx": ("nox", "ug/m3"),
    "O3": ("o3", "ug/m3"),
    "CO": ("co", "mg/m3"),
    "PM10": ("pm10", "ug/m3"),
    "PM2.5": ("pm25", "ug/m3"),
    "H2S": ("h2s", "ug/m3"),
    "C6H6": ("c6h6", "ug/m3"),
}

# The feed writes an absent reading as a star.
MISSING = {"*", "-", "", "n/a"}


def _to_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    text = str(value).strip()
    if text in MISSING:
        return None
    try:
        return Decimal(text.replace(",", "."))
    except (InvalidOperation, ValueError):
        return None


class RhmzRsConnector:
    slug = "rhmzrs"
    parser_version = "rhmzrs-1"

    def targets(self) -> list[FetchTarget]:
        return [FetchTarget(id="ekopodaci", url=FEED)]

    def metadata(self) -> SourceMetadata:
        return SourceMetadata(
            slug=self.slug,
            name="Republicki hidrometeoroloski zavod Republike Srpske",
            operator="Republicki hidrometeoroloski zavod RS",
            tier="reference",
            base_url="https://www.rhmzrs.com/",
            attribution="Republicki hidrometeoroloski zavod Republike Srpske (rhmzrs.com)",
            is_primary=True,
            timezone="Europe/Sarajevo",
            archive_mode="bytes",
            notes=(
                "JSON feed holding only the current hour per station, so it must be polled "
                "hourly. Publishes CO in mg/m3 where FHMZ uses ug/m3. Carries station "
                "coordinates. Daily PDF reports are summaries, not hourly series."
            ),
        )

    def stations(self, raw: bytes, target: FetchTarget) -> list[ParsedStation]:
        current = self._current(raw, target)
        out = []
        for key, row in current.items():
            name = STATIONS.get(key, key)
            lat, lon = _to_decimal(row.get("Lat")), _to_decimal(row.get("Lon"))
            declared = tuple(
                PARAMETERS[f][0] for f in PARAMETERS if f in row
            )
            out.append(
                ParsedStation(
                    source_station_id=key,
                    name=name,
                    latitude=float(lat) if lat is not None else None,
                    longitude=float(lon) if lon is not None else None,
                    declared_parameters=declared,
                )
            )
        return out

    def parse(self, raw: bytes, target: FetchTarget) -> list[ParsedObservation]:
        current = self._current(raw, target)

        out: list[ParsedObservation] = []
        for key, row in current.items():
            start = self._timestamp(row.get("vrijeme"))
            if start is None:
                continue

            flags: tuple[str, ...] = ()
            if start.dst() != (start + timedelta(hours=1)).dst():
                flags = ("dst_ambiguous",)

            for field, (code, unit) in PARAMETERS.items():
                if field not in row:
                    continue
                value = _to_decimal(row[field])
                if value is None:
                    continue
                out.append(
                    ParsedObservation(
                        source_station_id=key,
                        parameter_code=code,
                        phenomenon_start=start,
                        phenomenon_end=start + timedelta(hours=1),
                        value=value,
                        unit=unit,
                        raw_value=str(row[field]).strip(),
                        raw_unit=unit,
                        quality_flags=flags,
                    )
                )
        return out

    @staticmethod
    def _current(raw: bytes, target: FetchTarget) -> dict[str, dict[str, Any]]:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ParseError(f"feed is not valid JSON: {e}", target_id=target.id,
                             snippet=raw[:500]) from e
        current = payload.get("trenutni")
        if not isinstance(current, dict) or not current:
            raise ParseError("feed has no 'trenutni' block", target_id=target.id,
                             snippet=raw[:500])
        return current

    @staticmethod
    def _timestamp(value: object) -> datetime | None:
        """Feed stamps read DD.MM.YYYY HH:MM:SS in local time."""
        if not value:
            return None
        try:
            naive = datetime.strptime(str(value).strip(), "%d.%m.%Y %H:%M:%S")
        except ValueError:
            return None
        return naive.replace(tzinfo=SOURCE_TZ)


_: Connector = RhmzRsConnector()

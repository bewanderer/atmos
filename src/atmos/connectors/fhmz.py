"""FHMZ BiH connector.

Federalni hidrometeoroloski zavod publishes one page per station, each holding
roughly six days of hourly values as plain HTML tables. Nothing older is
available anywhere, so a missed week is gone permanently.

Page layout, confirmed against fetched fixtures:

    <table>  SO2                      <- pollutant name, single cell
    <table>  datum | 0:00 ... 23:00   <- header, then 6 data rows, newest first
             28.8.2026. | 17.86 | ...

Three things the fixtures taught us, all of which the parser has to handle:

1. Pollutant sets differ by station. Vijecnica publishes 6, Tetovo publishes 8.
   We read whatever is present rather than assuming a fixed set.

2. NOx tables carry values but leave the date column completely blank, on every
   station page checked. Those dates are taken from a sibling table at the same
   row position and the observations are flagged date_inferred. Where no table
   on the page dates a row, its values are dropped rather than guessed.

3. Not every page is live. Zenica Tetovo was serving August 2024 data when
   fetched in August 2026, and its rows are not even a contiguous window. So
   dates are always read, never assumed from position in the window. Staleness
   is left for monitoring to detect and report, since a reference station that
   has published nothing for two years is a finding in its own right.

Exceedance summary tables are ignored. We derive exceedances from concentrations
ourselves, consistently with computing every index rather than ingesting one.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from atmos.connectors.base import (
    Connector,
    FetchTarget,
    ParsedObservation,
    ParsedStation,
    ParseError,
    SourceMetadata,
)

# Hour labels on the page are local wall clock, with no timezone stated anywhere.
LOCAL_TZ = ZoneInfo("Europe/Sarajevo")

# Page name -> human readable station name. Taken from the station links on
# AQI-satne.php. Refresh with discover_stations() rather than editing blind,
# since the site's own station count is inconsistent across its pages.
STATIONS: dict[str, str] = {
    "amsVijecnica": "Sarajevo Vijecnica",
    "amsBjelave": "Sarajevo Bjelave",
    "amsOtoka": "Sarajevo Otoka",
    "amsSPolje": "Sarajevo Polje",
    "amsIlidza": "Ilidza",
    "amsVogosca": "Vogosca",
    "amsISedlo": "Ivan Sedlo",
    "amsHadzici": "Hadzici",
    "amsIlijas": "Ilijas",
    "amsVisoko": "Visoko Centar",
    "amsKakanj": "Kakanj Centar",
    "amsKakanjOpcina": "Kakanj Opcina",
    "amsVares": "Vares Centar",
    "amsBrist": "Zenica Brist",
    "amsTetovo": "Zenica Tetovo",
    "amsCentarZE": "Zenica Centar",
    "amsRadakovo": "Zenica Radakovo",
    "amsVranduk": "Zenica Vranduk",
    "amsMaglaj": "Maglaj",
    "amsTesanj": "Tesanj Vatrogasni dom",
    "amsGorazde": "Gorazde Rasadnik",
    "amsTravnik": "Travnik Centar",
    "amsJajce": "Jajce Harmani",
    "amsBihac": "Bihac Nova cetvrt",
    "amsLivno": "Livno Centar",
    "amsMostar": "Mostar Bijeli Brijeg",
    "amsMostarHNK": "Mostar Kampus",
    "amsZivinice": "Zivinice Centar",
    "amsTrnovac": "Tuzla Trnovac",
    "amsSkver": "Tuzla Skver",
    "amsBukinje": "Tuzla Bukinje",
    "amsBKC": "Tuzla BKC",
    "amsLukavac": "Lukavac Centar",
}

# Their label -> our parameter code. Anything unlisted is ignored rather than
# guessed at, and reported as a warning so we notice a new pollutant appearing.
PARAMETERS: dict[str, str] = {
    "SO2": "so2",
    "NO2": "no2",
    "NOX": "nox",
    "NO": "no",
    "CO": "co",
    "O3": "o3",
    "PM10": "pm10",
    "PM2.5": "pm25",
    "PM2,5": "pm25",
}

# Everything on these pages is published in ug/m3, including CO. That is worth
# stating because CO is conventionally reported in mg/m3 and Tuzla Canton does
# exactly that. Observed CO here runs 300 to 3100, which is only sensible as
# ug/m3. Conversion to canonical units happens downstream, not in the parser.
PUBLISHED_UNIT = "ug/m3"

BASE = "https://www.fhmzbih.gov.ba/latinica/ZRAK"

_TABLE = re.compile(r"<table.*?</table>", re.S | re.I)
_ROW = re.compile(r"<tr.*?</tr>", re.S | re.I)
_CELL = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")
_DATE = re.compile(r"^\s*(\d{1,2})\.(\d{1,2})\.(\d{4})\.?\s*$")
_HOUR = re.compile(r"^\s*(\d{1,2}):00\s*$")


def _text(html: str) -> str:
    s = _TAG.sub("", html)
    s = s.replace("&nbsp;", " ").replace("&micro;", "u").replace("&amp;", "&")
    return s.strip()


def _rows(table_html: str) -> list[list[str]]:
    return [[_text(c) for c in _CELL.findall(r)] for r in _ROW.findall(table_html)]


def _normalise_label(label: str) -> str:
    """Pollutant label to lookup key.

    Whitespace is stripped entirely, not just trimmed. Most stations write PM10,
    but Vares marks its pollutants up with subscripts that flatten to 'PM 10'.
    Matching on the trimmed string alone silently dropped that whole station.
    """
    return re.sub(r"\s+", "", label).upper()


class FhmzConnector:
    slug = "fhmz"
    parser_version = "fhmz-1"

    def targets(self) -> list[FetchTarget]:
        return [
            FetchTarget(id=page, url=f"{BASE}/{page}.php", station_hint=page)
            for page in STATIONS
        ]

    def metadata(self) -> SourceMetadata:
        return SourceMetadata(
            slug=self.slug,
            name="Federalni hidrometeoroloski zavod BiH",
            operator="Federalni hidrometeoroloski zavod",
            tier="reference",
            base_url="https://www.fhmzbih.gov.ba/",
            attribution="Federalni hidrometeoroloski zavod BiH (fhmzbih.gov.ba)",
            is_primary=True,
            archive_mode="bytes",
            notes=(
                "Rolling 6 day window, nothing older published. Operator states data is "
                "unvalidated at publication and may be amended without notice, so "
                "revisions are expected here. Instruments, methods and QA are not "
                "documented on the site."
            ),
        )

    def stations(self, raw: bytes, target: FetchTarget) -> list[ParsedStation]:
        # The pages carry no coordinates. Name only; siting is filled in separately.
        name = STATIONS.get(target.id)
        if not name:
            return []
        return [ParsedStation(source_station_id=target.id, name=name)]

    def parse(self, raw: bytes, target: FetchTarget) -> list[ParsedObservation]:
        try:
            html = raw.decode("utf-8")
        except UnicodeDecodeError:
            html = raw.decode("windows-1250", errors="replace")

        tables = self._pollutant_tables(html)
        if not tables:
            raise ParseError(
                "no pollutant tables found, page layout probably changed",
                target_id=target.id,
                snippet=raw[:500],
            )

        # NOx tables carry values but no dates, so the date has to come from a
        # sibling table at the same row position. Only safe because every dated
        # table on a page lists the same days in the same order.
        sequence = self._date_sequence(tables)

        out: list[ParsedObservation] = []
        for code, rows in tables:
            hours = self._hour_columns(rows[1])
            if not hours:
                continue
            for i, row in enumerate(rows[2:]):
                own = self._parse_date(row[0]) if row else None
                day = own if own is not None else (sequence[i] if i < len(sequence) else None)
                if day is None:
                    # No table on the page dates this row. We do not invent one.
                    continue
                out.extend(
                    self._parse_day(row, hours, code, target, day, inferred=own is None)
                )
        return out

    @staticmethod
    def _pollutant_tables(html: str) -> list[tuple[str, list[list[str]]]]:
        """Tables that hold hourly values, as (parameter code, rows).

        Exceedance summary tables are excluded here: they have a different shape
        and we derive exceedances from concentrations ourselves.
        """
        found: list[tuple[str, list[list[str]]]] = []
        for table in _TABLE.findall(html):
            rows = _rows(table)
            if len(rows) < 3 or not rows[0] or len(rows[0]) != 1:
                continue
            code = PARAMETERS.get(_normalise_label(rows[0][0]))
            if code is None:
                continue
            header = rows[1]
            if not header or header[0].strip().lower() != "datum":
                continue
            found.append((code, rows))
        return found

    @staticmethod
    def _date_sequence(tables: list[tuple[str, list[list[str]]]]) -> list[date | None]:
        """Merged day-per-row-position across every dated table on the page.

        A position resolves only when the tables that date it agree. On conflict
        it stays None and undated tables lose that row, which is the safe failure.
        """
        length = max((len(rows) - 2 for _, rows in tables), default=0)
        merged: list[date | None] = [None] * length
        conflicted: set[int] = set()

        for _, rows in tables:
            for i, row in enumerate(rows[2:]):
                if not row:
                    continue
                d = FhmzConnector._parse_date(row[0])
                if d is None or i in conflicted:
                    continue
                if merged[i] is None:
                    merged[i] = d
                elif merged[i] != d:
                    merged[i] = None
                    conflicted.add(i)
        return merged

    @staticmethod
    def _parse_date(cell: str) -> date | None:
        m = _DATE.match(cell)
        if not m:
            return None
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None

    @staticmethod
    def _hour_columns(header: list[str]) -> dict[int, int]:
        """Column index -> hour. Read from the header rather than assumed."""
        hours: dict[int, int] = {}
        for idx, cell in enumerate(header):
            m = _HOUR.match(cell)
            if m:
                h = int(m.group(1))
                if 0 <= h <= 23:
                    hours[idx] = h
        return hours

    def _parse_day(
        self,
        row: list[str],
        hours: dict[int, int],
        code: str,
        target: FetchTarget,
        day_date: date,
        inferred: bool,
    ) -> list[ParsedObservation]:
        if not row:
            return []
        year, month, day = day_date.year, day_date.month, day_date.day

        out: list[ParsedObservation] = []
        for idx, hour in hours.items():
            if idx >= len(row):
                continue
            cell = row[idx].strip()
            if not cell or cell in {"-", "--"}:
                # No value published. Absence is not a withdrawal at parse time,
                # that determination belongs to ingest, which knows what we held before.
                continue

            value = self._to_decimal(cell)
            if value is None:
                continue

            start = datetime(year, month, day, hour, tzinfo=LOCAL_TZ)
            flags: list[str] = []
            # The date came from a sibling table, not this one. True for NOx.
            if inferred:
                flags.append("date_inferred")
            # On the autumn DST changeover the local hour 02:00 occurs twice and
            # the page gives no way to tell them apart. We take the first and flag it.
            if start.dst() != (start + timedelta(hours=1)).dst():
                flags.append("dst_ambiguous")

            out.append(
                ParsedObservation(
                    source_station_id=target.id,
                    parameter_code=code,
                    phenomenon_start=start,
                    phenomenon_end=start + timedelta(hours=1),
                    value=value,
                    unit=PUBLISHED_UNIT,
                    raw_value=cell,
                    raw_unit=PUBLISHED_UNIT,
                    quality_flags=tuple(flags),
                )
            )
        return out

    @staticmethod
    def _to_decimal(cell: str) -> Decimal | None:
        try:
            return Decimal(cell.replace(",", ".").replace(" ", ""))
        except (InvalidOperation, ValueError):
            return None


def discover_stations(raw: bytes) -> list[str]:
    """Station page names linked from AQI-satne.php.

    Kept separate from targets() because the site disagrees with itself about how
    many stations exist: the hourly table shows 31, the links number 33, and the
    daily page lists more. Running this against the live index tells us when the
    set changes rather than us assuming it never does.
    """
    html = raw.decode("utf-8", errors="replace")
    return sorted(set(re.findall(r'href="(ams[A-Za-z0-9]+)\.php"', html)))


_: Connector = FhmzConnector()

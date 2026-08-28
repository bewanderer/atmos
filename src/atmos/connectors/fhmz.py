"""FHMZ BiH connector.

Federalni hidrometeoroloski zavod publishes one page per station, each holding
roughly six days of hourly values as plain HTML tables. Nothing older is
available anywhere, so a missed week is gone permanently.

Page layout, confirmed against fetched fixtures:

    <table>  SO2                      <- pollutant name, single cell
    <table>  datum | 0:00 ... 23:00   <- header, then 6 data rows, newest first
             28.8.2026. | 17.86 | ...

Three quirks the parser has to handle:

1. Pollutant sets differ by station. Vijecnica has 6, Tetovo has 8.
2. NOx tables have values but a blank date column. Dates come from a sibling
   table at the same row, flagged date_inferred. Undated rows are dropped.
3. Some pages are years out of date. Tetovo served 2024 data in 2026, so dates
   are always read, never assumed from row position.

Exceedance tables are ignored. We work those out from concentrations.
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

# FHMZ publishes in local time.
SOURCE_TZ = ZoneInfo("Europe/Sarajevo")

# Page name -> station name, from the links on AQI-satne.php.
# Use discover_stations() to refresh. The site disagrees with itself on how
# many stations it has.
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

# Their label -> our code. Unlisted labels are ignored, not guessed at.
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

# Everything here is ug/m3, including CO. Worth flagging because Tuzla Canton
# publishes CO in mg/m3. Conversion happens downstream, not in the parser.
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
    """Label to lookup key. Strips all whitespace, not just the ends."""
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
            timezone="Europe/Sarajevo",
            archive_mode="bytes",
            notes=(
                "Rolling 6 day window, nothing older published. Operator states data is "
                "unvalidated at publication and may be amended without notice, so "
                "revisions are expected here. Instruments, methods and QA are not "
                "documented on the site."
            ),
        )

    def stations(self, raw: bytes, target: FetchTarget) -> list[ParsedStation]:
        # No coordinates on these pages. They are in the annual reports.
        name = STATIONS.get(target.id)
        if not name:
            return []

        # Read from page structure, not values. Vares has empty PM tables.
        try:
            html = raw.decode("utf-8")
        except UnicodeDecodeError:
            html = raw.decode("windows-1250", errors="replace")
        declared = tuple(dict.fromkeys(code for code, _ in self._pollutant_tables(html)))

        return [
            ParsedStation(
                source_station_id=target.id,
                name=name,
                declared_parameters=declared,
            )
        ]

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

        # NOx has no dates of its own. Safe to borrow because every dated
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
        """Hourly value tables, as (parameter code, rows). Skips summaries."""
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
        """Day per row position, merged across dated tables.

        A row resolves only if the tables that date it agree. On conflict it
        stays None and undated tables lose that row.
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

            start = datetime(year, month, day, hour, tzinfo=SOURCE_TZ)
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

    Separate from targets() so we notice when the set changes. The site's own
    pages disagree: 31 in the hourly table, 33 links, more on the daily page.
    """
    html = raw.decode("utf-8", errors="replace")
    return sorted(set(re.findall(r'href="(ams[A-Za-z0-9]+)\.php"', html)))


_: Connector = FhmzConnector()

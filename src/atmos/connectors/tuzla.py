"""Tuzla Canton connector.

Ministry of Spatial Planning and Environmental Protection, monitoringzrakatk.info.
Static HTML, one page per station per day, with only today and yesterday kept.
About 48 hours of retention, the shortest of any source, so a missed day is gone.

Each station has three pages: <station>.html for the latest hour,
<station>-today.html and <station>-yesterday.html for full days.

Columns are read from the header, never assumed by position, because two things
break positional parsing:

1. Some pages render the SO2 value twice, once formatted and once rounded
   (15.0 then 15), which shifts every later column right by one. Verified
   against FHMZ, which publishes four of the same stations: dropping the
   duplicate makes all 23 comparable hours match to the decimal.
2. Row 24:00 does not line up with FHMZ at either candidate hour, so it is
   skipped rather than guessed at. Everything else agrees exactly.

The daily average row is ignored. We compute our own aggregates.
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

# Tuzla Canton publishes in local time.
SOURCE_TZ = ZoneInfo("Europe/Sarajevo")

BASE = "http://monitoringzrakatk.info"

STATIONS: dict[str, str] = {
    "skver": "Tuzla Skver",
    "bkc": "Tuzla BKC",
    "bukinje": "Tuzla Bukinje",
    "lukavac": "Lukavac",
    "zivinice": "Zivinice",
    "mobilna": "Mobilna stanica",
    "mobilna-banovici": "Banovici",
    "mobilna-celic": "Celic",
    "mobilna-doboj-istok": "Doboj Istok",
    "mobilna-gracanica": "Gracanica",
    "mobilna-gradacac": "Gradacac",
    "mobilna-kalesija": "Kalesija",
    "mobilna-kladanj": "Kladanj",
    "mobilna-sapna": "Sapna",
    "mobilna-srebrenik": "Srebrenik",
    "mobilna-teocak": "Teocak",
}

MOBILE = {s for s in STATIONS if s.startswith("mobilna")}

# Header text -> our code. Checked in order, so PM2.5 is tested before PM10.
HEADER_PATTERNS: list[tuple[str, str]] = [
    ("PM2.5", "pm25"),
    ("PM10", "pm10"),
    ("SO2", "so2"),
    ("NO2", "no2"),
    ("O3", "o3"),
    ("CO", "co"),
    ("RELATIVNAVLAZNOST", "rh"),
    ("ZRACNIPRITISAK", "press"),
    ("TEMPERATURAZRAKA", "temp"),
    ("BRZINAVJETRA", "wspd"),
    ("SMJERVJETRA", "wdir"),
]

_TABLE = re.compile(r"<table.*?</table>", re.S | re.I)
_ROW = re.compile(r"<tr.*?</tr>", re.S | re.I)
_CELL = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")
_HOUR = re.compile(r"^\s*(\d{1,2}):00\s*$")
_PAGE_DATE = re.compile(r"Satni\s+podaci:\s*(\d{2})\.(\d{2})\.(\d{4})")
_UNIT = re.compile(r"\(([^)]+)\)")

# Subscripts and superscripts, so SO₂ (µg/m³) compares as SO2 (ug/m3).
_DIGITS = str.maketrans("₀₁₂₃₄₅₆₇₈₉⁰¹²³⁴⁵⁶⁷⁸⁹", "01234567890123456789")


def _text(html: str) -> str:
    s = _TAG.sub("", html)
    s = s.replace("&nbsp;", " ").replace("&micro;", "u").replace("&amp;", "&")
    return re.sub(r"\s+", " ", s).strip()


def _rows(table_html: str) -> list[list[str]]:
    return [[_text(c) for c in _CELL.findall(r)] for r in _ROW.findall(table_html)]


def _norm(s: str) -> str:
    """Header text to a comparable key."""
    return re.sub(r"[^A-Z0-9.]", "", s.translate(_DIGITS).upper())


def _to_decimal(cell: str) -> Decimal | None:
    cell = cell.strip()
    if not cell or cell in {"--", "-", "---"}:
        return None
    try:
        return Decimal(cell.replace(",", ".").replace(" ", ""))
    except (InvalidOperation, ValueError):
        return None


class TuzlaConnector:
    slug = "tuzla"
    parser_version = "tuzla-1"

    def targets(self) -> list[FetchTarget]:
        out = []
        for page in STATIONS:
            for suffix in ("today", "yesterday"):
                out.append(
                    FetchTarget(
                        id=f"{page}-{suffix}",
                        url=f"{BASE}/{page}-{suffix}.html",
                        station_hint=page,
                    )
                )
        return out

    def metadata(self) -> SourceMetadata:
        return SourceMetadata(
            slug=self.slug,
            name="Tuzlanski kanton, monitoring kvaliteta zraka",
            operator="Ministarstvo prostornog uredenja i zastite okolice TK",
            tier="reference",
            base_url=BASE,
            attribution="Tuzlanski kanton, Ministarstvo prostornog uredenja i zastite okolice",
            is_primary=True,
            timezone="Europe/Sarajevo",
            archive_mode="bytes",
            notes=(
                "About 48 hours of retention, today and yesterday only. Publishes CO in "
                "mg/m3 where FHMZ uses ug/m3. Carries co-located meteorology per station."
            ),
        )

    def stations(self, raw: bytes, target: FetchTarget) -> list[ParsedStation]:
        page = target.station_hint or target.id.rsplit("-", 1)[0]
        name = STATIONS.get(page)
        if not name:
            return []
        columns = self._columns(self._data_table(raw.decode("utf-8", errors="replace")))
        return [
            ParsedStation(
                source_station_id=page,
                name=name,
                is_mobile=page in MOBILE,
                declared_parameters=tuple(dict.fromkeys(c for c, _ in columns if c)),
            )
        ]

    def parse(self, raw: bytes, target: FetchTarget) -> list[ParsedObservation]:
        html = raw.decode("utf-8", errors="replace")
        page = target.station_hint or target.id.rsplit("-", 1)[0]

        day = self._page_date(html)
        if day is None:
            raise ParseError("no page date found", target_id=target.id, snippet=raw[:500])

        rows = self._data_table(html)
        columns = self._columns(rows)
        if not columns:
            raise ParseError("no known columns in header", target_id=target.id, snippet=raw[:500])

        out: list[ParsedObservation] = []
        for row in rows[1:]:
            if not row:
                continue
            m = _HOUR.match(row[0])
            if not m:
                # Daily average row and anything else that is not an hour.
                continue
            hour = int(m.group(1))

            values = self._align(row[1:], len(columns))
            if values is None:
                continue
            if hour == 24:
                # Does not match FHMZ at either candidate hour. Not guessed at.
                continue

            start = datetime(day.year, day.month, day.day, hour, tzinfo=SOURCE_TZ)
            flags: tuple[str, ...] = ()
            if start.dst() != (start + timedelta(hours=1)).dst():
                flags = ("dst_ambiguous",)

            for (code, unit), cell in zip(columns, values, strict=False):
                if code is None:
                    continue
                value = _to_decimal(cell)
                if value is None:
                    continue
                out.append(
                    ParsedObservation(
                        source_station_id=page,
                        parameter_code=code,
                        phenomenon_start=start,
                        phenomenon_end=start + timedelta(hours=1),
                        value=value,
                        unit=unit,
                        raw_value=cell.strip(),
                        raw_unit=unit,
                        quality_flags=flags,
                    )
                )
        return out

    @staticmethod
    def _page_date(html: str) -> date | None:
        """The day the page covers, from its 'Satni podaci: DD.MM.YYYY' heading."""
        m = _PAGE_DATE.search(_TAG.sub(" ", html))
        if not m:
            return None
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None

    @staticmethod
    def _data_table(html: str) -> list[list[str]]:
        """First table whose header names a parameter we know."""
        for table in _TABLE.findall(html):
            rows = _rows(table)
            if len(rows) < 2 or len(rows[0]) < 2:
                continue
            joined = _norm(" ".join(rows[0]))
            if any(pat in joined for pat, _ in HEADER_PATTERNS):
                return rows
        return []

    @staticmethod
    def _columns(rows: list[list[str]]) -> list[tuple[str | None, str]]:
        """Header cells to (parameter code, unit), skipping the row label column."""
        if not rows:
            return []
        out: list[tuple[str | None, str]] = []
        for cell in rows[0][1:]:
            key = _norm(cell)
            code = next((c for pat, c in HEADER_PATTERNS if pat in key), None)
            unit_m = _UNIT.search(cell.translate(_DIGITS))
            unit = unit_m.group(1).replace("µ", "u").strip() if unit_m else ""
            out.append((code, unit))
        return out

    @staticmethod
    def _align(values: list[str], expected: int) -> list[str] | None:
        """Fix the duplicated SO2 cell, or give up on the row.

        Some pages print SO2 twice, once formatted and once rounded (15.0 then
        15), pushing every later value one column right. Only the exact
        duplicate is removed. Any other mismatch returns None and the row is
        skipped rather than repaired on a guess.
        """
        if len(values) == expected:
            return values
        if len(values) != expected + 1:
            return None

        first, second = _to_decimal(values[0]), _to_decimal(values[1])
        if first is None or second is None:
            return None
        # Equal, or the second is the first rounded to a whole number.
        if second == first or second == first.to_integral_value():
            return values[:1] + values[2:]
        return None


def _check() -> Connector:
    return TuzlaConnector()

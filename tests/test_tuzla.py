"""Tuzla Canton parser tests.

The strongest test here is cross_source: FHMZ publishes four of the same
stations, so the parser can be checked against a second, independent rendering
of the same instruments rather than only against itself.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from atmos.connectors.base import FetchTarget, ParseError
from atmos.connectors.fhmz import FhmzConnector
from atmos.connectors.tuzla import STATIONS, SOURCE_TZ, TuzlaConnector

FIXTURES = Path(__file__).parent / "fixtures" / "tuzla"
SNAPSHOTS = Path(__file__).parents[1] / "data" / "snapshots" / "fhmz" / "2026-08-28"

OVERLAP = [
    ("skver-yesterday", "skver", "amsSkver"),
    ("bukinje-yesterday", "bukinje", "amsBukinje"),
    ("lukavac-yesterday", "lukavac", "amsLukavac"),
    ("zivinice-yesterday", "zivinice", "amsZivinice"),
]


@pytest.fixture
def conn() -> TuzlaConnector:
    return TuzlaConnector()


def load(name: str) -> bytes:
    return (FIXTURES / f"{name}.html").read_bytes()


def target(name: str, station: str) -> FetchTarget:
    return FetchTarget(id=name, url=f"x/{name}.html", station_hint=station)


def test_targets_cover_today_and_yesterday(conn: TuzlaConnector) -> None:
    targets = conn.targets()
    assert len(targets) == len(STATIONS) * 2 == 32
    assert len({t.id for t in targets}) == len(targets)


def test_page_date_is_read_not_assumed(conn: TuzlaConnector) -> None:
    obs = conn.parse(load("skver-yesterday"), target("skver-yesterday", "skver"))
    days = {o.phenomenon_start.astimezone(SOURCE_TZ).date().isoformat() for o in obs}
    assert days == {"2026-08-27"}


def test_columns_come_from_the_header(conn: TuzlaConnector) -> None:
    obs = conn.parse(load("skver-yesterday"), target("skver-yesterday", "skver"))
    assert {o.parameter_code for o in obs} >= {"so2", "no2", "o3", "pm25", "pm10"}


def test_co_is_published_in_mg_not_ug(conn: TuzlaConnector) -> None:
    """Opposite of FHMZ. Recorded as published, converted downstream."""
    obs = conn.parse(load("bukinje-yesterday"), target("bukinje-yesterday", "bukinje"))
    co = [o for o in obs if o.parameter_code == "co"]
    assert co
    assert all(o.unit == "mg/m3" for o in co)


def test_duplicated_so2_cell_does_not_shift_columns(conn: TuzlaConnector) -> None:
    """Skver renders SO2 twice. PM2.5 must not end up holding the O3 value."""
    obs = conn.parse(load("skver-yesterday"), target("skver-yesterday", "skver"))
    at01 = {
        o.parameter_code: o.raw_value
        for o in obs
        if o.phenomenon_start.astimezone(SOURCE_TZ).hour == 1
    }
    assert at01["so2"] == "11.1"
    assert at01["no2"] == "20.4"
    assert at01["o3"] == "37.7"
    assert at01["pm25"] == "7.2"
    assert at01["pm10"] == "12.0"


def test_pm25_never_exceeds_pm10(conn: TuzlaConnector) -> None:
    """Physically impossible, so it would mean the columns are misaligned."""
    for fixture, station, _ in OVERLAP:
        obs = conn.parse(load(fixture), target(fixture, station))
        by_hour: dict[int, dict[str, Decimal]] = {}
        for o in obs:
            by_hour.setdefault(o.phenomenon_start.hour, {})[o.parameter_code] = o.value
        for hour, vals in by_hour.items():
            if "pm25" in vals and "pm10" in vals:
                assert vals["pm25"] <= vals["pm10"], f"{fixture} {hour}:00"


def test_daily_average_row_is_not_an_observation(conn: TuzlaConnector) -> None:
    obs = conn.parse(load("skver-yesterday"), target("skver-yesterday", "skver"))
    hours = {o.phenomenon_start.astimezone(SOURCE_TZ).hour for o in obs}
    assert 0 not in hours or len(hours) <= 24


def test_hour_24_is_skipped_not_guessed(conn: TuzlaConnector) -> None:
    """It matches FHMZ at neither candidate hour, so we do not place it."""
    obs = conn.parse(load("skver-yesterday"), target("skver-yesterday", "skver"))
    pm10 = {
        o.phenomenon_start.astimezone(SOURCE_TZ).hour: o.raw_value
        for o in obs
        if o.parameter_code == "pm10"
    }
    assert max(pm10) == 23
    assert "14.8" not in pm10.values()


def test_values_are_stored_exactly_as_published(conn: TuzlaConnector) -> None:
    obs = conn.parse(load("skver-yesterday"), target("skver-yesterday", "skver"))
    pm10 = [o for o in obs if o.parameter_code == "pm10"]
    assert any(o.raw_value == "12.0" for o in pm10)  # not normalised to "12"


def test_missing_values_are_skipped(conn: TuzlaConnector) -> None:
    for fixture, station, _ in OVERLAP:
        obs = conn.parse(load(fixture), target(fixture, station))
        assert all(o.value is not None for o in obs)
        assert not any(o.raw_value.strip() in {"--", ""} for o in obs)


def test_layout_change_raises(conn: TuzlaConnector) -> None:
    with pytest.raises(ParseError):
        conn.parse(b"<html><body>redesigned</body></html>", target("skver-today", "skver"))


@pytest.mark.skipif(not SNAPSHOTS.exists(), reason="FHMZ snapshots not present")
def test_cross_source_agreement_with_fhmz(conn: TuzlaConnector) -> None:
    """FHMZ publishes the same four stations. Everything must match except CO,
    which differs by exactly 1000 because the two use mg/m3 and ug/m3."""
    fh = FhmzConnector()
    compared = identical = co_ratio_ok = other = 0

    for fixture, station, page in OVERLAP:
        t = {
            (o.parameter_code, o.phenomenon_start): Decimal(o.raw_value)
            for o in conn.parse(load(fixture), target(fixture, station))
        }
        f = {
            (o.parameter_code, o.phenomenon_start): Decimal(o.raw_value)
            for o in fh.parse((SNAPSHOTS / f"{page}.html").read_bytes(),
                              FetchTarget(id=page, url="x"))
        }
        for k in set(t) & set(f):
            compared += 1
            if t[k] == f[k]:
                identical += 1
            elif k[0] == "co" and t[k] != 0 and f[k] / t[k] == 1000:
                co_ratio_ok += 1
            else:
                other += 1

    assert compared > 400
    assert other == 0, "a non-CO reading disagreed with FHMZ"
    assert co_ratio_ok > 0
    assert identical + co_ratio_ok == compared

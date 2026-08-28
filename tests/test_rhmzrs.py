"""RHMZ RS parser tests."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from atmos.connectors.base import FetchTarget, ParseError
from atmos.connectors.rhmzrs import STATIONS, SOURCE_TZ, RhmzRsConnector

FIXTURE = Path(__file__).parent / "fixtures" / "rhmzrs" / "EkoPodaci.json"


@pytest.fixture
def conn() -> RhmzRsConnector:
    return RhmzRsConnector()


@pytest.fixture
def raw() -> bytes:
    return FIXTURE.read_bytes()


def target() -> FetchTarget:
    return FetchTarget(id="ekopodaci", url="x")


def test_single_target(conn: RhmzRsConnector) -> None:
    assert len(conn.targets()) == 1


def test_parses_all_reporting_stations(conn: RhmzRsConnector, raw: bytes) -> None:
    obs = conn.parse(raw, target())
    assert obs
    assert {o.source_station_id for o in obs} <= set(STATIONS)


def test_co_is_mg_and_others_are_ug(conn: RhmzRsConnector, raw: bytes) -> None:
    """Matches Tuzla, opposite to FHMZ. Confirmed from the operator daily report."""
    obs = conn.parse(raw, target())
    for o in obs:
        assert o.unit == ("mg/m3" if o.parameter_code == "co" else "ug/m3")


def test_benzene_and_h2s_are_captured(conn: RhmzRsConnector, raw: bytes) -> None:
    """Brod reports both. No other source we collect publishes benzene."""
    obs = conn.parse(raw, target())
    codes = {o.parameter_code for o in obs}
    assert "c6h6" in codes
    assert "h2s" in codes


def test_missing_readings_marked_with_a_star_are_skipped(
    conn: RhmzRsConnector, raw: bytes
) -> None:
    obs = conn.parse(raw, target())
    assert all(o.value is not None for o in obs)
    assert not any(o.raw_value.strip() in {"*", "-", ""} for o in obs)


def test_station_with_no_readings_still_yields_metadata(
    conn: RhmzRsConnector, raw: bytes
) -> None:
    """Doboj returns coordinates but every value is a star."""
    stations = {s.source_station_id for s in conn.stations(raw, target())}
    reporting = {o.source_station_id for o in conn.parse(raw, target())}
    assert "Doboj" in stations
    assert "Doboj" not in reporting


def test_coordinates_are_read(conn: RhmzRsConnector, raw: bytes) -> None:
    """The feed gives coordinates, which the FHMZ pages do not."""
    stations = {s.source_station_id: s for s in conn.stations(raw, target())}
    bl = stations["Banjaluka"]
    assert bl.latitude is not None and bl.longitude is not None
    assert 42 < bl.latitude < 46
    assert 15 < bl.longitude < 20


def test_timestamps_are_local_and_hour_long(conn: RhmzRsConnector, raw: bytes) -> None:
    for o in conn.parse(raw, target()):
        assert o.phenomenon_start.tzinfo is not None
        assert (o.phenomenon_end - o.phenomenon_start).total_seconds() == 3600
        assert o.phenomenon_start.astimezone(SOURCE_TZ).minute == 0


def test_values_stored_exactly_as_published(conn: RhmzRsConnector, raw: bytes) -> None:
    obs = conn.parse(raw, target())
    co = [o for o in obs if o.parameter_code == "co"]
    assert co
    assert all(o.value == Decimal(o.raw_value) for o in obs)


def test_operator_index_is_not_ingested(conn: RhmzRsConnector, raw: bytes) -> None:
    """The feed carries an indeks block. We compute indices ourselves."""
    obs = conn.parse(raw, target())
    assert not any(o.parameter_code in {"indeks", "index", "aqi"} for o in obs)


def test_bad_payload_raises(conn: RhmzRsConnector) -> None:
    with pytest.raises(ParseError):
        conn.parse(b"not json at all", target())
    with pytest.raises(ParseError):
        conn.parse(b'{"indeks": {}}', target())

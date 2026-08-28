"""Sensor.Community parser tests."""

from __future__ import annotations

from datetime import UTC
from pathlib import Path

import pytest

from atmos.connectors.base import FetchTarget, ParseError
from atmos.connectors.sensorcommunity import AREAS, SensorCommunityConnector

FIXTURE = Path(__file__).parent / "fixtures" / "sensorcommunity" / "area_sarajevo.json"


@pytest.fixture
def conn() -> SensorCommunityConnector:
    return SensorCommunityConnector()


@pytest.fixture
def raw() -> bytes:
    return FIXTURE.read_bytes()


def target() -> FetchTarget:
    return FetchTarget(id="sarajevo", url="x", station_hint="sarajevo")


def test_targets_cover_the_country(conn: SensorCommunityConnector) -> None:
    targets = conn.targets()
    assert len(targets) == len(AREAS) == 6
    assert all("area=" in t.url for t in targets)


def test_timestamps_are_utc(conn: SensorCommunityConnector, raw: bytes) -> None:
    """This source publishes UTC, unlike the institutional ones."""
    obs = conn.parse(raw, target())
    assert obs
    assert all(o.phenomenon_start.tzinfo is UTC for o in obs)


def test_readings_are_instantaneous(conn: SensorCommunityConnector, raw: bytes) -> None:
    """Not hourly means. An equal start and end says so rather than inventing a window."""
    for o in conn.parse(raw, target()):
        assert o.phenomenon_start == o.phenomenon_end


def test_only_bosnian_sensors_are_kept(conn: SensorCommunityConnector, raw: bytes) -> None:
    """The API has no country filter, so the radius picks up neighbours."""
    import json

    payload = json.loads(raw.decode("utf-8"))
    countries = {r["location"]["country"] for r in payload}
    kept = {s.source_station_id for s in conn.stations(raw, target())}
    ba_ids = {
        str(r["location"]["id"]) for r in payload if r["location"]["country"] == "BA"
    }
    assert kept == ba_ids
    if countries - {"BA"}:
        assert len(kept) < len({str(r["location"]["id"]) for r in payload})


def test_particulates_are_mapped_from_p_codes(
    conn: SensorCommunityConnector, raw: bytes
) -> None:
    codes = {o.parameter_code for o in conn.parse(raw, target())}
    assert "pm10" in codes  # P1
    assert "pm25" in codes  # P2


def test_pressure_is_pascals_as_published(
    conn: SensorCommunityConnector, raw: bytes
) -> None:
    """Canonical unit is hPa. Conversion is downstream, not in the parser."""
    press = [o for o in conn.parse(raw, target()) if o.parameter_code == "press"]
    if press:
        assert all(o.unit == "Pa" for o in press)
        assert all(o.value > 10000 for o in press)


def test_derived_sealevel_pressure_is_not_ingested(
    conn: SensorCommunityConnector, raw: bytes
) -> None:
    """The source computes it from pressure and altitude. We keep the measurement."""
    import json

    payload = json.loads(raw.decode("utf-8"))
    present = any(
        v["value_type"] == "pressure_at_sealevel"
        for r in payload
        for v in r["sensordatavalues"]
    )
    obs = conn.parse(raw, target())
    if present:
        assert all(o.raw_unit != "sealevel" for o in obs)
    assert {o.parameter_code for o in obs}.isdisjoint({"press_sealevel"})


def test_reduced_precision_locations_are_flagged(
    conn: SensorCommunityConnector, raw: bytes
) -> None:
    """Many locations are rounded for privacy, which matters for radius matching."""
    stations = conn.stations(raw, target())
    assert stations
    assert any(s.location_precise is False for s in stations)


def test_stations_carry_coordinates(conn: SensorCommunityConnector, raw: bytes) -> None:
    for s in conn.stations(raw, target()):
        assert s.latitude is not None and s.longitude is not None
        assert 42 < s.latitude < 46
        assert 15 < s.longitude < 20


def test_values_stored_exactly_as_published(
    conn: SensorCommunityConnector, raw: bytes
) -> None:
    from decimal import Decimal

    for o in conn.parse(raw, target()):
        assert o.value == Decimal(o.raw_value)


def test_bad_payload_raises(conn: SensorCommunityConnector) -> None:
    with pytest.raises(ParseError):
        conn.parse(b"nonsense", target())
    with pytest.raises(ParseError):
        conn.parse(b'{"not": "a list"}', target())


def test_empty_result_is_not_an_error(conn: SensorCommunityConnector) -> None:
    """A radius with no sensors reporting is normal, not a failure."""
    assert conn.parse(b"[]", target()) == []

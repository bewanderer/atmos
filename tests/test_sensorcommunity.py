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


# --- archive backfill -------------------------------------------------------

ARCHIVE_FIXTURES = {
    "sds011": ("archive_sds011_84500.csv", {"pm10", "pm25"}),
    "dht22": ("archive_dht22_36416.csv", {"temp", "rh"}),
    "bme280": ("archive_bme280_80927.csv", {"press", "temp", "rh"}),
}


def archive(name: str) -> bytes:
    return (FIXTURE.parent / name).read_bytes()


def archive_target(conn: SensorCommunityConnector) -> FetchTarget:
    import datetime as dt

    return conn.archive_target("84500", "SDS011", dt.date(2026, 8, 26))


def test_archive_url_is_built_not_looked_up(conn: SensorCommunityConnector) -> None:
    """Day listings are 4.6 MB and flaky, so URLs are constructed directly."""
    t = archive_target(conn)
    assert t.url.endswith("/2026-08-26/2026-08-26_sds011_sensor_84500.csv")
    assert t.station_hint == "84500"


@pytest.mark.parametrize("kind", list(ARCHIVE_FIXTURES))
def test_archive_csv_parses_per_sensor_type(
    conn: SensorCommunityConnector, kind: str
) -> None:
    """Columns differ by sensor type, so they are read from the header."""
    name, expected = ARCHIVE_FIXTURES[kind]
    obs = conn.parse_archive(archive(name), archive_target(conn))
    assert obs
    assert {o.parameter_code for o in obs} == expected


def test_archive_timestamps_are_utc_and_instantaneous(
    conn: SensorCommunityConnector
) -> None:
    obs = conn.parse_archive(archive("archive_sds011_84500.csv"), archive_target(conn))
    for o in obs:
        assert o.phenomenon_start.tzinfo is UTC
        assert o.phenomenon_start == o.phenomenon_end


def test_archive_ignores_sensor_diagnostics(conn: SensorCommunityConnector) -> None:
    """durP1, ratioP1 and the rest describe the instrument, not the air."""
    obs = conn.parse_archive(archive("archive_sds011_84500.csv"), archive_target(conn))
    assert {o.parameter_code for o in obs} == {"pm10", "pm25"}


def test_archive_ignores_derived_sealevel_pressure(
    conn: SensorCommunityConnector
) -> None:
    obs = conn.parse_archive(archive("archive_bme280_80927.csv"), archive_target(conn))
    assert "press" in {o.parameter_code for o in obs}
    header = archive("archive_bme280_80927.csv").decode().splitlines()[0]
    assert "pressure_sealevel" in header, "fixture should contain the column we skip"
    assert len({o.parameter_code for o in obs}) == 3


def test_archive_pressure_is_pascals_as_published(
    conn: SensorCommunityConnector
) -> None:
    obs = conn.parse_archive(archive("archive_bme280_80927.csv"), archive_target(conn))
    press = [o for o in obs if o.parameter_code == "press"]
    assert all(o.unit == "Pa" for o in press)
    assert all(o.value > 10000 for o in press)


def test_archive_station_comes_from_the_location_column(
    conn: SensorCommunityConnector
) -> None:
    """The CSV keys on location, not sensor, matching the live feed."""
    stations = conn.archive_stations(archive("archive_sds011_84500.csv"),
                                     archive_target(conn))
    assert len(stations) == 1
    s = stations[0]
    assert s.source_station_id == "74725"
    assert s.latitude is not None and s.longitude is not None
    assert s.location_precise is False


def test_archive_values_stored_exactly_as_published(
    conn: SensorCommunityConnector
) -> None:
    from decimal import Decimal

    for name, _ in ARCHIVE_FIXTURES.values():
        for o in conn.parse_archive(archive(name), archive_target(conn)):
            assert o.value == Decimal(o.raw_value)


def test_archive_rejects_an_unexpected_header(conn: SensorCommunityConnector) -> None:
    with pytest.raises(ParseError):
        conn.parse_archive(b"a;b;c\n1;2;3\n", archive_target(conn))


def test_archive_tolerates_a_short_row(conn: SensorCommunityConnector) -> None:
    """A truncated line is skipped, not guessed at."""
    good = archive("archive_sds011_84500.csv").decode().splitlines()
    broken = "\n".join(good[:3] + ["84500;SDS011;74725"]).encode()
    obs = conn.parse_archive(broken, archive_target(conn))
    assert len(obs) == 4  # two rows, two parameters each


def test_archive_empty_file_is_not_an_error(conn: SensorCommunityConnector) -> None:
    assert conn.parse_archive(b"", archive_target(conn)) == []

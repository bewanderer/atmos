"""FHMZ parser tests, run against archived fixtures.

Parsers are pure functions over bytes, so these need no network and no database.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from atmos.connectors.base import FetchTarget
from atmos.connectors.fhmz import (
    FHMZ,
    MIKK,
    REGISTRY,
    STATIONS,
    ZZJZKS,
    FhmzConnector,
    discover_stations,
)

FIXTURES = Path(__file__).parent / "fixtures" / "fhmz"


def load(name: str) -> bytes:
    return (FIXTURES / f"{name}.html").read_bytes()


@pytest.fixture
def conn() -> FhmzConnector:
    return FhmzConnector()


def target(name: str) -> FetchTarget:
    return FetchTarget(id=name, url=f"x/{name}.php", station_hint=name)


def test_targets_cover_every_known_station(conn: FhmzConnector) -> None:
    targets = conn.targets()
    assert len(targets) == len(STATIONS) == 33
    assert all(t.url.endswith(".php") for t in targets)
    assert len({t.id for t in targets}) == len(targets)


def test_parses_vijecnica(conn: FhmzConnector) -> None:
    obs = conn.parse(load("amsVijecnica"), target("amsVijecnica"))
    assert obs

    codes = {o.parameter_code for o in obs}
    assert codes == {"so2", "no2", "nox", "no", "co", "pm10"}
    assert "o3" not in codes  # this station does not publish ozone


def test_parses_tetovo_with_wider_parameter_set(conn: FhmzConnector) -> None:
    obs = conn.parse(load("amsTetovo"), target("amsTetovo"))
    codes = {o.parameter_code for o in obs}
    assert codes == {"so2", "no2", "nox", "no", "co", "o3", "pm10", "pm25"}


def test_known_value_is_read_exactly(conn: FhmzConnector) -> None:
    """PM10 at Vijecnica, 27.8.2026 00:00 local, published as 6.42."""
    obs = conn.parse(load("amsVijecnica"), target("amsVijecnica"))
    hit = [
        o
        for o in obs
        if o.parameter_code == "pm10"
        and o.phenomenon_start.astimezone(UTC)
        == datetime(2026, 8, 26, 22, 0, tzinfo=UTC)
    ]
    assert len(hit) == 1
    assert hit[0].value == Decimal("6.42")
    assert hit[0].raw_value == "6.42"


def test_local_time_is_converted_to_utc(conn: FhmzConnector) -> None:
    """August is CEST, UTC+2. Local 00:00 must land on 22:00 UTC the day before."""
    obs = conn.parse(load("amsVijecnica"), target("amsVijecnica"))
    o = min(obs, key=lambda x: x.phenomenon_start)
    utc = o.phenomenon_start.astimezone(UTC)
    assert utc.utcoffset() == datetime.now(UTC).utcoffset()
    assert o.phenomenon_start.utcoffset().total_seconds() == 7200


def test_windows_are_one_hour_and_ordered(conn: FhmzConnector) -> None:
    for o in conn.parse(load("amsTetovo"), target("amsTetovo")):
        assert o.phenomenon_end > o.phenomenon_start
        assert (o.phenomenon_end - o.phenomenon_start).total_seconds() == 3600


def test_units_are_published_units_not_canonical(conn: FhmzConnector) -> None:
    """CO is published in ug/m3 here. Converting is downstream work, not the parser's."""
    obs = conn.parse(load("amsVijecnica"), target("amsVijecnica"))
    co = [o for o in obs if o.parameter_code == "co"]
    assert co
    assert all(o.unit == "ug/m3" for o in co)
    # Sanity: ug/m3 CO in an urban street sits in the hundreds, not the units.
    assert max(o.value for o in co) > 100


def test_no_duplicate_readings(conn: FhmzConnector) -> None:
    obs = conn.parse(load("amsTetovo"), target("amsTetovo"))
    keys = [(o.parameter_code, o.phenomenon_start) for o in obs]
    assert len(keys) == len(set(keys))


def test_missing_cells_produce_no_observation(conn: FhmzConnector) -> None:
    """The current day is partial. Blank cells must be skipped, not read as zero."""
    obs = conn.parse(load("amsVijecnica"), target("amsVijecnica"))
    assert all(o.value is not None for o in obs)
    assert not any(o.raw_value.strip() == "" for o in obs)


def test_station_metadata(conn: FhmzConnector) -> None:
    stations = conn.stations(load("amsVijecnica"), target("amsVijecnica"))
    assert len(stations) == 1
    st = stations[0]
    assert st.source_station_id == "amsVijecnica"
    assert st.name == "Sarajevo Vijecnica"
    # Coordinates come from the annual report, not the page.
    assert (st.latitude, st.longitude) == (43.859, 18.434)
    assert st.elevation_m == 554
    assert st.station_type == "traffic"
    assert st.area_type == "urban"
    # Published by FHMZ, run by the Sarajevo public health institute.
    assert st.operator == ZZJZKS


def test_station_without_a_registry_entry_gets_no_coordinates(
    conn: FhmzConnector, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A guessed position would silently break every comparison by distance.

    Every page has an entry today. A new station will appear on the site before
    it appears in a report, so this path has to keep working.
    """
    monkeypatch.delitem(REGISTRY, "amsSPolje")
    st = conn.stations(load("amsSPolje"), target("amsSPolje"))[0]
    assert st.name == "Sarajevo Polje"
    assert st.latitude is None
    assert st.longitude is None
    assert st.elevation_m is None
    assert st.operator is None
    # Still useful: we know what it claims to publish.
    assert st.declared_parameters


def test_every_page_has_a_position(conn: FhmzConnector) -> None:
    """Comparable sets match stations by distance. One without a position
    silently drops out of every comparison."""
    assert set(REGISTRY) == set(STATIONS)


def test_the_two_stations_added_in_2025_are_loaded(conn: FhmzConnector) -> None:
    """Both postdate the 2024 report, so they read as missing until 2025.

    Saraj Polje is a Novi Grad neighbourhood, not the US embassy station, which
    left the network in 2025. Mostar Kampus is at the Dzemal Bijedic campus.
    """
    st = conn.stations(load("amsSPolje"), target("amsSPolje"))[0]
    assert (st.latitude, st.longitude, st.elevation_m) == (43.837, 18.342, 512)
    assert st.operator == ZZJZKS

    assert REGISTRY["amsMostarHNK"][:6] == (
        "BA0072A", 43.354, 17.809, 72, "background", "urban")


def test_registry_positions_are_inside_bosnia() -> None:
    """A transposed lat/lon or a stray digit puts a station in another country."""
    for page, (code, lat, lon, elevation, _type, _area, operator) in REGISTRY.items():
        assert page in STATIONS, page
        assert code.startswith("BA"), page
        assert 42.5 < lat < 45.4, page
        assert 15.6 < lon < 19.7, page
        assert 0 < elevation < 2400, page
        assert operator, page


def test_registry_codes_and_positions_are_not_reused() -> None:
    """Two stations sharing a code or a position would collapse into one."""
    codes = [v[0] for v in REGISTRY.values()]
    assert len(set(codes)) == len(codes)
    # Kakanj Centar and Kakanj Opcina sit close but are distinct instruments.
    positions = [(v[1], v[2]) for v in REGISTRY.values()]
    assert len(set(positions)) == len(positions)


def test_source_metadata_declares_licensing_fields(conn: FhmzConnector) -> None:
    m = conn.metadata()
    assert m.tier == "reference"
    assert m.is_primary is True
    assert m.attribution
    assert m.archive_mode == "bytes"


def test_layout_change_raises_rather_than_silently_returning_nothing(
    conn: FhmzConnector,
) -> None:
    from atmos.connectors.base import ParseError

    with pytest.raises(ParseError):
        conn.parse(b"<html><body><p>redesigned</p></body></html>", target("amsVijecnica"))


def test_discover_stations_reads_links() -> None:
    html = b'<a href="amsVijecnica.php">x</a><a href="amsTetovo.php">y</a><a href="other.php">z</a>'
    assert discover_stations(html) == ["amsTetovo", "amsVijecnica"]


def test_nox_is_parsed_despite_blank_date_column(conn: FhmzConnector) -> None:
    """NOx tables carry values but no dates. They must not be silently dropped."""
    obs = conn.parse(load("amsVijecnica"), target("amsVijecnica"))
    nox = [o for o in obs if o.parameter_code == "nox"]
    assert len(nox) == 118
    assert all("date_inferred" in o.quality_flags for o in nox)


def test_only_nox_carries_inferred_dates(conn: FhmzConnector) -> None:
    """Everything else dates itself, so nothing else should be flagged."""
    obs = conn.parse(load("amsVijecnica"), target("amsVijecnica"))
    flagged = {o.parameter_code for o in obs if "date_inferred" in o.quality_flags}
    assert flagged == {"nox"}


def test_stale_station_dates_are_read_not_assumed(conn: FhmzConnector) -> None:
    """Tetovo was serving 2024 data in 2026. Dates must come from the page."""
    obs = conn.parse(load("amsTetovo"), target("amsTetovo"))
    years = {o.phenomenon_start.year for o in obs}
    assert years == {2024}


def test_undated_rows_are_dropped_not_invented(conn: FhmzConnector) -> None:
    """The Tetovo page has a row no table dates. Its values must not be kept."""
    obs = conn.parse(load("amsTetovo"), target("amsTetovo"))
    days = sorted({o.phenomenon_start.date() for o in obs})
    assert len(days) == 5
    assert str(days[0]) == "2024-07-23"
    assert str(days[-1]) == "2024-08-12"


def test_inferred_dates_match_a_sibling_table(conn: FhmzConnector) -> None:
    """NOx days must be exactly the days the dated tables report, no extras."""
    obs = conn.parse(load("amsTetovo"), target("amsTetovo"))
    nox_days = {o.phenomenon_start.date() for o in obs if o.parameter_code == "nox"}
    o3_days = {o.phenomenon_start.date() for o in obs if o.parameter_code == "o3"}
    assert nox_days == o3_days


def test_h2s_is_parsed_where_the_station_measures_it(conn: FhmzConnector) -> None:
    """H2S sits in a normal data table. Dropping it lost readings silently."""
    obs = conn.parse(load("amsIlidza"), target("amsIlidza"))
    h2s = [o for o in obs if o.parameter_code == "h2s"]
    assert h2s
    assert all(o.unit == "ug/m3" for o in h2s)
    assert all(o.value is not None and o.value >= 0 for o in h2s)
    # Declared too, so a station whose analyser goes quiet is still known.
    st = conn.stations(load("amsIlidza"), target("amsIlidza"))[0]
    assert "h2s" in st.declared_parameters


def test_stations_without_h2s_report_none(conn: FhmzConnector) -> None:
    """Only three stations measure it. The rest must not gain an empty series."""
    obs = conn.parse(load("amsVijecnica"), target("amsVijecnica"))
    assert not [o for o in obs if o.parameter_code == "h2s"]
    st = conn.stations(load("amsVijecnica"), target("amsVijecnica"))[0]
    assert "h2s" not in st.declared_parameters


def test_operators_match_the_report_not_the_network() -> None:
    """These four sit in one network but are run by another body.

    Read from table 2 of the annual report. A summary of that table put all of
    them with their network and got every one of these wrong.
    """
    assert REGISTRY["amsBjelave"][6] == FHMZ
    assert REGISTRY["amsTrnovac"][6] == FHMZ
    assert REGISTRY["amsMaglaj"][6] == MIKK
    assert REGISTRY["amsTesanj"][6] == MIKK


def test_industrial_stations_have_no_invented_area_type() -> None:
    """The report classifies these as industrial and says nothing about area."""
    for page in ("amsTetovo", "amsBukinje"):
        assert REGISTRY[page][4] == "industrial"
        assert REGISTRY[page][5] == "unknown"

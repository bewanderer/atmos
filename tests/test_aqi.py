"""Air quality index tests.

These need a live Postgres, so they skip when one is not configured.

The band boundaries below are transcribed from the published table, not from the
database, so the test fails if the stored bands ever drift from the source:

    ETC HE Report 2024/17, Table 5.2, final agreed updated EEA European air
    quality index. Confirmed against airindex.eea.europa.eu on 2026-09-01.

    pollutant  good     fair      moderate   poor       very poor  extremely
    PM2.5      0-5      6-15      16-50      51-90      91-140     >140
    PM10       0-15     16-45     46-120     121-195    196-270    >270
    O3         0-60     61-100    101-120    121-160    161-180    >180
    NO2        0-10     11-25     26-60      61-100     101-150    >150
    SO2        0-20     21-40     41-125     126-190    191-275    >275
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from atmos.connectors.base import ParsedObservation, ParsedStation
from atmos.core import ingest as ing
from tests.conftest import DSN, NOTE

psycopg = pytest.importorskip("psycopg")

pytestmark = pytest.mark.skipif(DSN is None, reason=NOTE)

WHEN = dt.datetime(2026, 4, 7, 9, tzinfo=dt.UTC)

# Upper bound of each band, straight from the published table.
PUBLISHED = {
    "pm25": [5, 15, 50, 90, 140],
    "pm10": [15, 45, 120, 195, 270],
    "o3":   [60, 100, 120, 160, 180],
    "no2":  [10, 25, 60, 100, 150],
    "so2":  [20, 40, 125, 190, 275],
}


@pytest.fixture
def db():
    with psycopg.connect(DSN) as conn:
        yield conn
        conn.rollback()


@pytest.fixture
def station(db):
    """One station to hang readings off."""
    cur = db.cursor()
    cur.execute(
        """insert into sources (slug,name,tier,attribution,is_primary,timezone)
           values ('aqi_src','AQI test','reference','Test',true,'Europe/Sarajevo')
           on conflict (slug) do update set name=excluded.name returning id"""
    )
    source_id = cur.fetchone()[0]
    sid = ing.upsert_station(
        cur, source_id,
        ParsedStation(source_station_id="aqi_stn", name="AQI Station",
                      latitude=43.85, longitude=18.41),
    )
    cur.execute(
        """insert into fetches (source_id,url,requested_at,ok,archive_mode,storage_key)
           values (%s,'http://test',now(),true,'bytes','k') returning id""",
        (source_id,),
    )
    return cur, sid, int(cur.fetchone()[0]), ing.parameter_ids(cur)


def put(cur, sid, fetch_id, params, values: dict[str, str]) -> None:
    obs = [
        ParsedObservation(
            source_station_id="aqi_stn", parameter_code=code,
            phenomenon_start=WHEN, phenomenon_end=WHEN,
            value=Decimal(v), unit="mg/m3" if code == "co" else "ug/m3",
            raw_value=v, raw_unit="mg/m3" if code == "co" else "ug/m3")
        for code, v in values.items()
    ]
    ing.ingest_observations(cur, {"aqi_stn": sid}, params, fetch_id, "t", obs,
                            seen_at=WHEN)


def index(cur, sid):
    cur.execute(
        """select band, driver, driver_value, pollutants_used, missing, complete
             from station_aqi(%s, %s)""",
        (sid, WHEN),
    )
    return cur.fetchone()


def test_the_scale_records_where_its_numbers_came_from(station) -> None:
    """A band nobody can trace is a band nobody should trust."""
    cur, _, _, _ = station
    cur.execute("select code, revision, citation, verified_on from aqi_scales where code='eaqi'")
    row = cur.fetchone()
    assert row is not None
    assert "2024" in row[1]
    assert "Table 5.2" in row[2]
    assert row[3] is not None, "the date the figures were checked"


@pytest.mark.parametrize("code,bounds", PUBLISHED.items())
def test_bands_match_the_published_table(station, code, bounds) -> None:
    """Boundary values sit in the lower band, and just above moves up one."""
    cur, _, _, _ = station
    for i, upper in enumerate(bounds, start=1):
        cur.execute("select aqi_band(%s, %s)", (code, Decimal(str(upper))))
        assert cur.fetchone()[0] == i, f"{code} at {upper} should be band {i}"
        cur.execute("select aqi_band(%s, %s)", (code, Decimal(str(upper)) + Decimal("0.1")))
        assert cur.fetchone()[0] == i + 1, f"{code} just above {upper} should be band {i+1}"
    cur.execute("select aqi_band(%s, %s)", (code, Decimal(str(bounds[-1] * 10))))
    assert cur.fetchone()[0] == 6, "the top band is open ended"


def test_zero_is_the_best_band_not_a_missing_value(station) -> None:
    cur, _, _, _ = station
    for code in PUBLISHED:
        cur.execute("select aqi_band(%s, 0)", (code,))
        assert cur.fetchone()[0] == 1


def test_pollutants_outside_the_scale_have_no_band(station) -> None:
    """CO and H2S are measured but are not EAQI pollutants."""
    cur, _, _, _ = station
    for code, v in (("co", "0.5"), ("h2s", "2.0")):
        cur.execute("select aqi_band(%s, %s)", (code, Decimal(v)))
        assert cur.fetchone()[0] is None


def test_the_worst_pollutant_sets_the_index(station) -> None:
    """Not an average. One pollutant in poor puts the station in poor."""
    cur, sid, fetch_id, params = station
    # PM10 30 is fair, NO2 70 is poor.
    put(cur, sid, fetch_id, params, {"pm10": "30", "pm25": "8", "no2": "70"})
    band, driver, value, used, missing, complete = index(cur, sid)
    assert band == 4
    assert driver == "no2"
    assert value == Decimal("70")


def test_a_partial_index_says_what_it_rests_on(station) -> None:
    """Missing pollutants can only make the true value worse, so the reader has
    to be told the figure is a floor."""
    cur, sid, fetch_id, params = station
    put(cur, sid, fetch_id, params, {"pm10": "30", "pm25": "8"})
    band, driver, value, used, missing, complete = index(cur, sid)
    assert used == 2
    assert complete is False
    assert set(missing) == {"no2", "o3", "so2"}


def test_a_complete_index_says_so(station) -> None:
    cur, sid, fetch_id, params = station
    put(cur, sid, fetch_id, params,
        {"pm10": "30", "pm25": "8", "no2": "20", "o3": "40", "so2": "10"})
    band, driver, value, used, missing, complete = index(cur, sid)
    assert used == 5
    assert complete is True
    assert missing == []


def test_no_particulates_means_no_index(station) -> None:
    """PM drives the index nearly everywhere here. Without it we show the
    measurements and no figure, rather than a weak one."""
    cur, sid, fetch_id, params = station
    put(cur, sid, fetch_id, params, {"no2": "70", "so2": "10", "o3": "40"})
    assert index(cur, sid) is None


def test_pm25_alone_is_enough(station) -> None:
    cur, sid, fetch_id, params = station
    put(cur, sid, fetch_id, params, {"pm25": "60"})
    band, driver, value, used, missing, complete = index(cur, sid)
    assert band == 4 and driver == "pm25"


def test_a_flagged_reading_does_not_drive_the_index(station) -> None:
    """A stuck analyser reading zero would otherwise quietly improve a station's
    index, which is the wrong direction to be wrong in."""
    cur, sid, fetch_id, params = station
    put(cur, sid, fetch_id, params, {"pm10": "30", "no2": "500"})
    before = index(cur, sid)
    assert before[0] == 6 and before[1] == "no2"

    cur.execute(
        """insert into observation_flags
             (station_id, parameter_id, phenomenon_start, phenomenon_end,
              revision, flag, ruleset_version)
           values (%s,%s,%s,%s,1,'out_of_range','test')""",
        (sid, params["no2"], WHEN, WHEN),
    )
    after = index(cur, sid)
    assert after[1] == "pm10", "the flagged reading no longer sets the index"
    assert after[3] == 1


def test_co_and_h2s_never_enter_the_index(station) -> None:
    """They are measured and shown, but the scale does not include them."""
    cur, sid, fetch_id, params = station
    put(cur, sid, fetch_id, params, {"pm10": "10", "co": "9", "h2s": "40"})
    band, driver, value, used, missing, complete = index(cur, sid)
    assert band == 1, "a large CO or H2S value must not move the index"
    assert driver == "pm10"
    assert used == 1

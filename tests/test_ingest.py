"""Ingest and revision ledger tests.

These need a live Postgres, so they skip when one is not configured. Set
ATMOS_TEST_DSN to run them, for example:

    ATMOS_TEST_DSN="host=localhost dbname=atmos user=atmos_ingest password=dev"
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

START = dt.datetime(2026, 1, 15, 10, tzinfo=dt.UTC)
END = START + dt.timedelta(hours=1)


@pytest.fixture
def db():
    with psycopg.connect(DSN) as conn:
        yield conn
        conn.rollback()


@pytest.fixture
def scene(db):
    """A source, a station and a fetch to hang observations off."""
    cur = db.cursor()
    cur.execute(
        """insert into sources (slug,name,tier,attribution,is_primary,timezone)
           values ('t_src','Test','reference','Test',true,'Europe/Sarajevo')
           on conflict (slug) do update set name=excluded.name returning id"""
    )
    source_id = cur.fetchone()[0]
    station_id = ing.upsert_station(
        cur, source_id,
        ParsedStation(source_station_id="t_stn", name="Test Station",
                      latitude=43.85, longitude=18.41,
                      declared_parameters=("pm10",)),
    )
    cur.execute(
        """insert into fetches (source_id,url,requested_at,ok,archive_mode,storage_key)
           values (%s,'http://test',now(),true,'bytes','k') returning id""",
        (source_id,),
    )
    fetch_id = cur.fetchone()[0]
    return cur, {"t_stn": station_id}, ing.parameter_ids(cur), fetch_id, station_id


def reading(value: str) -> ParsedObservation:
    return ParsedObservation(
        source_station_id="t_stn", parameter_code="pm10",
        phenomenon_start=START, phenomenon_end=END,
        value=Decimal(value), unit="ug/m3", raw_value=value, raw_unit="ug/m3",
    )


def history(cur, station_id, param_id):
    cur.execute(
        """select revision, value, previous_value, revision_kind, confirmations
             from observations
            where station_id=%s and parameter_id=%s and phenomenon_start=%s
            order by revision""",
        (station_id, param_id, START),
    )
    return cur.fetchall()


def test_first_reading_is_revision_one(scene) -> None:
    cur, stations, params, fetch_id, station_id = scene
    r = ing.ingest_observations(cur, stations, params, fetch_id, "t", [reading("10.0")])
    assert (r.inserted, r.confirmed, r.revisions) == (1, 0, 0)
    rows = history(cur, station_id, params["pm10"])
    assert rows[0][0] == 1
    assert rows[0][3] is None  # an original, not a change


def test_unchanged_value_confirms_without_new_row(scene) -> None:
    cur, stations, params, fetch_id, station_id = scene
    for _ in range(3):
        ing.ingest_observations(cur, stations, params, fetch_id, "t", [reading("10.0")])
    rows = history(cur, station_id, params["pm10"])
    assert len(rows) == 1, "re-observing must not duplicate rows"
    assert rows[0][4] == 3  # confirmations


def test_changed_value_appends_a_revision(scene) -> None:
    cur, stations, params, fetch_id, station_id = scene
    ing.ingest_observations(cur, stations, params, fetch_id, "t", [reading("10.0")])
    ing.ingest_observations(cur, stations, params, fetch_id, "t", [reading("20.0")])
    rows = history(cur, station_id, params["pm10"])
    assert [r[0] for r in rows] == [1, 2]
    assert rows[0][1] == Decimal("10.0"), "the original must not move"
    assert rows[1][1] == Decimal("20.0")
    assert rows[1][2] == Decimal("10.0")  # previous_value
    assert rows[1][3] == "value_change"


def test_repeated_changes_chain_without_limit(scene) -> None:
    """A source may amend as many times as it likes."""
    cur, stations, params, fetch_id, station_id = scene
    for v in ("10.0", "20.0", "30.0", "40.0", "50.0"):
        ing.ingest_observations(cur, stations, params, fetch_id, "t", [reading(v)])
    rows = history(cur, station_id, params["pm10"])
    assert [r[0] for r in rows] == [1, 2, 3, 4, 5]
    assert [str(r[1]) for r in rows] == ["10.0", "20.0", "30.0", "40.0", "50.0"]
    assert rows[0][1] == Decimal("10.0")


def test_reverting_to_an_earlier_value_is_recorded_not_swallowed(scene) -> None:
    """The source changing its mind back is itself information."""
    cur, stations, params, fetch_id, station_id = scene
    ing.ingest_observations(cur, stations, params, fetch_id, "t", [reading("10.0")])
    ing.ingest_observations(cur, stations, params, fetch_id, "t", [reading("20.0")])
    ing.ingest_observations(cur, stations, params, fetch_id, "t", [reading("10.0")])
    rows = history(cur, station_id, params["pm10"])
    assert [r[0] for r in rows] == [1, 2, 3]
    assert rows[2][1] == Decimal("10.0")
    assert rows[2][3] == "value_change"


def test_withdrawal_then_return_is_a_reinstatement(scene) -> None:
    cur, stations, params, fetch_id, station_id = scene
    ing.ingest_observations(cur, stations, params, fetch_id, "t", [reading("10.0")])
    ing.withdraw_missing(cur, station_id, params["pm10"], START,
                         END + dt.timedelta(hours=1), set(), fetch_id, "t")
    ing.ingest_observations(cur, stations, params, fetch_id, "t", [reading("15.0")])

    rows = history(cur, station_id, params["pm10"])
    kinds = [r[3] for r in rows]
    assert kinds == [None, "withdrawal", "reinstatement"]
    assert rows[1][1] is None, "a withdrawal stores no value"
    assert rows[1][2] == Decimal("10.0"), "but records what was withdrawn"


def test_full_sequence_keeps_revision_one_canonical(scene) -> None:
    """Change, change, remove, return, revert. The public number never moves."""
    cur, stations, params, fetch_id, station_id = scene
    ing.ingest_observations(cur, stations, params, fetch_id, "t", [reading("10.0")])
    ing.ingest_observations(cur, stations, params, fetch_id, "t", [reading("20.0")])
    ing.ingest_observations(cur, stations, params, fetch_id, "t", [reading("30.0")])
    ing.withdraw_missing(cur, station_id, params["pm10"], START,
                         END + dt.timedelta(hours=1), set(), fetch_id, "t")
    ing.ingest_observations(cur, stations, params, fetch_id, "t", [reading("15.0")])
    ing.ingest_observations(cur, stations, params, fetch_id, "t", [reading("10.0")])

    rows = history(cur, station_id, params["pm10"])
    assert [r[3] for r in rows] == [
        None, "value_change", "value_change", "withdrawal",
        "reinstatement", "value_change",
    ]
    assert rows[0][1] == Decimal("10.0")


def test_observations_cannot_be_updated_or_deleted(scene) -> None:
    """The integrity guarantee is enforced by the database, not by this code."""
    cur, stations, params, fetch_id, station_id = scene
    ing.ingest_observations(cur, stations, params, fetch_id, "t", [reading("10.0")])
    for statement in ("update observations set value = 999",
                      "delete from observations",
                      "truncate observations"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute(statement)
        cur.connection.rollback()


def test_units_are_converted_on_the_way_in(scene) -> None:
    """FHMZ publishes CO in ug/m3, canonical is mg/m3."""
    cur, stations, params, fetch_id, station_id = scene
    o = ParsedObservation(
        source_station_id="t_stn", parameter_code="co",
        phenomenon_start=START, phenomenon_end=END,
        value=Decimal("1051"), unit="ug/m3", raw_value="1051", raw_unit="ug/m3",
    )
    ing.ingest_observations(cur, stations, params, fetch_id, "t", [o])
    cur.execute(
        """select value, unit, raw_value, raw_unit from observations
             where station_id=%s and parameter_id=%s""",
        (station_id, params["co"]),
    )
    value, unit, raw_value, raw_unit = cur.fetchone()
    assert value == Decimal("1.051")
    assert unit == "mg/m3"
    assert raw_value == "1051", "what the source published must survive"
    assert raw_unit == "ug/m3"


def test_two_sources_with_different_units_agree_after_conversion(scene) -> None:
    """The 1000x error this exists to prevent."""
    cur, stations, params, fetch_id, station_id = scene
    micro = ParsedObservation(
        source_station_id="t_stn", parameter_code="co",
        phenomenon_start=START, phenomenon_end=END,
        value=Decimal("1051"), unit="ug/m3", raw_value="1051", raw_unit="ug/m3")
    milli = ParsedObservation(
        source_station_id="t_stn", parameter_code="co",
        phenomenon_start=START, phenomenon_end=END,
        value=Decimal("1.051"), unit="mg/m3", raw_value="1.051", raw_unit="mg/m3")

    r1 = ing.ingest_observations(cur, stations, params, fetch_id, "t", [micro])
    r2 = ing.ingest_observations(cur, stations, params, fetch_id, "t", [milli])
    assert r1.inserted == 1
    assert r2.confirmed == 1, "the same reading in another unit is not a revision"
    assert r2.revisions == 0


def test_unconvertible_unit_is_skipped_not_stored(scene) -> None:
    cur, stations, params, fetch_id, station_id = scene
    o = ParsedObservation(
        source_station_id="t_stn", parameter_code="pm10",
        phenomenon_start=START, phenomenon_end=END,
        value=Decimal("5"), unit="furlongs", raw_value="5", raw_unit="furlongs")
    r = ing.ingest_observations(cur, stations, params, fetch_id, "t", [o])
    assert r.inserted == 0
    assert r.skipped


def test_withdraw_absent_needs_a_window_not_one_reading(scene) -> None:
    """A single reading is not a window, so absence proves nothing."""
    cur, stations, params, fetch_id, station_id = scene
    ing.ingest_observations(cur, stations, params, fetch_id, "t", [reading("10.0")])
    n = ing.withdraw_absent(cur, stations, params, fetch_id, "t", [reading("10.0")])
    assert n == 0


def test_withdraw_absent_detects_a_dropped_hour(scene) -> None:
    cur, stations, params, fetch_id, station_id = scene

    def at(hour: int, value: str) -> ParsedObservation:
        s = START.replace(hour=hour)
        return ParsedObservation(
            source_station_id="t_stn", parameter_code="pm10",
            phenomenon_start=s, phenomenon_end=s + dt.timedelta(hours=1),
            value=Decimal(value), unit="ug/m3", raw_value=value, raw_unit="ug/m3")

    full = [at(10, "10"), at(11, "11"), at(12, "12")]
    ing.ingest_observations(cur, stations, params, fetch_id, "t", full)

    reduced = [full[0], full[2]]  # the middle hour vanishes
    ing.ingest_observations(cur, stations, params, fetch_id, "t", reduced)
    n = ing.withdraw_absent(cur, stations, params, fetch_id, "t", reduced)
    assert n == 1

    cur.execute(
        """select revision, value, previous_value, revision_kind from observations
             where station_id=%s and parameter_id=%s and phenomenon_start=%s
             order by revision""",
        (station_id, params["pm10"], START.replace(hour=11)),
    )
    rows = cur.fetchall()
    assert [r[3] for r in rows] == [None, "withdrawal"]
    assert rows[1][2] == Decimal("11")


def test_withdraw_absent_does_nothing_when_nothing_changed(scene) -> None:
    cur, stations, params, fetch_id, station_id = scene

    def at(hour: int) -> ParsedObservation:
        s = START.replace(hour=hour)
        return ParsedObservation(
            source_station_id="t_stn", parameter_code="pm10",
            phenomenon_start=s, phenomenon_end=s + dt.timedelta(hours=1),
            value=Decimal("5"), unit="ug/m3", raw_value="5", raw_unit="ug/m3")

    batch = [at(10), at(11), at(12)]
    ing.ingest_observations(cur, stations, params, fetch_id, "t", batch)
    assert ing.withdraw_absent(cur, stations, params, fetch_id, "t", batch) == 0


def test_range_flags_catch_impossible_values_and_exclude_them(scene) -> None:
    """A community sensor reporting -144 C must not reach consensus."""
    cur, stations, params, fetch_id, station_id = scene
    broken = ParsedObservation(
        source_station_id="t_stn", parameter_code="temp",
        phenomenon_start=START, phenomenon_end=END,
        value=Decimal("-143.16"), unit="degC", raw_value="-143.16", raw_unit="degC")
    fine = ParsedObservation(
        source_station_id="t_stn", parameter_code="temp",
        phenomenon_start=START.replace(hour=11),
        phenomenon_end=END.replace(hour=12),
        value=Decimal("21.5"), unit="degC", raw_value="21.5", raw_unit="degC")

    ing.ingest_observations(cur, stations, params, fetch_id, "t", [broken, fine])
    cur.execute("select refresh_station_status()")
    cur.execute("select apply_range_flags()")
    assert cur.fetchone()[0] >= 1

    cur.execute(
        """select flag from observation_flags
             where station_id=%s and parameter_id=%s and phenomenon_start=%s""",
        (station_id, params["temp"], START),
    )
    flags = {r[0] for r in cur.fetchall()}
    assert "out_of_range" in flags
    assert "negative" not in flags, "temperature below zero is weather, not a fault"

    cur.execute(
        """select count(*) from consensus_eligible
             where station_id=%s and parameter_id=%s and value < -60""",
        (station_id, params["temp"]),
    )
    assert cur.fetchone()[0] == 0


def test_negative_concentration_is_flagged_as_negative(scene) -> None:
    cur, stations, params, fetch_id, station_id = scene
    o = ParsedObservation(
        source_station_id="t_stn", parameter_code="pm10",
        phenomenon_start=START, phenomenon_end=END,
        value=Decimal("-5"), unit="ug/m3", raw_value="-5", raw_unit="ug/m3")
    ing.ingest_observations(cur, stations, params, fetch_id, "t", [o])
    cur.execute("select apply_range_flags()")
    cur.execute(
        """select flag from observation_flags
             where station_id=%s and parameter_id=%s""",
        (station_id, params["pm10"]),
    )
    assert {r[0] for r in cur.fetchall()} == {"negative"}


def test_station_declaring_a_parameter_it_never_sends_is_never_reported(scene) -> None:
    """Vares renders PM tables with every cell empty. That is not the same as absent."""
    cur, stations, params, fetch_id, station_id = scene
    cur.execute("select source_id from stations where id=%s", (station_id,))
    source_id = cur.fetchone()[0]
    ing.upsert_station(
        cur, source_id,
        ParsedStation(source_station_id="t_stn", name="Test Station",
                      declared_parameters=("pm10", "so2")),
    )
    cur.execute("select refresh_station_status()")
    cur.execute(
        """select status from station_status
             where station_id=%s and parameter_id=%s""",
        (station_id, params["so2"]),
    )
    row = cur.fetchone()
    assert row and row[0] == "never_reported"

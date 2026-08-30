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


def test_a_conflicting_insert_costs_one_row_not_the_run(scene) -> None:
    """Two ingests can race. Losing the whole run over one row is unacceptable
    on an eighty thousand row backfill."""
    cur, stations, params, fetch_id, station_id = scene

    # Simulate the race: another writer got there between our read and write.
    ing.ingest_observations(cur, stations, params, fetch_id, "t", [reading("10.0")])

    # A second attempt at the identical reading must confirm, never raise.
    r = ing.ingest_observations(cur, stations, params, fetch_id, "t", [reading("10.0")])
    assert r.confirmed == 1
    assert r.inserted == 0

    # And the run continues: later readings in the same batch still land.
    later = ParsedObservation(
        source_station_id="t_stn", parameter_code="pm10",
        phenomenon_start=START + dt.timedelta(hours=2),
        phenomenon_end=END + dt.timedelta(hours=2),
        value=Decimal("11.0"), unit="ug/m3", raw_value="11.0", raw_unit="ug/m3")
    r2 = ing.ingest_observations(cur, stations, params, fetch_id, "t",
                                 [reading("10.0"), later])
    assert r2.inserted == 1
    assert r2.confirmed == 1


def test_concurrent_writers_leave_exactly_one_row(scene) -> None:
    """Integrity under concurrency is the database's job, not the code's.

    This test has to commit, and observations cannot be deleted, so it uses its
    own station and a timestamp unique to the run. Otherwise its rows would
    outlive it and collide with every other test.
    """
    import threading
    import uuid

    cur, _stations, params, fetch_id, _station_id = scene
    cur.execute("select source_id from stations where id=%s", (_station_id,))
    source_id = cur.fetchone()[0]

    tag = uuid.uuid4().hex[:8]
    own_id = ing.upsert_station(
        cur, source_id,
        ParsedStation(source_station_id=f"race_{tag}", name="Race Station",
                      declared_parameters=("pm10",)),
    )
    # Far from any other test's window, and unique per run.
    when = dt.datetime(2019, 1, 1, tzinfo=dt.UTC) + dt.timedelta(
        seconds=int(uuid.uuid4().int % 1_000_000)
    )
    cur.connection.commit()

    def one() -> ParsedObservation:
        return ParsedObservation(
            source_station_id=f"race_{tag}", parameter_code="pm10",
            phenomenon_start=when, phenomenon_end=when + dt.timedelta(hours=1),
            value=Decimal("77.0"), unit="ug/m3", raw_value="77.0", raw_unit="ug/m3")

    errors: list[str] = []

    def worker() -> None:
        try:
            with psycopg.connect(DSN) as conn:
                c = conn.cursor()
                ing.ingest_observations(c, {f"race_{tag}": own_id},
                                        ing.parameter_ids(c), fetch_id, "t", [one()])
                conn.commit()
        except Exception as e:  # noqa: BLE001
            errors.append(f"{type(e).__name__}: {e}")

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"a writer lost its run: {errors}"
    cur.connection.commit()
    cur.execute(
        """select count(*) from observations
             where station_id=%s and phenomenon_start=%s""",
        (own_id, when),
    )
    assert cur.fetchone()[0] == 1


def test_confirmations_are_batched_not_one_per_row(scene) -> None:
    """Re-observing is the common case. One round trip per row made re-ingest
    seven times slower than a fresh load."""
    cur, stations, params, fetch_id, station_id = scene

    batch = []
    for i in range(50):
        s = START + dt.timedelta(minutes=i)
        batch.append(ParsedObservation(
            source_station_id="t_stn", parameter_code="pm10",
            phenomenon_start=s, phenomenon_end=s,
            value=Decimal("5.0"), unit="ug/m3", raw_value="5.0", raw_unit="ug/m3"))

    first = ing.ingest_observations(cur, stations, params, fetch_id, "t", batch)
    assert first.inserted == 50

    second = ing.ingest_observations(cur, stations, params, fetch_id, "t", batch)
    assert second.confirmed == 50
    assert second.inserted == 0

    cur.execute(
        """select min(confirmations), max(confirmations) from observations
             where station_id=%s and parameter_id=%s""",
        (station_id, params["pm10"]),
    )
    lo, hi = cur.fetchone()
    assert lo == hi == 2, "every row must be confirmed exactly once"


def test_batched_confirm_still_cannot_alter_a_value(scene) -> None:
    """The batch path has the same guarantee as the single row one."""
    cur, stations, params, fetch_id, station_id = scene
    ing.ingest_observations(cur, stations, params, fetch_id, "t", [reading("10.0")])
    ing.ingest_observations(cur, stations, params, fetch_id, "t", [reading("10.0")])

    rows = history(cur, station_id, params["pm10"])
    assert len(rows) == 1
    assert rows[0][1] == Decimal("10.0"), "the value must be untouched"
    assert rows[0][4] == 2, "only the counter moved"


def test_a_stuck_sensor_is_flagged(scene) -> None:
    """A repeated value is a stuck instrument, not twenty measurements."""
    cur, stations, params, fetch_id, station_id = scene

    def at(hour: int, value: str) -> ParsedObservation:
        s = START + dt.timedelta(hours=hour)
        return ParsedObservation(
            source_station_id="t_stn", parameter_code="pm10",
            phenomenon_start=s, phenomenon_end=s + dt.timedelta(hours=1),
            value=Decimal(value), unit="ug/m3", raw_value=value, raw_unit="ug/m3")

    stuck = [at(i, "12.5") for i in range(20)]
    varying = [at(30 + i, str(10 + i)) for i in range(20)]
    ing.ingest_observations(cur, stations, params, fetch_id, "t", stuck + varying)

    cur.execute("select apply_sequence_flags(%s)", (START - dt.timedelta(days=1),))
    assert cur.fetchone()[0] >= 20

    cur.execute(
        """select count(*) from observation_flags
             where station_id=%s and flag='flatline'""", (station_id,))
    assert cur.fetchone()[0] == 20

    # The varying readings must be untouched.
    cur.execute(
        """select count(*) from observation_flags f
             join observations o
               on o.station_id=f.station_id and o.phenomenon_start=f.phenomenon_start
            where f.station_id=%s and o.value > 20""", (station_id,))
    assert cur.fetchone()[0] == 0


def test_a_run_of_zeros_is_flagged_separately(scene) -> None:
    """One zero is often a real below-limit reading. Twenty is a dead sensor."""
    cur, stations, params, fetch_id, station_id = scene
    batch = []
    for i in range(20):
        s = START + dt.timedelta(hours=i)
        batch.append(ParsedObservation(
            source_station_id="t_stn", parameter_code="pm10",
            phenomenon_start=s, phenomenon_end=s + dt.timedelta(hours=1),
            value=Decimal("0"), unit="ug/m3", raw_value="0", raw_unit="ug/m3"))
    ing.ingest_observations(cur, stations, params, fetch_id, "t", batch)
    cur.execute("select apply_sequence_flags(%s)", (START - dt.timedelta(days=1),))

    cur.execute(
        """select flag, count(*) from observation_flags
            where station_id=%s group by flag""", (station_id,))
    flags = dict(cur.fetchall())
    assert flags.get("zero_run") == 20
    assert "flatline" not in flags, "zeros are their own case"


def test_a_short_run_is_not_flagged(scene) -> None:
    """Below the threshold is ordinary steady air, not a fault."""
    cur, stations, params, fetch_id, station_id = scene
    batch = []
    for i in range(5):
        s = START + dt.timedelta(hours=i)
        batch.append(ParsedObservation(
            source_station_id="t_stn", parameter_code="pm10",
            phenomenon_start=s, phenomenon_end=s + dt.timedelta(hours=1),
            value=Decimal("9.9"), unit="ug/m3", raw_value="9.9", raw_unit="ug/m3"))
    ing.ingest_observations(cur, stations, params, fetch_id, "t", batch)
    cur.execute("select apply_sequence_flags(%s)", (START - dt.timedelta(days=1),))
    cur.execute("select count(*) from observation_flags where station_id=%s", (station_id,))
    assert cur.fetchone()[0] == 0


def test_reprocess_recovers_missed_readings_without_counting_confirmations(
    scene,
) -> None:
    """What a parser fix looks like: the old bytes, read better.

    The reading the parser used to miss lands. The one already held keeps its
    confirmation count, because reading the same page twice is not the source
    publishing it twice.
    """
    cur, stations, params, fetch_id, station_id = scene
    later = START + dt.timedelta(hours=1)

    def at(start, value):
        return ParsedObservation(
            source_station_id="t_stn", parameter_code="pm10",
            phenomenon_start=start, phenomenon_end=start,
            value=Decimal(value), unit="ug/m3", raw_value=value, raw_unit="ug/m3")

    # First pass, before the fix: one of the two readings was not parsed.
    ing.ingest_observations(cur, stations, params, fetch_id, "t", [at(START, "10.0")])

    # Same bytes, fixed parser.
    r = ing.ingest_observations(cur, stations, params, fetch_id, "t",
                                [at(START, "10.0"), at(later, "11.0")],
                                count_confirmations=False)
    assert r.inserted == 1
    assert r.confirmed == 0
    assert r.revisions == 0

    cur.execute(
        """select phenomenon_start, value, revision, confirmations
             from observations where station_id=%s and parameter_id=%s
            order by phenomenon_start""",
        (station_id, params["pm10"]),
    )
    rows = cur.fetchall()
    assert [r[1] for r in rows] == [Decimal("10.0"), Decimal("11.0")]
    assert all(r[2] == 1 for r in rows), "no revision should have been appended"
    assert rows[0][3] == 1, "already held, so its count must not move"


def test_reprocess_still_records_a_genuine_value_change(scene) -> None:
    """Not counting confirmations must not mean ignoring changed values."""
    cur, stations, params, fetch_id, station_id = scene
    ing.ingest_observations(cur, stations, params, fetch_id, "t", [reading("10.0")])
    r = ing.ingest_observations(cur, stations, params, fetch_id, "t",
                                [reading("12.0")], count_confirmations=False)
    assert r.revisions == 1
    rows = history(cur, station_id, params["pm10"])
    assert [x[1] for x in rows] == [Decimal("10.0"), Decimal("12.0")]
    assert rows[1][3] == "value_change"


def test_rounded_coordinates_are_recorded_as_imprecise(scene) -> None:
    """Sensor.Community rounds positions for privacy. Distance matching has to
    know, or it treats a kilometre of slack as a surveyed location."""
    cur, stations, params, fetch_id, station_id = scene
    cur.execute("select source_id from stations where id=%s", (station_id,))
    source_id = cur.fetchone()[0]

    rough = ing.upsert_station(
        cur, source_id,
        ParsedStation(source_station_id="t_rough", name="Rounded",
                      latitude=43.8, longitude=18.4, location_precise=False),
    )
    cur.execute("select location_precise from stations where id=%s", (rough,))
    assert cur.fetchone()[0] is False

    # And the default stays true for a surveyed site.
    cur.execute("select location_precise from stations where id=%s", (station_id,))
    assert cur.fetchone()[0] is True


def _dup(value: str, start=None) -> ParsedObservation:
    s = start or START
    return ParsedObservation(
        source_station_id="t_stn", parameter_code="pm10",
        phenomenon_start=s, phenomenon_end=s,
        value=Decimal(value), unit="ug/m3", raw_value=value, raw_unit="ug/m3")


def test_a_payload_disagreeing_with_itself_is_not_a_revision(scene) -> None:
    """One payload, one reading, two values. Sensor.Community's archive does
    this. Nothing changed between publications, so nothing may be recorded as
    a change, however many times the archive is re-read."""
    cur, stations, params, fetch_id, station_id = scene

    r = ing.ingest_observations(cur, stations, params, fetch_id, "t",
                                [_dup("10.0"), _dup("12.0")])
    assert r.inserted == 1
    assert r.duplicates == 1

    rows = history(cur, station_id, params["pm10"])
    assert len(rows) == 1, "a second row here would be an invented revision"
    assert rows[0][1] == Decimal("10.0"), "first value published wins"

    # The bug was that re-reading the same bytes appended a revision each time.
    for _ in range(3):
        again = ing.ingest_observations(cur, stations, params, fetch_id, "t",
                                        [_dup("10.0"), _dup("12.0")])
        assert again.revisions == 0
    assert len(history(cur, station_id, params["pm10"])) == 1


def test_the_kept_reading_says_its_payload_disagreed(scene) -> None:
    """Keeping one value quietly would hide that the source gave two."""
    cur, stations, params, fetch_id, station_id = scene
    ing.ingest_observations(cur, stations, params, fetch_id, "t",
                            [_dup("10.0"), _dup("12.0")])
    cur.execute(
        """select quality_flags from observations
            where station_id=%s and parameter_id=%s and phenomenon_start=%s""",
        (station_id, params["pm10"], START),
    )
    assert "source_duplicate" in cur.fetchone()[0]


def test_an_identical_duplicate_is_not_flagged(scene) -> None:
    """Repeating the same value says nothing is wrong. Only disagreement does."""
    cur, stations, params, fetch_id, station_id = scene
    r = ing.ingest_observations(cur, stations, params, fetch_id, "t",
                                [_dup("10.0"), _dup("10.0")])
    assert r.duplicates == 1
    cur.execute(
        """select quality_flags from observations
            where station_id=%s and parameter_id=%s and phenomenon_start=%s""",
        (station_id, params["pm10"], START),
    )
    assert cur.fetchone()[0] == []


def test_a_not_a_number_reading_is_refused(scene) -> None:
    """NaN never equals itself, so one stored row would append a revision on
    every ingest, forever. It is also not a measurement."""
    cur, stations, params, fetch_id, station_id = scene
    r = ing.ingest_observations(cur, stations, params, fetch_id, "t",
                                [_dup("NaN")])
    assert r.inserted == 0
    assert any("NaN" in s for s in r.skipped)
    assert history(cur, station_id, params["pm10"]) == []

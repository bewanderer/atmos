"""Consensus and divergence tests.

These need a live Postgres, so they skip when one is not configured.

Everything here builds its own stations at known positions and asserts against
hand-computed statistics. The point is not that the SQL runs, it is that the
numbers are the ones a person working them out on paper would get, and that the
degenerate cases say what they cannot support rather than producing a figure
that reads as a finding.
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

# Its own hour, so nothing here collides with the other database tests.
WHEN = dt.datetime(2026, 3, 11, 5, tzinfo=dt.UTC)
LATER = WHEN + dt.timedelta(hours=1)

# Three stations roughly a kilometre apart, and one far enough to be excluded.
NEAR = [(43.8500, 18.4100), (43.8560, 18.4100), (43.8620, 18.4100)]
FAR = (44.5400, 18.6700)


@pytest.fixture
def db():
    with psycopg.connect(DSN) as conn:
        yield conn
        conn.rollback()


@pytest.fixture
def world(db):
    """Sources and stations for one comparable set, plus a distant station."""
    cur = db.cursor()

    def source(slug: str) -> int:
        cur.execute(
            """insert into sources (slug,name,tier,attribution,is_primary,timezone)
               values (%s,%s,'reference','Test',true,'Europe/Sarajevo')
               on conflict (slug) do update set name=excluded.name returning id""",
            (slug, f"Test {slug}"),
        )
        return int(cur.fetchone()[0])

    a, b = source("c_src_a"), source("c_src_b")

    def station(source_id: int, sid: str, lat: float, lon: float,
                precise: bool = True) -> int:
        return ing.upsert_station(
            cur, source_id,
            ParsedStation(source_station_id=sid, name=f"Station {sid}",
                          latitude=lat, longitude=lon,
                          location_precise=precise,
                          declared_parameters=("pm10",)),
        )

    ids = {
        "s1": station(a, "c_s1", *NEAR[0]),
        "s2": station(a, "c_s2", *NEAR[1]),
        "s3": station(a, "c_s3", *NEAR[2]),
        "far": station(a, "c_far", *FAR),
    }
    # Same instrument as s1, published by the other source.
    ids["s1_copy"] = station(b, "c_s1_copy", *NEAR[0])
    # No position at all, like a mobile unit we cannot place.
    ids["nowhere"] = ing.upsert_station(
        cur, a,
        ParsedStation(source_station_id="c_nowhere", name="Station nowhere",
                      declared_parameters=("pm10",)),
    )

    cur.execute(
        """insert into fetches (source_id,url,requested_at,ok,archive_mode,storage_key)
           values (%s,'http://test',now(),true,'bytes','k') returning id""",
        (a,),
    )
    fetch_id = int(cur.fetchone()[0])
    params = ing.parameter_ids(cur)

    # consensus_eligible only takes reporting stations. Set that directly rather
    # than running the refresh over the whole table.
    for station_id in ids.values():
        cur.execute(
            """insert into station_status
                 (station_id, parameter_id, status, last_observation_at)
               values (%s,%s,'active',%s)
               on conflict (station_id, parameter_id)
               do update set status='active'""",
            (station_id, params["pm10"], WHEN),
        )
    return cur, ids, params, fetch_id


def put(cur, ids, params, fetch_id, values: dict[str, str], when=WHEN) -> None:
    """Record one pm10 reading per named station."""
    obs = [
        ParsedObservation(
            source_station_id=name_to_sid(name), parameter_code="pm10",
            phenomenon_start=when, phenomenon_end=when,
            value=Decimal(v), unit="ug/m3", raw_value=v, raw_unit="ug/m3")
        for name, v in values.items()
    ]
    station_ids = {name_to_sid(n): ids[n] for n in values}
    ing.ingest_observations(cur, station_ids, params, fetch_id, "t", obs,
                            seen_at=when)


def name_to_sid(name: str) -> str:
    return {"s1": "c_s1", "s2": "c_s2", "s3": "c_s3", "far": "c_far",
            "s1_copy": "c_s1_copy", "nowhere": "c_nowhere"}[name]


def sets(cur, radius=5000, sources=None, when=WHEN):
    cur.execute(
        """select anchor_station_id, n, median, mad, mean, min_value, max_value,
                  q1, q3, basis
             from consensus('pm10', %s, %s, %s, %s)""",
        (when, when + dt.timedelta(hours=1), radius, sources),
    )
    return {r[0]: r[1:] for r in cur.fetchall()}


def diverge(cur, radius=5000, sources=None, when=WHEN):
    cur.execute(
        """select station_id, value, n, median, mad, deviation, modified_z,
                  proportional, basis
             from divergence('pm10', %s, %s, %s, %s)""",
        (when, when + dt.timedelta(hours=1), radius, sources),
    )
    return {r[0]: r[1:] for r in cur.fetchall()}


def test_median_and_mad_are_what_you_get_on_paper(world) -> None:
    """Values 10, 40, 46. Median 40. Deviations 30, 0, 6, so MAD is 6."""
    cur, ids, params, fetch_id = world
    put(cur, ids, params, fetch_id, {"s1": "10", "s2": "40", "s3": "46"})

    row = sets(cur)[ids["s2"]]
    n, median, mad, mean, lo, hi, q1, q3, basis = row
    assert n == 3
    assert median == Decimal("40")
    assert mad == Decimal("6")
    assert lo == Decimal("10")
    assert hi == Decimal("46")
    assert mean == Decimal("32")
    assert basis == "robust"


def test_the_mean_would_have_hidden_the_outlier(world) -> None:
    """The reason the median drives detection and the mean never does."""
    cur, ids, params, fetch_id = world
    put(cur, ids, params, fetch_id, {"s1": "10", "s2": "40", "s3": "46"})
    n, median, mad, mean, *_ = sets(cur)[ids["s2"]]
    # The low reading drags the mean 8 down and partly conceals itself. The
    # median does not move at all.
    assert mean == Decimal("32")
    assert median == Decimal("40")


def test_modified_z_flags_the_outlier_and_spares_the_rest(world) -> None:
    """0.6745 * (value - median) / MAD, threshold 3.5."""
    cur, ids, params, fetch_id = world
    put(cur, ids, params, fetch_id, {"s1": "10", "s2": "40", "s3": "46"})
    d = diverge(cur)

    # 0.6745 * (10 - 40) / 6 = -3.3725
    assert d[ids["s1"]][5] == Decimal("-3.3725")
    assert d[ids["s2"]][5] == Decimal("0.0000")
    # 0.6745 * (46 - 40) / 6 = 0.6745
    assert d[ids["s3"]][5] == Decimal("0.6745")


def test_one_reading_is_unconfirmed_not_agreed(world) -> None:
    """n = 1 has no consensus. It must not read as a station agreeing with
    anything, and MAD must not print as zero spread."""
    cur, ids, params, fetch_id = world
    put(cur, ids, params, fetch_id, {"far": "33"})

    n, median, mad, mean, lo, hi, q1, q3, basis = sets(cur)[ids["far"]]
    assert n == 1
    assert median == Decimal("33")
    assert mad is None
    assert basis == "unconfirmed"
    assert diverge(cur)[ids["far"]][5] is None, "no z-score from one value"


def test_two_readings_measure_a_difference_but_cannot_attribute_it(world) -> None:
    """We can say they differ by 20. We cannot say which one is wrong."""
    cur, ids, params, fetch_id = world
    put(cur, ids, params, fetch_id, {"s1": "30", "s2": "50"})

    n, median, mad, *rest = sets(cur)[ids["s1"]]
    assert n == 2
    assert median == Decimal("40")
    assert rest[-1] == "two_sources"

    d = diverge(cur)
    assert d[ids["s1"]][5] is None, "attribution is not available at n=2"
    assert d[ids["s2"]][5] is None
    assert d[ids["s1"]][4] == Decimal("-10"), "the difference is still reported"


def test_exact_agreement_reports_a_proportion_not_an_undefined_z(world) -> None:
    """MAD of zero makes the z-score a division by zero."""
    cur, ids, params, fetch_id = world
    put(cur, ids, params, fetch_id, {"s1": "20", "s2": "20", "s3": "20"})

    n, median, mad, *rest = sets(cur)[ids["s1"]]
    assert n == 3
    assert mad == Decimal("0")
    assert rest[-1] == "exact_agreement"

    d = diverge(cur)[ids["s1"]]
    assert d[5] is None, "undefined, so not reported"
    assert d[6] == Decimal("0.0000"), "proportional deviation instead"


def test_a_distant_station_is_not_in_the_set(world) -> None:
    """Comparable means near. The far station is its own set of one."""
    cur, ids, params, fetch_id = world
    put(cur, ids, params, fetch_id,
        {"s1": "10", "s2": "40", "s3": "46", "far": "999"})

    s = sets(cur)
    assert s[ids["s1"]][0] == 3, "the far station must not join this set"
    assert s[ids["far"]][0] == 1
    assert s[ids["s1"]][1] == Decimal("40"), "and must not move the median"


def test_a_tighter_radius_shrinks_the_set(world) -> None:
    """Radius is a parameter, so a reader can see how the answer depends on it."""
    cur, ids, params, fetch_id = world
    put(cur, ids, params, fetch_id, {"s1": "10", "s2": "40", "s3": "46"})

    assert sets(cur, radius=5000)[ids["s1"]][0] == 3
    # s1 to s3 is about 1.3 km, s1 to s2 about 660 m.
    assert sets(cur, radius=1000)[ids["s1"]][0] == 2


def test_a_station_with_no_position_still_reports(world) -> None:
    """It cannot join a set, but it must not vanish from the record either."""
    cur, ids, params, fetch_id = world
    put(cur, ids, params, fetch_id, {"nowhere": "27"})

    row = sets(cur)[ids["nowhere"]]
    assert row[0] == 1
    assert row[-1] == "unconfirmed"
    assert diverge(cur)[ids["nowhere"]][0] == Decimal("27")


def test_excluding_a_source_recomputes_everything(world) -> None:
    """Source selection is the reader's lever, so the answer has to move."""
    cur, ids, params, fetch_id = world
    put(cur, ids, params, fetch_id, {"s1": "10", "s2": "40", "s3": "46"})

    both = sets(cur)[ids["s2"]]
    assert both[0] == 3 and both[1] == Decimal("40")

    only_b = sets(cur, sources=["c_src_b"])
    assert ids["s2"] not in only_b, "excluded entirely, not silently kept"


def test_one_instrument_published_twice_votes_once(world) -> None:
    """Two publishers carrying one machine must not look like two sources
    agreeing. That is the false confidence this project exists to expose."""
    cur, ids, params, fetch_id = world
    cur.execute(
        """insert into station_identity
             (station_id, canonical_station_id, valid_from, valid_to, evidence,
              readings_compared, readings_identical, status, confirmed_by,
              confirmed_at)
           values (%s,%s,%s,null,'test',10,10,'confirmed','test',now())""",
        (ids["s1_copy"], ids["s1"], WHEN - dt.timedelta(days=1)),
    )
    put(cur, ids, params, fetch_id,
        {"s1": "10", "s1_copy": "10", "s2": "40", "s3": "46"})

    row = sets(cur)[ids["s2"]]
    assert row[0] == 3, "four rows, three instruments"
    assert row[1] == Decimal("40")


def test_consensus_input_holds_one_row_per_instrument(world) -> None:
    """The consensus functions rely on this instead of collapsing again.

    If the view ever stops guaranteeing it, an instrument gets counted twice and
    nothing else would notice.
    """
    cur, ids, params, fetch_id = world
    cur.execute(
        """insert into station_identity
             (station_id, canonical_station_id, valid_from, valid_to, evidence,
              readings_compared, readings_identical, status, confirmed_by,
              confirmed_at)
           values (%s,%s,%s,null,'test',10,10,'confirmed','test',now())""",
        (ids["s1_copy"], ids["s1"], WHEN - dt.timedelta(days=1)),
    )
    put(cur, ids, params, fetch_id, {"s1": "10", "s1_copy": "10"})
    cur.execute(
        """select count(*) from (
             select canonical_station_id, parameter_id, phenomenon_start,
                    phenomenon_end
               from consensus_eligible
              where phenomenon_start = %s
              group by 1,2,3,4 having count(*) > 1) x""",
        (WHEN,),
    )
    assert cur.fetchone()[0] == 0


def test_the_instrument_keeps_its_vote_when_the_canonical_publisher_is_quiet(
    world,
) -> None:
    """Counting an instrument once must not mean losing the reading whenever the
    publisher we prefer happens not to carry that hour."""
    cur, ids, params, fetch_id = world
    cur.execute(
        """insert into station_identity
             (station_id, canonical_station_id, valid_from, valid_to, evidence,
              readings_compared, readings_identical, status, confirmed_by,
              confirmed_at)
           values (%s,%s,%s,null,'test',10,10,'confirmed','test',now())""",
        (ids["s1_copy"], ids["s1"], WHEN - dt.timedelta(days=1)),
    )
    # Only the copy reports this hour.
    put(cur, ids, params, fetch_id, {"s1_copy": "10", "s2": "40", "s3": "46"})

    row = sets(cur)[ids["s2"]]
    assert row[0] == 3, "the reading is still there, under the canonical station"
    assert row[1] == Decimal("40")


def test_a_flagged_reading_is_left_out(world) -> None:
    """Flags exclude from consensus without deleting anything."""
    cur, ids, params, fetch_id = world
    put(cur, ids, params, fetch_id, {"s1": "10", "s2": "40", "s3": "46"})
    cur.execute(
        """insert into observation_flags
             (station_id, parameter_id, phenomenon_start, phenomenon_end,
              revision, flag, ruleset_version)
           values (%s,%s,%s,%s,1,'out_of_range','test')""",
        (ids["s1"], params["pm10"], WHEN, WHEN),
    )
    row = sets(cur)[ids["s2"]]
    assert row[0] == 2, "the flagged reading no longer votes"
    # And it is still stored, since flagging is not deletion.
    cur.execute(
        """select count(*) from observations
            where station_id=%s and phenomenon_start=%s""",
        (ids["s1"], WHEN),
    )
    assert cur.fetchone()[0] == 1


def test_a_rounded_position_widens_the_radius(world) -> None:
    """A sensor reported 1.2 km away could be at 300 m. Treating the distance as
    surveyed would be false precision."""
    cur, ids, params, fetch_id = world
    cur.execute("update stations set location_precise = false where id = %s",
                (ids["s3"],))
    put(cur, ids, params, fetch_id, {"s1": "10", "s2": "40", "s3": "46"})

    # s1 to s3 is about 1.3 km, outside a 1 km radius when both are surveyed.
    assert sets(cur, radius=1000)[ids["s1"]][0] == 3


# Bucketing. The engine could not compare a low-cost sensor with the reference
# station beside it until readings were binned to a shared resolution.

def test_readings_at_different_seconds_still_compare(world) -> None:
    """The bug this fixes. Sensor.Community reports at arbitrary seconds and
    FHMZ on the hour, so exact timestamp matching never put them together."""
    cur, ids, params, fetch_id = world
    put(cur, ids, params, fetch_id, {"s1": "10"}, when=WHEN)
    put(cur, ids, params, fetch_id, {"s2": "40"}, when=WHEN + dt.timedelta(minutes=3, seconds=47))
    put(cur, ids, params, fetch_id, {"s3": "46"}, when=WHEN + dt.timedelta(minutes=51))

    row = sets(cur)[ids["s2"]]
    assert row[0] == 3, "three instruments in the same hour must form one set"
    assert row[1] == Decimal("40")


def test_many_readings_in_one_hour_are_one_vote(world) -> None:
    """A sensor reporting every two minutes must not outvote an hourly station
    twenty four times over."""
    cur, ids, params, fetch_id = world
    for i in range(10):
        put(cur, ids, params, fetch_id, {"s1": "10"},
            when=WHEN + dt.timedelta(minutes=i * 5))
    put(cur, ids, params, fetch_id, {"s2": "40"})
    put(cur, ids, params, fetch_id, {"s3": "46"})

    row = sets(cur)[ids["s2"]]
    assert row[0] == 3, "ten readings from one sensor are still one vote"


def test_the_bucket_value_is_the_median_of_its_readings(world) -> None:
    """One spike inside the hour must not carry the hour."""
    cur, ids, params, fetch_id = world
    for i, v in enumerate(["10", "12", "500"]):
        put(cur, ids, params, fetch_id, {"s1": v},
            when=WHEN + dt.timedelta(minutes=i * 5))
    put(cur, ids, params, fetch_id, {"s2": "40"})
    put(cur, ids, params, fetch_id, {"s3": "46"})

    d = diverge(cur)[ids["s1"]]
    assert d[0] == Decimal("12"), "median of 10, 12, 500, not the mean"


def test_how_many_readings_formed_a_bucket_is_reported(world) -> None:
    """One reading in an hour is not as well observed as twenty four, and the
    only way to see that is the count."""
    cur, ids, params, fetch_id = world
    for i in range(4):
        put(cur, ids, params, fetch_id, {"s1": "10"},
            when=WHEN + dt.timedelta(minutes=i * 5))
    put(cur, ids, params, fetch_id, {"s2": "40"})
    put(cur, ids, params, fetch_id, {"s3": "46"})

    cur.execute(
        """select station_id, readings from divergence('pm10', %s, %s)""",
        (WHEN, LATER),
    )
    counted = dict(cur.fetchall())
    assert counted[ids["s1"]] == 4
    assert counted[ids["s2"]] == 1


def test_a_reading_longer_than_the_bucket_is_left_out(world) -> None:
    """A 24 hour mean is not an hour. Folding one into an hourly bucket is a
    methodological error nothing downstream could detect."""
    cur, ids, params, fetch_id = world
    put(cur, ids, params, fetch_id, {"s2": "40"})
    put(cur, ids, params, fetch_id, {"s3": "46"})
    # A daily value from s1, spanning far more than the bucket.
    ing.ingest_observations(
        cur, {"c_s1": ids["s1"]}, params, fetch_id, "t",
        [ParsedObservation(
            source_station_id="c_s1", parameter_code="pm10",
            phenomenon_start=WHEN, phenomenon_end=WHEN + dt.timedelta(hours=24),
            value=Decimal("10"), unit="ug/m3", raw_value="10", raw_unit="ug/m3")],
        seen_at=WHEN)

    row = sets(cur)[ids["s2"]]
    assert row[0] == 2, "the daily value must not join an hourly set"
    assert ids["s1"] not in diverge(cur)


def test_readings_in_different_hours_do_not_mix(world) -> None:
    """Binning must not become smearing."""
    cur, ids, params, fetch_id = world
    put(cur, ids, params, fetch_id, {"s1": "10", "s2": "40"}, when=WHEN)
    put(cur, ids, params, fetch_id, {"s3": "46"}, when=WHEN + dt.timedelta(hours=1))

    cur.execute(
        """select bucket_start, n from consensus('pm10', %s, %s, 5000, null)
            where anchor_station_id = %s order by bucket_start""",
        (WHEN, WHEN + dt.timedelta(hours=2), ids["s1"]),
    )
    rows = cur.fetchall()
    assert [r[1] for r in rows] == [2, 1], "one set per hour, not one merged set"


def test_the_bucket_size_is_a_parameter(world) -> None:
    """A reader comparing daily figures needs a daily bucket."""
    cur, ids, params, fetch_id = world
    put(cur, ids, params, fetch_id, {"s1": "10", "s2": "40"}, when=WHEN)
    put(cur, ids, params, fetch_id, {"s3": "46"}, when=WHEN + dt.timedelta(hours=1))

    cur.execute(
        """select n from consensus('pm10', %s, %s, 5000, null, %s)
            where anchor_station_id = %s""",
        (WHEN, WHEN + dt.timedelta(hours=2), dt.timedelta(hours=12), ids["s1"]),
    )
    assert [r[0] for r in cur.fetchall()] == [3], "a wider bucket holds all three"


def test_buckets_are_aligned_to_the_epoch_not_to_the_window(world) -> None:
    """A real limit, asserted so it cannot surprise anyone later.

    Bins start from the epoch, so 05:00 and 06:00 fall either side of a six hour
    boundary even though they are an hour apart. Hours line up fine. A daily
    bucket lands on UTC midnight, not Sarajevo midnight, which is why daily
    aggregates need an origin argument before they can be trusted.
    """
    cur, ids, params, fetch_id = world
    put(cur, ids, params, fetch_id, {"s1": "10", "s2": "40"}, when=WHEN)
    put(cur, ids, params, fetch_id, {"s3": "46"}, when=WHEN + dt.timedelta(hours=1))

    cur.execute(
        """select n from consensus('pm10', %s, %s, 5000, null, %s)
            where anchor_station_id = %s order by bucket_start""",
        (WHEN, WHEN + dt.timedelta(hours=2), dt.timedelta(hours=6), ids["s1"]),
    )
    assert [r[0] for r in cur.fetchall()] == [2, 1]


def test_the_aggregation_method_is_stated(world) -> None:
    """A derived value has to say how it was derived."""
    cur, ids, params, fetch_id = world
    put(cur, ids, params, fetch_id, {"s1": "10", "s2": "40", "s3": "46"})
    cur.execute(
        """select distinct aggregate from consensus('pm10', %s, %s)""",
        (WHEN, LATER),
    )
    assert cur.fetchone()[0] == "median"


def test_a_reading_still_counts_after_its_station_goes_quiet(world) -> None:
    """Liveness is not eligibility.

    Station status is computed from now() minus the last observation, so it
    decays with the calendar. Filtering historical readings by it made the whole
    archive vanish from consensus three days after collection paused, and meant
    a published figure would not reproduce next month.
    """
    cur, ids, params, fetch_id = world
    put(cur, ids, params, fetch_id, {"s1": "10", "s2": "40", "s3": "46"})
    assert sets(cur)[ids["s2"]][0] == 3

    # Time passes and every station goes stale, then dormant.
    for status in ("stale", "dormant", "campaign_ended"):
        cur.execute(
            "update station_status set status=%s where station_id = any(%s)",
            (status, list(ids.values())),
        )
        assert sets(cur)[ids["s2"]][0] == 3, (
            f"readings disappeared once stations were {status}"
        )

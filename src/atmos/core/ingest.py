"""Ingest parsed observations into Postgres.

This is where the revision ledger actually happens. For each reading:

    nothing stored yet          -> insert revision 1, which is canonical
    a stored revision matches   -> no new row, bump its confirmation counter
    no stored revision matches  -> insert the next revision, flagged

Withdrawals, where a source removes a value it previously published, are handled
separately by withdraw_missing() because they need the whole window compared, not
one reading at a time.

Nothing here updates a value. It cannot: the database revokes UPDATE and DELETE
on observations from this role. The only sanctioned write to an existing row is
confirm_observation(), which touches two counter columns.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal

import psycopg

from atmos.connectors.base import (
    Connector,
    ParsedObservation,
    ParsedStation,
)
from atmos.core.fetch import FetchResult
from atmos.core.units import UnknownConversion, convert

# Marks a reading whose payload also carried a different value for it.
DUPLICATE_FLAG = "source_duplicate"

# What we hold for one reading: (station, parameter, start, end).
Key = tuple[int, int, datetime, datetime]
# Its newest revision: (revision, value, unit, revision_kind, previous_value).
State = tuple[int, Decimal | None, str, str | None, Decimal | None]


@dataclass
class IngestResult:
    inserted: int = 0
    confirmed: int = 0
    revisions: int = 0
    withdrawn: int = 0
    duplicates: int = 0
    stations_seen: int = 0
    skipped: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return (
            f"{self.inserted} new, {self.confirmed} confirmed, "
            f"{self.revisions} revisions, {self.stations_seen} stations"
        )


def upsert_source(cur: psycopg.Cursor, connector: Connector) -> int:
    m = connector.metadata()
    cur.execute(
        """
        insert into sources
          (slug, name, operator, tier, base_url, attribution, is_primary,
           timezone, license, license_url, terms_url, notes)
        values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        on conflict (slug) do update set
          name = excluded.name,
          operator = excluded.operator,
          tier = excluded.tier,
          attribution = excluded.attribution,
          timezone = excluded.timezone,
          license = excluded.license,
          notes = excluded.notes
        returning id
        """,
        (m.slug, m.name, m.operator, m.tier, m.base_url, m.attribution,
         m.is_primary, m.timezone, m.license, m.license_url, m.terms_url, m.notes),
    )
    row = cur.fetchone()
    assert row is not None  # insert ... returning always yields a row
    return int(row[0])


def upsert_station(cur: psycopg.Cursor, source_id: int, st: ParsedStation) -> int:
    geom = None
    if st.latitude is not None and st.longitude is not None:
        geom = f"SRID=4326;POINT({st.longitude} {st.latitude})"
    cur.execute(
        """
        insert into stations
          (source_id, source_station_id, name, geom, elevation_m,
           station_type, area_type, is_indoor, is_mobile, operator,
           location_precise, last_seen_at)
        values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
        on conflict (source_id, source_station_id) do update set
          name = excluded.name,
          geom = coalesce(excluded.geom, stations.geom),
          elevation_m = coalesce(excluded.elevation_m, stations.elevation_m),
          station_type = excluded.station_type,
          area_type = excluded.area_type,
          operator = coalesce(excluded.operator, stations.operator),
          location_precise = excluded.location_precise,
          last_seen_at = now()
        returning id
        """,
        (source_id, st.source_station_id, st.name, geom, st.elevation_m,
         st.station_type, st.area_type, st.is_indoor, st.is_mobile, st.operator,
         st.location_precise),
    )
    row = cur.fetchone()
    assert row is not None
    station_id = int(row[0])

    # What the station declares it publishes, values or not. This is the only
    # way a station that has never reported can get a never_reported status.
    for code in st.declared_parameters:
        cur.execute(
            """
            insert into station_parameters (station_id, parameter_id, last_seen_at)
            select %s, id, now() from parameters where code = %s
            on conflict (station_id, parameter_id) do update set last_seen_at = now()
            """,
            (station_id, code),
        )
    return station_id


def record_fetch(cur: psycopg.Cursor, source_id: int, res: FetchResult,
                 archive_mode: str, storage_key: str | None) -> int:
    cur.execute(
        """
        insert into fetches
          (source_id, url, requested_at, http_status, content_sha256, content_bytes,
           storage_key, archive_mode, ok, error, duration_ms)
        values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        returning id
        """,
        (source_id, res.url, res.requested_at, res.http_status,
         bytes.fromhex(res.sha256) if res.sha256 else None,
         res.content_bytes, storage_key, archive_mode, res.ok, res.error,
         res.duration_ms),
    )
    row = cur.fetchone()
    assert row is not None  # insert ... returning always yields a row
    return int(row[0])


def parameter_ids(cur: psycopg.Cursor) -> dict[str, int]:
    cur.execute("select code, id from parameters")
    return {code: pid for code, pid in cur.fetchall()}


def ingest_observations(
    cur: psycopg.Cursor,
    station_ids: dict[str, int],
    param_ids: dict[str, int],
    fetch_id: int,
    parser_version: str,
    observations: list[ParsedObservation],
    seen_at: datetime | None = None,
    is_backfill: bool = False,
    count_confirmations: bool = True,
) -> IngestResult:
    """Apply a batch of parsed readings.

    Set count_confirmations False when re-reading bytes already ingested, after
    a parser fix. Readings the parser used to miss still get inserted, but a
    value we already hold is left alone: reading the same page twice is not the
    source publishing it twice.

    Batched deliberately. A row at a time meant one round trip to read the
    current state and more to write, and wrapping each write in a savepoint
    added two more. Past 64 subtransactions Postgres spills its cache and
    degrades progressively, so an eight hundred thousand row backfill crawled.
    This reads the whole batch's state in one query and writes in one call.
    """
    result = IngestResult(stations_seen=len(station_ids))
    now = seen_at or datetime.now(UTC)

    # Resolve and convert first. Anything unusable drops out here.
    prepared: list[tuple[Key, ParsedObservation, Decimal, str]] = []
    for o in observations:
        station_id = station_ids.get(o.source_station_id)
        param_id = param_ids.get(o.parameter_code)
        if station_id is None or param_id is None:
            result.skipped.append(f"{o.source_station_id}/{o.parameter_code}")
            continue
        if o.value is None:
            # A parser has no business emitting a null. Withdrawals are recorded
            # by withdraw_missing, not by ingesting an empty reading.
            result.skipped.append(f"{o.source_station_id}/{o.parameter_code}: null value")
            continue
        if not o.value.is_finite():
            # NaN is not a measurement, and it never compares equal to itself, so
            # one stored row would append a fresh revision on every ingest.
            result.skipped.append(
                f"{o.source_station_id}/{o.parameter_code}: {o.value}")
            continue
        try:
            value, unit, _factor = convert(o.value, o.unit, o.parameter_code)
        except UnknownConversion as e:
            # A wrong unit is worse than a missing reading, so refuse it.
            result.skipped.append(f"{o.source_station_id}/{o.parameter_code}: {e}")
            continue
        prepared.append(((station_id, param_id, o.phenomenon_start,
                          o.phenomenon_end), o, value, unit))

    prepared = _collapse_duplicates(prepared, result)

    if not prepared:
        return result

    existing = _current_state(cur, [k for k, _, _, _ in prepared])

    to_insert: list[tuple[object, ...]] = []
    to_confirm: list[tuple[int, int, datetime, datetime, int]] = []

    for key, o, value, unit in prepared:
        latest = existing.get(key)

        if latest is None:
            to_insert.append(_row(key, o, 1, None, None, fetch_id,
                                  parser_version, now, is_backfill, value, unit))
            result.inserted += 1
            continue

        revision, stored_value, stored_unit, kind, prior = latest

        if kind != "withdrawal" and value == stored_value and unit == stored_unit:
            # Unchanged since we last looked. Count it, store nothing.
            if count_confirmations:
                to_confirm.append((*key, revision))
                result.confirmed += 1
            continue

        # Something changed. Append, never replace. A value returning after a
        # withdrawal is a reinstatement; anything else is a change, including a
        # return to a value this reading held before.
        if kind == "withdrawal":
            new_kind, previous = "reinstatement", prior
        else:
            new_kind, previous = "value_change", stored_value

        to_insert.append(_row(key, o, revision + 1, new_kind, previous, fetch_id,
                              parser_version, now, is_backfill, value, unit))
        result.revisions += 1

    _insert_many(cur, to_insert)
    _flush_confirmations(cur, to_confirm, now)
    return result


def _collapse_duplicates(
    prepared: list[tuple[Key, ParsedObservation, Decimal, str]],
    result: IngestResult,
) -> list[tuple[Key, ParsedObservation, Decimal, str]]:
    """Keep one row per reading when a payload carries the same one twice.

    Sensor.Community's archive does this: one location, one parameter, one
    timestamp, two rows, two values. Nothing was changed between publications,
    so it is not a revision. Left alone it became one: whichever row landed first
    was stored, the next ingest read the other as a change, and the two took
    turns appending revisions forever.

    First value wins, the same rule the ledger uses everywhere. A kept reading
    whose twin disagreed is marked, so the ambiguity stays visible rather than
    being quietly resolved.
    """
    seen: dict[Key, int] = {}
    out: list[tuple[Key, ParsedObservation, Decimal, str]] = []
    for key, o, value, unit in prepared:
        pos = seen.get(key)
        if pos is None:
            seen[key] = len(out)
            out.append((key, o, value, unit))
            continue

        result.duplicates += 1
        kept_key, kept_o, kept_value, kept_unit = out[pos]
        differs = kept_value != value or kept_unit != unit
        if differs and DUPLICATE_FLAG not in kept_o.quality_flags:
            marked = replace(
                kept_o,
                quality_flags=(*kept_o.quality_flags, DUPLICATE_FLAG),
            )
            out[pos] = (kept_key, marked, kept_value, kept_unit)
    return out


def _current_state(
    cur: psycopg.Cursor,
    keys: list[Key],
) -> dict[Key, State]:
    """Newest revision held for each key, in one query rather than one each.

    Only the newest matters: comparing against older revisions would treat a
    source reverting to an earlier value as if nothing had happened.
    """
    state: dict[Key, State] = {}
    chunk = 10000
    for i in range(0, len(keys), chunk):
        part = keys[i : i + chunk]
        cur.execute(
            """
            select distinct on (o.station_id, o.parameter_id,
                                o.phenomenon_start, o.phenomenon_end)
                   o.station_id, o.parameter_id, o.phenomenon_start,
                   o.phenomenon_end, o.revision, o.value, o.unit,
                   o.revision_kind, o.previous_value
              from observations o
              join unnest(%s::bigint[], %s::smallint[],
                          %s::timestamptz[], %s::timestamptz[])
                as w(station_id, parameter_id, phenomenon_start, phenomenon_end)
                on w.station_id = o.station_id
               and w.parameter_id = o.parameter_id
               and w.phenomenon_start = o.phenomenon_start
               and w.phenomenon_end = o.phenomenon_end
             order by o.station_id, o.parameter_id, o.phenomenon_start,
                      o.phenomenon_end, o.revision desc
            """,
            (
                [k[0] for k in part],
                [k[1] for k in part],
                [k[2] for k in part],
                [k[3] for k in part],
            ),
        )
        for row in cur.fetchall():
            state[(row[0], row[1], row[2], row[3])] = (
                row[4], row[5], row[6], row[7], row[8],
            )
    return state


def _row(key: Key, o: ParsedObservation,
         revision: int, kind: str | None, previous: object, fetch_id: int,
         parser_version: str, now: datetime, is_backfill: bool,
         value: object, unit: str) -> tuple[object, ...]:
    """One row for the bulk insert. value and unit are canonical; raw_* keep
    what the source published."""
    return (*key, value, unit, o.raw_value, o.unit, revision, kind, previous,
            now, now, fetch_id, parser_version, is_backfill,
            list(o.quality_flags))


def _insert_many(cur: psycopg.Cursor, rows: list[tuple[object, ...]],
                 chunk: int = 5000) -> None:
    """Write in batches, with no savepoints.

    on conflict do nothing covers the race where another writer inserted the
    same reading between our read and this write. That row is not lost, it is
    theirs; we simply do not also count a confirmation for it.
    """
    if not rows:
        return
    sql = """
        insert into observations
          (station_id, parameter_id, phenomenon_start, phenomenon_end,
           value, unit, raw_value, raw_unit, revision, revision_kind,
           previous_value, first_seen_at, last_confirmed_at, fetch_id,
           parser_version, is_backfill, quality_flags)
        values (%s,%s,%s,%s, %s,%s,%s,%s, %s,%s,%s, %s,%s,%s,%s, %s,%s)
        on conflict do nothing
    """
    for i in range(0, len(rows), chunk):
        cur.executemany(sql, rows[i : i + chunk])


def _flush_confirmations(
    cur: psycopg.Cursor,
    pending: list[tuple[int, int, datetime, datetime, int]],
    now: datetime,
    chunk: int = 5000,
) -> None:
    """Send collected confirmations in a few calls rather than thousands."""
    for i in range(0, len(pending), chunk):
        batch = pending[i : i + chunk]
        cur.execute(
            "select confirm_observations(%s,%s,%s,%s,%s,%s)",
            (
                [b[0] for b in batch],
                [b[1] for b in batch],
                [b[2] for b in batch],
                [b[3] for b in batch],
                [b[4] for b in batch],
                now,
            ),
        )


def withdraw_missing(
    cur: psycopg.Cursor,
    station_id: int,
    param_id: int,
    window_start: datetime,
    window_end: datetime,
    still_present: set[datetime],
    fetch_id: int,
    parser_version: str,
    seen_at: datetime | None = None,
) -> int:
    """Record readings the source has stopped publishing.

    Only safe to call when the source republished the whole window, otherwise a
    partial fetch looks like a withdrawal. A value disappearing matters as much
    as a value changing, and a system that only tracked changes would miss it.
    """
    now = seen_at or datetime.now(UTC)
    cur.execute(
        """
        select distinct on (phenomenon_start)
               phenomenon_start, phenomenon_end, unit, value, revision, revision_kind
          from observations
         where station_id = %s and parameter_id = %s
           and phenomenon_start >= %s and phenomenon_start < %s
         order by phenomenon_start, revision desc
        """,
        (station_id, param_id, window_start, window_end),
    )
    withdrawn = 0
    for start, end, unit, value, revision, kind in cur.fetchall():
        if start in still_present or kind == "withdrawal":
            continue
        cur.execute(
            """
            insert into observations
              (station_id, parameter_id, phenomenon_start, phenomenon_end,
               value, unit, revision, revision_kind, previous_value,
               first_seen_at, last_confirmed_at, fetch_id, parser_version)
            values (%s,%s,%s,%s, null,%s, %s,'withdrawal',%s, %s,%s,%s,%s)
            """,
            (station_id, param_id, start, end, unit, revision + 1, value,
             now, now, fetch_id, parser_version),
        )
        withdrawn += 1
    return withdrawn


def withdraw_absent(
    cur: psycopg.Cursor,
    station_ids: dict[str, int],
    param_ids: dict[str, int],
    fetch_id: int,
    parser_version: str,
    observations: list[ParsedObservation],
    seen_at: datetime | None = None,
) -> int:
    """Record readings the source dropped from a window it just republished.

    The window and the surviving timestamps are taken from the parse itself, per
    station and parameter, so this only ever looks at ground the source just
    covered. Callers must check republishes_window first: on a snapshot feed a
    reading being absent means nothing.
    """
    present: dict[tuple[int, int], set[datetime]] = {}
    span: dict[tuple[int, int], tuple[datetime, datetime]] = {}

    for o in observations:
        station_id = station_ids.get(o.source_station_id)
        param_id = param_ids.get(o.parameter_code)
        if station_id is None or param_id is None:
            continue
        k = (station_id, param_id)
        present.setdefault(k, set()).add(o.phenomenon_start)
        lo, hi = span.get(k, (o.phenomenon_start, o.phenomenon_start))
        span[k] = (min(lo, o.phenomenon_start), max(hi, o.phenomenon_start))

    total = 0
    for k, seen in present.items():
        lo, hi = span[k]
        if lo == hi:
            # A single reading is not a window, so absence proves nothing.
            continue
        total += withdraw_missing(
            cur, k[0], k[1], lo, hi, seen, fetch_id, parser_version, seen_at
        )
    return total

"""Command line entry points."""

from __future__ import annotations

import json
import pathlib
from datetime import UTC, datetime

import typer

from atmos.connectors.base import Connector, FetchTarget
from atmos.connectors.fhmz import FhmzConnector
from atmos.connectors.rhmzrs import RhmzRsConnector
from atmos.connectors.sensorcommunity import SensorCommunityConnector
from atmos.connectors.tuzla import TuzlaConnector
from atmos.core.fetch import Fetcher

app = typer.Typer(add_completion=False, help="Atmos collector")

def _count(cur: object) -> int:
    """A count from the next row. Guards the None that mypy keeps catching."""
    row = cur.fetchone()  # type: ignore[attr-defined]
    if row is None:
        raise RuntimeError("expected a row, got none")
    return int(row[0])


CONNECTORS: dict[str, Connector] = {
    "fhmz": FhmzConnector(),
    "tuzla": TuzlaConnector(),
    "rhmzrs": RhmzRsConnector(),
    "sensorcommunity": SensorCommunityConnector(),
}


@app.command()
def collect(
    connector: str = typer.Option(..., "--connector", "-c", help="Connector slug"),
    out: pathlib.Path = typer.Option(pathlib.Path("archive"), "--out", "-o"),
    min_interval: float = typer.Option(2.0, "--min-interval"),
) -> None:
    """Fetch every target for a connector and write the bytes plus a manifest.

    Deliberately does not parse. Parsing can happen any time afterwards, from
    the archive. Fetching cannot, because the sources discard their data.
    """
    conn = CONNECTORS.get(connector)
    if conn is None:
        typer.echo(f"unknown connector: {connector}", err=True)
        raise typer.Exit(2)

    dest, ok_count, total = _collect_run(conn, connector, out, min_interval)
    typer.echo(f"\n{ok_count}/{total} ok -> {dest}")
    # A run where nothing succeeded is a failure worth surfacing to CI.
    if ok_count == 0:
        raise typer.Exit(1)


def _collect_run(
    conn: Connector, connector: str, out: pathlib.Path, min_interval: float
) -> tuple[pathlib.Path, int, int]:
    """Fetch every target and archive the bytes. Returns where, and how many.

    Split out of the command so a scheduled run can collect and load in one go
    rather than shelling out to itself.
    """
    run_started = datetime.now(UTC)
    stamp = run_started.strftime("%Y-%m-%dT%H%M%SZ")
    dest = out / connector / run_started.strftime("%Y/%m/%d") / stamp
    dest.mkdir(parents=True, exist_ok=True)

    records = []
    ok_count = 0
    with Fetcher(min_interval_s=min_interval) as f:
        targets = conn.targets()
        for i, target in enumerate(targets, 1):
            res = f.fetch(target)
            if res.body:
                (dest / f"{target.id}.html").write_bytes(res.body)
            rec = res.as_record()
            rec["stored_as"] = f"{target.id}.html" if res.body else None
            records.append(rec)
            ok_count += res.ok
            typer.echo(
                f"[{i:3d}/{len(targets)}] {target.id:20s} "
                f"{res.http_status or '---'} {res.content_bytes:>8d}B "
                f"{res.duration_ms:>5d}ms {'ok' if res.ok else res.error}"
            )

    manifest = {
        "connector": connector,
        "parser_version": conn.parser_version,
        "run_started": run_started.isoformat(),
        "run_finished": datetime.now(UTC).isoformat(),
        "targets": len(records),
        "ok": ok_count,
        "fetches": records,
    }
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return dest, ok_count, len(records)


@app.command()
def parse(
    connector: str = typer.Option(..., "--connector", "-c"),
    path: pathlib.Path = typer.Option(..., "--path", "-p", help="Directory of archived pages"),
) -> None:
    """Parse an archived directory. Read only, and safe to re-run at any time."""
    conn = CONNECTORS.get(connector)
    if conn is None:
        typer.echo(f"unknown connector: {connector}", err=True)
        raise typer.Exit(2)

    from atmos.connectors.base import ParseError

    total = 0
    for f in sorted(path.glob("*.html")):
        target = FetchTarget(id=f.stem, url="")
        try:
            obs = conn.parse(f.read_bytes(), target)
        except ParseError as e:
            typer.echo(f"{f.stem:20s} PARSE FAILED: {e}")
            continue
        last = max((o.phenomenon_start for o in obs), default=None)
        total += len(obs)
        typer.echo(
            f"{f.stem:20s} {len(obs):5d} obs  "
            f"last={last.date() if last else '-'}"
        )
    typer.echo(f"\ntotal observations: {total}")


@app.command()
def ingest(
    connector: str = typer.Option(..., "--connector", "-c"),
    path: pathlib.Path = typer.Option(..., "--path", "-p", help="An archived run directory"),
    dsn: str = typer.Option(None, "--dsn", envvar="ATMOS_DATABASE_URL"),
    reprocess: bool = typer.Option(
        False, "--reprocess",
        help="Re-read bytes already ingested, without counting confirmations",
    ),
) -> None:
    """Load an archived run into Postgres.

    Safe to re-run. A reading already held is confirmed, not duplicated, and a
    changed one is appended as a new revision. Nothing is ever overwritten.

    Use --reprocess after fixing a parser. Readings it used to miss are picked
    up, but values we already hold are left untouched, because re-reading the
    same page is not the source publishing it again.
    """
    import psycopg

    from atmos.core import ingest as ing
    from atmos.core.fetch import FetchResult

    conn_impl = CONNECTORS.get(connector)
    if conn_impl is None:
        typer.echo(f"unknown connector: {connector}", err=True)
        raise typer.Exit(2)
    if not dsn:
        typer.echo("no database DSN, set ATMOS_DATABASE_URL", err=True)
        raise typer.Exit(2)

    manifest_path = path / "manifest.json"
    if not manifest_path.exists():
        typer.echo(f"no manifest.json in {path}", err=True)
        raise typer.Exit(2)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    # A backfill run holds archive files in the source's own format, not the
    # shape the live parser expects, so the connector parses them differently.
    is_backfill = manifest.get("mode") == "backfill"
    parse_one = getattr(conn_impl, "parse_archive", None) if is_backfill else None
    stations_of = getattr(conn_impl, "archive_stations", None) if is_backfill else None
    if is_backfill and (parse_one is None or stations_of is None):
        typer.echo(f"{connector} cannot read its own archive format", err=True)
        raise typer.Exit(2)

    totals = ing.IngestResult()
    started = datetime.now(UTC)
    flagged = 0
    with psycopg.connect(dsn) as db:
        with db.cursor() as cur:
            source_id = ing.upsert_source(cur, conn_impl)
            params = ing.parameter_ids(cur)
            meta = conn_impl.metadata()

            for rec in manifest["fetches"]:
                stored = rec.get("stored_as")
                if not rec.get("ok") or not stored:
                    continue
                blob = (path / stored).read_bytes()
                t = FetchTarget(id=rec["target_id"], url=rec["url"],
                                station_hint=rec["target_id"].rsplit("-", 1)[0])

                res = FetchResult(
                    target_id=rec["target_id"], url=rec["url"],
                    requested_at=datetime.fromisoformat(rec["requested_at"]),
                    http_status=rec.get("http_status"), body=b"",
                    sha256=rec.get("content_sha256") or "",
                    content_bytes=rec.get("content_bytes") or len(blob),
                    duration_ms=rec.get("duration_ms") or 0, ok=True,
                )
                fetch_id = ing.record_fetch(cur, source_id, res, "bytes", stored)

                try:
                    found = (stations_of or conn_impl.stations)(blob, t)
                    stations = {
                        s.source_station_id: ing.upsert_station(cur, source_id, s)
                        for s in found
                    }
                    observations = (parse_one or conn_impl.parse)(blob, t)
                except Exception as e:  # noqa: BLE001
                    # Recoverable: the bytes are archived, so a fixed parser can
                    # be re-run over them. Record it as work to do.
                    cur.execute(
                        """insert into parse_failures
                             (fetch_id, parser_version, error) values (%s,%s,%s)""",
                        (fetch_id, conn_impl.parser_version, f"{type(e).__name__}: {e}"),
                    )
                    typer.echo(f"  {rec['target_id']}: parse failed, {e}")
                    continue

                r = ing.ingest_observations(cur, stations, params, fetch_id,
                                            conn_impl.parser_version, observations,
                                            is_backfill=is_backfill,
                                            count_confirmations=not reprocess)
                totals.inserted += r.inserted
                totals.confirmed += r.confirmed
                totals.revisions += r.revisions

                # Only meaningful when the fetch reprinted a whole window. On a
                # snapshot feed a reading being absent says nothing at all.
                #
                # Skipped when reprocessing. An older page cannot show readings
                # published after it, and calling those withdrawn would be a
                # withdrawal we invented.
                withdrawn = 0
                if (meta.republishes_window and observations
                        and not is_backfill and not reprocess):
                    withdrawn = ing.withdraw_absent(
                        cur, stations, params, fetch_id,
                        conn_impl.parser_version, observations)
                    totals.withdrawn += withdrawn

                extra = f", {withdrawn} withdrawn" if withdrawn else ""
                typer.echo(f"  {rec['target_id']:24s} {r}{extra}")

            cur.execute("select refresh_station_status()")
            cur.execute("select apply_range_flags()")
            row = cur.fetchone()
            flagged = int(row[0]) if row and row[0] is not None else 0
            # Stuck instruments need neighbouring readings, so this runs after
            # the batch has landed rather than per row.
            cur.execute("select apply_sequence_flags(now() - interval '30 days')")
            row = cur.fetchone()
            flagged += int(row[0]) if row and row[0] is not None else 0

            cur.execute(
                """insert into collector_runs
                     (source_id, started_at, finished_at, targets_total, targets_ok,
                      observations_inserted, revisions_inserted, ok)
                   values (%s,%s,now(),%s,%s,%s,%s,%s)""",
                (source_id, started, len(manifest["fetches"]),
                 sum(1 for r in manifest["fetches"] if r.get("ok")),
                 totals.inserted, totals.revisions, totals.inserted > 0 or totals.confirmed > 0),
            )
        db.commit()

    if flagged:
        typer.echo(f"{flagged} reading(s) flagged as implausible")

    typer.echo(f"{totals}")


@app.command()
def backfill(
    days: int = typer.Option(30, "--days", "-d", help="How far back to walk"),
    out: pathlib.Path = typer.Option(pathlib.Path("archive"), "--out", "-o"),
    min_interval: float = typer.Option(2.0, "--min-interval"),
    give_up_after: int = typer.Option(30, "--give-up-after",
                                      help="Consecutive missing days before a sensor is dropped"),
) -> None:
    """Pull historical Sensor.Community data from its daily archive.

    The only source where history can be fetched rather than waited for. One file
    per sensor per day, roughly 40 KB, back to 2015.

    A 404 means that sensor published nothing that day, which is ordinary. After
    enough consecutive misses the sensor is assumed not to have existed yet and is
    dropped, so we do not walk a 2025 sensor back to 2015 for nothing.
    """
    from datetime import date, timedelta

    from atmos.connectors.sensorcommunity import SensorCommunityConnector
    from atmos.core.fetch import Fetcher

    conn = SensorCommunityConnector()
    dest = out / conn.slug / "archive"
    dest.mkdir(parents=True, exist_ok=True)

    # Which sensors exist, and of what type, comes from the live API. The archive
    # has no index we can rely on: its day listings are 4.6 MB and flaky.
    sensors: dict[str, str] = {}
    with Fetcher(min_interval_s=min_interval) as f:
        for t in conn.targets():
            res = f.fetch(t)
            if not res.ok:
                continue
            import json as _json

            for rec in _json.loads(res.body.decode("utf-8")):
                loc = rec.get("location") or {}
                if loc.get("country") != "BA":
                    continue
                sensors[str(rec["sensor"]["id"])] = rec["sensor"]["sensor_type"]["name"]

    typer.echo(f"{len(sensors)} sensors in Bosnia and Herzegovina")


    today = date.today()
    records, fetched, missing, skipped = [], 0, 0, 0

    with Fetcher(min_interval_s=min_interval) as f:
        for sensor_id, sensor_type in sorted(sensors.items(), key=lambda x: int(x[0])):
            consecutive_misses = 0
            got = 0
            for back in range(1, days + 1):
                day = today - timedelta(days=back)
                target = conn.archive_target(sensor_id, sensor_type, day)
                path = dest / f"{target.id}.csv"

                if path.exists():
                    skipped += 1
                    consecutive_misses = 0
                    continue

                res = f.fetch(target)
                if res.http_status == 404:
                    missing += 1
                    consecutive_misses += 1
                    if consecutive_misses >= give_up_after:
                        break
                    continue
                if not res.ok:
                    consecutive_misses += 1
                    continue

                path.write_bytes(res.body)
                rec = res.as_record()
                rec["stored_as"] = path.name
                rec["sensor_type"] = sensor_type
                records.append(rec)
                fetched += 1
                got += 1
                consecutive_misses = 0

            typer.echo(f"  sensor {sensor_id:>6} {sensor_type:8s} {got:4d} day(s)")

    manifest = {
        "connector": conn.slug,
        "parser_version": conn.parser_version,
        "mode": "backfill",
        "run_started": datetime.now(UTC).isoformat(),
        "targets": len(records),
        "ok": fetched,
        "fetches": records,
    }
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    typer.echo(
        f"fetched {fetched}, already held {skipped}, absent {missing} -> {dest}"
    )


@app.command()
def status(
    dsn: str = typer.Option(None, "--dsn", envvar="ATMOS_DATABASE_URL"),
) -> None:
    """What we hold, and how much of it is trustworthy."""
    import psycopg

    if not dsn:
        typer.echo("no database DSN, set ATMOS_DATABASE_URL", err=True)
        raise typer.Exit(2)

    with psycopg.connect(dsn) as db, db.cursor() as cur:
        cur.execute("select count(*) from observations")
        total = _count(cur)
        if not total:
            typer.echo("no observations held")
            return

        typer.echo("SOURCES")
        cur.execute("""
            select s.slug, s.tier, count(distinct st.id) as stations,
                   count(o.*) as observations,
                   min(o.phenomenon_start)::date, max(o.phenomenon_start)::date
              from sources s
              left join stations st on st.source_id = s.id
              left join observations o on o.station_id = st.id
             group by s.slug, s.tier order by count(o.*) desc
        """)
        for slug, tier, stations, obs, lo, hi in cur.fetchall():
            span = f"{lo} to {hi}" if lo else "no data"
            typer.echo(f"  {slug:16s} {tier:12s} {stations:4d} stations "
                       f"{obs:>9,} obs   {span}")

        typer.echo("")
        typer.echo("RECORD INTEGRITY")
        cur.execute("""
            select count(*) filter (where revision = 1) as originals,
                   count(*) filter (where revision > 1) as revisions,
                   count(*) filter (where revision_kind = 'withdrawal') as withdrawals,
                   count(*) filter (where is_backfill) as backfilled
              from observations
        """)
        row = cur.fetchone()
        assert row is not None
        originals, revisions, withdrawals, backfilled = row
        typer.echo(f"  originals   {originals:>9,}")
        typer.echo(f"  revisions   {revisions:>9,}   (a source changed a published value)")
        typer.echo(f"  withdrawals {withdrawals:>9,}   (a source removed one)")
        typer.echo(f"  backfilled  {backfilled:>9,}   (history, not watched live)")

        typer.echo("")
        typer.echo("QUALITY")
        cur.execute("select count(*) from consensus_eligible")
        eligible = _count(cur)
        cur.execute("""
            select f.flag, count(*) from observation_flags f
             group by f.flag order by count(*) desc
        """)
        flags = cur.fetchall()
        share = 100.0 * eligible / originals if originals else 0
        typer.echo(f"  eligible for consensus {eligible:>9,}  ({share:.1f}% of originals)")
        for flag, n in flags:
            typer.echo(f"  flagged {flag:22s} {n:>9,}")
        if not flags:
            typer.echo("  no flags raised")

        typer.echo("")
        typer.echo("STATIONS BY STATUS")
        cur.execute("""
            select status, count(*) from station_status
             group by status order by count(*) desc
        """)
        for st, n in cur.fetchall():
            typer.echo(f"  {st:16s} {n:4d} station/parameter pairs")

        typer.echo("")
        typer.echo("GAPS, worst first")
        cur.execute("""
            select s.slug, stn.name, p.code, ss.status,
                   ss.last_observation_at::date
              from station_status ss
              join stations stn on stn.id = ss.station_id
              join sources s on s.id = stn.source_id
              join parameters p on p.id = ss.parameter_id
             where ss.status in ('never_reported','dormant','stale')
             order by ss.last_observation_at nulls first
             limit 10
        """)
        rows = cur.fetchall()
        for slug, name, code, st, last in rows:
            typer.echo(f"  {slug:14s} {name[:22]:22s} {code:5s} {st:15s} "
                       f"{last or 'never'}")
        if not rows:
            typer.echo("  none")


@app.command()
def health(
    days: int = typer.Option(7, "--days", "-d", help="Window to judge, in days"),
    source: str = typer.Option(None, "--source", "-s", help="One source slug"),
    dsn: str = typer.Option(None, "--dsn", envvar="ATMOS_DATABASE_URL"),
) -> None:
    """Which stations look reliable, and which do not.

    Reports, it does not decide. A station listed here is still stored and still
    served. Whether to exclude it is the reader's call, which is the whole point
    of showing the numbers rather than quietly dropping the station.
    """
    import psycopg

    if not dsn:
        typer.echo("no database DSN, set ATMOS_DATABASE_URL", err=True)
        raise typer.Exit(2)

    with psycopg.connect(dsn) as db, db.cursor() as cur:
        cur.execute(
            """
            with r as (
              select o.station_id, o.parameter_id, o.phenomenon_start,
                     o.phenomenon_end, o.revision, o.value
                from observations o
               where o.revision = 1
                 and o.phenomenon_start >= now() - make_interval(days => %s)
            )
            select so.slug, st.name, p.code,
                   count(*) as readings,
                   count(*) filter (where r.value = 0) as zeros,
                   count(f.flag) as flagged,
                   max(r.phenomenon_start) as last_reading
              from r
              join stations st on st.id = r.station_id
              join sources so on so.id = st.source_id
              join parameters p on p.id = r.parameter_id
              left join observation_flags f
                on f.station_id = r.station_id
               and f.parameter_id = r.parameter_id
               and f.phenomenon_start = r.phenomenon_start
               and f.phenomenon_end = r.phenomenon_end
               and f.revision = r.revision
             where (%s::text is null or so.slug = %s::text)
             group by so.slug, st.name, p.code
             having count(*) >= 20
             order by
               (count(*) filter (where r.value = 0))::numeric / count(*) desc,
               count(f.flag)::numeric / count(*) desc
            """,
            (days, source, source),
        )
        rows = cur.fetchall()

    if not rows:
        typer.echo(f"nothing reported in the last {days} days")
        return

    def share(part: int, whole: int) -> int:
        return round(100 * part / whole) if whole else 0

    # A reading of exactly zero is not credible for these, so a station
    # producing them in quantity is reporting a fault, not clean air.
    suspect = [r for r in rows if share(r[4], r[3]) >= 5 or share(r[5], r[3]) >= 20]
    fine = [r for r in rows if r not in suspect]

    typer.echo(f"STATION HEALTH, last {days} days")
    typer.echo("")

    if suspect:
        typer.echo("Worth a look")
        for slug, name, code, readings, zeros, flagged, last in suspect:
            notes = []
            if share(zeros, readings) >= 5:
                notes.append(f"{share(zeros, readings)}% of readings are exactly zero")
            if share(flagged, readings) >= 20:
                notes.append(f"{share(flagged, readings)}% flagged")
            typer.echo(f"  {name} ({slug}) {code}")
            typer.echo(f"      {', '.join(notes)}, over {readings:,} readings")
            typer.echo(f"      last reported {last:%Y-%m-%d %H:%M}")
        typer.echo("")

    typer.echo(f"Reporting normally: {len(fine)} station and parameter pairs")
    typer.echo("")
    typer.echo("Listed, not excluded. Every reading above is still stored and served.")


@app.command()
def sync(
    connector: list[str] = typer.Option(
        None, "--connector", "-c",
        help="Repeatable. Defaults to every live connector",
    ),
    out: pathlib.Path = typer.Option(pathlib.Path("archive"), "--out", "-o"),
    dsn: str = typer.Option(None, "--dsn", envvar="ATMOS_DATABASE_URL"),
    min_interval: float = typer.Option(2.0, "--min-interval"),
) -> None:
    """Collect from every source and load the result, in one command.

    This is what a timer runs. Collecting and loading were separate commands and
    the database drifted four days behind while collection carried on fine,
    because keeping up meant remembering to run several things in order.

    A source that fails does not stop the others. Fetching is the part that
    cannot be recovered, so one broken parser must never cost another source its
    window.
    """
    slugs = list(connector) if connector else [
        s for s in CONNECTORS if s != "sensorcommunity-archive"
    ]
    unknown = [s for s in slugs if s not in CONNECTORS]
    if unknown:
        typer.echo(f"unknown connector(s): {', '.join(unknown)}", err=True)
        raise typer.Exit(2)
    if not dsn:
        typer.echo("no database DSN, set ATMOS_DATABASE_URL", err=True)
        raise typer.Exit(2)

    collected: list[str] = []
    failed: list[str] = []

    for slug in slugs:
        conn = CONNECTORS[slug]
        typer.echo(f"\n=== {slug} ===")
        try:
            dest, ok_count, total = _collect_run(conn, slug, out, min_interval)
        except Exception as e:  # noqa: BLE001
            typer.echo(f"  collect failed: {type(e).__name__}: {e}", err=True)
            failed.append(slug)
            continue

        typer.echo(f"  fetched {ok_count}/{total} -> {dest}")
        if ok_count == 0:
            typer.echo("  nothing fetched, not loading", err=True)
            failed.append(slug)
            continue

        try:
            ingest(connector=slug, path=dest, dsn=dsn, reprocess=False)
            collected.append(slug)
        except typer.Exit as e:
            if e.exit_code:
                typer.echo(f"  load failed with exit {e.exit_code}", err=True)
                failed.append(slug)
            else:
                collected.append(slug)
        except Exception as e:  # noqa: BLE001
            # The bytes are archived either way, so this is recoverable.
            typer.echo(f"  load failed: {type(e).__name__}: {e}", err=True)
            failed.append(slug)

    typer.echo(f"\nloaded: {', '.join(collected) or 'none'}")
    if failed:
        typer.echo(f"failed: {', '.join(failed)}", err=True)
        raise typer.Exit(1)


if __name__ == "__main__":
    app()

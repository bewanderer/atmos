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

    typer.echo(f"\n{ok_count}/{len(records)} ok -> {dest}")
    # A run where nothing succeeded is a failure worth surfacing to CI.
    if ok_count == 0:
        raise typer.Exit(1)


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
) -> None:
    """Load an archived run into Postgres.

    Safe to re-run. A reading already held is confirmed, not duplicated, and a
    changed one is appended as a new revision. Nothing is ever overwritten.
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
                    stations = {
                        s.source_station_id: ing.upsert_station(cur, source_id, s)
                        for s in conn_impl.stations(blob, t)
                    }
                    observations = conn_impl.parse(blob, t)
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
                                            conn_impl.parser_version, observations)
                totals.inserted += r.inserted
                totals.confirmed += r.confirmed
                totals.revisions += r.revisions

                # Only meaningful when the fetch reprinted a whole window. On a
                # snapshot feed a reading being absent says nothing at all.
                withdrawn = 0
                if meta.republishes_window and observations:
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


if __name__ == "__main__":
    app()

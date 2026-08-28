"""Command line entry points."""

from __future__ import annotations

import json
import pathlib
from datetime import UTC, datetime

import typer

from atmos.connectors.base import Connector
from atmos.connectors.fhmz import FhmzConnector
from atmos.core.fetch import Fetcher

app = typer.Typer(add_completion=False, help="Atmos collector")

CONNECTORS: dict[str, Connector] = {
    "fhmz": FhmzConnector(),
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

    from atmos.connectors.base import FetchTarget, ParseError

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


if __name__ == "__main__":
    app()

"""Command line tests.

The CLI is what the scheduled workflow actually runs, so its failure paths
matter as much as its happy one. A collector that exits zero having archived
nothing is the worst outcome available.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from atmos.cli import CONNECTORS, app
from tests.conftest import DSN, requires_db

runner = CliRunner()
FIXTURES = Path(__file__).parent / "fixtures"


def test_every_registered_connector_is_usable() -> None:
    for slug, conn in CONNECTORS.items():
        assert conn.slug == slug
        assert conn.targets(), f"{slug} has no targets"
        assert conn.metadata().attribution, f"{slug} declares no attribution"
        assert conn.parser_version


def test_unknown_connector_exits_with_an_error() -> None:
    result = runner.invoke(app, ["collect", "--connector", "nope", "--out", "x"])
    assert result.exit_code == 2
    assert "unknown connector" in result.output


@pytest.fixture
def archived_run(tmp_path: Path) -> Path:
    """An archive directory shaped exactly like a real collect run."""
    run = tmp_path / "fhmz" / "2026" / "08" / "28" / "2026-08-28T000000Z"
    run.mkdir(parents=True)
    page = "amsVijecnica"
    shutil.copy(FIXTURES / "fhmz" / f"{page}.html", run / f"{page}.html")
    body = (run / f"{page}.html").read_bytes()

    manifest = {
        "connector": "fhmz",
        "parser_version": "fhmz-1",
        "run_started": datetime.now(UTC).isoformat(),
        "targets": 1,
        "ok": 1,
        "fetches": [{
            "target_id": page,
            "url": f"https://www.fhmzbih.gov.ba/latinica/ZRAK/{page}.php",
            "requested_at": datetime.now(UTC).isoformat(),
            "http_status": 200,
            "content_sha256": "ab" * 32,
            "content_bytes": len(body),
            "duration_ms": 120,
            "ok": True,
            "error": None,
            "stored_as": f"{page}.html",
        }],
    }
    (run / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return run


def test_parse_reads_an_archive_without_touching_the_network(archived_run: Path) -> None:
    result = runner.invoke(app, ["parse", "--connector", "fhmz",
                                 "--path", str(archived_run)])
    assert result.exit_code == 0
    assert "total observations:" in result.output
    assert "amsVijecnica" in result.output


def test_parse_reports_a_broken_page_instead_of_crashing(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    (run / "amsVijecnica.html").write_bytes(b"<html>redesigned</html>")
    result = runner.invoke(app, ["parse", "--connector", "fhmz", "--path", str(run)])
    assert result.exit_code == 0
    assert "PARSE FAILED" in result.output


def test_parse_rejects_an_unknown_connector(archived_run: Path) -> None:
    result = runner.invoke(app, ["parse", "--connector", "nope",
                                 "--path", str(archived_run)])
    assert result.exit_code == 2


def test_ingest_without_a_manifest_exits(tmp_path: Path) -> None:
    result = runner.invoke(app, ["ingest", "--connector", "fhmz",
                                 "--path", str(tmp_path), "--dsn", "postgres://x"])
    assert result.exit_code == 2
    assert "manifest" in result.output


def test_ingest_without_a_dsn_exits(archived_run: Path) -> None:
    env = {k: v for k, v in os.environ.items() if k != "ATMOS_DATABASE_URL"}
    result = runner.invoke(app, ["ingest", "--connector", "fhmz",
                                 "--path", str(archived_run)], env=env)
    assert result.exit_code == 2


@requires_db
def test_ingest_loads_an_archive_and_is_idempotent(archived_run: Path) -> None:
    first = runner.invoke(app, ["ingest", "--connector", "fhmz",
                                "--path", str(archived_run), "--dsn", DSN])
    assert first.exit_code == 0, first.output
    assert "new" in first.output

    second = runner.invoke(app, ["ingest", "--connector", "fhmz",
                                 "--path", str(archived_run), "--dsn", DSN])
    assert second.exit_code == 0, second.output
    assert "0 new" in second.output, "re-ingesting must confirm, not duplicate"


@pytest.fixture
def backfill_run(tmp_path: Path) -> Path:
    """An archive directory shaped like a real backfill run."""
    run = tmp_path / "sensorcommunity" / "archive"
    run.mkdir(parents=True)
    src = FIXTURES / "sensorcommunity"
    files = [
        ("74725-2026-08-26", "archive_sds011_84500.csv"),
        ("70385-2026-08-26", "archive_bme280_80927.csv"),
    ]
    fetches = []
    for target_id, name in files:
        shutil.copy(src / name, run / f"{target_id}.csv")
        body = (run / f"{target_id}.csv").read_bytes()
        fetches.append({
            "target_id": target_id,
            "url": f"https://archive.sensor.community/2026-08-26/{name}",
            "requested_at": datetime.now(UTC).isoformat(),
            "http_status": 200,
            "content_sha256": "cd" * 32,
            "content_bytes": len(body),
            "duration_ms": 90,
            "ok": True,
            "error": None,
            "stored_as": f"{target_id}.csv",
        })
    (run / "manifest.json").write_text(json.dumps({
        "connector": "sensorcommunity",
        "parser_version": "sensorcommunity-1",
        "mode": "backfill",
        "run_started": datetime.now(UTC).isoformat(),
        "targets": len(fetches), "ok": len(fetches), "fetches": fetches,
    }), encoding="utf-8")
    return run


@requires_db
def test_ingest_reads_backfill_archives(backfill_run: Path) -> None:
    """Backfill files are CSV, not the JSON the live parser expects."""
    result = runner.invoke(app, ["ingest", "--connector", "sensorcommunity",
                                 "--path", str(backfill_run), "--dsn", DSN])
    assert result.exit_code == 0, result.output
    assert "parse failed" not in result.output
    assert "new" in result.output


@requires_db
def test_backfilled_rows_are_marked_as_such(backfill_run: Path) -> None:
    """So historical loads are never confused with what we watched happen live."""
    import psycopg

    runner.invoke(app, ["ingest", "--connector", "sensorcommunity",
                        "--path", str(backfill_run), "--dsn", DSN])
    with psycopg.connect(DSN) as conn:
        cur = conn.cursor()
        cur.execute("""
            select count(*) filter (where is_backfill),
                   count(*) filter (where not is_backfill)
              from observations o
              join stations st on st.id = o.station_id
              join sources s on s.id = st.source_id
             where s.slug = 'sensorcommunity'
               and o.phenomenon_start::date = date '2026-08-26'
        """)
        backfilled, live = cur.fetchone()
    assert backfilled > 0
    assert live == 0


def test_backfill_rejects_a_connector_without_an_archive(tmp_path: Path) -> None:
    """FHMZ has no archive format, so a backfill manifest must be refused."""
    run = tmp_path / "run"
    run.mkdir()
    (run / "manifest.json").write_text(json.dumps({
        "connector": "fhmz", "mode": "backfill", "fetches": [],
    }), encoding="utf-8")
    result = runner.invoke(app, ["ingest", "--connector", "fhmz",
                                 "--path", str(run), "--dsn", "postgres://x"])
    assert result.exit_code == 2
    assert "archive format" in result.output

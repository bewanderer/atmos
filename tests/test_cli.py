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


DSN = os.environ.get("ATMOS_TEST_DSN")


@pytest.mark.skipif(not DSN, reason="ATMOS_TEST_DSN not set")
def test_ingest_loads_an_archive_and_is_idempotent(archived_run: Path) -> None:
    first = runner.invoke(app, ["ingest", "--connector", "fhmz",
                                "--path", str(archived_run), "--dsn", DSN])
    assert first.exit_code == 0, first.output
    assert "new" in first.output

    second = runner.invoke(app, ["ingest", "--connector", "fhmz",
                                 "--path", str(archived_run), "--dsn", DSN])
    assert second.exit_code == 0, second.output
    assert "0 new" in second.output, "re-ingesting must confirm, not duplicate"

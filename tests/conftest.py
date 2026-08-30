"""Shared test setup.

Database-backed tests need Postgres. The rule is deliberately strict:

    ATMOS_TEST_DSN unset      -> skip. Nobody asked for a database.
    ATMOS_TEST_DSN set        -> the database is expected. Unreachable is a
                                 failure, not a skip.

Skipping an unreachable database would let CI go green on broken infrastructure,
which is the same silent failure this project exists to catch. Checked once with
a short timeout so a wrong DSN reports immediately instead of hanging the suite
on connection attempts.
"""

from __future__ import annotations

import os

import pytest

RAW_DSN = os.environ.get("ATMOS_TEST_DSN")

# Fail fast rather than sit on the default connect timeout.
CONNECT_TIMEOUT = "connect_timeout=3"


class DatabaseUnreachable(Exception):
    """ATMOS_TEST_DSN is set but nothing answers."""


def _probe(dsn: str | None) -> tuple[str | None, str]:
    """Return (dsn, note). Raises if a database was expected and is missing."""
    if not dsn:
        return None, "ATMOS_TEST_DSN not set"

    import psycopg

    full = dsn if "connect_timeout" in dsn else f"{dsn} {CONNECT_TIMEOUT}"
    try:
        with psycopg.connect(full) as conn:
            conn.execute("select 1")
    except Exception as e:  # noqa: BLE001
        raise DatabaseUnreachable(
            f"ATMOS_TEST_DSN is set but the database did not answer: "
            f"{type(e).__name__}. Start it, or unset ATMOS_TEST_DSN to skip "
            f"the database tests deliberately."
        ) from e
    return full, "enabled"


DSN, NOTE = _probe(RAW_DSN)

requires_db = pytest.mark.skipif(DSN is None, reason=NOTE)


def pytest_report_header(config: pytest.Config) -> str:
    return f"database tests: {NOTE if DSN else 'skipped, ' + NOTE}"

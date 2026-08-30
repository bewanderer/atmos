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
CONNECT_TIMEOUT_S = 3


class DatabaseUnreachable(Exception):
    """ATMOS_TEST_DSN is set but nothing answers."""


class DatabaseDsnInvalid(Exception):
    """ATMOS_TEST_DSN is set but is not a DSN Postgres accepts."""


def _with_timeout(dsn: str) -> str:
    """Add the connect timeout without assuming which DSN form was given.

    Both forms are valid and both get used: a URI wants ?connect_timeout=3, a
    keyword string wants a space. Pasting one onto the other yields a DSN
    Postgres rejects, which then reads as an unreachable database.
    """
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    try:
        parts = conninfo_to_dict(dsn)
    except Exception as e:  # noqa: BLE001
        raise DatabaseDsnInvalid(
            f"ATMOS_TEST_DSN is not a DSN Postgres accepts: {type(e).__name__}. "
            f"Expected either postgresql://user:pass@host:port/db or "
            f"host=... dbname=... form."
        ) from e
    if "connect_timeout" in parts:
        return dsn
    return make_conninfo(dsn, connect_timeout=CONNECT_TIMEOUT_S)


def _probe(dsn: str | None) -> tuple[str | None, str]:
    """Return (dsn, note). Raises if a database was expected and is missing."""
    if not dsn:
        return None, "ATMOS_TEST_DSN not set"

    import psycopg

    full = _with_timeout(dsn)
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

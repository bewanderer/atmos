"""Shared test setup.

Database-backed tests skip unless ATMOS_TEST_DSN points at a reachable Postgres.
Checked once, with a short timeout, because a DSN that is set but unreachable
would otherwise hang the whole suite on connection attempts rather than saying so.
"""

from __future__ import annotations

import os

import pytest

RAW_DSN = os.environ.get("ATMOS_TEST_DSN")

# Fail fast rather than sit on the default connect timeout.
CONNECT_TIMEOUT = "connect_timeout=3"


def _usable(dsn: str | None) -> tuple[str | None, str]:
    """Return (dsn, reason). A dsn of None means skip, and reason says why."""
    if not dsn:
        return None, "ATMOS_TEST_DSN not set"
    try:
        import psycopg
    except ImportError:  # pragma: no cover
        return None, "psycopg not installed"

    full = dsn if "connect_timeout" in dsn else f"{dsn} {CONNECT_TIMEOUT}"
    try:
        with psycopg.connect(full) as conn:
            conn.execute("select 1")
    except Exception as e:  # noqa: BLE001
        return None, f"database unreachable: {type(e).__name__}"
    return full, ""


DSN, SKIP_REASON = _usable(RAW_DSN)

requires_db = pytest.mark.skipif(DSN is None, reason=SKIP_REASON)


def pytest_report_header(config: pytest.Config) -> str:
    if DSN:
        return "database tests: enabled"
    return f"database tests: skipped, {SKIP_REASON}"

"""Fetcher tests.

The fetcher is the part that cannot fail quietly. Sources discard their data, so
a fetch not made is lost for everyone. These cover the paths that only run when
something goes wrong, which are exactly the ones never exercised by hand.
"""

from __future__ import annotations

import httpx

from atmos.connectors.base import FetchTarget
from atmos.core.fetch import DEFAULT_UA, Fetcher, FetchResult


def target(url: str = "https://example.test/page") -> FetchTarget:
    return FetchTarget(id="t1", url=url)


def fetcher_with(handler, **kw) -> Fetcher:
    """A Fetcher whose HTTP calls are served by handler, with no waiting."""
    f = Fetcher(min_interval_s=0, backoff_base_s=0, **kw)
    f._client = httpx.Client(transport=httpx.MockTransport(handler),
                             headers={"User-Agent": DEFAULT_UA},
                             follow_redirects=True)
    return f


def test_successful_fetch_records_hash_and_size() -> None:
    body = b"<html>hello</html>"
    with fetcher_with(lambda r: httpx.Response(200, content=body)) as f:
        res = f.fetch(target())
    assert res.ok
    assert res.body == body
    assert res.content_bytes == len(body)
    assert len(res.sha256) == 64
    assert res.error is None


def test_hash_is_of_the_body() -> None:
    import hashlib

    body = b"measurements"
    with fetcher_with(lambda r: httpx.Response(200, content=body)) as f:
        res = f.fetch(target())
    assert res.sha256 == hashlib.sha256(body).hexdigest()


def test_identifies_itself_with_a_contact_url() -> None:
    """Politeness is an availability requirement, not a courtesy."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ua"] = request.headers.get("user-agent")
        return httpx.Response(200, content=b"x")

    with fetcher_with(handler) as f:
        f.fetch(target())
    assert "Atmos" in seen["ua"]
    assert "github.com" in seen["ua"]


def test_server_error_is_retried_then_reported() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, content=b"")

    with fetcher_with(handler, max_retries=3) as f:
        res = f.fetch(target())
    assert calls["n"] == 3
    assert not res.ok
    assert "503" in res.error


def test_rate_limit_is_retried() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429 if calls["n"] < 3 else 200, content=b"ok")

    with fetcher_with(handler, max_retries=4) as f:
        res = f.fetch(target())
    assert calls["n"] == 3
    assert res.ok


def test_transient_failure_then_success() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, content=b"recovered")

    with fetcher_with(handler, max_retries=3) as f:
        res = f.fetch(target())
    assert res.ok
    assert res.body == b"recovered"


def test_network_failure_is_recorded_not_raised() -> None:
    """A failed fetch must still produce a record. Silence is the enemy."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("host is down")

    with fetcher_with(handler, max_retries=2) as f:
        res = f.fetch(target())
    assert isinstance(res, FetchResult)
    assert not res.ok
    assert res.error and "ConnectError" in res.error
    assert res.body == b""


def test_client_error_is_not_retried() -> None:
    """A 404 will not fix itself, so hammering it is rude and pointless."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404, content=b"")

    with fetcher_with(handler, max_retries=4) as f:
        res = f.fetch(target())
    assert calls["n"] == 1
    assert not res.ok
    assert "404" in res.error


def test_empty_body_is_not_success() -> None:
    with fetcher_with(lambda r: httpx.Response(200, content=b"")) as f:
        res = f.fetch(target())
    assert not res.ok


def test_redirects_are_followed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/page":
            return httpx.Response(302, headers={"location": "https://example.test/moved"})
        return httpx.Response(200, content=b"final")

    with fetcher_with(handler) as f:
        res = f.fetch(target())
    assert res.ok
    assert res.body == b"final"


def test_rate_limiting_is_per_host() -> None:
    """One slow source must not throttle the others."""
    f = fetcher_with(lambda r: httpx.Response(200, content=b"x"))
    f.min_interval_s = 5.0
    f.fetch(FetchTarget(id="a", url="https://one.test/x"))
    import time

    t0 = time.monotonic()
    f.fetch(FetchTarget(id="b", url="https://two.test/x"))  # different host, no wait
    assert time.monotonic() - t0 < 1.0
    f._client.close()


def test_record_maps_onto_the_fetches_table() -> None:
    with fetcher_with(lambda r: httpx.Response(200, content=b"data")) as f:
        rec = f.fetch(target()).as_record()
    for field in ("url", "requested_at", "http_status", "content_sha256",
                  "content_bytes", "duration_ms", "ok", "error"):
        assert field in rec

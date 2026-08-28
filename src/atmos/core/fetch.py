"""The fetcher.

Downloads bytes and records what happened. It never interprets content, so it
has no reason to break when a source redesigns its pages.

Politeness here is an availability requirement, not a courtesy. Being rate
limited or blocked is the one failure we cannot recover from, because sources
like Tuzla Canton discard data after about 48 hours.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx

from atmos.connectors.base import FetchTarget

DEFAULT_UA = "Atmos/0.1.0 (+https://github.com/bewanderer/atmos)"


@dataclass(frozen=True)
class FetchResult:
    """One request, whether it worked or not. Failures are recorded, not discarded."""

    target_id: str
    url: str
    requested_at: datetime
    http_status: int | None
    body: bytes
    sha256: str
    content_bytes: int
    duration_ms: int
    ok: bool
    error: str | None = None

    def as_record(self) -> dict[str, object]:
        """Provenance record. Maps onto the fetches table when ingest exists."""
        return {
            "target_id": self.target_id,
            "url": self.url,
            "requested_at": self.requested_at.isoformat(),
            "http_status": self.http_status,
            "content_sha256": self.sha256,
            "content_bytes": self.content_bytes,
            "duration_ms": self.duration_ms,
            "ok": self.ok,
            "error": self.error,
        }


class Fetcher:
    def __init__(
        self,
        user_agent: str = DEFAULT_UA,
        timeout_s: float = 30.0,
        min_interval_s: float = 2.0,
        max_retries: int = 4,
        backoff_base_s: float = 2.0,
    ) -> None:
        self.min_interval_s = min_interval_s
        self.max_retries = max_retries
        self.backoff_base_s = backoff_base_s
        # Per host, so one slow source does not throttle the others.
        self._last_request: dict[str, float] = {}
        self._client = httpx.Client(
            headers={"User-Agent": user_agent},
            timeout=timeout_s,
            follow_redirects=True,
        )

    def __enter__(self) -> Fetcher:
        return self

    def __exit__(self, *exc: object) -> None:
        self._client.close()

    def _wait_turn(self, url: str) -> None:
        host = urlsplit(url).netloc
        last = self._last_request.get(host)
        if last is not None:
            gap = time.monotonic() - last
            if gap < self.min_interval_s:
                time.sleep(self.min_interval_s - gap)
        self._last_request[host] = time.monotonic()

    def fetch(self, target: FetchTarget) -> FetchResult:
        started = datetime.now(UTC)
        t0 = time.monotonic()
        status: int | None = None
        error: str | None = None
        body = b""

        for attempt in range(self.max_retries):
            self._wait_turn(target.url)
            try:
                r = self._client.get(target.url)
                status = r.status_code
                if r.status_code >= 500 or r.status_code == 429:
                    # Their problem, probably transient. Back off and try again.
                    error = f"HTTP {r.status_code}"
                    if attempt < self.max_retries - 1:
                        time.sleep(self.backoff_base_s * (2**attempt))
                        continue
                else:
                    body = r.content
                    error = None if r.status_code < 400 else f"HTTP {r.status_code}"
                break
            except httpx.HTTPError as e:
                error = f"{type(e).__name__}: {e}"
                if attempt < self.max_retries - 1:
                    time.sleep(self.backoff_base_s * (2**attempt))

        ok = bool(body) and status is not None and status < 400
        return FetchResult(
            target_id=target.id,
            url=target.url,
            requested_at=started,
            http_status=status,
            body=body,
            sha256=hashlib.sha256(body).hexdigest() if body else "",
            content_bytes=len(body),
            duration_ms=int((time.monotonic() - t0) * 1000),
            ok=ok,
            error=error,
        )

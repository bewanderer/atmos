"""Connector interface.

Adding a source means adding a module here plus a row in the sources table.
The ingest core never changes.

Parsers are pure functions over bytes. No network, no clock, no database.
That is what makes reprocessing the whole archive routine, and it is why a
connector can be reviewed and tested without any credentials.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class FetchTarget:
    """One thing to request. Stable id, because it is part of the archive key."""

    id: str
    url: str
    station_hint: str | None = None


@dataclass(frozen=True)
class ParsedObservation:
    """One reading, as published. Units are converted later, not here."""

    source_station_id: str
    parameter_code: str
    phenomenon_start: datetime
    phenomenon_end: datetime
    value: Decimal | None
    unit: str
    raw_value: str
    raw_unit: str | None = None
    quality_flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class ParsedStation:
    """Station metadata, where the source exposes it. Most BiH sources expose little."""

    source_station_id: str
    name: str
    latitude: float | None = None
    longitude: float | None = None
    elevation_m: float | None = None
    station_type: str = "unknown"
    area_type: str = "unknown"
    is_indoor: bool = False
    is_mobile: bool = False
    # False when the source rounds coordinates for privacy, which matters when
    # matching stations to each other by distance.
    location_precise: bool = True
    # What the page declares, even if it delivered nothing. Without this we
    # cannot tell a broken station from one that never existed.
    declared_parameters: tuple[str, ...] = ()


@dataclass(frozen=True)
class SourceMetadata:
    """Recorded against the source before it goes live. Licensing is not optional."""

    slug: str
    name: str
    operator: str | None
    tier: str
    base_url: str
    attribution: str
    is_primary: bool
    # IANA name of the timezone the source publishes in. Display is always
    # Europe/Sarajevo; this is kept so the original can be shown alongside.
    timezone: str = "Europe/Sarajevo"
    license: str | None = None
    license_url: str | None = None
    terms_url: str | None = None
    archive_mode: str = "bytes"
    notes: str | None = None


@runtime_checkable
class Connector(Protocol):
    slug: str
    parser_version: str

    def targets(self) -> list[FetchTarget]:
        """What to request this cycle."""

    def parse(self, raw: bytes, target: FetchTarget) -> list[ParsedObservation]:
        """Pure. Same bytes in, same observations out, forever."""

    def stations(self, raw: bytes, target: FetchTarget) -> list[ParsedStation]:
        """Station metadata found in the same payload. Often empty."""

    def metadata(self) -> SourceMetadata:
        """Static description of the source."""


class ParseError(Exception):
    """Raised when bytes cannot be parsed.

    Recoverable by design. The bytes are already archived, so the fix is a parser
    change followed by a reprocess.
    """

    def __init__(self, message: str, target_id: str, snippet: bytes = b"") -> None:
        super().__init__(message)
        self.target_id = target_id
        # Kept short. Useful in the admin panel without dragging whole pages around.
        self.snippet = snippet[:500]


@dataclass
class ParseResult:
    observations: list[ParsedObservation] = field(default_factory=list)
    stations: list[ParsedStation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

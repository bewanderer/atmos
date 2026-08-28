"""Raw response archive.

The fetcher never interprets what it downloads. It stores the bytes and moves on.
Everything else in the system reads from here, which is what makes a broken parser
a recoverable problem rather than lost data.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

import boto3
from botocore.config import Config

from atmos.config import settings


@dataclass(frozen=True)
class ArchivedObject:
    storage_key: str
    sha256: bytes
    size: int


def _client():  # type: ignore[no-untyped-def]
    return boto3.client(
        "s3",
        endpoint_url=settings.archive_endpoint,
        aws_access_key_id=settings.archive_access_key,
        aws_secret_access_key=settings.archive_secret_key,
        config=Config(signature_version="s3v4", retries={"max_attempts": 5}),
    )


def build_key(source_slug: str, target_id: str, fetched_at: datetime, digest: bytes) -> str:
    """Date-partitioned key. The hash suffix keeps two fetches in the same second apart."""
    d = fetched_at.strftime("%Y/%m/%d")
    stamp = fetched_at.strftime("%H%M%S")
    return f"raw/{source_slug}/{d}/{target_id}/{stamp}-{digest[:6].hex()}"


def store(source_slug: str, target_id: str, fetched_at: datetime, body: bytes) -> ArchivedObject:
    """Write bytes to the archive and return what is needed for the fetch record."""
    digest = hashlib.sha256(body).digest()
    key = build_key(source_slug, target_id, fetched_at, digest)

    _client().put_object(
        Bucket=settings.archive_bucket,
        Key=key,
        Body=body,
        # Stored so the object can be integrity checked without our database.
        Metadata={"sha256": digest.hex(), "source": source_slug},
    )
    return ArchivedObject(storage_key=key, sha256=digest, size=len(body))


def load(storage_key: str) -> bytes:
    """Read bytes back for reprocessing."""
    obj = _client().get_object(Bucket=settings.archive_bucket, Key=storage_key)
    return obj["Body"].read()  # type: ignore[no-any-return]


def verify(storage_key: str, expected_sha256: bytes) -> bool:
    """Check an archived object still matches what we recorded when we fetched it."""
    return hashlib.sha256(load(storage_key)).digest() == expected_sha256

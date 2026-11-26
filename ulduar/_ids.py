"""Record identifiers: client-generated UUIDv7 (RFC 9562), the only id generator of the package.

A record's `event_at` is `uuid7_time(id)`; the Archivum's CHECK constraints hold the two equal.
`uuid.uuid7` is not used because it is absent before Python 3.14.
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import UTC, datetime, timedelta

UUID_VERSION = 7
_TIMESTAMP_BITS = 48
_VERSION_SHIFT = 76
_VARIANT_SHIFT = 62
_RANDOM_BITS = 74


def uuid7(unix_ms: int | None = None) -> uuid.UUID:
    """A UUIDv7: 48-bit Unix milliseconds, version 7, variant 10, 74 random bits from `os.urandom`."""
    if unix_ms is None:
        unix_ms = time.time_ns() // 1_000_000
    if not 0 <= unix_ms < 1 << _TIMESTAMP_BITS:
        raise ValueError(f"unix_ms {unix_ms} does not fit in {_TIMESTAMP_BITS} bits")
    entropy = int.from_bytes(os.urandom(10), "big") & ((1 << _RANDOM_BITS) - 1)
    rand_a, rand_b = entropy >> 62, entropy & ((1 << 62) - 1)
    value = (unix_ms << 80) | (UUID_VERSION << _VERSION_SHIFT) | (rand_a << 64) | (0b10 << _VARIANT_SHIFT) | rand_b
    return uuid.UUID(int=value)


def uuid7_time(value: uuid.UUID) -> datetime:
    """The UTC time (millisecond precision) a UUIDv7 carries; `ValueError` for any other version."""
    if value.version != UUID_VERSION:  # `version` is None for a non-RFC 4122 variant
        raise ValueError(f"{value} is not a version 7 UUID")
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=value.int >> 80)

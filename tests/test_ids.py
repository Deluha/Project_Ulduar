"""ulduar._ids: UUIDv7 layout, the millisecond round trip, ordering, and agreement with the server's reading."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import psycopg
import pytest

from ulduar._ids import uuid7, uuid7_time

SAMPLE_MS = 1_764_115_200_123  # 2025-11-26 00:00:00.123 UTC


def test_version_and_variant_bits() -> None:
    for _ in range(200):
        value = uuid7()
        assert value.version == 7
        assert value.variant == uuid.RFC_4122
        assert (value.int >> 62) & 0b11 == 0b10


def test_timestamp_round_trip_to_the_millisecond() -> None:
    value = uuid7(SAMPLE_MS)
    assert value.int >> 80 == SAMPLE_MS
    assert uuid7_time(value) == datetime(2025, 11, 26, 0, 0, 0, 123000, tzinfo=UTC)


def test_default_time_is_now() -> None:
    before = datetime.now(UTC)
    stamp = uuid7_time(uuid7())
    assert abs((stamp - before).total_seconds()) < 5


def test_ids_from_increasing_milliseconds_sort_in_order() -> None:
    ids = [uuid7(SAMPLE_MS + step) for step in (0, 1, 2, 1000, 86_400_000)]
    assert ids == sorted(ids)
    assert [i.bytes for i in ids] == sorted(i.bytes for i in ids)


def test_the_random_part_differs() -> None:
    assert len({uuid7(SAMPLE_MS) for _ in range(100)}) == 100


@pytest.mark.parametrize("bad", [-1, 1 << 48])
def test_timestamp_outside_48_bits_is_refused(bad: int) -> None:
    with pytest.raises(ValueError, match="48 bits"):
        uuid7(bad)


@pytest.mark.parametrize("value", [uuid.UUID(int=0), uuid.uuid4(), uuid.uuid1()])
def test_time_of_another_version_is_refused(value: uuid.UUID) -> None:
    with pytest.raises(ValueError, match="not a version 7"):
        uuid7_time(value)


def test_server_reads_the_same_time_and_version(db_conn: psycopg.Connection) -> None:
    value = uuid7(SAMPLE_MS)
    stamp, version = db_conn.execute(
        "SELECT uuid_extract_timestamp(%s::uuid), uuid_extract_version(%s::uuid)", (value, value)
    ).fetchone() or (None, None)
    assert stamp == uuid7_time(value)
    assert version == 7

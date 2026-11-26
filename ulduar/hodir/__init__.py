"""Hodir, the cold archive: immutable, content-addressed storage of files, dumps, reports and models.

The facade of the component: `open_archive` yields an `Archive` that stores a file or a stream (`put_file`,
`put_stream` with its `PutSink`), gets one back verified (`get`, `get_stream`) and re-verifies the archive
(`verify`, returning a `Verification` of `Finding`s). Retention is two calls on a `Config`: `declare_retention`
(ledger role) appends an `ArchiveRetentionRule` and `apply_retention` lists, or deletes, the objects whose every
registration has expired (a `RetentionRun` of `DueAddress`es, `OpenIntent`s and `DeletionOutcome`s). More calls
arrive in later Phase 2 commits. The storage interface and its two backends (a local directory and an
S3-compatible bucket, chosen by the `[archive]` section of the configuration), the object layout, Hodir's SQL and
the deletion protocol are private.
"""

from __future__ import annotations

from ulduar.hodir._records import (
    ArchiveRetentionRule,
    DeletionOutcome,
    DueAddress,
    Finding,
    OpenIntent,
    RetentionRun,
    Verification,
)
from ulduar.hodir.archive import Archive, PutSink, open_archive
from ulduar.hodir.retention import apply_retention, declare_retention

__all__ = [
    "Archive",
    "ArchiveRetentionRule",
    "DeletionOutcome",
    "DueAddress",
    "Finding",
    "OpenIntent",
    "PutSink",
    "RetentionRun",
    "Verification",
    "apply_retention",
    "declare_retention",
    "open_archive",
]

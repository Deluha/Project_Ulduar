"""Hodir, the cold archive: immutable, content-addressed storage of files, dumps, reports and models.

The facade of the component: `open_archive` yields an `Archive` that stores a file or a stream (`put_file`,
`put_stream` with its `PutSink`), gets one back verified (`get`, `get_stream`), re-verifies the archive (`verify`,
returning a `Verification` of `Finding`s), compares it with the Archivum (`reconcile`, a `Reconciliation`) and
restores a lost Archivum from the store (`rebuild`, a `Rebuild`). Retention is two calls on a `Config`:
`declare_retention` (ledger role) appends an `ArchiveRetentionRule` and `apply_retention` lists, or deletes, the
objects whose every registration has expired (a `RetentionRun` of `DueAddress`es, `OpenIntent`s and
`DeletionOutcome`s). `clean_incomplete` is the built-in rule for what an interrupted put left behind (a
`CleanupRun`). The storage interface and its two backends (a local directory and an S3-compatible bucket, chosen by
the `[archive]` section of the configuration), the object layout, Hodir's SQL and the deletion protocol are private.
"""

from __future__ import annotations

from ulduar.hodir._records import (
    Adoption,
    ArchiveRetentionRule,
    CleanupRun,
    DeletionOutcome,
    DueAddress,
    Finding,
    OpenIntent,
    Orphan,
    Rebuild,
    Reconciliation,
    RetentionRun,
    Verification,
)
from ulduar.hodir._store import UnfinishedUpload
from ulduar.hodir.archive import Archive, PutSink, open_archive
from ulduar.hodir.cleanup import clean_incomplete
from ulduar.hodir.retention import apply_retention, declare_retention

__all__ = [
    "Adoption",
    "Archive",
    "ArchiveRetentionRule",
    "CleanupRun",
    "DeletionOutcome",
    "DueAddress",
    "Finding",
    "OpenIntent",
    "Orphan",
    "PutSink",
    "Rebuild",
    "Reconciliation",
    "RetentionRun",
    "UnfinishedUpload",
    "Verification",
    "apply_retention",
    "clean_incomplete",
    "declare_retention",
    "open_archive",
]

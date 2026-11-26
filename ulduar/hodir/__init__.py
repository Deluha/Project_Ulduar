"""Hodir, the cold archive: immutable, content-addressed storage of files, dumps, reports and models.

The facade of the component: `open_archive` yields an `Archive` that stores a file or a stream (`put_file`,
`put_stream` with its `PutSink`). More calls arrive in later Phase 2 commits. The storage interface and its two
backends (a local directory and an S3-compatible bucket, chosen by the `[archive]` section of the configuration),
the object layout and Hodir's SQL are private.
"""

from __future__ import annotations

from ulduar.hodir.archive import Archive, PutSink, open_archive

__all__ = ["Archive", "PutSink", "open_archive"]

"""The record sweep: copy every maintenance record the Archivum holds to the archive store, once each.

Input: a catalog session and a writing store. Output: `sync_records`, which lists `<project>/_records` once and writes,
with `put_bytes_new`, every deletion, redaction and history-retention action whose key is absent (H4). The Archivum
is the authority and the store its copy for a rebuild (D32), so the sweep never fails a call: a failure is logged as a
warning, and the next call writes what is missing. Private: Hodir's calls that maintain the archive end with it.
"""

from __future__ import annotations

import logging

from ulduar.errors import UlduarError
from ulduar.freya import Catalog
from ulduar.hodir import _archivum, _layout
from ulduar.hodir._store import WritingStore

logger = logging.getLogger(__name__)


def sync_records(catalog: Catalog, store: WritingStore) -> int:
    """Write the records the store lacks; return how many were written. A store or Archivum failure is logged."""
    project = catalog.project
    written = 0
    try:
        present = {stored.key for stored in store.list(f"{project.name}/_records")}
        records = catalog.transaction(
            "Reading the maintenance records to copy to the archive",
            lambda tx: _archivum.maintenance_records(tx, project),
        )
        for record in records:
            key = _layout.record_key(project.name, record.identifier)
            if key not in present and store.put_bytes_new(key, record.encode()):
                written += 1
    except UlduarError as e:
        logger.warning("the maintenance records were not all copied to the archive: %s", e)
    if written:
        logger.info("copied %d maintenance record(s) to the archive", written)
    return written

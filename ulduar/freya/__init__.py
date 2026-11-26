"""Freya: the catalog and lineage of a project's data, kept in the Archivum (PostgreSQL).

This facade only re-exports the public surface; the code sits in the modules beside it: the schema ledger
(`install`, `schema_status`), the catalog session (`open_catalog`, the records, lineage edges and walks) and
the ledger-role calls on history (onboarding, retention, redaction and the re-application of removals).
`ledger_transaction` is the hook through which Ulduar's components run their own ledger-role calls, the
counterpart of `Catalog.transaction`.
"""

from ulduar.freya._records import (
    Artifact,
    Connection,
    Dataset,
    LineageEdge,
    LineageNode,
    NodeRef,
    Project,
    RetentionAction,
)
from ulduar.freya.catalog import ARTIFACT_KINDS, Catalog, open_catalog
from ulduar.freya.history import (
    ReapplyResult,
    Redaction,
    RetentionResult,
    RetentionRule,
    apply_history_retention,
    declare_history_retention,
    onboard_project,
    reapply_removals,
    redact,
)
from ulduar.freya.ledger import SchemaStatus, install, ledger_transaction, schema_status

__all__ = [
    "ARTIFACT_KINDS",
    "Artifact",
    "Catalog",
    "Connection",
    "Dataset",
    "LineageEdge",
    "LineageNode",
    "NodeRef",
    "Project",
    "ReapplyResult",
    "Redaction",
    "RetentionAction",
    "RetentionResult",
    "RetentionRule",
    "SchemaStatus",
    "apply_history_retention",
    "declare_history_retention",
    "install",
    "ledger_transaction",
    "onboard_project",
    "open_catalog",
    "reapply_removals",
    "redact",
    "schema_status",
]

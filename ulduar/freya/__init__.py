"""Freya: the catalog and lineage of a project's data, kept in the Archivum (PostgreSQL).

This facade only re-exports the public surface; the code sits in the modules beside it: the schema ledger
(`install`, `schema_status`), the catalog session (`open_catalog`, the records) and the ledger-role calls on
history (onboarding, retention, redaction). The lineage calls arrive in the next commit of Phase 1.
"""

from ulduar.freya.catalog import ARTIFACT_KINDS, Artifact, Catalog, Dataset, NodeRef, Project, open_catalog
from ulduar.freya.history import (
    Redaction,
    RetentionResult,
    RetentionRule,
    apply_history_retention,
    declare_history_retention,
    onboard_project,
    redact,
)
from ulduar.freya.ledger import SchemaStatus, Step, install, load_steps, schema_status

__all__ = [
    "ARTIFACT_KINDS",
    "Artifact",
    "Catalog",
    "Dataset",
    "NodeRef",
    "Project",
    "Redaction",
    "RetentionResult",
    "RetentionRule",
    "SchemaStatus",
    "Step",
    "apply_history_retention",
    "declare_history_retention",
    "install",
    "load_steps",
    "onboard_project",
    "open_catalog",
    "redact",
    "schema_status",
]

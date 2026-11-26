"""Freya: the catalog and lineage of a project's data, kept in the Archivum (PostgreSQL).

This facade only re-exports the public surface; the code sits in the modules beside it: the schema ledger
(`install`, `schema_status`), the catalog session (`open_catalog`, the records, lineage edges and walks) and
the ledger-role calls on history (onboarding, retention, redaction).
"""

from ulduar.freya._records import Artifact, Connection, Dataset, LineageEdge, LineageNode, NodeRef, Project
from ulduar.freya.catalog import ARTIFACT_KINDS, Catalog, open_catalog
from ulduar.freya.history import (
    Redaction,
    RetentionResult,
    RetentionRule,
    apply_history_retention,
    declare_history_retention,
    onboard_project,
    redact,
)
from ulduar.freya.ledger import SchemaStatus, install, schema_status

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
    "Redaction",
    "RetentionResult",
    "RetentionRule",
    "SchemaStatus",
    "apply_history_retention",
    "declare_history_retention",
    "install",
    "onboard_project",
    "open_catalog",
    "redact",
    "schema_status",
]

"""The records Hodir returns: what a verification found, object by object.

Input: nothing; these are plain values. Output: `Finding`, `Verification` and the outcome vocabulary
`CHECK_OUTCOMES`, which equals the list the Archivum's `archive_finding` table accepts (a test holds the two
equal). Private module; the facade `ulduar.hodir` re-exports the two records.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

PASSED = "passed"
CHECK_OUTCOMES = (
    PASSED,
    "mismatch",
    "missing",
    "unreadable",
    "not_immediate",
    "object_without_entry",
    "entry_without_object",
    "unfinished_upload",
)


@dataclass(frozen=True, slots=True)
class Finding:
    """What a check found for one object: its address in *scope* and an outcome from `CHECK_OUTCOMES`.

    *checksum* and *size* are the recorded ones (None when the finding names no content, as a stray temporary file).
    """

    finding_id: UUID
    scope: str
    checksum: bytes | None
    size: int | None
    outcome: str


@dataclass(frozen=True, slots=True)
class Verification:
    """One verification of a scope: the check's id and time, its mode, how many objects it judged, and each finding."""

    check_id: UUID
    scope: str
    mode: Literal["full", "sample"]
    objects_checked: int
    findings: tuple[Finding, ...]
    event_at: datetime

    @property
    def failed(self) -> tuple[Finding, ...]:
        """The findings whose outcome is not `passed`."""
        return tuple(finding for finding in self.findings if finding.outcome != PASSED)

"""The records Hodir returns: what a check found, a retention rule, what retention, a cleanup or a rebuild did.

Input: nothing; these are plain values. Output: `Finding`, `Verification`, `Reconciliation` and the outcome
vocabulary `CHECK_OUTCOMES`, which equals the list the Archivum's `archive_finding` table accepts (a test holds the
two equal); `ArchiveRetentionRule`, `DueAddress`, `OpenIntent`, `DeletionOutcome` and `RetentionRun`; `Orphan`,
`Adoption`, `UnfinishedUpload` and `CleanupRun` (the built-in incomplete-put rule); `Rebuild`; and the two rows
retention reads, `LiveRegistration` and `LatestRule`. Private module; the facade `ulduar.hodir` re-exports the
public records.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, NamedTuple
from uuid import UUID

from ulduar.freya import Artifact, Redaction, RetentionAction

Stage = Literal["confirmed", "cancelled", "open", "skipped"]
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


@dataclass(frozen=True, slots=True)
class Reconciliation:
    """One reconciliation of a scope: the store's objects against the Archivum's live addresses, and what differed.

    *objects_listed* is how many objects the store holds under the scope's object prefix; *skipped_young* how many
    objects and unfinished uploads were not judged because they are younger than the grace period; *findings* are
    the disagreements only (outcomes `object_without_entry`, `entry_without_object`, `unfinished_upload`);
    *skipped_busy* how many live addresses missing from the listing were left unchecked because their address lock
    was held too long (the grace-free re-check could not be made; none of them is a finding).
    """

    check_id: UUID
    scope: str
    objects_listed: int
    skipped_young: int
    findings: tuple[Finding, ...]
    event_at: datetime
    skipped_busy: int = 0


@dataclass(frozen=True, slots=True)
class ArchiveRetentionRule:
    """A declared retention rule: objects of *retention_class* in *project* are kept *keep_days* days (None: for ever).

    *keep_newest* is how many of a dataset's newest live registrations age alone never deletes;
    *shortening_confirmed* records that the rule kept less than the one before it and the caller said so.
    """

    rule_id: UUID
    project: str
    retention_class: str
    keep_days: int | None
    keep_newest: int
    shortening_confirmed: bool
    event_at: datetime


class LatestRule(NamedTuple):
    """The latest rule of one retention class, as retention reads it."""

    rule_id: UUID
    keep_days: int | None
    keep_newest: int


class LiveRegistration(NamedTuple):
    """One registration whose artifact is still archived; *recorded_at* is the database's clock, retention's age."""

    scope: str
    checksum: bytes
    size: int
    artifact_id: UUID
    dataset_id: UUID | None
    retention_class: str | None
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class DueAddress:
    """An object (*scope*, *checksum*) every registration of which has expired.

    *rule_id* is the rule whose expiry is the latest, the binding one; *artifact_ids* are the registrations.
    """

    scope: str
    checksum: bytes
    size: int
    rule_id: UUID
    artifact_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class OpenIntent:
    """An intent to delete the object (*scope*, *checksum*) that no outcome settled; *rule_id* is the rule applied."""

    deletion_id: UUID
    scope: str
    checksum: bytes
    size: int
    rule_id: UUID


@dataclass(frozen=True, slots=True)
class DeletionOutcome:
    """What became of one deletion; *deletion_id* is the intent's (None when skipped).

    `confirmed`; `cancelled` (a put relied on the object); `open` (the intent stays open: a version remains or the
    store refused the delete); `skipped` (not due any more, or settled by another run). *detail* says why for every
    stage but `confirmed`.
    """

    scope: str
    checksum: bytes
    size: int
    deletion_id: UUID | None
    stage: Stage
    cause: str
    detail: str | None


@dataclass(frozen=True, slots=True)
class RetentionRun:
    """One application of retention: what was due and, for a real run, the outcome of each deletion.

    *open_intents* are the retention intents a failed run left open whose objects are not due now; a real run
    passes each through the deletion protocol (its `still_due` cancels it), a dry run only lists them. Their
    outcomes are in *outcomes*, ahead of the due objects'.
    """

    dry_run: bool
    due: tuple[DueAddress, ...]
    outcomes: tuple[DeletionOutcome, ...]
    open_intents: tuple[OpenIntent, ...] = ()


@dataclass(frozen=True, slots=True)
class Orphan:
    """An object (*scope*, *checksum*) of *size* bytes that no live registration names."""

    scope: str
    checksum: bytes
    size: int


@dataclass(frozen=True, slots=True)
class Adoption:
    """A manifest without a registration whose object exists: the artifact the cleanup registers (or did)."""

    artifact_id: UUID
    scope: str
    checksum: bytes
    size: int


@dataclass(frozen=True, slots=True)
class UnfinishedUpload:
    """What an interrupted put left behind: *key* names the object it was storing, *started* is when it began."""

    key: str
    started: datetime


@dataclass(frozen=True, slots=True)
class CleanupRun:
    """One run of the built-in incomplete-put rule over one or every scope, with what it found and (real run) did.

    The listing fields are filled by a dry run too: *adopt* are the manifests without a registration whose object
    exists, past the grace period; *remove* the objects without an entry or a manifest, past the grace period;
    *abort* the unfinished uploads past it; *open_intents* the incomplete-put deletions a failed run left open for
    objects not in *remove*. A real run fills the rest: *adopted* are the artifacts registered (an artifact the
    catalog already held is returned as it is, its origin kept), *outcomes* the deletions, open intents first;
    *datasets_missing* counts the adopted artifacts whose dataset the Archivum does not hold, registered without one.
    *stale_manifests* (listed by a dry run too, deleted by a real run) are the keys of the manifests, past the grace
    period, of artifacts registered whose object a confirmed deletion removed: leftovers of that deletion.
    """

    dry_run: bool
    adopt: tuple[Adoption, ...]
    remove: tuple[Orphan, ...]
    abort: tuple[UnfinishedUpload, ...]
    open_intents: tuple[Orphan, ...] = ()
    outcomes: tuple[DeletionOutcome, ...] = ()
    adopted: tuple[Artifact, ...] = ()
    datasets_missing: int = 0
    stale_manifests: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Rebuild:
    """What a rebuild put back into the Archivum from the store.

    *deletions_restored* are the deletion records the Archivum lacked; *adopted* the artifacts registered again
    from manifests under their original ids; *datasets_missing* those registered without the dataset their
    manifest names, which the Archivum does not hold; *removals* the redactions and history-retention actions the
    store holds, in event order, for `freya.reapply_removals` (the recording role cannot apply them).
    """

    deletions_restored: int
    adopted: tuple[Artifact, ...]
    datasets_missing: int
    removals: tuple[Redaction | RetentionAction, ...]

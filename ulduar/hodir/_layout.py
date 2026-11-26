"""The archive's object layout: every key, the format marker and the manifest, defined once.

Input: project and scope names (`check_name`), checksums, record ids. Output: the store keys of an object, of a
manifest and of a record; the bytes of the format marker, of a manifest and of a deletion record, and their strict
readers. Private:
Hodir's modules import it, a consumer never does. A manifest carries no origin and no dataset name or namespace
(F18), and no error built here quotes a stored payload (hard rule 11).

Layout (`LAYOUT_TEXT` is the copy stored beside the marker): fixed segments start with `_`, which no scope can,
so later scope levels fit between `<project>/` and `/_objects` without moving a v0.1 key.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TypeVar
from uuid import UUID

from ulduar import _ids
from ulduar.config import NAME_PATTERN, check_name
from ulduar.errors import ArchiveFormatError

FORMAT_MARKER_KEY = "ulduar-archive.json"
FORMAT_NAME = "ulduar-archive"
FORMAT_VERSION = 1
LAYOUT_KEY = "LAYOUT.txt"
MANIFEST_FORMAT = 1
RECORD_FORMAT = 1
DELETION_TYPE = "archive_deletion"
CHECKSUM_PREFIX = "sha256:"
LAYOUT_TEXT = """\
This is an Ulduar archive (format version 1). Do not change, rename or delete anything in it by hand.

<project>/<scope>/_objects/sha256/<xx>/<hash>
    The stored bytes of one object, named by the SHA-256 of those bytes in lower-case hex (<xx> is its first two
    characters). Written once, never overwritten.
<project>/<scope>/_manifests/<yyyy>/<mm>/<artifact id>.json
    One JSON file per registration of an object: artifact id, time, project, scope, kind, checksum, size,
    dataset id and retention class. <yyyy>/<mm> is the month (UTC) inside the artifact id.
<project>/_records/<yyyy>/<mm>/<record id>.json
    One JSON file per maintenance record (deletions, purges, retention actions, redactions).
ulduar-archive.json
    The format marker: {"format":"ulduar-archive","version":1}.
"""

_MANIFEST_FIELDS = (
    "format",
    "artifact_id",
    "event_at",
    "project",
    "scope",
    "kind",
    "checksum",
    "size",
    "dataset_id",
    "retention_class",
)
_DELETION_FIELDS = (
    "format",
    "type",
    "deletion_id",
    "event_at",
    "recorded_at",
    "project",
    "scope",
    "checksum",
    "size",
    "stage",
    "intent_id",
    "cause",
    "rule_id",
    "reason",
)
DELETION_STAGES = ("intent", "confirmed", "cancelled")
DELETION_CAUSES = ("retention", "incomplete_put", "purge")
PURGE_REASONS = ("personal_data", "secret", "recorded_by_mistake")
T = TypeVar("T")
_HEX = re.compile(r"[0-9a-f]{64}")
_EVENT_AT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")
_OBJECT_TAIL = re.compile(r"(?P<scope>[^/]+)/_objects/sha256/(?P<head>[0-9a-f]{2})/(?P<hash>[0-9a-f]{64})")


def _canonical(value: dict[str, object]) -> bytes:
    """Sorted keys, no spaces, one trailing newline: the same value is always the same bytes."""
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")


def _format_time(moment: datetime) -> str:
    """ISO 8601 UTC with milliseconds and `Z`."""
    utc = moment.astimezone(UTC)
    return f"{utc:%Y-%m-%dT%H:%M:%S}.{utc.microsecond // 1000:03d}Z"


def _load_fields(data: bytes, names: tuple[str, ...], noun: str, version: int) -> dict[str, object]:
    """The JSON object in *data* when it has exactly *names* and `format` *version*; `ArchiveFormatError` otherwise."""
    try:
        raw = json.loads(data)
    except ValueError:
        raise ArchiveFormatError(f"The {noun} is not valid JSON") from None
    if not isinstance(raw, dict):
        raise ArchiveFormatError(f"The {noun} is not a JSON object")
    if len(raw) != len(names) or any(name not in raw for name in names):
        raise ArchiveFormatError(f"The {noun} lacks a field or holds one this library does not know")
    if type(raw["format"]) is not int or raw["format"] != version:
        raise ArchiveFormatError(f"The {noun} is not format {version}")
    return {name: raw[name] for name in names}


def objects_prefix(project: str, scope: str) -> str:
    """The prefix under which a scope's objects sit."""
    return f"{check_name(project, 'project')}/{check_name(scope, 'scope')}/_objects/sha256"


def object_key(project: str, scope: str, checksum: bytes) -> str:
    """The key of the object whose SHA-256 is *checksum*."""
    digest = checksum.hex()
    return f"{objects_prefix(project, scope)}/{digest[:2]}/{digest}"


def parse_object_key(key: str) -> tuple[str, bytes] | None:
    """The (scope, checksum) of a project-relative object key `<scope>/_objects/sha256/<xx>/<hash>`, or None.

    The inverse of `object_key` below the project; anything else, a mismatched `<xx>` included, is None.
    """
    match = _OBJECT_TAIL.fullmatch(key)
    if match is None or not NAME_PATTERN.fullmatch(match["scope"]) or match["hash"][:2] != match["head"]:
        return None
    return match["scope"], bytes.fromhex(match["hash"])


def manifests_prefix(project: str, scope: str) -> str:
    """The prefix under which a scope's manifests sit."""
    return f"{check_name(project, 'project')}/{check_name(scope, 'scope')}/_manifests"


def _month_directory(record_id: UUID) -> str:
    moment = _ids.uuid7_time(record_id).astimezone(UTC)
    return f"{moment.year:04d}/{moment.month:02d}"


def manifest_key(project: str, scope: str, artifact_id: UUID) -> str:
    """The key of an artifact's manifest, in the month its id carries (UTC)."""
    return f"{manifests_prefix(project, scope)}/{_month_directory(artifact_id)}/{artifact_id}.json"


def record_key(project: str, record_id: UUID) -> str:
    """The key of a maintenance record, in the month its id carries (UTC)."""
    return f"{check_name(project, 'project')}/_records/{_month_directory(record_id)}/{record_id}.json"


def marker_bytes() -> bytes:
    """The format marker's content."""
    return _canonical({"format": FORMAT_NAME, "version": FORMAT_VERSION})


def check_marker(data: bytes) -> None:
    """Accept the marker of this library's format and version; `ArchiveFormatError` for any other."""
    try:
        marker = json.loads(data)
    except ValueError:
        marker = None
    if not isinstance(marker, dict) or marker.get("format") != FORMAT_NAME:
        raise ArchiveFormatError("The archive root holds a foreign format marker")
    version = marker.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise ArchiveFormatError("The archive root holds a foreign format marker")
    if version != FORMAT_VERSION:
        raise ArchiveFormatError(
            f"The archive has format version {version}; this library knows version {FORMAT_VERSION}"
        )


@dataclass(frozen=True, slots=True)
class Manifest:
    """One registration of an object, as stored beside it: what a rebuild needs to register the artifact again."""

    artifact_id: UUID
    event_at: datetime
    project: str
    scope: str
    kind: str
    checksum: bytes
    size: int
    dataset_id: UUID | None
    retention_class: str | None

    def encode(self) -> bytes:
        """The canonical JSON of the manifest (format `MANIFEST_FORMAT`)."""
        return _canonical(
            {
                "format": MANIFEST_FORMAT,
                "artifact_id": str(self.artifact_id),
                "event_at": _format_time(self.event_at),
                "project": self.project,
                "scope": self.scope,
                "kind": self.kind,
                "checksum": CHECKSUM_PREFIX + self.checksum.hex(),
                "size": self.size,
                "dataset_id": None if self.dataset_id is None else str(self.dataset_id),
                "retention_class": self.retention_class,
            }
        )

    @classmethod
    def decode(cls, data: bytes) -> Manifest:
        """The manifest in *data*; `ArchiveFormatError` naming the problem for anything but a format-1 manifest."""
        fields = _load_fields(data, _MANIFEST_FIELDS, "manifest", MANIFEST_FORMAT)
        size = fields["size"]
        if type(size) is not int or size < 0:
            raise ArchiveFormatError("The manifest field `size` must be a non-negative integer")
        try:
            return cls(
                artifact_id=_uuid7(fields["artifact_id"], "artifact_id"),
                event_at=_event_at(fields["event_at"], "event_at"),
                project=_name(fields["project"], "project"),
                scope=_name(fields["scope"], "scope"),
                kind=_text(fields["kind"], "kind"),
                checksum=_checksum(fields["checksum"]),
                size=size,
                dataset_id=_optional(fields["dataset_id"], lambda v: _uuid7(v, "dataset_id")),
                retention_class=_optional(fields["retention_class"], lambda v: _name(v, "retention_class")),
            )
        except ValueError as e:
            raise ArchiveFormatError(f"The manifest field {e}") from None


@dataclass(frozen=True, slots=True)
class DeletionRecord:
    """One row of `archive_deletion` as stored in `_records/`: an intent, or the outcome of one.

    *project* is the project's name, not its id (a rebuild maps names); *intent_id* is None for an intent and
    *rule_id* is set exactly for a retention deletion, *reason* exactly for a purge.
    """

    deletion_id: UUID
    event_at: datetime
    recorded_at: datetime
    project: str
    scope: str
    checksum: bytes
    size: int
    stage: str
    intent_id: UUID | None
    cause: str
    rule_id: UUID | None
    reason: str | None

    def encode(self) -> bytes:
        """The canonical JSON of the record (format `RECORD_FORMAT`, type `archive_deletion`)."""
        return _canonical(
            {
                "format": RECORD_FORMAT,
                "type": DELETION_TYPE,
                "deletion_id": str(self.deletion_id),
                "event_at": _format_time(self.event_at),
                "recorded_at": _format_time(self.recorded_at),
                "project": self.project,
                "scope": self.scope,
                "checksum": CHECKSUM_PREFIX + self.checksum.hex(),
                "size": self.size,
                "stage": self.stage,
                "intent_id": None if self.intent_id is None else str(self.intent_id),
                "cause": self.cause,
                "rule_id": None if self.rule_id is None else str(self.rule_id),
                "reason": self.reason,
            }
        )

    @classmethod
    def decode(cls, data: bytes) -> DeletionRecord:
        """The record in *data*; `ArchiveFormatError` naming the problem for anything but a format-1 deletion record."""
        fields = _load_fields(data, _DELETION_FIELDS, "deletion record", RECORD_FORMAT)
        if fields["type"] != DELETION_TYPE:
            raise ArchiveFormatError(f"The record is not of type {DELETION_TYPE}")
        size = fields["size"]
        if type(size) is not int or size < 0:
            raise ArchiveFormatError("The deletion record field `size` must be a non-negative integer")
        try:
            record = cls(
                deletion_id=_uuid7(fields["deletion_id"], "deletion_id"),
                event_at=_event_at(fields["event_at"], "event_at"),
                recorded_at=_event_at(fields["recorded_at"], "recorded_at"),
                project=_name(fields["project"], "project"),
                scope=_name(fields["scope"], "scope"),
                checksum=_checksum(fields["checksum"]),
                size=size,
                stage=_choice(fields["stage"], "stage", DELETION_STAGES),
                intent_id=_optional(fields["intent_id"], lambda v: _uuid7(v, "intent_id")),
                cause=_choice(fields["cause"], "cause", DELETION_CAUSES),
                rule_id=_optional(fields["rule_id"], lambda v: _uuid7(v, "rule_id")),
                reason=_optional(fields["reason"], lambda v: _choice(v, "reason", PURGE_REASONS)),
            )
        except ValueError as e:
            raise ArchiveFormatError(f"The deletion record field {e}") from None
        if (
            (record.stage == "intent") != (record.intent_id is None)
            or (record.cause == "retention") != (record.rule_id is not None)
            or (record.cause == "purge") != (record.reason is not None)
        ):
            raise ArchiveFormatError("The deletion record's stage, cause, intent, rule and reason do not agree")
        return record


def _bad(field: str, wanted: str) -> ValueError:
    """The problem of one stored field, worded for `The <manifest or record> field ...`; the value is never quoted."""
    return ValueError(f"`{field}` must be {wanted}")


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise _bad(field, "a non-empty string")
    return value


def _name(value: object, field: str) -> str:
    if not isinstance(value, str) or not NAME_PATTERN.fullmatch(value):
        raise _bad(field, "a name (lower-case letters, digits, `.`, `_`, `-`)")
    return value


def _uuid7(value: object, field: str) -> UUID:
    try:
        identifier = UUID(_text(value, field))
        _ids.uuid7_time(identifier)
    except ValueError:
        raise _bad(field, "a version 7 UUID") from None
    return identifier


def _event_at(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not _EVENT_AT.fullmatch(value):
        raise _bad(field, "an ISO 8601 UTC time with milliseconds and `Z`")
    try:
        return datetime.strptime(value[:-1], "%Y-%m-%dT%H:%M:%S.%f").replace(tzinfo=UTC)
    except ValueError:
        raise _bad(field, "an ISO 8601 UTC time with milliseconds and `Z`") from None


def _choice(value: object, field: str, allowed: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise _bad(field, f"one of {', '.join(allowed)}")
    return value


def _optional(value: object, read: Callable[[object], T]) -> T | None:
    """None stays None; anything else is read by *read*."""
    return None if value is None else read(value)


def _checksum(value: object) -> bytes:
    if (
        not isinstance(value, str)
        or not value.startswith(CHECKSUM_PREFIX)
        or not _HEX.fullmatch(value[len(CHECKSUM_PREFIX) :])
    ):
        raise _bad("checksum", "`sha256:` and 64 lower-case hex digits")
    return bytes.fromhex(value[len(CHECKSUM_PREFIX) :])

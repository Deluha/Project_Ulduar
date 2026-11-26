"""The archive's object layout: every key, the format marker and the manifest, defined once.

Input: project and scope names (`check_name`), checksums, record ids. Output: the store keys of an object, of a
manifest and of a record (and the inverse reading of a listed key); the bytes of the format marker, of a manifest
and of the three maintenance records (a deletion, a redaction, a history-retention action), and their strict
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
from typing import TypeVar, cast
from uuid import UUID

from ulduar import _ids
from ulduar._schema import (
    ARTIFACT_KINDS,
    HISTORIES,
    REDACTABLE_FIELDS,
    REDACTION_REASONS,
    RedactionHistory,
    RedactionReason,
    RetentionHistory,
)
from ulduar.config import NAME_PATTERN, check_name
from ulduar.errors import ArchiveFormatError

FORMAT_MARKER_KEY = "ulduar-archive.json"
FORMAT_NAME = "ulduar-archive"
FORMAT_VERSION = 1
LAYOUT_KEY = "LAYOUT.txt"
MANIFEST_FORMAT = 1
RECORD_FORMAT = 1
DELETION_TYPE = "archive_deletion"
REDACTION_TYPE = "redaction"
RETENTION_ACTION_TYPE = "history_retention_action"
CHECKSUM_PREFIX = "sha256:"
# How many hex digits of a checksum a log line shows.
_SHORT_CHECKSUM_HEX = 12
LAYOUT_TEXT = """\
This is an Ulduar archive (format version 1). Do not change, rename or delete anything in it by hand.

<project>/<scope>/_objects/sha256/<xx>/<hash>
    The stored bytes of one object, named by the SHA-256 of those bytes in lower-case hex (<xx> is its first two
    characters). Written once, never overwritten.
<project>/<scope>/_manifests/<yyyy>/<mm>/<artifact id>.json
    One JSON file per registration of an object: artifact id, time, project, scope, kind, checksum, size,
    dataset id and retention class. <yyyy>/<mm> is the month (UTC) inside the artifact id.
<project>/_records/<yyyy>/<mm>/<record id>.json
    One JSON file per maintenance record (deletions, purges, history-retention actions, redactions).
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
_REDACTION_FIELDS = (
    "format",
    "type",
    "redaction_id",
    "event_at",
    "project",
    "history",
    "record_id",
    "field",
    "reason",
)
_RETENTION_ACTION_FIELDS = (
    "format",
    "type",
    "action_id",
    "event_at",
    "project",
    "history",
    "rule_id",
    "keep_days",
    "rule_event_at",
    "cutoff",
    "rows_removed",
)
DELETION_STAGES = ("intent", "confirmed", "cancelled")
DELETION_CAUSES = ("retention", "incomplete_put", "purge")
T = TypeVar("T")
_HEX = re.compile(r"[0-9a-f]{64}")
_EVENT_AT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")
_MICROSECONDS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z")
_OBJECT_TAIL = re.compile(r"(?P<scope>[^/]+)/_objects/sha256/(?P<head>[0-9a-f]{2})/(?P<hash>[0-9a-f]{64})")
_MANIFEST_TAIL = re.compile(r"(?P<scope>[^/]+)/_manifests/\d{4}/\d{2}/(?P<id>[0-9a-f-]{36})\.json")
_RECORD_TAIL = re.compile(r"_records/\d{4}/\d{2}/(?P<id>[0-9a-f-]{36})\.json")


def _canonical(value: dict[str, object]) -> bytes:
    """Sorted keys, no spaces, one trailing newline: the same value is always the same bytes."""
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")


def _format_time(moment: datetime, *, micro: bool = False) -> str:
    """ISO 8601 UTC with milliseconds (microseconds when *micro*: a cutoff must not lose its last digits) and `Z`."""
    utc = moment.astimezone(UTC)
    fraction = f"{utc.microsecond:06d}" if micro else f"{utc.microsecond // 1000:03d}"
    return f"{utc:%Y-%m-%dT%H:%M:%S}.{fraction}Z"


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


def short_checksum(checksum: bytes) -> str:
    """The first hex digits of *checksum*: all a log line shows of one."""
    return checksum.hex()[:_SHORT_CHECKSUM_HEX]


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


def _parse_uuid7(text: str) -> UUID | None:
    try:
        identifier = UUID(text)
        _ids.uuid7_time(identifier)
    except ValueError:
        return None
    return identifier


def parse_manifest_key(key: str) -> tuple[str, UUID] | None:
    """The (scope, artifact id) of a project-relative manifest key `<scope>/_manifests/<yyyy>/<mm>/<id>.json`, or None.

    The inverse of `manifest_key` below the project; a key in the wrong month's directory is None.
    """
    match = _MANIFEST_TAIL.fullmatch(key)
    artifact_id = None if match is None else _parse_uuid7(match["id"])
    if match is None or artifact_id is None or not NAME_PATTERN.fullmatch(match["scope"]):
        return None
    expected = f"{match['scope']}/_manifests/{_month_directory(artifact_id)}/{artifact_id}.json"
    return (match["scope"], artifact_id) if key == expected else None


def is_since(record_id: UUID, since: datetime | None) -> bool:
    """True when the month inside *record_id* is not before the month of *since* (UTC); always True for None."""
    if since is None:
        return True
    moment, limit = _ids.uuid7_time(record_id).astimezone(UTC), since.astimezone(UTC)
    return (moment.year, moment.month) >= (limit.year, limit.month)


def parse_record_key(key: str) -> UUID | None:
    """The record id of a project-relative record key `_records/<yyyy>/<mm>/<id>.json`, or None."""
    match = _RECORD_TAIL.fullmatch(key)
    record_id = None if match is None else _parse_uuid7(match["id"])
    if record_id is None or key != f"_records/{_month_directory(record_id)}/{record_id}.json":
        return None
    return record_id


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
                kind=_choice(fields["kind"], "kind", ARTIFACT_KINDS),
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

    @property
    def identifier(self) -> UUID:
        """The record's own id, the one its key in `_records/` carries."""
        return self.deletion_id

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
        _check_type(fields, DELETION_TYPE)
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
                reason=_optional(fields["reason"], lambda v: _choice(v, "reason", REDACTION_REASONS)),
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


@dataclass(frozen=True, slots=True)
class RedactionRecord:
    """One row of `redaction` as stored in `_records/`: which field of which record was redacted, and why.

    *project* is the project's name. The record holds no value: a redaction never carries what it removed.
    """

    redaction_id: UUID
    event_at: datetime
    project: str
    history: RedactionHistory
    record_id: UUID
    field: str
    reason: RedactionReason

    @property
    def identifier(self) -> UUID:
        """The record's own id, the one its key in `_records/` carries (`record_id` is the redacted record's)."""
        return self.redaction_id

    def encode(self) -> bytes:
        """The canonical JSON of the record (format `RECORD_FORMAT`, type `redaction`)."""
        return _canonical(
            {
                "format": RECORD_FORMAT,
                "type": REDACTION_TYPE,
                "redaction_id": str(self.redaction_id),
                "event_at": _format_time(self.event_at),
                "project": self.project,
                "history": self.history,
                "record_id": str(self.record_id),
                "field": self.field,
                "reason": self.reason,
            }
        )

    @classmethod
    def decode(cls, data: bytes) -> RedactionRecord:
        """The record in *data*; `ArchiveFormatError` naming the problem for anything but a format-1 redaction."""
        fields = _load_fields(data, _REDACTION_FIELDS, "redaction record", RECORD_FORMAT)
        _check_type(fields, REDACTION_TYPE)
        try:
            history = cast(RedactionHistory, _choice(fields["history"], "history", tuple(REDACTABLE_FIELDS)))
            field = _choice(fields["field"], "field", REDACTABLE_FIELDS[history])
            return cls(
                redaction_id=_uuid7(fields["redaction_id"], "redaction_id"),
                event_at=_event_at(fields["event_at"], "event_at"),
                project=_name(fields["project"], "project"),
                history=history,
                record_id=_uuid7(fields["record_id"], "record_id"),
                field=field,
                reason=cast(RedactionReason, _choice(fields["reason"], "reason", REDACTION_REASONS)),
            )
        except ValueError as e:
            raise ArchiveFormatError(f"The redaction record field {e}") from None


@dataclass(frozen=True, slots=True)
class HistoryRetentionRecord:
    """One row of `history_retention_action` as stored in `_records/`, with the rule it applied.

    *keep_days* and *rule_event_at* are the rule's; *cutoff* is kept to the microsecond, because the rows removed
    are those older than it. *project* is the project's name.
    """

    action_id: UUID
    event_at: datetime
    project: str
    history: RetentionHistory
    rule_id: UUID
    keep_days: int | None
    rule_event_at: datetime
    cutoff: datetime
    rows_removed: int

    @property
    def identifier(self) -> UUID:
        """The record's own id, the one its key in `_records/` carries."""
        return self.action_id

    def encode(self) -> bytes:
        """The canonical JSON of the record (format `RECORD_FORMAT`, type `history_retention_action`)."""
        return _canonical(
            {
                "format": RECORD_FORMAT,
                "type": RETENTION_ACTION_TYPE,
                "action_id": str(self.action_id),
                "event_at": _format_time(self.event_at),
                "project": self.project,
                "history": self.history,
                "rule_id": str(self.rule_id),
                "keep_days": self.keep_days,
                "rule_event_at": _format_time(self.rule_event_at),
                "cutoff": _format_time(self.cutoff, micro=True),
                "rows_removed": self.rows_removed,
            }
        )

    @classmethod
    def decode(cls, data: bytes) -> HistoryRetentionRecord:
        """The record in *data*; `ArchiveFormatError` naming the problem for anything but a format-1 action."""
        fields = _load_fields(data, _RETENTION_ACTION_FIELDS, "history retention record", RECORD_FORMAT)
        _check_type(fields, RETENTION_ACTION_TYPE)
        try:
            return cls(
                action_id=_uuid7(fields["action_id"], "action_id"),
                event_at=_event_at(fields["event_at"], "event_at"),
                project=_name(fields["project"], "project"),
                history=cast(RetentionHistory, _choice(fields["history"], "history", HISTORIES)),
                rule_id=_uuid7(fields["rule_id"], "rule_id"),
                keep_days=_optional(fields["keep_days"], lambda v: _integer(v, "keep_days", 1)),
                rule_event_at=_event_at(fields["rule_event_at"], "rule_event_at"),
                cutoff=_event_at(fields["cutoff"], "cutoff", micro=True),
                rows_removed=_integer(fields["rows_removed"], "rows_removed", 0),
            )
        except ValueError as e:
            raise ArchiveFormatError(f"The history retention record field {e}") from None


MaintenanceRecord = DeletionRecord | RedactionRecord | HistoryRetentionRecord
_RECORD_TYPES: dict[str, type[MaintenanceRecord]] = {
    DELETION_TYPE: DeletionRecord,
    REDACTION_TYPE: RedactionRecord,
    RETENTION_ACTION_TYPE: HistoryRetentionRecord,
}


def decode_record(data: bytes) -> MaintenanceRecord:
    """The maintenance record in *data*, by its `type`; `ArchiveFormatError` for an unknown type or a bad record."""
    try:
        raw = json.loads(data)
    except ValueError:
        raise ArchiveFormatError("The record is not valid JSON") from None
    kind = raw.get("type") if isinstance(raw, dict) else None
    if not isinstance(kind, str) or kind not in _RECORD_TYPES:
        raise ArchiveFormatError("The record has no type this library knows")
    return _RECORD_TYPES[kind].decode(data)


def _check_type(fields: dict[str, object], record_type: str) -> None:
    if fields["type"] != record_type:
        raise ArchiveFormatError(f"The record is not of type {record_type}")


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
    identifier = _parse_uuid7(_text(value, field))
    if identifier is None:
        raise _bad(field, "a version 7 UUID")
    return identifier


def _event_at(value: object, field: str, *, micro: bool = False) -> datetime:
    """The UTC time in *value*, written with milliseconds (microseconds when *micro*)."""
    wanted = f"an ISO 8601 UTC time with {'micro' if micro else 'milli'}seconds and `Z`"
    if not isinstance(value, str) or not (_MICROSECONDS if micro else _EVENT_AT).fullmatch(value):
        raise _bad(field, wanted)
    try:
        return datetime.strptime(value[:-1], "%Y-%m-%dT%H:%M:%S.%f").replace(tzinfo=UTC)
    except ValueError:
        raise _bad(field, wanted) from None


def _integer(value: object, field: str, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise _bad(field, f"an integer of at least {minimum}")
    return value


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

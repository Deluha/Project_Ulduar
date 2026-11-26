"""The S3-compatible backend of the storage interface: an archive kept in one bucket under a key prefix.

Input: the `ArchiveSettings` of an `s3` archive and the environment variables it names. Output: an `S3Store`
(writing) or an `S3DeletingStore`, built on a boto3 client (the optional extra `project-ulduar[s3]`). Nothing is
overwritten: a put is conditional (`If-None-Match: *`), and every body travels with its SHA-256 so the store itself
refuses a wrong one. The writing store holds the writer's credential and has no delete call; the deleting store
holds the deleter's (hard rule 5). Private: reached through `open_store` only. No message or log line built here
carries a credential, a proxy address or a full endpoint URL (hard rule 11): a failure is worded with the
operation, the object key and the error code or class name only. Only `check_endpoint` names a host (never a
credential), as `_db` names the Archivum's.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
from collections.abc import Iterable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import urlsplit

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from ulduar import _db
from ulduar.config import ArchiveSettings, CredentialSettings
from ulduar.errors import ArchiveUnavailable, ConfigurationError, IntegrityError
from ulduar.hodir import _store
from ulduar.hodir._store import DeletingStore, StoredObject, UnfinishedUpload, WritingStore, check_key, check_size

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_S = 10
READ_TIMEOUT_S = 60
MAX_ATTEMPTS = 3
COLD_STORAGE_CLASSES = {"GLACIER", "DEEP_ARCHIVE"}
# The archive tiers of INTELLIGENT_TIERING, which `head_object` reports as `ArchiveStatus`, not as a storage class.
COLD_ARCHIVE_STATUSES = {"ARCHIVE_ACCESS", "DEEP_ARCHIVE_ACCESS"}
# What a store answers when a conditional put finds the key taken, and when it refuses a body's checksum.
_PRECONDITION_FAILED = {"PreconditionFailed"}
_CHECKSUM_REFUSED = {"XAmzContentChecksumMismatch", "BadDigest"}
_MISSING_OBJECT = {"404", "NoSuchKey", "NotFound"}
_Parts = list[dict[str, Any]]


def _code(error: ClientError) -> str:
    return str(error.response.get("Error", {}).get("Code", "unknown error"))


def _precondition_failed(error: ClientError) -> bool:
    status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return _code(error) in _PRECONDITION_FAILED or status == 412


def _checksum(data: bytes) -> str:
    return base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")


@contextmanager
def _translated(operation: str, key: str) -> Iterator[None]:
    """Re-raise a client error as the package's exception, worded `<operation> <key> failed: <code or class>`.

    A refused checksum is an `IntegrityError`. `from None` for a transport error: its text can quote the endpoint
    or a proxy address, which may carry a credential (hard rule 11).
    """
    try:
        yield
    except ClientError as e:
        code = _code(e)
        if code in _CHECKSUM_REFUSED:
            raise IntegrityError(f"Object {key} does not match its recorded checksum: the store refused it") from e
        raise ArchiveUnavailable(f"{operation} {key} failed: {code}") from e
    except BotoCoreError as e:
        raise ArchiveUnavailable(f"{operation} {key} failed: {type(e).__name__}") from None


def is_immediate(storage_class: str | None, archive_status: str | None = None) -> bool:
    """False when the object is in a cold storage class or an archive tier: it must be restored before it is read."""
    return storage_class not in COLD_STORAGE_CLASSES and archive_status not in COLD_ARCHIVE_STATUSES


class S3Store:
    """A writing store over one bucket and the key prefix *prefix* inside it (the store's own root)."""

    def __init__(self, client: Any, bucket: str, prefix: str) -> None:
        self._client = client
        self._bucket = bucket
        self._prefix = prefix

    def _full(self, key: str) -> str:
        """The key inside the bucket: the store's prefix and *key* ("" names the prefix itself)."""
        return "/".join(part for part in (self._prefix, key) if part)

    def _relative(self, full: str) -> str:
        return full[len(self._prefix) + 1 :] if self._prefix else full

    def _under(self, prefix: str) -> str:
        """The bucket prefix that matches the keys below *prefix* and nothing else."""
        full = self._full(prefix)
        return full + "/" if full else ""

    @staticmethod
    def _stored(
        key: str, size: int, modified: Any, storage_class: str | None, archive_status: str | None = None
    ) -> StoredObject:
        return StoredObject(key, size, modified.astimezone(UTC), is_immediate(storage_class, archive_status))

    def put_new(self, key: str, source: Path, size: int, sha256: bytes) -> bool:
        check_key(key)
        check_size(size)
        with _store.open_regular(source) as handle:
            if os.fstat(handle.fileno()).st_size != size:
                raise IntegrityError(f"Object {key} does not match its recorded size: nothing was stored")
            with _translated("Storing", key):
                if self.head(key) is not None:
                    return False
                if size < _store.MULTIPART_THRESHOLD:
                    return self._put_single(key, handle, size, base64.b64encode(sha256).decode("ascii"))
                return self._put_multipart(key, handle, size, sha256)

    def put_bytes_new(self, key: str, data: bytes) -> bool:
        check_key(key)
        with _translated("Storing", key):
            if self.head(key) is not None:
                return False
            return self._put_single(key, data, len(data), _checksum(data))

    def _put_single(self, key: str, body: BinaryIO | bytes, size: int, checksum: str) -> bool:
        try:
            self._client.put_object(
                Bucket=self._bucket,
                Key=self._full(key),
                Body=body,
                IfNoneMatch="*",
                ChecksumSHA256=checksum,
                ContentLength=size,
            )
        except ClientError as e:
            if _precondition_failed(e):
                return False
            raise
        logger.debug("stored object %s (%d bytes)", key, size)
        return True

    def _put_multipart(self, key: str, handle: BinaryIO, size: int, sha256: bytes) -> bool:
        full = self._full(key)
        upload_id = self._client.create_multipart_upload(Bucket=self._bucket, Key=full, ChecksumAlgorithm="SHA256")[
            "UploadId"
        ]
        try:
            parts = self._upload_parts(full, upload_id, handle, size, sha256)
            self._client.complete_multipart_upload(
                Bucket=self._bucket,
                Key=full,
                UploadId=upload_id,
                IfNoneMatch="*",
                MultipartUpload={"Parts": parts},
            )
        except ClientError as e:
            self._abort(key, full, upload_id)
            if _precondition_failed(e):
                return False
            raise
        except BaseException:
            self._abort(key, full, upload_id)
            raise
        logger.debug("stored object %s (%d bytes) in %d parts", key, size, len(parts))
        return True

    def _upload_parts(self, full: str, upload_id: str, handle: BinaryIO, size: int, sha256: bytes) -> _Parts:
        """Upload *handle* in parts, hashing what is read; `IntegrityError` when it is not *size* bytes of *sha256*."""
        length = _store.part_size(size)
        digest = hashlib.sha256()
        parts: _Parts = []
        total = 0
        while data := handle.read(length):
            total += len(data)
            digest.update(data)
            number = len(parts) + 1
            checksum = _checksum(data)
            response = self._client.upload_part(
                Bucket=self._bucket,
                Key=full,
                UploadId=upload_id,
                PartNumber=number,
                Body=data,
                ChecksumSHA256=checksum,
            )
            parts.append({"PartNumber": number, "ETag": response["ETag"], "ChecksumSHA256": checksum})
        if total != size or digest.digest() != sha256:
            raise IntegrityError(
                f"Object {self._relative(full)} does not match its recorded checksum and size: nothing was stored"
            )
        return parts

    def _abort(self, key: str, full: str, upload_id: str) -> None:
        """Abort an unfinished upload on a failure path; a failure here is logged, the original error is raised."""
        try:
            self._client.abort_multipart_upload(Bucket=self._bucket, Key=full, UploadId=upload_id)
        except (ClientError, BotoCoreError) as e:
            reason = _code(e) if isinstance(e, ClientError) else type(e).__name__
            logger.warning("aborting the unfinished upload of %s failed: %s", key, reason)

    def head(self, key: str) -> StoredObject | None:
        check_key(key)
        with _translated("Reading", key):
            try:
                response = self._client.head_object(Bucket=self._bucket, Key=self._full(key))
            except ClientError as e:
                if _code(e) in _MISSING_OBJECT:
                    return None
                raise
            return self._stored(
                key,
                response["ContentLength"],
                response["LastModified"],
                response.get("StorageClass"),
                response.get("ArchiveStatus"),
            )

    def read(self, key: str) -> AbstractContextManager[BinaryIO]:
        check_key(key)
        return self._read(key)

    @contextmanager
    def _read(self, key: str) -> Iterator[BinaryIO]:
        with _translated("Reading", key):
            try:
                response = self._client.get_object(Bucket=self._bucket, Key=self._full(key))
            except ClientError as e:
                if _code(e) in _MISSING_OBJECT:
                    raise IntegrityError(f"Object {key} is missing from the archive") from e
                raise
            with response["Body"] as body:
                yield body

    def list(self, prefix: str) -> Iterator[StoredObject]:
        check_key(prefix, allow_root=True)
        found = [
            self._stored(self._relative(item["Key"]), item["Size"], item["LastModified"], item.get("StorageClass"))
            for page in self._pages("list_objects_v2", "Listing", prefix, Prefix=self._under(prefix))
            for item in page.get("Contents", [])
        ]
        return iter(sorted(found, key=lambda stored: stored.key))

    def list_unfinished(self, prefix: str) -> Iterator[UnfinishedUpload]:
        check_key(prefix, allow_root=True)
        # NOTE: no server-side `Prefix`: MinIO reads it as one exact key and lists nothing else, so the bucket's
        # unfinished uploads (few, by nature) are filtered here, which any S3 server answers the same way.
        under = self._under(prefix)
        found = [
            UnfinishedUpload(self._relative(item["Key"]), item["UploadId"], item["Initiated"].astimezone(UTC))
            for page in self._pages("list_multipart_uploads", "Listing", prefix)
            for item in page.get("Uploads", [])
            if item["Key"].startswith(under)
        ]
        return iter(sorted(found, key=lambda upload: (upload.key, upload.upload_id)))

    def abort_unfinished(self, upload: UnfinishedUpload) -> None:
        check_key(upload.key)
        with _translated("Aborting", upload.key):
            try:
                self._client.abort_multipart_upload(
                    Bucket=self._bucket, Key=self._full(upload.key), UploadId=upload.upload_id
                )
            except ClientError as e:
                if _code(e) != "NoSuchUpload":
                    raise

    def _pages(self, operation: str, what: str, name: str, **arguments: Any) -> Iterable[Mapping[str, Any]]:
        """Every page of a paginated listing, fetched now so a failure is raised as the package's exception."""
        with _translated(what, name or "the archive root"):
            return list(self._client.get_paginator(operation).paginate(Bucket=self._bucket, **arguments))


class S3DeletingStore(S3Store):
    """An `S3Store` that can also delete, built on the deleter's credential."""

    def delete_every_version(self, key: str) -> None:
        check_key(key)
        full = self._full(key)
        with _translated("Deleting", key):
            versions = self._versions(key, "Versions") + self._versions(key, "DeleteMarkers")
            if not versions:
                self._client.delete_object(Bucket=self._bucket, Key=full)
            for item in versions:
                self._client.delete_object(Bucket=self._bucket, Key=full, VersionId=item["VersionId"])
        logger.debug("deleted every version of object %s", key)

    def any_version_left(self, key: str) -> bool:
        check_key(key)
        return bool(self._versions(key, "Versions"))

    def _versions(self, key: str, kind: str) -> _Parts:
        """The *kind* (`Versions` or `DeleteMarkers`) of exactly *key*, as the store lists them."""
        full = self._full(key)
        pages = self._pages("list_object_versions", "Listing", key, Prefix=full)
        return [item for page in pages for item in page.get(kind, []) if item["Key"] == full]


def _variable(name: str, what: str) -> str:
    """The value of environment variable *name*; it is never named in an error."""
    value = os.environ.get(name)
    if not value:
        raise ConfigurationError(f"Environment variable {name} is not set — it holds {what}")
    return value


def _credential_arguments(credential: CredentialSettings | None) -> dict[str, str]:
    """The session arguments for *credential*; none, the client's standard chain, when it is None."""
    if credential is None:
        return {}
    arguments = {
        "aws_access_key_id": _variable(credential.access_key_env, "an access key"),
        "aws_secret_access_key": _variable(credential.secret_key_env, "a secret key"),
    }
    if credential.session_token_env:
        arguments["aws_session_token"] = _variable(credential.session_token_env, "a session token")
    return arguments


def check_endpoint(endpoint: str) -> str:
    """*endpoint* when it is `https://`, or `http://` to this machine; otherwise `ConfigurationError`.

    The URL itself is never echoed: it may carry a credential.
    """
    parts = urlsplit(endpoint)
    if parts.scheme == "https" and parts.hostname:
        return endpoint
    if parts.scheme != "http" or not parts.hostname:
        raise ConfigurationError("The archive endpoint must be an http or https URL with a host")
    if parts.hostname not in _db.LOOPBACK_HOSTS:
        raise ConfigurationError(
            f"Archive endpoint host {parts.hostname} is not on this machine — a remote endpoint needs https"
        )
    return endpoint


def open_s3_store(settings: ArchiveSettings, *, deleting: bool) -> WritingStore | DeletingStore:
    """The S3 store of *settings*, on the writer's credential, or the deleter's when *deleting*."""
    credential = settings.writer
    if deleting:
        if settings.deleter is None:
            raise ConfigurationError("Configuration key `archive.deleter` is required to delete from the archive")
        credential = settings.deleter
    session = boto3.session.Session(**_credential_arguments(credential), region_name=settings.region)
    endpoint = (
        check_endpoint(_variable(settings.endpoint_url_env, "the archive endpoint URL"))
        if settings.endpoint_url_env
        else None
    )
    proxy = _variable(settings.proxy_env, "the proxy URL") if settings.proxy_env else None
    config = Config(
        connect_timeout=CONNECT_TIMEOUT_S,
        read_timeout=READ_TIMEOUT_S,
        retries={"mode": "standard", "max_attempts": MAX_ATTEMPTS},
        request_checksum_calculation="when_required",
        response_checksum_validation="when_required",
        # Empty, not unset: an unset value would let botocore route through the environment's proxy.
        proxies={"http": proxy, "https": proxy} if proxy else {},
        **({"s3": {"addressing_style": "path"}} if endpoint else {}),
    )
    client = session.client("s3", endpoint_url=endpoint, config=config)
    assert settings.bucket is not None
    store_class = S3DeletingStore if deleting else S3Store
    return store_class(client, settings.bucket, settings.prefix)

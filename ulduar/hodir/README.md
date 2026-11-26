# ulduar/hodir

## Purpose & scope

Hodir is the cold archive: immutable, content-addressed storage of files, database dumps, reports and models
(hard rule 5). The package holds the storage interface and its two backends (a bucket on an S3-compatible server and
a local directory), the object layout, and the writing half of the public surface: `open_archive` with `put_file`
and `put_stream`. The reading, verifying and retention calls arrive in later Phase 2 commits.

## Files & entry points

| File | Holds |
|---|---|
| `__init__.py` | The facade: `open_archive`, `Archive`, `PutSink` |
| `archive.py` | `open_archive`, `Archive` (`put_file`, `put_stream`) and `PutSink` |
| `_layout.py` | Private: the one home of the object layout: the keys, the format marker, the layout text, the `Manifest` |
| `_archivum.py` | Private: Hodir's SQL, the address lock and the registration insert |
| `_store.py` | Private: the `WritingStore` and `DeletingStore` protocols, `StoredObject`, `UnfinishedUpload`, `open_store`, `check_key` (the one key validator), `check_size`, `part_size`, `open_regular` and the size constants |
| `_directory.py` | Private: `DirectoryStore` and `DirectoryDeletingStore` |
| `_s3.py` | Private: `S3Store` and `S3DeletingStore` on a boto3 client, the transport guard `check_endpoint`, the error translation |

## How it works

### The layout (`_layout.py`)

All keys are relative to the store's prefix (`[archive] prefix`), with `<project>` and `<scope>` names that match
`NAME_PATTERN`; the fixed segments start with `_`, which no scope can, so later scope levels fit between
`<project>/` and `/_objects` without moving a key.

| Key | Holds |
|---|---|
| `ulduar-archive.json` | The format marker `{"format":"ulduar-archive","version":1}`, written once, checked on every open |
| `LAYOUT.txt` | A plain-text description of this layout, written once beside the marker |
| `<project>/<scope>/_objects/sha256/<h[0:2]>/<h>` | The object: its bytes, named by the SHA-256 of those bytes in lower-case hex; written once, never overwritten |
| `<project>/<scope>/_manifests/<yyyy>/<mm>/<artifact_id>.json` | One manifest per registration (month of the artifact id, UTC) |
| `<project>/_records/<yyyy>/<mm>/<record_id>.json` | Maintenance records (later commits) |

A manifest is canonical JSON (sorted keys, no spaces, a trailing newline): `format` 1, `artifact_id`, `event_at`
(ISO 8601 UTC with milliseconds and `Z`), `project`, `scope`, `kind`, `checksum` (`sha256:<hex>`), `size`,
`dataset_id` and `retention_class` (both null when absent). It holds no origin and no dataset name or namespace, so
nothing that may later be redacted. `Manifest.decode` is strict: invalid JSON, a missing or unknown field, a wrong
type or another `format` is an `ArchiveFormatError` that names the problem and never quotes the payload.

### Opening and putting

- `open_archive(config)` needs the `[archive]` section (`ConfigurationError`), opens the Freya catalog session as the
  recording role and the writing store, then makes sure of the format: a store without a marker gets the marker and
  `LAYOUT.txt` (each written once), and the marker the store then holds is always read back and checked
  (`ArchiveFormatError`: another format, or "the archive has format version N; this library knows version 1").
  `Archive.project` and `Archive.scope` are the catalog's. One `Archive` serves one thread; a thread opens its own.
- `put_file(path, kind, *, dataset=None, origin=None, scope=None, retention_class=None, record_id=None) -> Artifact`
  validates before any I/O (`ValueError`: the kind, the origin, the scope and the retention class as names, a
  `record_id` that is not a UUIDv7, a source that is not a regular file or is a symbolic link), then, in this order:
  hashes the file in 1 MiB chunks; stores the object (`put_new`: an object that exists is relied on);
  writes the manifest (an existing one under that artifact id must decode to the same manifest, else
  `UlduarError`); registers the artifact in the catalog (`record_id` is the idempotency key); and, in one catalog
  transaction under the address lock, checks that the object is there and writes the registration.
  The order is object, manifest, artifact, registration (D31): a failure leaves at worst an object without an
  entry, never a registration without its object, and the same put repeated with the same `record_id` completes.
  Putting the same content twice is one object with two manifests, artifacts and registrations.
- The address lock is a transaction-scoped advisory lock on (project, scope, checksum), named
  `ulduar.archive:<project_id>:<scope>:<hex>` (`_archivum.address_lock_name`); a deletion (later commits) takes the
  same lock, so a put and a deletion of one object are ordered. A put that finds the object missing under the lock
  stores it again, once; a second miss is an `IntegrityError` and nothing is registered.
- A registration's `retention_class` is fixed at archive time (NULL keeps for ever). Registering an artifact
  again with another class is a `UlduarError`; so is a repeat of a `record_id` with another class, which differs
  in its manifest first.
- `put_stream(kind, **same keywords)` is a context manager yielding a `PutSink`: `write(bytes)` spools to a
  `0o600` file in `spool_dir` (the system temporary directory by default) while hashing, refusing growth past
  `spool_limit_gib` with `UlduarError`; `commit()` stores it as `put_file` does (no second hashing) and returns the
  artifact, the same one when called again. Leaving the block without `commit`, or with an exception, stores
  nothing; the spool is always deleted. A sink refuses writes after `commit`, after its block, and after the limit
  error.
- Each put logs one `info` line (`ulduar.hodir.archive`): the first 12 hex digits of the checksum, the size, the
  scope and whether the object was new or already stored; no origin, no name.

### The storage interface

- `open_store(settings, *, deleting=False)` chooses the backend from `[archive]` (`ulduar/README.md`) and returns a
  writing store, or with `deleting=True` a deleting one. The writing classes have no delete method at all; the
  deleting classes subclass them and add `delete_every_version` and `any_version_left`. The prefix of the
  settings is applied inside each backend; a caller sees only keys relative to it.
- A key is `/`-joined segments, none empty, `.` or `..`, none with a backslash or a control character, no
  leading or trailing `/`, at most 1024 bytes of UTF-8. `check_key` raises `ValueError` before any I/O, so a
  caller's name never leaves its prefix. A prefix argument may also be "" (the store's root).
- `put_new` and `put_bytes_new` store unless the key exists and return False when it does; they never
  overwrite. The bytes must hash to the given SHA-256 and have the given size, else `IntegrityError` and nothing
  stays stored. `read` streams and raises `IntegrityError` for a missing key; `list` is recursive, sorted by key,
  and matches only keys under `prefix + "/"`; an interrupted put is an `UnfinishedUpload` that `list_unfinished`
  finds and `abort_unfinished` removes.
- An object of 64 MiB or more is a multipart put in parts of at least 64 MiB (`part_size`: whole MiB, at most
  10 000 parts, at most 5 TiB). Each part carries its SHA-256, and the whole is hashed while read from the
  source and compared before the upload completes; a mismatch aborts the upload.
- Directory backend: a file is written to `.tmp-<hex>` beside its final name, flushed, hard-linked into place
  and the temporary removed, so the final name never holds a partial file and an existing name is never
  replaced. No symbolic link below the root is followed (each segment is opened relative to its parent with
  `O_NOFOLLOW`); a link or special file where an object belongs is an `IntegrityError`, and a listing skips one with
  a warning. A key segment may not start with `.tmp-` (those are the unfinished uploads). An operating-system
  error is an `ArchiveUnavailable`.
- S3 backend (needs the extra `project-ulduar[s3]`, else `ConfigurationError`): every write is conditional
  (`If-None-Match: *`) and carries its SHA-256; a checksum the store refuses is an `IntegrityError`. Timeouts:
  connect 10 s, read 60 s, three attempts. Environment proxies are never used; `proxy_env` names one. An
  endpoint must be `https://`, or `http://` to a loopback host. A failure is an `ArchiveUnavailable` worded
  `<Operation> <key> failed: <error code>` (or the error class name for a transport error), and never carries a
  key, secret, token, endpoint or proxy address (hard rule 11).
- `list_unfinished` on S3 lists every unfinished upload of the bucket and filters by prefix itself: MinIO reads a
  `Prefix` on that call as one exact key and lists nothing else.
- `delete_every_version` removes the object and, on a versioned bucket, every version and delete marker of
  exactly that key; `any_version_left` is True while a version (a delete marker does not count) remains.

### What the archive store must offer

- Writer credential: put, get, list, multipart upload including abort and listing of uploads, and (to read
  them) bucket versioning status and versions listing. No delete. Without `[archive.writer]` the client's
  standard credential chain is used.
- Deleter credential: a separate one, which also deletes objects and object versions. It is needed only by
  deleting calls; `[archive.writer]` and `[archive.deleter]` must name different credentials.
- Overwrites are refused by the library's conditional writes. Inside the store, the bucket should enforce the
  same: a bucket policy that requires conditional writes where the server offers one, or versioning, so that an
  overwrite by another client keeps the earlier version (MinIO offers versioning only). Object lock is
  recommended, not required.
- Objects must sit in an immediate-retrieval storage class: `GLACIER` and `DEEP_ARCHIVE` objects report
  `immediate=False` and cannot be read now. No lifecycle rule may expire or move an object.
- The directory backend's file permissions (`0o444`) are the weaker guarantee: the process that wrote a file can
  still remove it.

## Interfaces & contracts

- Public: `open_archive(config)` (a context manager yielding `Archive`), `Archive.project`, `Archive.scope`,
  `Archive.put_file(...)`, `Archive.put_stream(...)` and `PutSink` (`write`, `commit`), as described above.
  `Archive(catalog, store, settings)` is constructible directly (a test injects a wrapped store).
- Private: `open_store`, `WritingStore`, `DeletingStore`, `StoredObject(key, size, modified, immediate)`,
  `UnfinishedUpload(key, upload_id, started)`, `check_key`, the `_layout` functions and `Manifest`.
- Exceptions (`ulduar.errors`): `ArchiveUnavailable`, `IntegrityError`, `ArchiveFormatError` (a foreign marker or
  version, a manifest that is not format 1), `UnknownRecordError` (a scope the project lacks), `UlduarError`
  (another manifest or retention class under a `record_id`, a stream past the spool limit), `ConfigurationError`
  for no `[archive]`, an unset variable a setting names, a missing directory, a refused endpoint, a missing
  `deleter` or a missing extra.
- Library log lines (loggers `ulduar.hodir.*`): `info` per put (`archive`), `debug` per stored object and deletion,
  `warning` for a skipped link or a failed abort; none carries a credential or an address.

## Extending

A new backend is a module with a class pair that satisfies the two protocols, chosen in `open_store`; both
existing backends are tested by the same contract tests (`tests/test_hodir_store.py`). Never add a delete method
to a writing class (hard rule 5); the object layout and checksum definition are ask-first items.

## See also

`../README.md` · `../freya/README.md` · `../../ROADMAP.md` (Phase 2) · `../../tests/test_hodir_store.py` · `../../tests/test_hodir_put.py`

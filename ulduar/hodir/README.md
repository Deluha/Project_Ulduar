# ulduar/hodir

## Purpose & scope

Hodir is the cold archive: immutable, content-addressed storage of files, database dumps, reports and models
(hard rule 5). So far the package holds only its foundation, the private storage interface and its two backends:
a bucket on an S3-compatible server and a local directory. The facade's public calls (put, verify, restore,
retention) arrive in later Phase 2 commits, so `__all__` is empty and a consumer imports nothing from here yet.

## Files & entry points

| File | Holds |
|---|---|
| `__init__.py` | The facade; its public surface is empty for now |
| `_store.py` | Private: the `WritingStore` and `DeletingStore` protocols, `StoredObject`, `UnfinishedUpload`, `open_store`, `check_key` (the one key validator), `check_size`, `part_size`, `open_regular` and the size constants |
| `_directory.py` | Private: `DirectoryStore` and `DirectoryDeletingStore` |
| `_s3.py` | Private: `S3Store` and `S3DeletingStore` on a boto3 client, the transport guard `check_endpoint`, the error translation |

## How it works

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

- Nothing public yet. Private, for the next commits: `open_store`, `WritingStore`, `DeletingStore`,
  `StoredObject(key, size, modified, immediate)`, `UnfinishedUpload(key, upload_id, started)`, `check_key`.
- Exceptions (`ulduar.errors`): `ArchiveUnavailable`, `IntegrityError`, `ArchiveFormatError` (not raised yet),
  `ConfigurationError` for an unset variable a setting names, a missing directory, a refused endpoint, a missing
  `deleter` or a missing extra.
- Library log lines (loggers `ulduar.hodir.*`): `debug` per stored object and deletion, `warning` for a skipped
  link or a failed abort; none carries a credential or an address.

## Extending

A new backend is a module with a class pair that satisfies the two protocols, chosen in `open_store`; both
existing backends are tested by the same contract tests (`tests/test_hodir_store.py`). Never add a delete method
to a writing class (hard rule 5); the object layout and checksum definition are ask-first items.

## See also

`../README.md` · `../../ROADMAP.md` (Phase 2) · `../../tests/test_hodir_store.py`

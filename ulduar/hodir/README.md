# ulduar/hodir

## Purpose & scope

Hodir is the cold archive: immutable, content-addressed storage of files, database dumps, reports and models
(hard rule 5). The package holds the storage interface and its two backends (a bucket on an S3-compatible server and
a local directory), the object layout, and the public surface: `open_archive` with `put_file`, `put_stream`,
`get`, `get_stream`, `verify`, `reconcile` and `rebuild`, the retention calls `declare_retention` and
`apply_retention`, `clean_incomplete`, the built-in rule for what an interrupted put left behind, and `purge`, the
explicit removal of an object archived by mistake.

## Files & entry points

| File | Holds |
|---|---|
| `__init__.py` | The facade: `open_archive`, `Archive`, `PutSink`, `Verification`, `Finding`, `Reconciliation`, `Rebuild`, `declare_retention`, `apply_retention`, `ArchiveRetentionRule`, `RetentionRun`, `DueAddress`, `OpenIntent`, `DeletionOutcome`, `clean_incomplete`, `CleanupRun`, `Adoption`, `Orphan`, `UnfinishedUpload`, `purge` |
| `archive.py` | `open_archive` (the one way to make an `Archive`), `Archive` (`put_file`, `put_stream`, `get`, `get_stream`, `verify`, `reconcile`, `rebuild`) and `PutSink` |
| `retention.py` | `declare_retention`, `apply_retention` and the pure `due_addresses` |
| `cleanup.py` | `clean_incomplete`, the built-in incomplete-put rule (D38) |
| `_purge.py` | `purge`, the explicit recorded removal of an object archived by mistake (D39) |
| `_reconcile.py` | Private: `scan` (one scope's live addresses and store listing, with the database's clock and the grace cutoff) and `reconcile` |
| `_rebuild.py` | Private: `rebuild`, the restore of deletion records and registrations from the store (D32, H28) |
| `_registration.py` | Private: `register_object` (the one registration step a put, an adoption and a rebuild share), `unregistered_manifests`, `adopt_manifest` and `adopt_all` (the adopt-and-count loop `clean_incomplete` and a rebuild share) |
| `_sync.py` | Private: `sync_records`, the record sweep |
| `_deletion.py` | Private: `delete_address`, the one deletion protocol (retention, the incomplete-put clean-up and the purge), and `manifest_keys` |
| `_records.py` | Private: the records Hodir returns (`Finding`, `Verification`, `Reconciliation`, `ArchiveRetentionRule`, `DueAddress`, `OpenIntent`, `DeletionOutcome`, `RetentionRun`, `Orphan`, `Adoption`, `UnfinishedUpload(key, started)`, `CleanupRun`, `Rebuild`, re-exported by the facade), the two rows retention reads, and `CHECK_OUTCOMES` |
| `_layout.py` | Private: the one home of the object layout: the keys and their readers (`parse_object_key`, `parse_manifest_key`, `parse_record_key`), the format marker, the layout text, the `Manifest` and the three maintenance records (`DeletionRecord`, `RedactionRecord`, `HistoryRetentionRecord`, read by `decode_record`) |
| `_archivum.py` | Private: Hodir's SQL, the locks, the registration insert, the archived check, the scope check, the check and retention reads and writes, the deletion rows, and the reads the sweep and a rebuild make (the three kinds of record, a dataset by id, the project's scopes) |
| `_store.py` | Private: the `WritingStore` and `DeletingStore` protocols, `StoredObject`, `PendingUpload(key, upload_id, started)` (what the store reports of an interrupted put; the public `UnfinishedUpload` leaves the id out), `open_store`, `open_maintenance_store` (the preamble of every call that opens the archive: `[archive]` required, the store opened, the format marker written on first use and checked) with `ensure_format`, `read_small`, `check_key` (the one key validator), `check_size`, `part_size`, `open_regular` and the size constants |
| `_directory.py` | Private: `DirectoryStore` and `DirectoryDeletingStore` |
| `_s3.py` | Private: `S3Store` and `S3DeletingStore` on a boto3 client, the transport guard `check_endpoint`, `is_immediate` (cold storage classes and archive tiers), the error translation |

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
| `<project>/_records/<yyyy>/<mm>/<record_id>.json` | Maintenance records: a deletion's intent and its outcome, a redaction, a history-retention action |

A manifest is canonical JSON (sorted keys, no spaces, a trailing newline): `format` 1, `artifact_id`, `event_at`
(ISO 8601 UTC with milliseconds and `Z`), `project`, `scope`, `kind`, `checksum` (`sha256:<hex>`), `size`,
`dataset_id` and `retention_class` (both null when absent). It holds no origin and no dataset name or namespace, so
nothing that may later be redacted. `Manifest.decode` is strict: invalid JSON, a missing or unknown field, a wrong
type or another `format` is an `ArchiveFormatError` that names the problem and never quotes the payload.

A deletion record is canonical JSON as well, one file per `archive_deletion` row, named by the row's `deletion_id`:
`format` 1, `type` `archive_deletion`, `deletion_id`, `event_at`, `recorded_at` (the database's clock), `project` (the
name, not the id), `scope`, `checksum` (`sha256:<hex>`), `size`, `stage` (`intent`, `confirmed`, `cancelled`),
`intent_id` (null for an intent), `cause`, `rule_id` (set exactly for a retention deletion) and `reason` (set exactly
for a purge); times are ISO 8601 UTC with milliseconds and `Z`. `DeletionRecord.decode` is as strict as the manifest's
and also refuses a stage, cause, intent, rule and reason that do not agree.

The other two records (H4) are canonical JSON of the same kind and as strict, one file per row, named by its own id:
a `redaction` record (`format` 1, `type` `redaction`, `redaction_id`, `event_at`, `project`, `history`, `record_id` (the
redacted record's), `field`, `reason`) and a `history_retention_action` record (`format` 1, `type`
`history_retention_action`, `action_id`, `event_at`, `project`, `history`, `rule_id`, the rule's `keep_days` and
`rule_event_at`, `cutoff`, `rows_removed`). They hold ids, names, reasons, counts and times and never a removed value.
`cutoff` is written to the microsecond, because the rows an action removed are those older than it; every other
time is in milliseconds. `decode_record(data)` reads a record by its `type` (an unknown one is an `ArchiveFormatError`).
A manifest's `kind` must be one of the artifact kinds.

### Opening and putting

- `open_archive(config)` needs the `[archive]` section (`ConfigurationError`), opens the writing store and makes
  sure of the format, then opens the Freya catalog session as the recording role: a store without a marker gets the
  marker and `LAYOUT.txt` (each written once), and the marker the store then holds is always read back and checked
  (`ArchiveFormatError`: another format, or "the archive has format version N; this library knows version 1").
  Every call that opens the archive (`open_archive`, `apply_retention`, `clean_incomplete`, `purge`) does this through
  `_store.open_maintenance_store`. An `Archive` is made only by `open_archive`; `Archive.project` and
  `Archive.scope` are the catalog's. One `Archive` serves one thread; a thread opens its own.
- `put_file(path, kind, *, dataset=None, origin=None, scope=None, retention_class=None, record_id=None) -> Artifact`
  validates before any storage I/O (`ValueError`: the kind, the origin, the scope and the retention class as names, a
  `record_id` that is not a UUIDv7, a source that is not a regular file or is a symbolic link; `UnknownRecordError`:
  a scope the project lacks, `Scope <scope> is not onboarded for project <name>`, one catalog read before anything is
  hashed or stored), then, in this order:
  hashes the file in 1 MiB chunks; stores the object (`put_new`: an object that exists is relied on);
  writes the manifest (an existing one under that artifact id must decode to the same manifest, else
  `UlduarError`); registers the artifact in the catalog (`record_id` is the idempotency key); and, in one catalog
  transaction under the address lock, checks that the object is there and writes the registration.
  The order is object, manifest, artifact, registration (D31): a failure leaves at worst an object without an
  entry, never a registration without its object, and the same put repeated with the same `record_id` completes.
  Putting the same content twice is one object with two manifests, artifacts and registrations.
- A retry that names the `record_id` of an artifact the catalog already holds is accepted when its kind, checksum,
  size, scope and dataset match and its origin is the request's or none: an artifact adopted from a manifest
  (`clean_incomplete`, a rebuild) has no origin, because a manifest holds none, and the retry returns it as it is.
  If its registration exists but a confirmed deletion of the object came after it (retention removed the object
  meanwhile), the registration step, under the address lock, refuses with `Artifact <id> was deleted from the archive;
  put it again under a new record id`, and registers nothing. An object at the key whose size is not the content's is
  `IntegrityError` `Object <key> holds other bytes than its address`, never "disappeared".
- The address lock is a transaction-scoped advisory lock on (project, scope, checksum), named
  `ulduar.archive:<project_id>:<scope>:<hex>` (`_archivum.address_lock_name`); a deletion (the protocol below) takes
  the same lock, so a put and a deletion of one object are ordered. A put that finds the object missing under the lock
  stores it again, once; a second miss is an `IntegrityError` and nothing is registered.
- A registration's `retention_class` is fixed at archive time (NULL keeps for ever). Registering an artifact
  again with another class is a `UlduarError`; so is a repeat of a `record_id` with another class, which differs
  in its manifest first.
- `put_stream(kind, **same keywords)` is a context manager yielding a `PutSink`: `write(bytes)` spools to a
  `0o600` file in `spool_dir` (the system temporary directory by default) while hashing, refusing growth past
  `spool_limit_gib` with `UlduarError`; `commit()` stores it as `put_file` does (no second hashing) and returns the
  artifact, the same one when called again. Leaving the block without `commit`, or with an exception, stores
  nothing; the spool is always deleted. A sink refuses writes after `commit`, after its block, and after the limit
  error; a `commit` repeated after one whose store step raised says the put did not complete (put it again, with the
  same `record_id`, to finish it).
- Each put logs one `info` line (`ulduar.hodir.archive`): the first 12 hex digits of the checksum, the size, the
  scope and whether the object was new or already stored; no origin, no name.

### Getting

- `get(artifact, destination) -> Path` and `get_stream(artifact, out) -> int` take an `Artifact` or its id. Only
  the id is used: the artifact is always re-read from the catalog (`UnknownRecordError` when it is not recorded) and
  its recorded checksum, size and scope apply, never those of the object the caller holds. The artifact must be in the archive:
  registered, and with no confirmed deletion of its content recorded after the registration
  (`ulduar.artifact_is_archived`, called by `_archivum.is_archived` in a catalog transaction); otherwise
  `UnknownRecordError("Artifact <id> is not in the archive")`, before any read of the store.
- The object is read from the store in 1 MiB chunks and hashed (SHA-256) and counted as it goes (one helper serves
  both calls); at the end its size and checksum must equal the artifact's, else `IntegrityError` (`Object <key> does
  not match its recorded size` or `... recorded checksum`). A mismatch is never retried or downgraded. An object
  missing from the store is the store's `IntegrityError`.
- `get` refuses a destination that exists (`FileExistsError`; a dangling symbolic link counts) and one whose parent
  is not an existing directory (`FileNotFoundError`, or `NotADirectoryError` when the parent is a file), both before
  any read. It writes to a partial file in the destination's own directory, `.ulduar-partial-<hex>` (no part of the
  destination's name, so a long name never exceeds the file system's limit), created
  exclusively without following links (mode 0o666 under the umask), through one directory descriptor; after the
  verification it syncs the file, hard-links it to the destination name (an existing name is never replaced: a
  destination that appeared meanwhile is `FileExistsError`), removes the partial file and syncs the directory.
  A failure after the partial file was made removes it, best effort, and re-raises; a destination is therefore
  either absent or a verified, complete copy.
- `get_stream` writes each chunk to *out* as it is read, so on a mismatch `IntegrityError` is raised after the
  bytes were written: the caller must discard what it received. It returns the byte count.
- Each get logs one `info` line (`ulduar.hodir.archive`): the artifact id, the first 12 hex digits of the checksum
  and the size; never the destination, which may name a consumer's folder.

### Verifying

- `verify(*, sample=None, scope=None) -> Verification` re-reads objects and compares each with what the Archivum
  records. `scope` defaults to the session's (`check_name`) and must be one of the project's scopes
  (`UnknownRecordError`, read before any object); `sample` is None for a full verification, else an
  integer of at least 1 (a bool is refused): `ValueError` naming the argument, before any I/O.
- The objects judged are the scope's *live addresses*: the distinct (checksum, size) of registrations whose artifact
  is archived (`ulduar.artifact_is_archived`: registered, no confirmed deletion of the content recorded after it),
  by checksum. A sample takes the N whose latest verification finding, of any outcome (by the database's clock; a
  reconciliation's findings are not verifications), is oldest, those never verified first, then by checksum, so
  repeated samples visit every object once before any is visited again, a failing object included: it does not hold
  the rotation.
- Each address gets one outcome: `missing` (the store has no object, or it vanished between the head and the read:
  after a store `IntegrityError` the head is asked again, and an absent object is `missing`), `not_immediate` (a
  storage class that cannot be read now; the object is not read), `unreadable` (the store refused it as an
  `IntegrityError`: a directory, link or special file where the object belongs), `mismatch` (the SHA-256 or the size of the
  bytes read differs from the record) or `passed`. The other three outcomes of `CHECK_OUTCOMES`
  (`object_without_entry`, `entry_without_object`, `unfinished_upload`) belong to reconciliation (below).
  `ArchiveUnavailable` propagates, and nothing is recorded. The read-hash loop is the one `get` uses.
- The check id is made at the start; each finding id as its object is judged. At the end one catalog transaction
  (with the check's and findings' times, so a missing monthly partition is made and the work run again) writes one
  `archive_check` row (`kind` `verification`, `mode` `full` or `sample`, `objects_checked`, `findings` = the number
  of outcomes other than `passed`) and one `archive_finding` row per judged address, `passed` ones included (the
  rotation reads them). A scope of the project nothing was archived in verifies 0 objects and still records the check.
- `Verification(check_id, scope, mode, objects_checked, findings, event_at)` has a `failed` property (the findings
  other than `passed`); `Finding(finding_id, scope, checksum, size, outcome)` holds the recorded checksum and size.
  Both are frozen; they carry no content and no name. A verification ends with the record sweep (below).
- Logs (`ulduar.hodir.archive`): one `info` summary (scope, mode, checked, failed) and one `warning` per finding
  that is not `passed` with the first 12 hex digits of the checksum and the outcome.

### Retention

- A put names a retention class (a name; fixed at archive time, in the manifest and the registration); a
  registration without one is kept for ever. `declare_retention(config, project, retention_class, keep_days, *,
  keep_newest=1, confirm_shortening=False) -> ArchiveRetentionRule` runs as the ledger role (`freya.ledger_transaction`)
  and appends a row of `archive_retention_rule`: *keep_days* is None (for ever) or an integer of at least 1,
  *keep_newest* an integer of at least 0 (a bool is refused for both: `ValueError`, before any SQL). The latest rule
  of a (project, class) by `recorded_at DESC, rule_id DESC` applies; declaring takes the advisory lock
  `ulduar.archive-retention:<project_id>:<class>` (`_archivum.retention_lock_name`), so two declarations of one class
  are ordered. An unknown project is `UnknownRecordError` (`Project <name> is not onboarded`).
- A rule *shortens* when it has a `keep_days` and there is no rule yet, or the current one keeps for ever, or the
  new `keep_days` is smaller, or `keep_newest` is smaller than the current one's. A rule that keeps for ever
  (`keep_days` None) never shortens, whatever its `keep_newest`: it deletes nothing. A shortening rule without `confirm_shortening=True` is a `UlduarError` and records nothing; with it, the rule
  records `shortening_confirmed`. Nothing is deleted by declaring.
- An object (scope, checksum) is *due* when **every** live registration of it has expired (H13: the longest
  retention wins; a class without a rule, a rule that keeps for ever, and no class keep for ever). A registration
  has expired when it was recorded (its `recorded_at`, the database's clock, stamped by trigger) longer ago than
  `keep_days`, measured against the database's `now()` read in the same transaction as the registrations, and it
  is not among the `keep_newest` newest live registrations of its dataset (ranked by `recorded_at DESC, artifact_id
  DESC`, whatever their class; one without a dataset has no such protection). The age is the registration's, never the
  artifact's `event_at`: a rebuild registers again and so restarts it, which errs towards keeping. A registration
  expires at `recorded_at + keep_days`; the *binding* rule of an object is that of the registration that expires
  latest. `due_addresses(registrations, rules, now)` is that logic as a pure function (the caller names *now*; a test
  drives it); a `DueAddress(scope, checksum, size, rule_id, artifact_ids)` names the binding rule, and the list is
  ordered by scope and checksum. A *live* registration is one whose artifact `ulduar.artifact_is_archived`.
- `apply_retention(config, *, dry_run=True, allow_over_limit=False, scope=None) -> RetentionRun` needs `[archive]`
  (`ConfigurationError`) and a *scope* the project has (`ValueError` for a bad name, `UnknownRecordError`; default:
  every scope). It reads the registrations, the rules and the database's clock in one catalog transaction as the
  recording role. A dry run returns `RetentionRun(True, due, (), open_intents)` (the open retention intents it would
  settle, below) and deletes nothing: it opens the writing store (and checks the marker), to copy the records the
  store lacks, so it needs no deleter credential but an archive that exists. A real run opens the deleting
  store with the `deleter` credential (`ConfigurationError` without it, for s3) and checks the format marker, and
  refuses, before deleting anything, to delete more objects than `archive.max_deletions_per_run` (default 100) unless
  `allow_over_limit=True` (`UlduarError`). Each due object then goes through the deletion protocol with cause
  `retention` and the binding rule. A real run also settles the *open retention intents* of its scope(s) whose object
  is not due (a failed run left the intent open and a rule was lengthened since, or its registrations were
  replaced): each goes through the same protocol, whose transaction 2 asks `still_due` and cancels the intent, so no
  open retention intent outlives a run unless the store refused its delete. They do not count towards
  `max_deletions_per_run`; a dry run lists them (`RetentionRun.open_intents`, a tuple of `OpenIntent(deletion_id,
  scope, checksum, size, rule_id)`) and settles nothing. Open intents of another cause are not retention's to settle:
  a due object that has one is `skipped`, with the detail `an open <cause> deletion is pending`, and nothing is
  recorded (settle that deletion first).
  One `info` line per outcome (`ulduar.hodir.retention`: first 12 hex digits of the
  checksum, scope, stage) and one summary line. `RetentionRun(dry_run, due, outcomes, open_intents)`, the outcomes of
  the open intents ahead of those of the due objects; a
  `DeletionOutcome(scope, checksum, size, deletion_id, stage, cause, detail)` has the stage `confirmed`, `cancelled`,
  `open` or `skipped` and, but for `confirmed`, says why in `detail`. Either kind of run ends with the record sweep.

### Reconcile

- `reconcile(*, scope=None) -> Reconciliation` compares the store with the Archivum for one scope (default: the
  session's; `ValueError` for a bad name, `UnknownRecordError` for one the project lacks, before any listing). In
  one transaction it reads the database's clock (`SELECT now()`, never the client's) and the scope's live addresses,
  then lists the objects under `<project>/<scope>/_objects/sha256` and the unfinished uploads under
  `<project>/<scope>`. The Archivum is read first: a put writes its object before its registration (D31), so an
  object listed afterwards is registered, young, or an orphan; reading the store first would report a put that
  finished meanwhile as an entry without its object. `grace` is `archive.incomplete_put_grace_hours` (default 168,
  at least 24), compared with the store's own modification time (a bucket's last-modified, a file's mtime).
- Findings: `object_without_entry` (an object whose address is not live and whose age exceeds the grace; a key under
  `_objects` that does not parse as an object key counts too, with no checksum and no size), `entry_without_object`
  (a live address the store does not hold; no grace, because the object is written before the registration) and
  `unfinished_upload` (an interrupted put's leftover older than the grace: a bucket's unfinished multipart upload,
  a directory's `.tmp-` file; its checksum when its key parses, else none). Younger objects and uploads are not
  judged, only counted (`skipped_young`). An agreement records no finding: a reconciliation's `archive_finding`
  rows are the disagreements, and `archive_check` (`kind` `reconciliation`, `mode` `full`, `objects_checked` the
  objects listed, `findings` their number) is recorded in one catalog transaction with them.
- Reconcile only reports: it lists unfinished uploads and aborts none (`clean_incomplete` does, below), and it
  deletes and adopts nothing. A live address the listing did not show is looked at once more before it becomes an
  `entry_without_object`: under its address lock, it must still be live and the store must still lack the object, so
  an object a deletion is removing (transaction 2 running) is never reported, and neither is one put again meanwhile.
- `Reconciliation(check_id, scope, objects_listed, skipped_young, findings, event_at)`. One `warning` per finding
  and one `info` summary (`ulduar.hodir._reconcile`); a reconciliation ends with the record sweep.

### The incomplete-put rule (`clean_incomplete`)

- `clean_incomplete(config, *, dry_run=True, scope=None) -> CleanupRun` is the rule of D38, built in and not
  declared: scope default is every scope of the project. It opens the writing store for a dry run (it only lists
  and reads) and the deleting store with the `deleter` credential for a real one, checks the marker, and reads each
  scope as `reconcile` does (same grace, the database's clock). A dry run writes only what any call may: the format
  marker and `LAYOUT.txt` of a new store, and the records the Archivum holds that the store lacks; it adopts,
  removes and aborts nothing.
- It reads the scope's manifests once; the artifact ids with a registration are known, and only the other
  manifests (the *unregistered* ones) are read and decoded. A manifest that does not decode, or names another
  project, scope or artifact than its key does, is an `ArchiveFormatError` naming the key (never the content),
  before anything is changed: what it names might be adoptable, so nothing is removed on its absence.
- **Adopt**: an unregistered manifest older than the grace whose object exists with the manifest's size. The artifact
  is the catalog's own when it holds the id (its origin and dataset are kept; a manifest never holds an origin), else
  registered from the manifest under its id (`kind`, `checksum`, `size`, `scope`, and the dataset when the Archivum
  holds that id in that scope, else none, counted in `datasets_missing`); then the registration is made under the
  address lock by the step a put ends with (`_registration.register_object`) with the manifest's retention class.
- **Remove**: an object older than the grace that is not live (also not after the adoptions) and that no
  unregistered manifest, however young, names. It goes through the deletion protocol with cause `incomplete_put`,
  no rule, no manifests to remove, and a `still_due` that asks, under the address lock, that no registration of the
  address is live and the object (if the store still has it) is past the grace: a put that registered it meanwhile
  cancels the deletion. The record is the same as any deletion: intent, store record, outcome, store record.
- **Abort**: an unfinished upload older than the grace, `abort_unfinished`, a real run only (this is the only call
  that aborts one; `reconcile` reports them). The record sweep runs at the end of a real run even when a step of
  it raised (the error is re-raised after it). A real run also
  settles the open intents of cause `incomplete_put` in the scopes through the same protocol, ahead of the new
  removals (their `still_due` decides: a live registration or a young object cancels them); a dry run lists them.
- `CleanupRun(dry_run, adopt, remove, abort, open_intents, outcomes, adopted, datasets_missing)`: the first four are
  the listing (a dry run fills only these): `Adoption(artifact_id, scope, checksum, size)`, `Orphan(scope,
  checksum, size)` (also the open intents' objects), the `UnfinishedUpload(key, started)`s (Hodir's record: the store's
  upload id stays private). `outcomes` are the deletions, `adopted`
  the artifacts registered (an adoption whose object vanished is logged and left out). A real run adopts first, then
  removes, then aborts, and ends, like a dry run, with the record sweep. `archive.max_deletions_per_run` does not apply.

### Purge (`purge`)

- `purge(config, *, scope, checksum, reason) -> DeletionOutcome` is the explicit removal of an object archived by
  mistake (D39, H16): the one deletion nothing declared. It needs `[archive]` and the `deleter` credential
  (`ConfigurationError`) and checks the format marker. `reason` is one of Freya's `REDACTION_REASONS`
  (`personal_data`, `secret`, `recorded_by_mistake`) and is recorded on both rows; `ValueError` for a bad scope, a
  checksum that is not 32 bytes or another reason, and `UnknownRecordError` for a scope the project lacks, all before
  any store call.
- The object must be known to the project: some registration of (scope, checksum) exists, live or not, or the store
  holds the object (`UnknownRecordError` otherwise). It goes through the deletion protocol with cause `purge`, the
  reason, no rule and a `still_due` that is always true, so an intent is committed and written to the store, and
  under the address lock every version of the object and the manifest of every registration of it (live or not,
  however many artifacts share the content) are deleted and `confirmed` is recorded. A registration recorded after the
  intent (a put of the same content) cancels the purge and the object stays; purge again to remove it.
- What it removes: the object, its versions and its manifests. What it leaves: the catalog's entries (artifact,
  registration, lineage; all append-only), which keep naming the removed object (`get` says it is not in the archive,
  and a reconciliation does not report it, because the confirmed deletion explains it). A consumer redacts what they
  hold separately with `freya.redact`.
- An object already gone (every registration followed by a confirmed deletion, nothing in the store, no open intent)
  is `skipped` with a detail saying so, and nothing is recorded. A refused delete (the writer credential, an object
  lock) leaves the intent open (stage `open`); purging again resumes it, and its reason is the original intent's: another
  `reason`, or an open intent of another cause for the object, is `UlduarError` before anything changes, so a record
  never claims a reason or a cause it was not given.
- One `info` line (`ulduar.hodir._purge`: first 12 hex digits of the checksum, scope, reason, stage), never a name or
  content; the call ends with the record sweep.

### Rebuild

- `rebuild(*, since=None) -> Rebuild` rebuilds a lost Archivum from the store alone, as the recording role with the
  writing store. The project and its scopes must be onboarded first (the scopes are the Archivum's: a deletion record
  of another is `UnknownRecordError`, before anything is changed). `since` (timezone-aware) limits what is decoded: the
  whole `_records/` and `_manifests/` prefixes are listed, and only the keys of months from its month on are read
  and decoded (a month is in the key, so no object outside it is opened).
- Order matters. Whether an object is archived compares the `recorded_at` of a registration with that of a confirmed
  deletion of its content, and the database stamps both with its own clock when the row is inserted, whatever the
  record says. So **first** the deletion records of `<project>/_records` the Archivum lacks (by `deletion_id`) are
  inserted, with their original ids and `event_at`, intents before outcomes in one transaction (an outcome whose
  intent is neither in the Archivum nor read is a `UlduarError`: read from an earlier month); **then** every
  unregistered manifest whose object exists is adopted as `clean_incomplete` adopts it, with no grace period. A
  registration made after the deletions is newer than all of them, as a put made after a deletion is, so content
  deleted and archived again comes back live; done the other way round, it would be deleted again.
- A record or manifest that does not decode is an `ArchiveFormatError` naming its key; the records are all read
  before any is restored. A manifest whose object is gone is left. Rebuilding again changes nothing.
- The redactions and history-retention actions found are not applied (the recording role cannot): they come back as
  `Rebuild.removals`, Freya records (`Redaction`, `RetentionAction`) in event order, for `freya.reapply_removals`,
  which the ledger role runs afterwards. `Rebuild(deletions_restored, adopted, datasets_missing, removals)`; a
  rebuilt artifact has no origin and, when its dataset id is not in the new Archivum (a dataset is not in the store),
  no dataset (`datasets_missing`).

### The record sweep

- `_sync.sync_records(catalog, store) -> int` lists `<project>/_records` once and writes, with `put_bytes_new`, every
  deletion, redaction and history-retention action of the project whose key is absent; it returns how many it wrote.
  `verify`, `reconcile`, `clean_incomplete` and `apply_retention` (a dry run too: writing records needs only the
  writer) and `purge` end with it. The Archivum is the authority and the store its copy (D32), so the
  sweep never fails a call: a store or Archivum error (`UlduarError`) is logged as a `warning`
  (`ulduar.hodir._sync`) and the call's own result stands; the next call catches up. The recording role reads the
  two Freya tables through the grants of step 2.

### The deletion protocol (`_deletion.delete_address`)

One home for every deletion (retention, the incomplete-put clean-up and the purge use it), so a put and a
deletion of one object are ordered the same way whatever the cause (D37). Both transactions run on the catalog
session's connection (`Catalog.transaction`, the recording role); the credential that deletes is the store's.

1. **Transaction 1**, under the address lock: an open intent of the object (an `archive_deletion` row of stage
   `intent` with no outcome row) is reused when it is of the same cause; an open intent of another cause is not this
   deletion's to settle, so the outcome is `skipped` (detail `an open <cause> deletion is pending`) and nothing is
   recorded (purge refuses it explicitly instead). Otherwise the caller's `still_due` is asked, and when it says no the
   outcome is `skipped` and nothing is recorded; otherwise an intent row is inserted (UUIDv7 id, cause, rule id or
   reason). For retention `still_due` recomputes the due objects of the scope with fresh registrations and rules,
   so a put or a longer rule that arrived since the list was read stops the deletion (`skipped`). Commit.
2. The intent's record is written to the store (`record_key`, written once). A failure raises and nothing is
   deleted; the open intent is resumed by the next run.
3. **Transaction 2**, under the address lock, held for the object's deletes, the check that no version is left and
   the outcome row only (the manifests are not deleted under it): an intent another run settled meanwhile is
   `skipped`. **First** `any_version_left` is asked: when no version of the object remains (an earlier attempt deleted
   it and failed before it recorded anything) the outcome `confirmed` is inserted whatever else holds, so a half-done
   deletion is never cancelled while registrations stay live without their object. Otherwise a registration of the
   object recorded after the intent (the database's clock) means a put relied on it, and a `still_due` that now says
   no (a rule lengthened since transaction 1, an intent resumed after its cause went away) means the object is not
   due at the moment of deletion: either way the outcome `cancelled` is inserted and nothing is deleted. Otherwise
   `delete_every_version` removes the object; when `any_version_left` still finds a version the intent stays open (a
   `warning`, outcome `open`); else the outcome `confirmed` is inserted. The keys of the manifests of every
   registration of the address, live or not, are read in this transaction. Commit, then the outcome's record is
   written to the store (a failure is a `warning`, not an error: the Archivum is the authority, and a later sweep
   writes the missing records), and then the manifests are deleted, best effort: a failure is a `warning`. A manifest
   that stays is harmless: it names no object, a rebuild adopts a manifest only when its object exists, and
   `clean_incomplete` treats only a manifest without a registration as a claim on an object. The manifests are
   deleted after the commit so the lock is held no longer than the object's own delete (D37 asks no more); the
   keys are those of the registrations known under the lock, so a put that registers the content again afterwards
   keeps its own manifest.
   An `ArchiveUnavailable` from the store (an object-locked version, a refused credential, a timeout) or an
   `ArchivumUnavailable` from the Archivum (a statement, lock or call budget exceeded) rolls transaction 2 back: the
   intent stays open, the outcome is `open` with the error's message, a `warning` is logged and the run goes on with
   the next object, so one slow address never aborts a run.
- A put and a deletion of one object serialise on the address lock. A put that began after transaction 2 took it
  waits, then finds the object gone (the deletion confirmed) and stores it again, or finds it still there (the
  deletion was cancelled or stayed open). A put whose registration was committed between the two transactions
  cancels the deletion. Registrations recorded before the intent do not.
- An open intent is not an error state: the object is still stored and still registered, and `get` still works. The
  next `apply_retention` finds the object due again (it is still live) and resumes the same intent (no second
  intent row); transaction 1 does not ask `still_due` for a resumed intent, but transaction 2 always does (after the
  check for a version left), so a rule lengthened or a put registered since the intent cancels it. An intent whose
  object is not due any more is still settled: `apply_retention` passes it through the protocol too (see Retention),
  so it ends cancelled, not open. An object under object lock keeps its intent open for as long as the lock holds.
- Lock order: the address lock is taken first and held to the end of the transaction; under it a deletion inserts
  into `archive_deletion` and calls the store, a put inserts a registration. No path takes two address locks at once
  or the retention lock while holding one (reconcile takes the lock of one address at a time, in its own transaction).
  The transaction's own limits apply (the call budget, `archivum.call_budget_s`, bounds the whole deletion of one
  object including its store calls).
- A deletion record is the row as stored in `_records/` (see the layout above): an intent's and an outcome's are two
  files, written with `put_bytes_new`, so a repeated write is a no-op.

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
  and matches only keys under `prefix + "/"`; an interrupted put is a `PendingUpload` that `list_unfinished`
  finds and `abort_unfinished` removes.
- An object of 64 MiB or more is a multipart put in parts of at least 64 MiB (`part_size`: whole MiB, at most
  10 000 parts, at most 5 TiB). Each part carries its SHA-256, and the whole is hashed while read from the
  source and compared before the upload completes; a mismatch aborts the upload.
- Directory backend: a file is written to `.tmp-<hex>` beside its final name, flushed, hard-linked into place
  and the temporary removed, so the final name never holds a partial file and an existing name is never
  replaced. No symbolic link below the root is followed (each segment is opened relative to its parent with
  `O_NOFOLLOW`); a directory, link or special file where an object belongs is an `IntegrityError` naming the key
  (`head`, `read` and a put report it alike), and a listing skips a link or special file with a warning. A new
  key directory is made durable (the parent directory is flushed). A read translates only the store's own
  operating-system errors: an `OSError` raised by the caller's code while it reads (a full destination disk) is
  the caller's, never an `ArchiveUnavailable`. A key segment may not start with `.tmp-` (those are the unfinished uploads). An operating-system
  error is an `ArchiveUnavailable`.
- S3 backend (needs the extra `project-ulduar[s3]`, else `ConfigurationError`): every write is conditional
  (`If-None-Match: *`) and carries its SHA-256; a checksum the store refuses is an `IntegrityError`. Timeouts:
  connect 10 s, read 60 s, three attempts. Environment proxies are never used; `proxy_env` names one. An
  endpoint must be `https://`, or `http://` to a loopback host. A failure is an `ArchiveUnavailable` worded
  `<Operation> <key> failed: <error code>` (or the error class name for a transport error), and never carries a
  credential, a proxy address or a full endpoint URL (hard rule 11); a refused endpoint is named by its host only.
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
- Objects must sit in an immediate-retrieval storage class: `GLACIER` and `DEEP_ARCHIVE` objects, and
  INTELLIGENT_TIERING objects in the `ARCHIVE_ACCESS` or `DEEP_ARCHIVE_ACCESS` tier (`ArchiveStatus`), report
  `immediate=False` and cannot be read now. No lifecycle rule may expire or move an object.
- The directory backend's file permissions (`0o444`) are the weaker guarantee: the process that wrote a file can
  still remove it.

## Interfaces & contracts

- Public: `open_archive(config)` (a context manager yielding `Archive`), `Archive.project`, `Archive.scope`,
  `Archive.put_file(...)`, `Archive.put_stream(...)`, `PutSink` (`write`, `commit`), `Archive.get(...)`,
  `Archive.get_stream(...)`, `Archive.verify(...)`, `Archive.reconcile(...)`, `Archive.rebuild(...)`, `Verification`,
  `Reconciliation`, `Rebuild` and `Finding`, as described above; and `declare_retention(...)`,
  `apply_retention(...)` and `clean_incomplete(...)` with `ArchiveRetentionRule`, `RetentionRun`, `DueAddress`,
  `OpenIntent`, `DeletionOutcome`, `CleanupRun`, `Adoption`, `Orphan` and `UnfinishedUpload(key, started)`, which take
  a `Config` and need no `Archive`.
  An `Archive` is made only by `open_archive`.
- Private: `open_store`, `open_maintenance_store`, `WritingStore`, `DeletingStore`, `PendingUpload`, `StoredObject(key, size, modified, immediate)`,
  `check_key`, the `_layout` functions, `Manifest` and the three records,
  `_deletion.delete_address`.
- Exceptions (`ulduar.errors`): `ArchiveUnavailable`, `IntegrityError` (also a get of a missing or mismatching
  object), `ArchiveFormatError` (a foreign marker or version, a manifest or record that is not format 1 or cannot be
  read), `UnknownRecordError`
  (a scope the project lacks, for a put, a verification or retention; an unknown project when declaring; a get of an
  artifact that is not recorded or not in the archive), `UlduarError` (another manifest or retention class under a
  `record_id`, a `record_id` whose object was deleted since, a stream past the spool limit, a shortening rule not confirmed, a retention run over its limit),
  `ConfigurationError`
  for no `[archive]`, an unset variable a setting names, a missing directory, a refused endpoint, a missing
  `deleter` or a missing extra.
- Library log lines (loggers `ulduar.hodir.*`): `info` per put and per get (`archive`), per declared rule, per
  retention outcome and one summary per run (`retention`), per cleanup deletion and summary (`cleanup`), per adopted
  artifact (`_registration`), per reconciliation and a `warning` per finding (`_reconcile`), per rebuild summary
  (`_rebuild`), per sweep that wrote records (`_sync`), `debug` per stored object and deletion, `warning` for a
  skipped link, a failed abort, a verification finding, a deletion that stays open, an outcome record or a manifest
  that was not deleted or written (`_deletion`); none carries a credential or an address.

## Extending

A new backend is a module with a class pair that satisfies the two protocols, chosen in `open_store`; both
existing backends are tested by the same contract tests (`tests/test_hodir_store.py`). Never add a delete method
to a writing class (hard rule 5); the object layout and checksum definition are ask-first items.

## See also

`../README.md` · `../freya/README.md` · `../../ROADMAP.md` (Phase 2) · `../../tests/test_hodir_store.py` · `../../tests/test_hodir_put.py` · `../../tests/test_hodir_get.py` · `../../tests/test_hodir_verify.py` · `../../tests/test_hodir_retention.py` · `../../tests/test_hodir_reconcile.py`

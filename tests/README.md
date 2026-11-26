# tests

## Purpose & scope

The only test location (hard rule 4). Two kinds of test: pure tests that need nothing, and service tests
that run against the local test services — a PostgreSQL 18 server and an S3-compatible object store
(MinIO) — (hard rule 9). There are no mocks of either.

## Files & entry points

| File | Covers | Services |
|---|---|---|
| `conftest.py` | The database fixtures `db_dsn`, `db_conn` and `archivum`, the object-store fixtures `s3_settings`, `s3_client`, `s3_bucket`, the archive fixtures `archive_bucket`, `versioned_archive_bucket`, `archive_settings_s3`, `archive_settings_s3_versioned` and `archive_settings_dir` (and the helpers `temporary_bucket`, `s3_archive_settings`), the local-only guards, the `db` and `s3` markers, the required-run check; the helpers the Freya tests share: `CHECKSUM`, `digest`, `ms_ago`, `onboarded`, `recorder_of`, `partition_exists`, `failure`, `count`, `old_artifacts`, `PARTITION_LOCK_KEY` and `lineage_lock_name` (the advisory locks of step 1), and `wait_until_blocked` (returns once sessions queue for a lock, so a race test acts deterministically); the helpers the Hodir session tests share: `ARCHIVE_BACKENDS` and `ARCHIVE_SETTINGS_FIXTURES` (a directory and an unversioned bucket), `CONTENT` and `CONTENT_CHECKSUM`, the `config` and `source` fixtures, `object_key`, the fault injection `Fault`, `FaultyStore` and `faulty_archive`, `archive_config` (an onboarded config over given archive settings), `archive_file` (put one file), the `tamper` fixture (overwrite or remove a stored object as the test) and `record_confirmed_deletion` (a confirmed deletion recorded as a superuser) | — |
| `test_services.py` | The services themselves: PostgreSQL major version 18, rollback of uncommitted work, an object round trip, a fresh bucket per test; the containers bound to 127.0.0.1 and recreated on another image; the S3 client ignoring proxies; CI starting them through `dev_tools/services.py`; the dev container naming the same images, credentials and provisioning script; the writer and deleter policies (the writer cannot delete); the `services.py` command line; the fixtures' guards | both |
| `test_config.py` | `ulduar.config`: defaults, every unknown-key level, the version key, names, bounds, file errors; the `[archive]` section: both backends' defaults and full forms, keys refused on the wrong backend, bucket, prefix, region, variable-name and absolute-path rules, the writer and deleter tables, the bounds and their messages | no |
| `test_hodir_store.py` | Hodir's private storage interface, over a real directory, an unversioned bucket and a versioned bucket: put never overwrites (also with 8 writers at once), checksum and size mismatches store nothing, multipart (threshold lowered to 5 MiB) incl. a lost race and a mismatch, head, read, sorted recursive prefix-bounded list, unfinished uploads, the store prefix, the writing store without delete methods, deletion of every version and delete marker, the writer credential refused a delete by MinIO; the directory's symbolic-link and special-file refusals, modes and OS errors; key validation before any I/O; the s3 transport guard, proxies, unset variables, the missing extra, and that no secret or access key reaches an error message or an `ulduar` log record | both |
| `test_hodir_put.py` | Hodir's layout and manifest (keys, canonical round trip, strict refusals that never quote the payload, the marker), then `open_archive`, `put_file` and `put_stream` on a real Archivum over a directory and an unversioned bucket: marker and `LAYOUT.txt` written once and checked, foreign marker and version 2 refused, the object, manifest, artifact and registration of a put, the same content twice, the same `record_id` twice (and with another class), bad arguments, an unknown scope and sources refused before any storage I/O, streams (commit, no commit, an exception, the spool limit, no spool left), fault injection through a wrapper that forwards to the real store (a failure after the object, after the manifest and inside the registration transaction: no registration without its object, a rerun completes; an object that vanishes once or twice), two sessions putting the same content at once, isolation between projects, the recorder refused update, delete and truncate of the archive tables, no credential in the log | db, s3 (bucket cases) |
| `test_hodir_get.py` | `Archive.get` and `get_stream` on a real Archivum over a directory and an unversioned bucket: exact bytes by artifact and by id (several chunks too), a forged `Artifact` getting the recorded object (its id alone counts), the destination written through a partial file that never stays, the info log without the destination, a tampered object (same size, shorter, longer) and a removed one as an `IntegrityError` with no file and no partial file left (a stream gets its bytes first and the error at the end), an existing destination (file, dangling link) and a missing or non-directory parent refused before any read, a destination that appears during the get left untouched, a failure after the partial file removing it, an artifact recorded without an object, an unknown id, another project's artifact (not recorded there) and an object whose deletion was confirmed after its registration refused as not in the archive (and got again once archived again) | db, s3 (bucket cases) |
| `test_hodir_verify.py` | `Archive.verify` on a real Archivum over a directory and an unversioned bucket: a full verification of three objects (the check and one finding each read back as a superuser), the same content registered twice judged once, an empty scope still recording its check, a corrupted object (same size and other size) as `mismatch` and a removed one as `missing` with the check's failed count, the warning and summary logs, a symbolic link in an object's place as `unreadable` (directory), a cold object as `not_immediate` and never read, `ArchiveUnavailable` propagating with nothing recorded, the sample rotation (each object once, then the oldest pass; a failed object staying first; a sample larger than the scope), bad `sample` and `scope` refused before any I/O, a scope the project lacks refused with nothing read or recorded, a confirmed deletion excluding an address, another project's recorder seeing none of the checks, and `CHECK_OUTCOMES` equal to the CHECK of `archive_finding` | db, s3 (bucket cases) |
| `test_hodir_retention.py` | Hodir's retention over a real directory, an unversioned and a versioned bucket: the pure due logic (no rule, no class or no limit keeps; expiry at `keep_days`; `keep_newest` of a dataset whatever the class, none without a dataset; two classes of one object, the longer wins; the binding rule; the order), which rules shorten, the deletion record (canonical JSON, round trip, strict refusals that never quote it); `declare_retention` (a shortening rule needs confirmation and is marked, a lengthening one does not, the latest rule is read back, bad arguments before any SQL, an unknown project, no ledger variable, the retention lock); `apply_retention` (arguments, a dry run that opens no store, a real run deleting object, every version and manifest with an intent, a confirmation and both records in the store, `keep_newest`, a scope, history retention ageing the artifact out, `max_deletions_per_run` and `allow_over_limit`, a store that is unavailable, the writer credential refused by the store with the deleter resuming the same intent, an object under object lock, an object no longer due skipped after a put or a longer rule, an intent another run settled, a rule lengthened between the intent and the deletion or before a resumed intent runs again (cancelled, object kept), open intents of objects no longer due listed by a dry run and cancelled by a real one without counting towards the limit, an outcome record that cannot be written); the D37 races on two sessions (a put between the intent and the deletion cancels it; a put waiting on the address lock uploads the object again); no credential in the log of a real run | db, s3 (bucket cases) |
| `test_ids.py` | `ulduar._ids`: UUIDv7 bits, the millisecond round trip, ordering, the server's reading of the same id | db |
| `test_db.py` | `ulduar._db`: the transport guard (and the environment libpq falls back to), the DSN variable, error wrapping, the opened connection (pinned to READ COMMITTED), the statement, lock and budget limits, error translation by SQLSTATE | db |
| `test_freya_ledger.py` | The schema ledger: every step applied to an empty database, no step applied twice, checksums, breaking steps, minimum library, concurrent installs, the ledger lock, step headers; what steps 1 and 2 build (step 2 applied over an Archivum installed with step 1 only, its rows surviving; the widened history list; the registration trigger; the append-only and truncate refusals of the five archive tables and the retention mode passing only the registration and the two check histories; the partition functions covering `archive_check` and `archive_finding` with their grants kept; roles, isolation, append-only, partitions: concurrent callers, the time window, UTC bounds under another session time zone, truncate refused on partitions) | db |
| `test_freya_catalog.py` | The catalog session: opening and its refusals, the facade surface, dataset and artifact registration and lookup, idempotent retries, the partition created for an old month (two recorders at once, an edge recorder waiting meanwhile without deadlock, outside the window), every bad argument, isolation between two projects, append-only through the recording and the ledger role, `recorded_at` stamped by the database, a dataset only in its artifact's scope, `Catalog.transaction` (commit, rollback, error translation, the time limits, a partition made and the work run again) | db |
| `test_freya_history.py` | The ledger-role calls: onboarding (idempotent, one role one project), retention rules (dry run, apply, latest wins, dropped partitions, other projects untouched; no deadlock with a recorder holding locks, an edge recorder waiting for its project lock or, for the dry run, a recorder making a partition; a rule declared meanwhile honoured; among declares serialised by the retention lock the one that commits last wins), redaction, the records that can never be removed, a maintenance mode passing only its own operation, the missing ledger variable, `ledger_transaction` (commit, rollback, the time limits, the schema check, error translation, the ledger variable's message); an artifact whose object is archived not aging out until a confirmed deletion (and archived again by a later registration), its registration leaving with it, the two check histories ageing out, `HISTORIES` equal to the schema's CHECK list | db |
| `test_freya_lineage.py` | Lineage: edges between artifacts and datasets, the run reference, a missing partition created for an edge, cycles and unknown nodes refused with nothing kept, self-edge and empty parents, isolation between projects, idempotent edges by `record_ids`, opposite edges in flight together (also under a repeatable-read role default), an edge outside READ COMMITTED refused, ancestors and descendants on a diamond (each node once at its minimum depth), `max_depth`, a walk over a cycle | db |
| `test_check_conventions.py` | Convention rules C1–C9 on synthetic input, then the real repository | no |
| `test_check_docs_sync.py` | The docs-sync classifier, token rules, git parsing | no |
| `test_check_guidance_integrity.py` | Each guidance check on a miniature tree, then the real repository | no |
| `test_post_edit_check.py` | The post-edit hook: which file it checks, decision order, exit codes | no |
| `test_verify.py` | The verify step list and the service requirement it exports | no |

## How it works

- `db_conn` yields a connection to the test database and rolls its uncommitted work back when the test
  ends. Work a test commits is not undone: a test of committing code creates and drops its own schema
  until a later phase adds a fixture for that.
- `archivum` builds a throwaway Archivum for one test: a database `ulduar_test_<hex>` owned by a LOGIN role
  `ulduar_test_ledger_<hex>` with `CREATEROLE` (it is granted `ulduar_recorder` with admin option when that group
  already exists), and drops the database (`WITH (FORCE)`) and every role it made afterwards. Its DSNs are
  exported to environment variables through `monkeypatch`, so the library under test reads them as a
  consumer would. The group role `ulduar_recorder` itself is cluster-wide and stays.
- `s3_bucket` creates a bucket with a fresh `ulduar-test-` name and removes it, with its objects, when
  the test ends. `s3_client` reads its endpoint and keys from the settings only, never from an AWS
  profile or environment variable.
- `archive_bucket` and `versioned_archive_bucket` make a fresh `ulduar-test-` bucket (the second with versioning
  enabled) and remove it with every object version, delete marker and unfinished multipart upload.
  `archive_settings_s3` and `archive_settings_s3_versioned` return an `ArchiveSettings` for such a bucket that
  names the test variables below for the endpoint, the writer and the deleter and sets them (through
  `monkeypatch`) to the guarded values, so a test never names a credential; `archive_settings_dir` returns a
  directory archive under `tmp_path`. Archive tests reach a store only through `open_store(settings)` or `open_archive(config)`; fault injection wraps the
  store `open_store` returns in a class of the test file that forwards every call (hard rule 9, H22).
- Every test that takes a database fixture is marked `db`, every test that takes an object-store fixture
  `s3`, automatically. A test that carries an explicit `s3` or `db` marker (a parameter of
  `test_hodir_store.py`'s backends, where only the bucket cases are marked) counts towards the required-services
  switch the same way.
- The settings come from `ULDUAR_TEST_DSN` and `ULDUAR_TEST_S3_ENDPOINT`, `ULDUAR_TEST_S3_ACCESS_KEY`,
  `ULDUAR_TEST_S3_SECRET_KEY`, and the archive's two credentials `ULDUAR_TEST_S3_WRITER_ACCESS_KEY`,
  `ULDUAR_TEST_S3_WRITER_SECRET_KEY` (a user that cannot delete), `ULDUAR_TEST_S3_DELETER_ACCESS_KEY`,
  `ULDUAR_TEST_S3_DELETER_SECRET_KEY` (names and defaults: `dev_tools/services.py`); an object-store test
  skips or fails, per the table below, when any of the seven is unset. Every database `host` and
  `hostaddr` entry, as libpq reads them, must be `localhost`, `127.0.0.1` or `::1`; a host list or a
  `host=` / `hostaddr=` parameter naming any other host, a service entry or a missing host fails the
  test, and so does a set `PGHOSTADDR`, `PGSERVICE` or `PGSERVICEFILE`. The object-store endpoint must
  be an `http` or `https` URL on one of the same hosts, and the S3 client ignores `HTTP_PROXY` /
  `HTTPS_PROXY`, so no request leaves this machine.
- With `ULDUAR_REQUIRE_SERVICES=1` a run in which no database test or no object-store test passed fails
  as a whole, so deselecting the service tests cannot produce a green run.

| How pytest is run | Settings unset | Service unreachable |
|---|---|---|
| `pytest` | service tests skip with a notice | service tests fail |
| `python dev_tools/verify.py` | fall back to the local defaults | service tests fail |
| CI (`ULDUAR_REQUIRE_SERVICES=1`) | service tests fail | service tests fail |
| pre-push hook (`-m "not db and not s3"`, `ULDUAR_REQUIRE_SERVICES` unset) | service tests not selected | service tests not selected |

## Interfaces & contracts

- Start the services: `python dev_tools/services.py start`; their settings:
  `python dev_tools/services.py env`.
- A test never opens its own connection or S3 client, never names a connection string and never skips
  itself (checker rules C6 and C9). A test that needs a second connection takes it from the conftest
  helper `rolled_back_connection`, fed the guarded `db_dsn`. The two tests whose subject is a
  connection string may spell one: `test_services.py` (the guards) and `test_check_conventions.py`
  (the checker).
- `archivum.config(tmp_path, project=..., recorder=..., **timeouts)` writes an `ulduar.toml` naming the ledger
  and a recording role's variables and returns the loaded `Config`; `archivum.new_recorder()` makes a recording
  login role that belongs to no project until onboarded (a test may `GRANT ulduar_recorder` to it as
  superuser); `archivum.superuser()` yields an autocommit superuser connection to the throwaway database for
  a test that must tamper with or inspect it.
- With `ULDUAR_REQUIRE_SERVICES=1` exported, a targeted run that does not select a test of each service
  exits 1 by design.
- A changed behaviour means updating its test deliberately in the same PR, never loosening an assertion.

## Extending

Write a database test by taking `db_conn` (work that is rolled back) or `archivum` (a test that commits, installs
the schema or needs roles), an object-store test by taking `s3_bucket` and `s3_client`.
Create what the test needs inside the test; the rollback and the bucket removal clean up. Add the file
to the table above.

## See also

`dev_tools/README.md` · `.claude/skills/ulduar-development/references/qa-and-ci.md`

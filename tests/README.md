# tests

## Purpose & scope

The only test location (hard rule 4). Two kinds of test: pure tests that need nothing, and service tests
that run against the local test services — a PostgreSQL 18 server and an S3-compatible object store
(MinIO) — (hard rule 9). There are no mocks of either.

## Files & entry points

| File | Covers | Services |
|---|---|---|
| `conftest.py` | The database fixtures `db_dsn`, `db_conn` and `archivum`, the object-store fixtures `s3_settings`, `s3_client` and `s3_bucket`, the local-only guards, the `db` and `s3` markers, the required-run check | — |
| `test_services.py` | The services themselves: PostgreSQL major version 18, rollback of uncommitted work, an object round trip, a fresh bucket per test; the containers bound to 127.0.0.1 and recreated on another image; the S3 client ignoring proxies; CI starting them through `dev_tools/services.py`; the dev container naming the same images and credentials; the `services.py` command line; the fixtures' guards | both |
| `test_config.py` | `ulduar.config`: defaults, every unknown-key level, the version key, names, bounds, file errors | no |
| `test_ids.py` | `ulduar._ids`: UUIDv7 bits, the millisecond round trip, ordering, the server's reading of the same id | db |
| `test_db.py` | `ulduar._db`: the transport guard (and the environment libpq falls back to), the DSN variable, error wrapping, the opened connection, the statement, lock and budget limits, error translation by SQLSTATE | db |
| `test_freya_ledger.py` | The schema ledger: every step applied to an empty database, no step applied twice, checksums, breaking steps, minimum library, concurrent installs, the ledger lock, step headers; what step 1 builds (roles, isolation, append-only, partitions) | db |
| `test_freya_catalog.py` | The catalog session: opening and its refusals, dataset and artifact registration and lookup, idempotent retries, the partition created for an old month, every bad argument, isolation between two projects, append-only through the recording and the ledger role; the helpers `onboarded`, `ms_ago`, `partition_exists`, `failure` that the other Freya tests import | db |
| `test_freya_history.py` | The ledger-role calls: onboarding (idempotent, one role one project), retention rules (dry run, apply, latest wins, dropped partitions, other projects untouched), redaction, the records that can never be removed, the missing ledger variable | db |
| `test_freya_lineage.py` | Lineage: edges between artifacts and datasets, the run reference, a missing partition created for an edge, cycles and unknown nodes refused with nothing kept, self-edge and empty parents, isolation between projects, ancestors and descendants on a diamond (each node once at its minimum depth), `max_depth` | db |
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
- Every test that takes a database fixture is marked `db`, every test that takes an object-store fixture
  `s3`, automatically.
- The settings come from `ULDUAR_TEST_DSN` and `ULDUAR_TEST_S3_ENDPOINT`, `ULDUAR_TEST_S3_ACCESS_KEY`,
  `ULDUAR_TEST_S3_SECRET_KEY` (names and defaults: `dev_tools/services.py`). Every database `host` and
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

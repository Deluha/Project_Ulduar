# Workflow: Adding a Component or Module

For the first module of a component (`references/architecture.md#components`) or a new module inside one.
Rule: R10 (`references/conventions.md#r10`).

## Preconditions

- The component is in the current phase of `ROADMAP.md` and its sign-off items are confirmed and recorded
  there. A component from a later phase is not started early.
- ⚑ Its public surface (names, signatures, exceptions, CLI commands, configuration keys and exit codes) is
  ask-first.

## Steps

1. Grep for an existing helper before writing one (`references/recipes.md#grep`); shared helpers have one
   home (R2).
2. Create the module with a module docstring; state its public names explicitly.
3. Keep database access behind functions that take a connection and archive access behind the storage
   interface; compose SQL per R3.
4. Library code logs, it does not print; only the `mimiron` entry module parses a command line.
5. Write the tests with the code: pure logic as plain tests, anything touching the database or the archive
   through the conftest fixtures (R9, `tests/README.md`).
6. Name no consumer and assume no consumer's schema or layout (R1); write nothing to a consumer's systems
   (R6); keep credentials out of logs and records (R11).

## Verification

Run `references/qa-and-ci.md#verify` with the test services up.

- [ ] Every public function has a test; every SQL statement and storage call runs in at least one service test
- [ ] `python dev_tools/check_conventions.py` is clean

## Docs to update

- `ulduar/README.md` (files and public surface)
- `references/architecture.md` (the module map or the components table, when a row changes)
- `tests/README.md` (the inventory row)
- `RELEASE_NOTES.md` (the in-development entry)

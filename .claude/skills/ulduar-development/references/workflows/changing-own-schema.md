# Workflow: Changing the Archivum's Schema

For a change to an object Ulduar itself owns in the Archivum: a table, view, index or function of the
catalog, the lineage, the run ledger or the verdict history. Rules: R5 and R7
(`references/conventions.md#r5`, `references/conventions.md#r7`). The first such objects arrive in Phase 1
(`ROADMAP.md`); that phase adds the file and module names this workflow points at.

## Preconditions

- ⚑ The Archivum schema is ask-first: the owner confirms the exact DDL before code is written.
- The change is additive wherever possible. A consumer's existing Archivum must stay readable after the
  upgrade; a change that cannot keep that promise stops here and goes to the owner.

## Steps

1. Grep every statement that touches the object (`references/recipes.md#grep`).
2. Express the change as a new versioned SQL step, applied once and recorded in the Archivum's ledger
   table (R7). Never edit a step that has shipped in a release.
3. The step format and what the ledger records per step are fixed at the Phase 1 sign-off (`ROADMAP.md`).
4. Compose every statement per R3 (`references/conventions.md#r3`).
5. No step updates, deletes or truncates recorded history (R5); the declared retention and the redaction of
   history are run by the ledger role, never by a step.

## Verification

Run `references/qa-and-ci.md#verify`, then:

- [ ] A database test installs the previous release's schema, applies the new step and reads old rows back
- [ ] A database test applies every step in order to an empty database
- [ ] Running the ledger again applies no step a second time

## Docs to update

- `ulduar/README.md` (the object's contract)
- `RELEASE_NOTES.md` (an upgrade note when a consumer must act)

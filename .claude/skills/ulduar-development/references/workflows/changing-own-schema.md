# Workflow: Changing the Archivum's Schema

For a change to an object Ulduar itself owns in the Archivum: a table, view, index or function of the
catalog, the lineage, the run ledger or the verdict history. Rules: R5 and R7
(`references/conventions.md#r5`, `references/conventions.md#r7`). Steps live in
`ulduar/freya/steps/NNNN_<name>.sql`; `ulduar/freya/ledger.py` applies them.

## Preconditions

- ⚑ The Archivum schema is ask-first: the owner confirms the exact DDL before code is written.
- The change is additive wherever possible. A consumer's existing Archivum must stay readable after the
  upgrade; a change that cannot keep that promise stops here and goes to the owner.

## Steps

1. Grep every statement that touches the object (`references/recipes.md#grep`).
2. Express the change as a new versioned SQL step, applied once and recorded in the Archivum's ledger
   table (R7). Never edit a step that has shipped in a release.
3. The step file carries the headers `-- ulduar-kind: additive|breaking` and `-- ulduar-min-library: X.Y.Z`.
   The ledger table `ulduar.schema_ledger` records step, name, kind, SHA-256, minimum library version,
   applying version and time. A released step is never edited (its checksum is checked). A breaking step
   needs `install(..., allow_breaking=True)` and raises the minimum library version.
4. Compose every statement per R3 (`references/conventions.md#r3`).
5. No step updates, deletes or truncates recorded history, nor runs its retention or redaction (R5).

## Verification

Run `references/qa-and-ci.md#verify`, then:

- [ ] A database test installs the previous release's schema, applies the new step and reads old rows back
- [ ] A database test applies every step in order to an empty database
- [ ] Running the ledger again applies no step a second time
- [ ] The tests live in `tests/test_freya_ledger.py` and use the `archivum` fixture

## Docs to update

- `ulduar/README.md` (the object's contract)
- `RELEASE_NOTES.md` (an upgrade note when a consumer must act)

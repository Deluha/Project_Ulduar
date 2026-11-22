# Project Ulduar — Development Guide

A generic Python library and CLI that gives a data project a vault and someone watching it: a catalog and lineage
(Freya, its database the Archivum), a cold archive (Hodir), a run ledger (Thorim), monitoring (Algalon) and the
`mimiron` CLI with its connectors (Mimiron). Every fact lives in ONE file: this one holds the contract, rules and
ask-first gates; the skill (`.claude/skills/ulduar-development/SKILL.md` — read it first) routes each task to its
authoritative file, module detail to that module's `README.md`. Planned scope and phase process: `ROADMAP.md`.
Paths below that start with `references/` are under `.claude/skills/ulduar-development/`.

## Operating principles (NON-NEGOTIABLE)

1. **Flag uncertainties — ask, don't assume.** If intent, a design choice or a consumer-facing behaviour is unclear,
   ask before writing code. Validate every request against repository patterns first; if it conflicts with an
   established pattern, flag it — never silently comply. Unattended: take the most reasonable reading and record the
   assumption explicitly in the reply. The *Ask first* list below and workflow-file ⚑ marks are mandatory gates.
2. **The orchestrator designs and signs off; the writer implements; the reader reads; the reviewer reviews.** The
   session model owns ⚑ gates, design, briefs and the done gate (reads the diff, runs the verify). Repo edits →
   `implementer` (`effort: "high"` on the call for bug fixes, schema, archive and integrity work, edge cases) —
   except a small edit, which the orchestrator makes itself: one file, ≤ ~20 changed lines, no ⚑ item, no new
   logic. Reads spanning more than three files → `reader`; diff review → `reviewer`; never `general-purpose` for
   edits, never a `model` on the call. Briefs, effort, reruns → `references/recipes.md#orchestration`
3. **Be concise.** Lead with the outcome; checklists and tables over prose; no filler. Never suppress the required
   signals: a confidence label (High/Medium/Low) on architectural calls, a flag for pattern conflicts or spotted
   smells, for/against on non-trivial approaches, and a better approach when one exists.
4. **Modular — single source of truth.** One canonical definition per helper and constant; few surgical changes over
   sweeping refactors; every PR leaves touched files clearer, more concise and more modular than it found them.

**Workflow for any change:** understand the request (⚑ if ambiguous) → route through the skill and read the routed
file → grep the established pattern first (`references/recipes.md#grep`) → validate against patterns, flag conflicts →
implement → verify (`references/qa-and-ci.md#verify`) → update the changed area's docs → simplicity audit
(`references/conventions.md#simplicity`) and the `reviewer` agent on every non-trivial diff.

## Hard rules

Binding short form; the full form of rule N is section RN of `references/conventions.md`.

1. **Generic core** — no consumer's name, schema, table or convention inside the package; a consumer integrates
   through an adapter and a configuration file in its own repository.
2. **One canonical helper** — shared helpers are defined once and imported, never re-implemented; library code logs
   through `logging`, CLI output goes through the console helpers, never raw `print`.
3. **Safe SQL** — an identifier is composed with the driver's quoting, a value is bound as a parameter; never
   string formatting.
4. **No files the user didn't ask for** — `tests/` is the only test location, no markdown beyond the permanent docs.
5. **The vault is immutable** — an archived object is never overwritten; deletion happens only by a declared
   retention rule and is itself recorded; catalog entries, runs and verdicts are corrected by a new record, never
   by an edit.
6. **Read-only towards consumers** — never write to a consumer's database or source folder; read through a role
   that can only read; write only to the Archivum and the archive store.
7. **Own schema through the ledger** — the Archivum's tables are created and changed through the
   `caverns-of-time` ledger, never by ad-hoc DDL.
8. **Docs move with the code** — a documented area's change updates its `README.md` in the same PR (`[skip-docs]`:
   escape hatch).
9. **Real services in tests** — database behaviour on PostgreSQL 18, archive behaviour on an S3-compatible server,
   both local throwaway instances; never a mock; a skipped service test fails the done gate; tests never connect
   to a consumer's database, Drive folder or bucket.
10. **One module per component, flow-only entry** — each component sits behind its own public facade; the
    `mimiron` entry holds only command flow.
11. **No secrets stored or logged** — credentials arrive from the environment at run time; none is stored in the
    Archivum, written to a log or output, or committed.

## Ask first

⚑ Never change without explicit user confirmation (workflow files add task-specific ⚑ gates) — a wrong guess
corrupts a consumer's vault, its recorded history or its integration:

- The Archivum schema; the archive's object layout and checksum definition.
- Public API and CLI surface: signatures, commands, flags, output layout, exit codes, the configuration format.
- Any deletion from the archive and every retention default.
- Algalon's default thresholds; any relaxation of a check, guard or threshold.
- Adding a runtime dependency; the supported PostgreSQL versions; the licence; the scope rules of `ROADMAP.md`.
- Connecting any test, workflow or session to a system other than the local test services.
- The guidance machinery and CI: `CLAUDE.md`, `.claude/settings.json`, `.claude/agents/`, `.claude/hooks/`,
  `.github/workflows/`, `.pre-commit-config.yaml`, `.gitleaks.toml` (path-gated by `permissions.ask`; changing
  one: `references/workflows/changing-guidance.md`).

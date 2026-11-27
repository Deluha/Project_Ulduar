# Checkers — what each guard parses

Read before editing a checker or a line a checker greps. Gates and when they run:
`references/qa-and-ci.md#gates`.

## checker-project-conventions

`dev_tools/check_conventions.py` — pure functions over (path, source) pairs and a file list; `main()` feeds
it the tracked and the untracked-but-not-ignored files. Python is parsed with `ast`; a file with a syntax
error is left to ruff. Tests: `tests/test_check_conventions.py`.

| Rule | Hard rule | Scope | Fails on | Exempt |
|---|---|---|---|---|
| C1 | R1 | every tracked or untracked-but-not-ignored text file (binary skipped), and every path | a letter-run or path containing a word whose SHA-256 digest is in the deny-list (`FORBIDDEN_NAME_DIGESTS`, with the word lengths in `FORBIDDEN_NAME_LENGTHS`); the words are never written in the repository and the message shows only a digest prefix | none |
| C2 | R2 | package `.py` | a call to `print()` | the package's `console` module |
| C3 | R3 | package `.py` | plain text formatted into the first argument of `execute` / `executemany`: an f-string, or `%`, `+` or `.format()` on a string literal | the driver's own composition objects (no string-literal operand) |
| C4 | R4 | all files | a `test_*.py` / `*_test.py` outside `tests/`; a `.md` that is not a permanent doc, a `README.md` or under `.claude/` | — |
| C5 | style | package `.py` | a module without a docstring | — |
| C6 | R9 | `tests/*.py` | a call to `connect` (bare or as an attribute); an import of `connect`; an import of an S3 client library (`boto3`, `botocore`); a string containing a PostgreSQL connection-string scheme | `tests/conftest.py`; for the string only, the two tests whose subject is a connection string (`tests/test_services.py`, `tests/test_check_conventions.py`) |
| C7 | R10 | package `.py` | an import of `argparse`, `click` or `typer` | the package's `mimiron` module |
| C8 | R11 | all files | a tracked `.env*`, `*.pem`, `*.pfx`, `*.key`, `*.egg-info` or `settings.local.json` | — |
| C9 | R9 | `tests/*.py` | the names `skip`, `skipif`, `xfail`, `importorskip`, `SkipTest`, `skipTest`, `skipIf`, `skipUnless` or `expectedFailure`, as an attribute, a bare name or an import | `tests/conftest.py` |
| C10 | R10 | package `.py` inside a component (a sub-package `ulduar/<name>/` whose name does not start with `_`) | an import of `ulduar.<Y>.<anything>`, or of a `_`-prefixed name from the facade `ulduar.<Y>`, for another component Y; the facade itself, its public names, the shared `ulduar._*` modules, the file's own component and relative imports are allowed | — |

R5, R6, R7, R11 beyond C8, and the parts of R1, R3 and R10 a parser cannot see (that the `mimiron` entry holds
command flow only, that a facade only re-exports, a top-level module reaching into a component) are review-enforced
(`references/conventions.md`). C3 sees only a formatted string passed directly; a statement formatted on an
earlier line, or built from two variables, is the reviewer's to catch.

## checker-docs-sync

`dev_tools/check_docs_sync.py` — fails a change that touches code in a documented area without touching
that area's doc. Modes: `--staged <commit-msg-file>` (commit-msg hook) and `--base <ref>` (verify, CI).
Tests: `tests/test_check_docs_sync.py`.

| Area | Counts as code | Its doc |
|---|---|---|
| `ulduar/` | `.py`, `.sql` | `ulduar/README.md` |
| `dev_tools/` | `.py`, `.sql` | `dev_tools/README.md` |
| `tests/` | `.py`, `.sql` | `tests/README.md` |
| `.github/workflows/` | `.yml`, `.yaml` | `references/qa-and-ci.md` |
| `.claude/agents/` | `.md` | `references/recipes.md` |
| `.claude/hooks/` | `.py`, `.sh` | `references/qa-and-ci.md` |

- A touch of `CLAUDE.md`, the main skill's `SKILL.md` or any file under its `references/` satisfies every
  area. Evals and command skills are not documentation.
- Adds, modifications, renames and deletions all count.
- `[skip-docs]` or `[docs-ok]` on a line of its own in a commit message exempts that commit's files; in
  `--base` mode a file is exempt only when every commit touching it carries the token.
- Dependabot's own PRs are exempt in `.github/workflows/` only (`--actor`).
- A git failure exits 2, never green. The guard cannot judge the prose; it only forces the touch.

## checker-guidance-integrity

`dev_tools/check_guidance_integrity.py` — scans `CLAUDE.md`, every skill file, `.claude/rules/`, the command
skills, the root docs, every `README.md` up to three levels deep and the agent files. Tests:
`tests/test_check_guidance_integrity.py`.

| Check | Fails on |
|---|---|
| Pointers | A backticked path containing `/` and ending `.md`, `.py`, `.sql`, `.sh`, `.json`, `.yml` or `.toml` that resolves neither from the repo root, the doc's directory, the skill root, the skill's `references/`, nor as a unique path suffix. Placeholders (`{}`, `<>`, `*`, `…`) are skipped |
| Bare docs | A backticked upper-case doc name (such as `README.md`) that resolves neither next to the doc, at the repo root, in the skill, nor uniquely repo-wide |
| Anchors | `file.md#anchor` where no heading of the target has that slug, starts with it, or has it as first token (`#r7` matches `## R7 …`) |
| Budget | `CLAUDE.md` over 80 lines or 8 KiB (⚑ raising it is ask-first) |
| Skill structure | `SKILL.md` without `name:` / `description:` frontmatter; a `references/` or `workflows/` file not named in it |
| Routing | A reference file named in more than one routing-table row |
| Single home | A rule token (`R<n>`, `C<n>`) heading a `##` / `###` section in more than one doc |
| Rule files | A `.claude/rules/*.md` without `paths:` frontmatter, with more than 6 non-blank body lines, or without a pointer |
| Command skills | A command skill without frontmatter or without a pointer |
| Final newline | A scanned doc that does not end with exactly one newline |

A path the docs name but that does not exist yet fails the pointer check: name a planned module in
prose, without a backticked path, until its phase creates it.

## checker-secret-scan

gitleaks with the default ruleset and `.gitleaks.toml`. Locally the pre-commit hook scans the staged
changes; in CI the hook is skipped (it would see nothing) and `ci-pre-commit.yml` runs a pinned,
checksum-verified gitleaks over the PR's commit range and the working tree. The version in the workflow
must equal the hook `rev` in `.pre-commit-config.yaml` (the workflow checks it); both are bumped by hand
with the sha256 from the release's checksums file. A real finding is fixed and the credential rotated;
⚑ an allow-list entry is ask-first. The test services' credentials in `dev_tools/services.py` are
throwaways for local instances that hold no data.

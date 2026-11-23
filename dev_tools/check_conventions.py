"""Mechanical convention checks C1–C9 for the hard rules of CLAUDE.md.

Each check is a pure function over (path, source) pairs or a file list, so the
tests feed it synthetic input; main() wires the tracked files of the real
repository. Exit 0 = clean, 1 = violations, 2 = a git failure (never green).
Stdlib only. Rule table and exemptions: the skill's
references/checkers.md#checker-project-conventions.
"""

from __future__ import annotations

import ast
import hashlib
import re
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from dev_tools.check_docs_sync import GitError, git  # noqa: E402

PACKAGE = "ulduar/"
TESTS = "tests/"

# C1: SHA-256 hex digests of lower-case words that name another project; no tracked text file or path may contain
# one inside a run of letters (the words themselves are kept out of the repository). To add one: append
# hashlib.sha256(word.encode()).hexdigest() and its len(word) to FORBIDDEN_NAME_LENGTHS.
FORBIDDEN_NAME_DIGESTS = frozenset({
    "07de97ba846bea2c77ab629017947804aa3bd8880dd65eea0cf5357b9cedf6b1",
    "382c41b564afb6e20445b140900ce246294d9deeaab6fde8eb7e1227e3a4bd01",
    "025052e1abee33b26fd59c106fe30649dd05066698cdf8a2d4dee089b90940ce",
    "31bb52dab0e870909011543ca762f0cbe56b182be748f42f905407b079e6d639",
    "1c1a31afd099e7a8025eb24230c224edcfb86a84f63294ab5febbc5eab8396f4",
    "f76ea091b81b488eaff139e449ca2b80ebb985cd94d901bc0108349158fd78ed",
})  # fmt: skip
FORBIDDEN_NAME_LENGTHS = frozenset({6, 7, 8, 9, 10})  # the lengths of the words behind the digests above
WORD_RE = re.compile(r"[A-Za-z]+")
# C2: the one package module allowed to call print().
CONSOLE_MODULE = PACKAGE + "console.py"
# C3: cursor / connection methods whose first argument is a SQL statement.
EXECUTE_METHODS = {"execute", "executemany"}
# C4: markdown allowed outside `.claude/` and beyond one README.md per directory.
PERMANENT_DOCS = {
    "CLAUDE.md", "README.md", "ROADMAP.md", "RELEASE_NOTES.md", "CONTRIBUTING.md", "SECURITY.md",
    ".github/pull_request_template.md",
}  # fmt: skip
TEST_FILE_RE = re.compile(r"(^|/)(test_[^/]*|[^/]*_test)\.py$")
# C6: the only test-side file that may open a database connection or an S3 client, or name a DSN.
CONNECTION_OWNERS = {TESTS + "conftest.py"}
# C6: tests whose subject is a connection string (the fixtures' guard, this checker); they may spell one.
DSN_LITERAL_OWNERS = CONNECTION_OWNERS | {TESTS + "test_services.py", TESTS + "test_check_conventions.py"}
# C6: S3 client libraries; a test gets its client from the conftest fixtures.
S3_CLIENT_MODULES = {"boto3", "botocore"}
# C6: connection-string schemes a test may not spell.
DSN_SCHEMES = ("postgresql://", "postgres://")
# C9: names that skip or excuse a test.
SKIP_NAMES = {
    "skip", "skipif", "xfail", "importorskip", "SkipTest", "skipTest", "skipIf", "skipUnless", "expectedFailure",
}  # fmt: skip
# C7: command-line parsing lives in the Mimiron entry module only.
CLI_MODULES = {"argparse", "click", "typer"}
CLI_ENTRY = PACKAGE + "mimiron.py"
# C8: secrets and build artifacts that must never be tracked.
FORBIDDEN_TRACKED_RE = re.compile(r"(^|/)(\.env(\..*)?|[^/]*\.(pem|pfx|key|egg-info)|settings\.local\.json)(/|$)")

Source = tuple[str, str]


def _parse(path: str, source: str) -> ast.Module | None:
    try:
        return ast.parse(source, filename=path)
    except SyntaxError:
        return None  # ruff reports syntax errors; this checker stays quiet on them


def _package_sources(sources: Iterable[Source]) -> Iterator[Source]:
    return ((p, s) for p, s in sources if p.startswith(PACKAGE) and p.endswith(".py"))


def _name_digests(run: str) -> list[str]:
    """Digests of the forbidden words contained in a run of letters, case-folded (sliding window per length)."""
    run = run.lower()
    hits = {
        digest
        for length in FORBIDDEN_NAME_LENGTHS
        for start in range(len(run) - length + 1)
        if (digest := hashlib.sha256(run[start : start + length].encode()).hexdigest()) in FORBIDDEN_NAME_DIGESTS
    }
    return sorted(hits)


def check_generic_core(sources: Iterable[Source]) -> list[str]:
    """C1 (R1): no tracked text file or path contains the name of another project; a word is found by its
    SHA-256 digest, so the report never spells it."""
    out = []
    cache: dict[str, list[str]] = {}  # each distinct run of letters is hashed once

    def digests(text: str) -> list[str]:
        found: dict[str, None] = {}
        for run in dict.fromkeys(WORD_RE.findall(text)):
            if run not in cache:
                cache[run] = _name_digests(run)
            found.update(dict.fromkeys(cache[run]))
        return list(found)

    for path, source in sources:
        for digest in digests(path):
            out.append(f"{path}: C1 name of another project in the path (digest {digest[:8]}…) (R1)")
        for number, line in enumerate(source.splitlines(), 1):
            for digest in digests(line):
                out.append(f"{path}:{number}: C1 name of another project (digest {digest[:8]}…) (R1)")
    return out


def check_no_print(sources: Iterable[Source]) -> list[str]:
    """C2 (R2): package code never calls print() outside the console module."""
    out = []
    for path, source in _package_sources(sources):
        tree = _parse(path, source)
        if tree is None or path == CONSOLE_MODULE:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print":
                out.append(
                    f"{path}:{node.lineno}: C2 print() in package code — use logging or the console helpers (R2)"
                )
    return out


def _is_text(node: ast.expr) -> bool:
    return isinstance(node, ast.JoinedStr) or (isinstance(node, ast.Constant) and isinstance(node.value, str))


def _is_formatted_string(node: ast.expr) -> bool:
    """A statement built by formatting plain text: an f-string, `"…" % x`, `"…" + x`, `"…".format(x)`.

    The driver's own composition (`sql.SQL("…").format(sql.Identifier(t))`, `sql.SQL(…) + sql.Identifier(t)`)
    has no plain-text operand and is not reported.
    """
    if isinstance(node, ast.JoinedStr):
        return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        return _is_text(node.left)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return any(_is_text(side) or _is_formatted_string(side) for side in (node.left, node.right))
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "format"
        and _is_text(node.func.value)
    )


def check_safe_sql(sources: Iterable[Source]) -> list[str]:
    """C3 (R3): no f-string, `%`, `+` or .format() statement passed straight to execute()."""
    out = []
    for path, source in _package_sources(sources):
        tree = _parse(path, source)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in EXECUTE_METHODS
                and node.args
                and _is_formatted_string(node.args[0])
            ):
                out.append(
                    f"{path}:{node.lineno}: C3 string-formatted SQL passed to {node.func.attr}() — "
                    "compose identifiers with the driver's quoting (R3)"
                )
    return out


def check_file_locations(files: Iterable[str]) -> list[str]:
    """C4 (R4): tests live under tests/ only; no markdown beyond the permanent docs."""
    out = []
    for path in files:
        if TEST_FILE_RE.search(path) and not path.startswith(TESTS):
            out.append(f"{path}: C4 test file outside tests/ (R4)")
        if path.endswith(".md") and not (
            path in PERMANENT_DOCS or path.startswith(".claude/") or path.rsplit("/", 1)[-1] == "README.md"
        ):
            out.append(f"{path}: C4 markdown file beyond the permanent docs (R4)")
    return out


def check_module_docstrings(sources: Iterable[Source]) -> list[str]:
    """C5: every package module opens with a module docstring."""
    out = []
    for path, source in _package_sources(sources):
        tree = _parse(path, source)
        if tree is not None and ast.get_docstring(tree) is None:
            out.append(f"{path}:1: C5 module docstring missing")
    return out


def _called_name(node: ast.Call) -> str | None:
    """The bare or attribute name a call invokes: `connect` for both `connect()` and `x.connect()`."""
    if isinstance(node.func, ast.Name):
        return node.func.id
    return node.func.attr if isinstance(node.func, ast.Attribute) else None


def _imported_modules(node: ast.AST) -> list[tuple[int, str]]:
    """(line, module) per module an import statement names: `a.b` for `import a.b` and `from a.b import c`."""
    if isinstance(node, ast.Import):
        return [(node.lineno, alias.name) for alias in node.names]
    if isinstance(node, ast.ImportFrom) and node.module:
        return [(node.lineno, node.module)]
    return []


def check_test_connections(sources: Iterable[Source]) -> list[str]:
    """C6 (R9): tests reach the database and the object store through the conftest fixtures only."""
    out = []
    fixture = "take the conftest fixture (R9)"
    for path, source in sources:
        if not (path.startswith(TESTS) and path.endswith(".py")) or path in CONNECTION_OWNERS:
            continue
        tree = _parse(path, source)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _called_name(node) == "connect":
                out.append(f"{path}:{node.lineno}: C6 connect() in a test — {fixture}")
            if isinstance(node, ast.ImportFrom) and any(alias.name == "connect" for alias in node.names):
                out.append(f"{path}:{node.lineno}: C6 `connect` imported in a test — {fixture}")
            for lineno, name in _imported_modules(node):
                if name.split(".")[0] in S3_CLIENT_MODULES:
                    out.append(f"{path}:{lineno}: C6 `{name}` imported in a test — {fixture}")
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and any(scheme in node.value for scheme in DSN_SCHEMES)
                and path not in DSN_LITERAL_OWNERS
            ):
                out.append(f"{path}:{node.lineno}: C6 connection string in a test — {fixture}")
    return out


def check_cli_entry(sources: Iterable[Source]) -> list[str]:
    """C7 (R10): only the Mimiron entry module parses a command line."""
    out = []
    for path, source in _package_sources(sources):
        tree = _parse(path, source)
        if tree is None or path == CLI_ENTRY:
            continue
        for node in ast.walk(tree):
            for lineno, name in _imported_modules(node):
                if name.split(".")[0] in CLI_MODULES:
                    out.append(f"{path}:{lineno}: C7 `{name}` imported outside {CLI_ENTRY} (R10)")
    return out


def check_forbidden_tracked(files: Iterable[str]) -> list[str]:
    """C8 (R11): no secret or build artifact is tracked."""
    return [f"{path}: C8 secret or build artifact tracked" for path in files if FORBIDDEN_TRACKED_RE.search(path)]


def check_test_skips(sources: Iterable[Source]) -> list[str]:
    """C9 (R9): a test never skips itself; only conftest decides, so verify can require the services."""
    out = []
    for path, source in sources:
        if not (path.startswith(TESTS) and path.endswith(".py")) or path in CONNECTION_OWNERS:
            continue
        tree = _parse(path, source)
        if tree is None:
            continue
        found: list[tuple[int, str]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                found.append((node.lineno, node.attr))
            elif isinstance(node, ast.Name):
                found.append((node.lineno, node.id))
            elif isinstance(node, ast.ImportFrom):
                found += [(node.lineno, alias.name) for alias in node.names]
        for lineno, name in sorted(found):
            if name in SKIP_NAMES:
                out.append(f"{path}:{lineno}: C9 `{name}` in a test — a skipped test fails the done gate (R9)")
    return out


def run_all(files: list[str], sources: list[Source]) -> list[str]:
    return (
        check_generic_core(sources)
        + check_no_print(sources)
        + check_safe_sql(sources)
        + check_file_locations(files)
        + check_module_docstrings(sources)
        + check_test_connections(sources)
        + check_cli_entry(sources)
        + check_forbidden_tracked(files)
        + check_test_skips(sources)
    )


def repo_inputs(repo_root: Path = REPO_ROOT) -> tuple[list[str], list[Source]]:
    """Tracked plus untracked-but-not-ignored files that exist, and the text of every non-binary one
    (C1 reads them all; the other rules pick their files by path)."""
    listed = git("ls-files", "-z", "--cached", "--others", "--exclude-standard").split("\0")
    files = sorted({f for f in listed if f and (repo_root / f).is_file()})
    sources = []
    for f in files:
        data = (repo_root / f).read_bytes()
        if b"\0" not in data:  # a file with a NUL byte is binary: no text to scan
            sources.append((f, data.decode("utf-8", errors="replace")))
    return files, sources


def main() -> int:
    try:
        violations = run_all(*repo_inputs())
    except GitError as e:
        print(f"❌ conventions: {e}")
        return 2
    if violations:
        print("❌ convention violations:\n")
        for v in violations:
            print(f"  - {v}")
        print(
            "\nSee .claude/skills/ulduar-development/references/"
            "checkers.md#checker-project-conventions for the rule table."
        )
        return 1
    print("✅ conventions C1–C9 hold")
    return 0


if __name__ == "__main__":
    sys.exit(main())

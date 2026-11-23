"""Tests for dev_tools/check_conventions.py: each rule C1–C9 on synthetic input, then the real repo."""

from __future__ import annotations

import hashlib
import pathlib
import re

import pytest

from dev_tools import check_conventions as cc

PKG = "ulduar/"


WORD = "zzforbiddenzz"  # a synthetic stand-in; the real deny-list holds digests only


@pytest.fixture
def synthetic_deny_list(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(cc, "FORBIDDEN_NAME_DIGESTS", frozenset({hashlib.sha256(WORD.encode()).hexdigest()}))
    monkeypatch.setattr(cc, "FORBIDDEN_NAME_LENGTHS", frozenset({len(WORD)}))
    return hashlib.sha256(WORD.encode()).hexdigest()[:8]


def test_c1_finds_the_word_whole_or_glued_into_a_longer_run(synthetic_deny_list: str) -> None:
    sources = [
        (PKG + "a.py", f'"""Doc."""\nX = "Project {WORD.title()}"\n'),
        ("tests/test_t.py", f"# {WORD}\n"),
        ("docs/notes.md", f"clean\nsee the {WORD.upper()}-project and {WORD} again\n"),
        (PKG + "ok.py", "X = 'zzforbidden zz'\nY = 'unrelated words only'\n"),
        ("README.md", f"{WORD}s and x{WORD} and {WORD.title()}Adapter\n"),
    ]
    message = f"C1 name of another project (digest {synthetic_deny_list}…) (R1)"
    assert cc.check_generic_core(sources) == [
        f"{PKG}a.py:2: {message}",
        f"tests/test_t.py:1: {message}",
        f"docs/notes.md:2: {message}",
        f"README.md:1: {message}",
    ]


def test_c1_message_carries_the_digest_prefix_and_never_the_word(synthetic_deny_list: str) -> None:
    out = cc.check_generic_core([("a.md", f"{WORD}\n"), (f"dir/{WORD}.txt", "clean\n")])
    assert len(out) == 2 and all(f"(digest {synthetic_deny_list}…)" in line for line in out)
    assert WORD not in out[0].lower()  # a path hit names the path, which holds the word by definition
    assert out[1] == f"dir/{WORD}.txt: C1 name of another project in the path (digest {synthetic_deny_list}…) (R1)"


@pytest.mark.usefixtures("synthetic_deny_list")
def test_c1_scans_paths() -> None:
    out = cc.check_generic_core([(f"docs/{WORD.title()}Notes/a.txt", "clean\n"), ("docs/other/a.txt", "clean\n")])
    assert len(out) == 1 and "C1 name of another project in the path" in out[0]


def test_c1_real_deny_list_is_six_sha256_digests() -> None:
    assert len(cc.FORBIDDEN_NAME_DIGESTS) == 6
    assert all(re.fullmatch(r"[0-9a-f]{64}", digest) for digest in cc.FORBIDDEN_NAME_DIGESTS)
    assert 1 <= len(cc.FORBIDDEN_NAME_LENGTHS) <= 6
    assert all(isinstance(n, int) and n > 0 for n in cc.FORBIDDEN_NAME_LENGTHS)


def test_repo_inputs_skips_binary_files_and_reads_invalid_utf8(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    (tmp_path / "bin.dat").write_bytes(b"abc\x00def")
    (tmp_path / "latin.txt").write_bytes(b"caf\xe9 text\n")
    (tmp_path / "ünï.md").write_text("plain\n", encoding="utf-8")
    monkeypatch.setattr(cc, "git", lambda *args: "bin.dat\0latin.txt\0ünï.md\0missing.txt\0")
    files, sources = cc.repo_inputs(tmp_path)
    assert files == ["bin.dat", "latin.txt", "ünï.md"]
    assert dict(sources) == {"latin.txt": "caf\ufffd text\n", "ünï.md": "plain\n"}


def test_c2_print_outside_console_module() -> None:
    sources = [(PKG + "a.py", "print('x')\n"), (PKG + "console.py", "print('x')\n"), ("dev_tools/v.py", "print('x')\n")]
    out = cc.check_no_print(sources)
    assert len(out) == 1 and out[0].startswith(f"{PKG}a.py:1: C2")


def test_c3_formatted_sql() -> None:
    bad = (
        "def f(cur, t):\n"
        "    cur.execute(f'SELECT * FROM {t}')\n"
        "    cur.execute('SELECT * FROM %s' % t)\n"
        "    cur.execute('SELECT * FROM ' + t)\n"
        "    cur.executemany('SELECT {}'.format(t), [])\n"
    )
    good = (
        "def f(cur, q, t, sql):\n"
        "    cur.execute(q)\n"
        "    cur.execute('SELECT %s', (t,))\n"
        "    cur.execute(sql.SQL('DROP TABLE {}').format(sql.Identifier(t)))\n"
        "    cur.execute(sql.SQL('DROP TABLE ') + sql.Identifier(t))\n"
    )
    assert [line.split(":")[1] for line in cc.check_safe_sql([(PKG + "a.py", bad)])] == ["2", "3", "4", "5"]
    assert cc.check_safe_sql([(PKG + "a.py", good), ("tests/t.py", bad)]) == []


def test_c4_file_locations() -> None:
    files = [
        "tests/test_a.py", "ulduar/test_b.py", "dev_tools/c_test.py", "dev_tools/test_db.py",
        "README.md", "ROADMAP.md", "ulduar/README.md", ".claude/agents/x.md",
        ".github/pull_request_template.md", "NOTES.md", "docs/plan.md",
    ]  # fmt: skip
    out = cc.check_file_locations(files)
    assert [line.split(":")[0] for line in out] == [
        "ulduar/test_b.py", "dev_tools/c_test.py", "dev_tools/test_db.py", "NOTES.md", "docs/plan.md",
    ]  # fmt: skip


def test_c5_module_docstring() -> None:
    sources = [(PKG + "a.py", "X = 1\n"), (PKG + "b.py", '"""Doc."""\n'), ("dev_tools/c.py", "X = 1\n")]
    assert cc.check_module_docstrings(sources) == [f"{PKG}a.py:1: C5 module docstring missing"]


def test_c6_test_connections() -> None:
    bad = "import psycopg\nc = psycopg.connect(dsn)\nD = 'postgresql://u@h/db'\n"
    out = cc.check_test_connections([("tests/test_a.py", bad), ("tests/conftest.py", bad), (PKG + "a.py", bad)])
    assert [line.split(":")[1] for line in out] == ["2", "3"]
    assert all(line.startswith("tests/test_a.py") for line in out)
    bare = "from psycopg import connect\nc = connect(dsn)\n"
    assert [line.split(":")[1] for line in cc.check_test_connections([("tests/test_b.py", bare)])] == ["1", "2"]
    # A test about connection strings may spell one, but still may not connect.
    out = cc.check_test_connections([("tests/test_services.py", bad)])
    assert [line.split(":")[1] for line in out] == ["2"]
    s3 = "import boto3\nfrom botocore.config import Config\nD = 'postgres://u@h/db'\nimport os\n"
    assert [line.split(":")[1] for line in cc.check_test_connections([("tests/test_c.py", s3)])] == ["1", "2", "3"]


def test_c7_cli_entry() -> None:
    sources = [
        (PKG + "a.py", "import argparse\nfrom click import command\nimport os\n"),
        (PKG + "mimiron.py", "import argparse\n"),
        ("dev_tools/v.py", "import argparse\n"),
    ]
    out = cc.check_cli_entry(sources)
    assert [line.split(":")[1] for line in out] == ["1", "2"]


def test_c8_forbidden_tracked() -> None:
    files = [".env", "a/.env.local", "certs/server.pem", "x.key", "pkg.egg-info/PKG-INFO",
             ".claude/settings.local.json", "environment.py", ".claude/settings.json", "keys.py"]  # fmt: skip
    out = cc.check_forbidden_tracked(files)
    assert [line.split(":")[0] for line in out] == files[:6]


def test_c9_test_skips() -> None:
    bad = "import pytest\n@pytest.mark.skipif(True, reason='x')\ndef test_a():\n    pytest.skip('x')\n"
    out = cc.check_test_skips([("tests/test_a.py", bad), ("tests/conftest.py", bad)])
    assert [line.split(":")[1] for line in out] == ["2", "4"]
    bare = "import unittest\nfrom pytest import skip\ndef test_a():\n    skip('x')\n    raise unittest.SkipTest\n"
    assert [line.split(":")[1] for line in cc.check_test_skips([("tests/test_b.py", bare)])] == ["2", "4", "5"]
    unit = "import unittest\n@unittest.skipUnless(False, 'x')\n@unittest.expectedFailure\ndef test_a(): ...\n"
    assert [line.split(":")[1] for line in cc.check_test_skips([("tests/test_b.py", unit)])] == ["2", "3"]
    case = "class T:\n    def test_a(self):\n        self.skipTest('no db')\n"
    assert [line.split(":")[1] for line in cc.check_test_skips([("tests/test_c.py", case)])] == ["3"]


def test_syntax_errors_are_left_to_ruff() -> None:
    assert cc.run_all([], [(PKG + "a.py", "def (:\n"), ("tests/test_a.py", "def (:\n")]) == []


def test_real_repository_is_clean() -> None:
    assert cc.run_all(*cc.repo_inputs()) == []

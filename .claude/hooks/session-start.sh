#!/bin/bash
# SessionStart hook: Claude Code cloud sessions get the dev tools (pytest, ruff, mypy,
# pre-commit), the git hooks and the local test services (PostgreSQL 18 and MinIO).
# Local sessions are left alone (the dev container does the same on create).
# ULDUAR_SKIP_DEV_SETUP=1 skips it. No failure here blocks the session: a failed
# install or a service that cannot start is reported on stderr, and verify then
# fails, which is the intended signal (hard rule 9).
set -euo pipefail
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ] || [ "${ULDUAR_SKIP_DEV_SETUP:-}" = "1" ]; then exit 0; fi
cd "${CLAUDE_PROJECT_DIR:-.}"
# Reinstall only when a file that decides what gets installed changed since the last success.
hashes="$(python3 -I -c 'import sysconfig; print(sysconfig.get_path("purelib"))')/.ulduar-session-start.sha256"
if ! sha256sum --check --status "$hashes" 2>/dev/null; then
  rm -f "$hashes"
  if python3 -m pip install --quiet --editable ".[dev]" >&2; then
    sha256sum .claude/hooks/session-start.sh pyproject.toml > "$hashes" || rm -f "$hashes"
  else
    echo "session-start: the dev install failed; run: pip install -e '.[dev]'" >&2
  fi
fi
# A fresh clone on a cached container has the packages but not the git hooks.
if [ ! -f .git/hooks/pre-commit ]; then python3 -m pre_commit install --allow-missing-config >&2 || true; fi
# The test services: the Docker daemon is not running in a fresh cloud container.
if command -v dockerd >/dev/null 2>&1 && ! docker info >/dev/null 2>&1; then
  (nohup dockerd >/tmp/ulduar-dockerd.log 2>&1 &)
  for _ in $(seq 1 30); do docker info >/dev/null 2>&1 && break; sleep 1; done
fi
python3 dev_tools/services.py start >&2 || true
# Later commands of the session read the test settings from the environment.
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  python3 dev_tools/services.py env | sed 's/^/export /' >> "$CLAUDE_ENV_FILE"
fi

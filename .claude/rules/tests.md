---
paths:
  - "tests/**"
---
Tests: read `tests/README.md` before editing. R9
(`.claude/skills/ulduar-development/references/conventions.md#r9`): database and archive behaviour is tested on
the local PostgreSQL 18 and object-store instances through the conftest fixtures; a test never opens its own
connection or client and never skips itself.

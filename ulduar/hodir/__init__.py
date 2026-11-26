"""Hodir, the cold archive: immutable, content-addressed storage of files, dumps, reports and models.

The facade of the component. Its public calls arrive in later Phase 2 commits; what exists so far is private: the
storage interface and its two backends (a local directory and an S3-compatible bucket), chosen by the
`[archive]` section of the configuration.
"""

from __future__ import annotations

__all__: list[str] = []

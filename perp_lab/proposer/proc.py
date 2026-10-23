"""The only module in this package that starts a process, and only from an allowlist.

Allowed: `lab judge NAME --tf TF --proposer`, `lab preflight FILE --tf TF`, the configured `claude`, and
`git rev-parse` / `git status`. Anything else raises before a process exists.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def lab_argv(*args: str) -> list[str]:
    return [str(Path(sys.executable).with_name("lab")), *args]


def allowed(argv: list[str], claude_bin: str) -> None:
    if not argv:
        raise ValueError("empty argv")
    head = argv[0]
    if head == claude_bin:
        return
    if head in (lab_argv()[0], "lab"):
        if len(argv) > 1 and argv[1] == "preflight":
            return
        if len(argv) > 1 and argv[1] == "judge" and "--proposer" in argv[2:]:
            return
    elif head == "git" and len(argv) > 1 and argv[1] in ("rev-parse", "status"):
        return
    raise ValueError(f"command not allowed for the proposer: {argv[:2]}")


def run(argv: list[str], *, claude_bin: str, **popen_kw) -> subprocess.Popen:
    allowed(argv, claude_bin)
    return subprocess.Popen(argv, **popen_kw)

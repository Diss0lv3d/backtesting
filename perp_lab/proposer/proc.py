"""The only module in this package that starts a process, and only from an allowlist.

Allowed, as exact argv shapes (a token-presence check is not enough: click parses `--tf --proposer` as the
value of --tf, so a loose check could run a judge that is not marked as the proposer's):
`<venv>/lab judge NAME --tf TF --proposer [--config CFG]`, `<venv>/lab preflight FILE --tf TF [--config CFG]`,
the configured `claude`, and `git rev-parse` / `git status`. The bare name `lab` is refused: it resolves
through PATH. Popen keywords are allowlisted too, so none can swap the binary or run code (`executable`,
`shell`, `preexec_fn`). Anything else raises before a process exists.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PIPE = subprocess.PIPE  # re-exported so callers needn't import subprocess (the boundary test bans it)
STDOUT = subprocess.STDOUT  # likewise: the judge's stderr joins its stdout in the human's log
TimeoutExpired = subprocess.TimeoutExpired  # likewise, for Popen.wait / communicate with a timeout
POPEN_KW = frozenset({"stdin", "stdout", "stderr", "cwd", "env", "text", "start_new_session"})


def lab_argv(*args: str) -> list[str]:
    return [str(Path(sys.executable).with_name("lab")), *args]


def _value(s) -> bool:
    return isinstance(s, str) and bool(s) and not s.startswith("-")


def _lab_shape(argv: list[str]) -> bool:
    if len(argv) < 5 or argv[0] != lab_argv()[0]:
        return False
    cmd, subject, tf_flag, tf = argv[1:5]
    rest = argv[5:]
    if cmd == "judge":
        if not rest or rest[0] != "--proposer":
            return False
        rest = rest[1:]
    elif cmd != "preflight":
        return False
    if rest and not (len(rest) == 2 and rest[0] == "--config" and _value(rest[1])):
        return False
    return tf_flag == "--tf" and _value(subject) and _value(tf)


def allowed(argv: list[str], claude_bin: str) -> None:
    if not argv:
        raise ValueError("empty argv")
    if argv[0] == claude_bin:
        return
    if _lab_shape(argv):
        return
    if argv[0] == "git" and len(argv) > 1 and argv[1] in ("rev-parse", "status"):
        return
    raise ValueError(f"command not allowed for the proposer: {argv[:2]}")


def run(argv: list[str], *, claude_bin: str, **popen_kw) -> subprocess.Popen:
    allowed(argv, claude_bin)
    extra = set(popen_kw) - POPEN_KW
    if extra:
        raise ValueError(f"Popen keywords not allowed for the proposer: {sorted(extra)}")
    return subprocess.Popen(argv, **popen_kw)

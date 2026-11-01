"""The harness pin: an unattended session must be judged by the code and settings it started with.

`lab judge` runs from the main checkout, so a branch switch or an edit under `perp_lab/` or `config/`
mid-session would change how later trials are judged without the record showing it. `take` refuses a dirty
harness and records the commit and branch; `check` runs before every preflight and judge. With HEAD
unchanged and porcelain empty, the tracked files equal HEAD's tree, so no tree hash is carried (R25).
Untracked files under `strategies/` are outside both paths and never trip it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from perp_lab.proposer import proc

PATHS = ("perp_lab", "config")


@dataclass(frozen=True)
class Pin:
    commit: str
    branch: str


class PinError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _git(repo: Path, claude_bin: str, *args: str) -> str:
    p = proc.run(["git", *args], claude_bin=claude_bin, cwd=repo, stdout=proc.PIPE,
                 stderr=proc.PIPE, text=True)
    out, err = p.communicate()
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed ({p.returncode}): {err.strip()}")
    return out.strip()


def _porcelain(repo: Path, claude_bin: str) -> str:
    return _git(repo, claude_bin, "status", "--porcelain", "--", *PATHS)


def take(repo: Path, claude_bin: str) -> Pin:
    if _porcelain(repo, claude_bin):
        raise PinError("dirty")
    return Pin(commit=_git(repo, claude_bin, "rev-parse", "HEAD"),
               branch=_git(repo, claude_bin, "rev-parse", "--abbrev-ref", "HEAD"))


def check(repo: Path, pin: Pin, claude_bin: str) -> None:
    if _git(repo, claude_bin, "rev-parse", "HEAD") != pin.commit or _porcelain(repo, claude_bin):
        raise PinError("harness_changed")

#!/usr/bin/env python3
"""A stand-in for the `claude` CLI: tests/test_model.py copies it, as `claude`, into a directory first on PATH.

The proposer runs the CLI with only HOME and PATH, so the scenario can't come from the environment: it is
read from `scenario.json` next to this file, and what the call looked like (argv, cwd and its files, the
environment, stdin, pid) is written to `seen.json` next to it.

scenario.json: {"lines": [...], "exit": 0, "version": "...", "child": 30}. A string in `lines` is printed as
one stream line and flushed; a number is a pause, in seconds, so a test can show a call killed mid-stream.
`child` starts a `sleep` of that many seconds first, its pid written to `child.pid`.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _environ() -> dict:
    # What execve passed: os.environ also holds what Python adds itself (PEP 538 sets LC_CTYPE in a C locale).
    try:
        raw = Path("/proc/self/environ").read_bytes()
    except OSError:
        return dict(os.environ)
    return dict(e.decode().split("=", 1) for e in raw.split(b"\0") if e)


def main() -> int:
    scenario = json.loads((HERE / "scenario.json").read_text())
    if sys.argv[1:] == ["--version"]:
        print(scenario.get("version", "0.0.0 (fake)"))
        return scenario.get("exit", 0)
    cwd = os.getcwd()
    seen = {"argv": sys.argv, "cwd": cwd, "cwd_files": os.listdir(cwd), "env": _environ(),
            "stdin": sys.stdin.buffer.read().decode(), "pid": os.getpid()}
    (HERE / "seen.json").write_text(json.dumps(seen))
    if "child" in scenario:  # a process of its own that inherits stdout, as a CLI's helper might
        child = subprocess.Popen(["sleep", str(scenario["child"])])
        (HERE / "child.pid").write_text(str(child.pid))
    for item in scenario.get("lines", []):
        if isinstance(item, str):
            sys.stdout.buffer.write(item.encode() + b"\n")
            sys.stdout.flush()
        else:
            time.sleep(item)
    return scenario.get("exit", 0)


if __name__ == "__main__":
    sys.exit(main())

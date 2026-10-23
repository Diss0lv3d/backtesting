from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

from perp_lab.config import load_config
from perp_lab.proposer import proc
from perp_lab.proposer.settings import StartRefused, load
from tests.conftest import REPO


def _cfg(**over):
    c = load_config(REPO / "config/lab.yaml")
    c.raw["proposer"] = {**c.raw["proposer"], **over}
    return c


def test_the_shipped_config_loads():
    pc = load(load_config(REPO / "config/lab.yaml"))
    assert pc.model == "claude-opus-5-5" and pc.model_training_cutoff == date(2026, 6, 30)
    assert pc.timeframes == ("15m", "1h", "4h") and pc.state_dir == Path("proposer")


@pytest.mark.parametrize("model", ["opus", "claude-opus", "claude-opus-latest", "sonnet"])
def test_an_alias_refuses_to_start(model):
    with pytest.raises(StartRefused):
        load(_cfg(model=model))


@pytest.mark.parametrize("cutoff", ["2026-10-01", date(2099, 1, 1), None, "soon"])
def test_a_cutoff_on_or_after_the_holdout_start_or_unreadable_refuses(cutoff):
    c = _cfg(model_training_cutoff=cutoff)
    if cutoff == "2026-10-01":
        c.raw["holdout_start"] = "2026-10-01"
    with pytest.raises(StartRefused):
        load(c)


def test_a_cutoff_equal_to_the_holdout_start_refuses():
    c = _cfg(model_training_cutoff="2026-06-30")
    c.raw["holdout_start"] = "2026-06-30"
    with pytest.raises(StartRefused):
        load(c)


def test_a_missing_cutoff_refuses():
    c = _cfg()
    del c.raw["proposer"]["model_training_cutoff"]
    with pytest.raises(StartRefused):
        load(c)


def test_an_iso_string_cutoff_is_accepted():
    assert load(_cfg(model_training_cutoff="2026-06-30")).model_training_cutoff == date(2026, 6, 30)


def test_an_unknown_timeframe_refuses():
    with pytest.raises(StartRefused):
        load(_cfg(timeframes=["1h", "3m"]))


LAB = proc.lab_argv()[0]


@pytest.mark.parametrize("argv", [
    ["lab", "exam"], ["lab", "withdraw", "x"], ["lab", "judge", "x", "--tf", "1h"],
    ["lab", "verdict-refresh", "x"], ["git", "checkout", "main"], ["sh", "-c", "x"], [], ["git"],
    [LAB, "exam"], [LAB, "withdraw", "x"], [LAB, "judge", "x", "--tf", "1h"],
    [LAB, "verdict-refresh", "x"],
    ["lab", "judge", "x", "--tf", "1h", "--proposer"],                        # bare lab goes through PATH
    ["lab", "preflight", "f.py", "--tf", "1h"],
    [LAB, "judge", "x", "--symbols", "--proposer", "--tf", "1h"],
    [LAB, "judge", "x", "--tf", "--proposer"],
    [LAB, "judge", "--tf", "1h", "--", "--proposer"],
    [LAB, "judge", "x", "--tf", "1h", "--config", "--proposer"],
    [LAB, "judge", "x", "--tf", "1h", "--proposer", "--workers", "64"],
    [LAB, "judge", "x", "--tf", "1h", "--proposer", "--config"],
    [LAB, "judge", "x", "--tf", "1h", "--proposer", "--config", "c.yaml", "--workers", "64"],
    [LAB, "judge", "-x", "--tf", "1h", "--proposer"],
    [LAB, "judge", "", "--tf", "1h", "--proposer"],
    [LAB, "preflight", "f.py", "--tf", "1h", "--workers", "64"],
    [LAB, "preflight", "f.py", "--tf", "1h", "--proposer"],
    [LAB, "preflight", "--tf", "1h", "--", "f.py"],
])
def test_proc_refuses_any_other_command(argv):
    with pytest.raises(ValueError):
        proc.allowed(argv, "claude")


@pytest.mark.parametrize("argv", [
    [LAB, "judge", "x", "--tf", "1h", "--proposer"], [LAB, "preflight", "f.py", "--tf", "1h"],
    [LAB, "judge", "x", "--tf", "1h", "--proposer", "--config", "c/lab.yaml"],
    [LAB, "preflight", "f.py", "--tf", "1h", "--config", "c/lab.yaml"],
    ["claude", "-p"], ["git", "rev-parse", "HEAD"], ["git", "status", "--short"]])
def test_proc_allows_the_listed_commands(argv):
    proc.allowed(argv, "claude")


def test_lab_argv_uses_the_venv_entry_point():
    assert proc.lab_argv("judge", "x")[0] == str(Path(sys.executable).with_name("lab"))
    proc.allowed(proc.lab_argv("preflight", "f.py", "--tf", "1h"), "claude")


def test_run_refuses_before_starting_anything():
    with pytest.raises(ValueError):
        proc.run(["sh", "-c", "touch /nonexistent/x"], claude_bin="claude")


@pytest.mark.parametrize("kw", [{"executable": "/bin/sh"}, {"shell": True}, {"preexec_fn": lambda: None},
                                {"stdout": None, "close_fds": False}])
def test_run_refuses_popen_keywords_that_bypass_the_allowlist(kw):
    with pytest.raises(ValueError):
        proc.run(["git", "status"], claude_bin="claude", **kw)


def test_run_starts_an_allowed_command_with_allowed_keywords():
    import subprocess
    p = proc.run(["git", "rev-parse", "--git-dir"], claude_bin="claude", stdout=subprocess.PIPE, text=True,
                 cwd=REPO)
    assert p.communicate()[0].strip() and p.returncode == 0

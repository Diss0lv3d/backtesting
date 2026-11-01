from __future__ import annotations

import subprocess

import pytest

from perp_lab.proposer import pin

CLAUDE = "claude"


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.name", "t")
    _git(tmp_path, "config", "user.email", "t@example.invalid")
    for rel in ("perp_lab/a.py", "config/lab.yaml", "strategies/s.py"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("x = 1\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def test_clean_repo_gives_a_pin(repo):
    p = pin.take(repo, CLAUDE)
    assert p.branch == "main"
    assert len(p.commit) == 40
    pin.check(repo, p, CLAUDE)


def test_edited_tracked_file_refuses_as_dirty(repo):
    (repo / "perp_lab/a.py").write_text("x = 2\n")
    with pytest.raises(pin.PinError) as e:
        pin.take(repo, CLAUDE)
    assert e.value.reason == "dirty"


def test_new_untracked_strategy_passes(repo):
    p = pin.take(repo, CLAUDE)
    (repo / "strategies/new.py").write_text("y = 1\n")
    pin.check(repo, p, CLAUDE)
    pin.take(repo, CLAUDE)


def test_commit_after_take_is_harness_changed(repo):
    p = pin.take(repo, CLAUDE)
    (repo / "strategies/s.py").write_text("x = 2\n")
    _git(repo, "commit", "-q", "-am", "later")
    with pytest.raises(pin.PinError) as e:
        pin.check(repo, p, CLAUDE)
    assert e.value.reason == "harness_changed"


def test_config_edit_is_harness_changed(repo):
    p = pin.take(repo, CLAUDE)
    (repo / "config/lab.yaml").write_text("x = 2\n")
    with pytest.raises(pin.PinError) as e:
        pin.check(repo, p, CLAUDE)
    assert e.value.reason == "harness_changed"


def test_git_failure_raises_not_an_empty_pin(tmp_path):
    with pytest.raises(RuntimeError):
        pin.take(tmp_path, CLAUDE)


def test_untracked_file_under_perp_lab_refuses_and_stops(repo):
    p = pin.take(repo, CLAUDE)
    (repo / "perp_lab/new.py").write_text("z = 1\n")
    with pytest.raises(pin.PinError) as e:
        pin.check(repo, p, CLAUDE)
    assert e.value.reason == "harness_changed"
    with pytest.raises(pin.PinError) as e:
        pin.take(repo, CLAUDE)
    assert e.value.reason == "dirty"

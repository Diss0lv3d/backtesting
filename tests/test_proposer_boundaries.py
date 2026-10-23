"""The proposer's import boundary and subprocess rule, pinned for every file in the package."""

from __future__ import annotations

import ast

import pytest

from tests.conftest import REPO

FORBIDDEN = ("perp_lab.judge", "perp_lab.holdout", "perp_lab.store", "perp_lab.data")
PROC_MODULES = {"subprocess", "pty", "multiprocessing"}
FILES = sorted((REPO / "perp_lab/proposer").rglob("*.py"))


def _tree(p):
    return ast.parse(p.read_text())


def _imports(tree):
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                yield a.name, a.name
        elif isinstance(n, ast.ImportFrom):
            if n.level:
                continue
            yield n.module or "", n.module or ""
            for a in n.names:                       # `from perp_lab import judge`
                yield f"{n.module}.{a.name}", n.module or ""


def _hit(name, mods):
    return any(name == m or name.startswith(m + ".") for m in mods)


def test_the_scan_covers_the_package():
    assert {p.name for p in FILES} >= {"__init__.py", "settings.py", "proc.py"}


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_the_proposer_imports_nothing_it_may_not(path):
    bad = [n for n, _ in _imports(_tree(path)) if _hit(n, FORBIDDEN)]
    assert not bad, f"{path.name} imports {bad}"


@pytest.mark.parametrize("path", [p for p in FILES if p.name != "proc.py"], ids=lambda p: p.name)
def test_only_proc_starts_processes(path):
    tree = _tree(path)
    bad = [n for n, _ in _imports(tree) if _hit(n, PROC_MODULES)]
    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "os" \
                and (n.attr == "system" or n.attr.startswith(("exec", "spawn", "popen", "fork"))):
            bad.append(f"os.{n.attr}")
        if isinstance(n, ast.ImportFrom) and n.module == "os":
            bad += [f"os.{a.name}" for a in n.names
                    if a.name == "system" or a.name.startswith(("exec", "spawn", "popen", "fork"))]
    assert not bad, f"{path.name} starts processes: {bad}"


def test_the_scan_catches_a_violation(tmp_path):
    p = tmp_path / "x.py"
    p.write_text("from perp_lab import judge\nimport subprocess\n")
    names = [n for n, _ in _imports(_tree(p))]
    assert any(_hit(n, FORBIDDEN) for n in names) and any(_hit(n, PROC_MODULES) for n in names)

"""The proposer's import boundary and subprocess rule, pinned for every file in the package."""

from __future__ import annotations

import ast
import subprocess
import sys

import pytest

from tests.conftest import REPO

PKG = REPO / "perp_lab/proposer"
PROC = PKG / "proc.py"
FILES = sorted(PKG.rglob("*.py"))

# A positive allowlist: any perp_lab.* import must be one of these or inside the proposer package.
ALLOWED = ("perp_lab.dsr", "perp_lab.config", "perp_lab.plugins", "perp_lab.proposer")
DYNAMIC = {"importlib", "runpy"}
PROC_MODULES = ("subprocess", "pty", "multiprocessing", "asyncio", "concurrent.futures", "_posixsubprocess")
OS_BAD = ("system", "exec", "spawn", "posix_spawn", "popen", "fork")


def _hit(name, mods):
    return any(name == m or name.startswith(m + ".") for m in mods)


def _package(path):
    return list(path.relative_to(REPO).with_suffix("").parts[:-1])


def _imports(tree, pkg):
    """Every dotted name the file imports, relative imports resolved against its own package."""
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                yield a.name
        elif isinstance(n, ast.ImportFrom):
            if n.level:
                base = pkg[: len(pkg) - (n.level - 1)] if n.level - 1 <= len(pkg) else []
                mod = ".".join(base + ([n.module] if n.module else []))
            else:
                mod = n.module or ""
            yield mod
            for a in n.names:
                yield f"{mod}.{a.name}" if mod else a.name


def _import_violations(src, pkg):
    tree = ast.parse(src)
    bad = []
    for name in _imports(tree, pkg):
        top = name.split(".")[0]
        if top == "perp_lab" and name != "perp_lab" and not _hit(name, ALLOWED):
            bad.append(name)
        if top in DYNAMIC:
            bad.append(name)
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and n.id in ("__import__", *DYNAMIC):
            bad.append(n.id)
        if isinstance(n, ast.ImportFrom) and any(a.name == "*" for a in n.names):
            bad.append(f"{n.module}.*")
    return bad


def _process_violations(src, pkg):
    tree = ast.parse(src)
    bad = [n for n in _imports(tree, pkg) if _hit(n, PROC_MODULES)]
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            bad += [f"import os as {a.asname}" for a in n.names if a.name == "os" and a.asname]
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "os" \
                and n.attr.startswith(OS_BAD):
            bad.append(f"os.{n.attr}")
        if isinstance(n, ast.ImportFrom) and n.module == "os":
            bad += [f"os.{a.name}" for a in n.names if a.name.startswith(OS_BAD)]
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "getattr" \
                and n.args and isinstance(n.args[0], ast.Name) and n.args[0].id == "os":
            bad.append("getattr(os, ...)")
    return bad


def test_the_scan_covers_the_package():
    assert {p.name for p in FILES} >= {"__init__.py", "settings.py", "proc.py"}


@pytest.mark.parametrize("path", FILES, ids=lambda p: str(p.relative_to(REPO)))
def test_the_proposer_imports_only_what_it_may(path):
    bad = _import_violations(path.read_text(), _package(path))
    assert not bad, f"{path.name} imports {bad}"


@pytest.mark.parametrize("path", [p for p in FILES if p != PROC], ids=lambda p: str(p.relative_to(REPO)))
def test_only_proc_starts_processes(path):
    bad = _process_violations(path.read_text(), _package(path))
    assert not bad, f"{path.name} starts processes: {bad}"


def test_importing_the_package_loads_none_of_the_forbidden_modules():
    mods = [".".join(p.relative_to(REPO).with_suffix("").parts[:-1] if p.stem == "__init__"
                     else p.relative_to(REPO).with_suffix("").parts) for p in FILES]
    code = ("import sys, importlib\n"
            f"for m in {mods!r}: importlib.import_module(m)\n"
            "bad = [m for m in ('perp_lab.judge', 'perp_lab.holdout', 'perp_lab.store', 'perp_lab.data',\n"
            "                   'perp_lab.sandbox', 'perp_lab.preflight') if m in sys.modules]\n"
            "print(bad)\nsys.exit(1 if bad else 0)\n")
    r = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


PKG_PARTS = ["perp_lab", "proposer"]


@pytest.mark.parametrize("src", [
    "from perp_lab import judge\n", "import perp_lab.judge\n", "from perp_lab.judge import x\n",
    "from .. import judge\n", "from ..store import db\n", "from ..holdout import read\n",
    "import importlib\n", "import runpy\n", "from importlib import import_module\n",
    "__import__('perp_lab.store')\n", "from perp_lab.preflight import preflight\n",
    "from perp_lab.sandbox import run\n", "from perp_lab.runs import x\n", "from perp_lab.cli import app\n",
    "from perp_lab.paper import x\n", "from perp_lab import *\n",
])
def test_the_import_scan_catches(src):
    assert _import_violations(src, PKG_PARTS)


@pytest.mark.parametrize("src", [
    "from perp_lab.dsr import x\n", "from perp_lab import config, plugins, dsr\n", "from . import settings\n",
    "from .proc import run\n", "import perp_lab.proposer.settings\n", "import re\n",
])
def test_the_import_scan_passes_what_is_allowed(src):
    assert not _import_violations(src, PKG_PARTS)


@pytest.mark.parametrize("src", [
    "import subprocess\n", "from subprocess import Popen\n", "import pty\n", "import multiprocessing\n",
    "import asyncio\n", "from asyncio import create_subprocess_exec\n", "import concurrent.futures\n",
    "from concurrent import futures\n", "from concurrent.futures import ProcessPoolExecutor\n",
    "import _posixsubprocess\n", "import os\nos.system('x')\n", "import os\nos.execv('x', [])\n",
    "import os\nos.spawnv(0, 'x', [])\n", "import os\nos.posix_spawn('x', [], {})\n",
    "import os\nos.posix_spawnp('x', [], {})\n", "import os\nos.popen('x')\n", "import os\nos.fork()\n",
    "import os as o\no.system('x')\n", "import os\ngetattr(os, 'system')('x')\n",
    "from os import system\n", "from os import posix_spawn\n",
])
def test_the_process_scan_catches(src):
    assert _process_violations(src, PKG_PARTS)


def test_the_process_scan_passes_plain_os_use():
    assert not _process_violations("import os\nos.environ.get('x')\nos.path.join('a')\n", PKG_PARTS)


def test_only_the_top_level_proc_is_exempt():
    sub = PKG / "sub" / "proc.py"
    assert sub != PROC and PROC == REPO / "perp_lab/proposer/proc.py"

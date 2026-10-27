"""The idea rules, plugin assembly and source checks, all in code (ADR 0006).

Every check raises `Rejected(message)`; the message goes back to the model as a fix-up. The model
never writes PARENT: the orchestrator inserts the champion for a refinement.
"""

from __future__ import annotations

import ast
import math
import re

from perp_lab.plugins import PluginMetaError, parse_meta
from perp_lab.proposer.contract import Proposal
from perp_lab.proposer.piles import best_pile, pile
from perp_lab.proposer.record import Record
from perp_lab.proposer.settings import ProposerConfig

ALLOWED_IMPORTS = ("math", "numpy", "pandas", "perp_lab.strategy", "perp_lab.indicators", "perp_lab.engine.backtest")
MAX_BYTES = 65_536
MAX_NAME = 40


class Rejected(Exception):
    """A check failed; the message is the fix-up the model receives."""


def _label(idea: dict) -> str:
    return f"#{idea['id']} ({idea['title']})"


def _judged(record: Record, idea_id: int) -> list[dict]:
    return [x for x in record.proposals(idea_id) if x.get("judge_exit") is not None]


def check_idea_rules(p: Proposal, record: Record, pc: ProposerConfig) -> dict | None:
    """The target idea (None for a new idea), or Rejected."""
    if p.timeframe not in pc.timeframes:
        raise Rejected(f"timeframe {p.timeframe!r} is not one of {', '.join(pc.timeframes)}")
    if p.kind == "new":
        if p.idea is None:
            raise Rejected("a new idea needs an idea card")
        title = p.idea.title.strip().casefold()
        if any(i["title"].strip().casefold() == title for i in record.ideas()):
            raise Rejected(f"an idea titled {p.idea.title!r} already exists; propose a different idea, "
                           "or a refinement or port of that one")
        return None
    block = p.refines if p.kind == "refinement" else p.ports if p.kind == "port" else None
    if block is None:
        raise Rejected(f"a {p.kind} needs its {'refines' if p.kind == 'refinement' else 'ports'} block")
    ids = {i["id"]: i for i in record.ideas()}
    idea = ids.get(block.idea_id)
    if idea is None:
        raise Rejected(f"there is no idea #{block.idea_id}")
    if idea["status"] == "excluded" or idea.get("excluded_reason"):
        raise Rejected(f"idea {_label(idea)} is excluded and takes neither refinements nor ports")
    if p.kind == "refinement":
        if idea["status"] != "open":
            raise Rejected(f"idea {_label(idea)} is {idea['status']}, not open; it takes no more refinements")
        if len(_judged(record, idea["id"])) >= pc.proposals_per_idea:
            raise Rejected(f"idea {_label(idea)} has used its {pc.proposals_per_idea} judged plugins")
        if p.timeframe != idea["timeframe"]:
            raise Rejected(f"a refinement keeps its idea's timeframe, {idea['timeframe']}; "
                           "to use another timeframe propose a port")
        return idea
    if idea["status"] not in ("open", "exhausted"):
        raise Rejected(f"idea {_label(idea)} is {idea['status']}; only open or exhausted ideas can be ported")
    best = best_pile([pile(x.get("verdict")) for x in record.proposals(idea["id"])
                      if x.get("judge_exit") is not None and x.get("duplicate_of") is None])
    if best == "promoted":
        raise Rejected(f"idea {_label(idea)} is promoted; its family is headed for an exam and gets no port")
    if best not in ("signal before costs", "close call"):
        raise Rejected(f"idea {_label(idea)} has best pile {best or 'none'}; only signal before costs "
                       "or close call can be ported")
    if p.timeframe == idea["timeframe"]:
        raise Rejected(f"a port needs a timeframe other than the idea's own, {idea['timeframe']}")
    if any(i.get("port_of") == idea["id"] and i["timeframe"] == p.timeframe for i in ids.values()):
        raise Rejected(f"idea {_label(idea)} already has a port at {p.timeframe}")
    return idea


def plugin_name(seq: int, title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")
    return f"px{seq:04d}_{slug}"[:MAX_NAME].rstrip("_")


def _normalise(text: str) -> str:
    return text.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")


def _parse(source: str) -> ast.Module:
    try:
        return ast.parse(source)
    except (SyntaxError, ValueError) as e:
        raise Rejected(f"the source does not parse: {e}") from e


def _targets(node: ast.AST):
    if isinstance(node, ast.Assign):
        yield from node.targets
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        yield node.target
    elif isinstance(node, ast.NamedExpr):
        yield node.target
    elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
        yield node.target
    elif isinstance(node, (ast.With, ast.AsyncWith)):
        for item in node.items:
            if item.optional_vars is not None:
                yield item.optional_vars


def check_model_source(source: str) -> None:
    for node in ast.walk(_parse(_normalise(source))):         # read as assemble reads it
        for t in _targets(node):
            if any(isinstance(n, ast.Name) and n.id == "PARENT" for n in ast.walk(t)):
                raise Rejected("the source assigns PARENT; the platform sets PARENT, remove that line")


def _docstring(h: str) -> str:
    return '"""' + h.replace("\\", "\\\\").replace('"""', '\\"\\"\\"') + '\n"""\n'


def assemble(p: Proposal, name: str, parent: str | None) -> bytes:
    src = _normalise(p.source)
    tree = _parse(src)
    lines = src.split("\n")
    doc = tree.body[0] if tree.body and isinstance(tree.body[0], ast.Expr) \
        and isinstance(tree.body[0].value, ast.Constant) and isinstance(tree.body[0].value.value, str) else None
    new_doc = _docstring(p.hypothesis.replace("\r\n", "\n").replace("\r", "\n"))
    if doc is not None:
        head, rest = lines[: doc.lineno - 1], lines[doc.end_lineno:]
        body = tree.body[1:]
    else:
        head, rest = [], lines
        body = tree.body
    out = "\n".join(head + [new_doc.rstrip("\n")]) + "\n"
    if parent is not None and p.kind == "refinement":
        # `from __future__` imports must come before any other statement, PARENT included.
        fut = [n for n in body if isinstance(n, ast.ImportFrom) and n.module == "__future__"]
        if fut:
            cut = fut[-1].end_lineno - (doc.end_lineno if doc else 0)
            out += "\n".join(rest[:cut]) + "\n" + f'PARENT = "{parent}"\n' + "\n".join(rest[cut:])
        else:
            out += f'PARENT = "{parent}"\n' + "\n".join(rest)
    else:
        out += "\n".join(rest)
    if not out.endswith("\n"):
        out += "\n"
    return out.encode("utf-8")


def _allowed(mod: str) -> bool:
    return any(mod == a or mod.startswith(a + ".") for a in ALLOWED_IMPORTS)


def check_source(text: bytes, name: str, tf: str, pc: ProposerConfig) -> None:
    if len(text) > MAX_BYTES:
        raise Rejected(f"the plugin is {len(text)} bytes; the limit is {MAX_BYTES}")
    try:
        meta = parse_meta(text, name)
    except PluginMetaError as e:
        raise Rejected(str(e)) from e
    if meta.timeframes != [tf]:
        raise Rejected(f"timeframes must be exactly [{tf!r}], not {meta.timeframes}")
    for n in ast.walk(_parse(text.decode("utf-8"))):
        mods = [a.name for a in n.names] if isinstance(n, ast.Import) else \
            [("." * n.level) + (n.module or "")] if isinstance(n, ast.ImportFrom) else []
        for m in mods:
            if not _allowed(m):
                raise Rejected(f"import of {m!r} is not allowed; allowed: {', '.join(ALLOWED_IMPORTS)}")
    for k, v in meta.grid.items():
        if not isinstance(v, list) or not v:
            raise Rejected(f"grid[{k!r}] must be a non-empty list of the values to try, such as [3, 5]; "
                           f"it is {type(v).__name__} {v!r:.40}")
    combos = math.prod(len(v) for v in meta.grid.values())
    if combos > pc.max_grid_combos:
        raise Rejected(f"the grid has {combos} combinations; the limit is {pc.max_grid_combos}")


def check_overlap_rules(report: dict, p: Proposal, target: dict | None, record: Record, pc: ProposerConfig) -> None:
    if not report.get("ok"):
        raise Rejected("the overlap rules apply only to a passing preflight report")
    if "overlap" in (report.get("skipped") or {}):
        raise Rejected("preflight did not measure overlap; the near-copy check was not made")
    exempt = target["id"] if target is not None and p.kind in ("refinement", "port") else None
    # Ideas own runs, not plugin names: a seed plugin can run under one idea at 4h and under its port at 1h.
    owner = {(x["plugin"], x["timeframe"]): x["idea_id"] for x in record.proposals() if x.get("plugin")}
    ideas = {i["id"]: i for i in record.ideas()}
    for e in report.get("overlap") or []:
        if e["overlap"] < pc.overlap_max:
            continue
        idea = ideas.get(owner.get((e["strategy"], e["timeframe"])))
        if idea is None or idea["id"] == exempt:
            continue
        if idea["status"] != "open":
            raise Rejected(f"near-copy of closed idea #{idea['id']} ({idea['title']})")
        # Another open idea's mechanism belongs in that idea, whatever this proposal's kind: one mechanism stays
        # one family with one holdout read.
        raise Rejected(f"propose it as a refinement (or a port) of idea #{idea['id']} ({idea['title']})")

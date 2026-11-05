"""Plugin source and metadata, read from the text with `ast`; no plugin code runs here, except in
sandbox workers for `reads_columns`'s dynamic half.

The hash is the git blob SHA-1 of the bytes read, so it equals `git hash-object` for the file.
"""

from __future__ import annotations

import ast
import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from perp_lab import strategy
from perp_lab.session_window import check_session

_CLASS_DEFAULTS = {"params": {}, "grid": {}, "timeframes": ["1h"], "session": {}}


def blob_hash(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


class PluginMetaError(ValueError):
    def __init__(self, name: str, hash: str, message: str):
        super().__init__(message)
        self.name, self.hash = name, hash


class UndeclaredTimeframe(ValueError):
    """A walk-forward asked for at a timeframe the plugin's `timeframes` literal doesn't list."""


def require_timeframe(src: PluginSource, tf: str, role: str = "") -> None:
    """Refuse `tf` unless `src` declares it. A walk-forward is a trial (N counts distinct (plugin
    version, timeframe) pairs) and can't be taken back, so a mistyped or defaulted `--tf` must not
    spend one. `role` prefixes the plugin's name in the message (e.g. "parent ")."""
    if tf not in src.meta.timeframes:
        raise UndeclaredTimeframe(f"{role}{src.name} declares timeframes {', '.join(src.meta.timeframes)}, not {tf}")


@dataclass(frozen=True, slots=True)
class PluginMeta:
    parent: str | None
    hypothesis: str
    params: dict
    grid: dict
    timeframes: list[str]
    session: dict


@dataclass(frozen=True, slots=True)
class PluginSource:
    name: str
    text: bytes
    hash: str
    meta: PluginMeta

    @classmethod
    def read(cls, name: str) -> PluginSource:
        return cls.from_text(name, strategy.plugin_path(name).read_bytes())

    @classmethod
    def from_text(cls, name: str, text: str | bytes) -> PluginSource:
        data = text.encode() if isinstance(text, str) else text
        return cls(name, data, blob_hash(data), parse_meta(data, name))


def _is_strategy_base(b: ast.expr) -> bool:
    return (isinstance(b, ast.Name) and b.id == "Strategy") or (
        isinstance(b, ast.Attribute) and b.attr == "Strategy")


def _targets(node: ast.AST) -> list[ast.expr]:
    if isinstance(node, ast.Assign):
        return node.targets
    if isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        return [node.target]
    return []


def _names(t: ast.expr):
    if isinstance(t, ast.Name):
        yield t.id
    elif isinstance(t, (ast.Tuple, ast.List)):
        for e in t.elts:
            yield from _names(e)
    elif isinstance(t, ast.Starred):
        yield from _names(t.value)


def _assigned(node: ast.AST, name: str) -> bool:
    return any(name in _names(t) for t in _targets(node))


def parse_meta(text: bytes, name: str) -> PluginMeta:
    h = blob_hash(text)

    def refuse(msg: str):
        raise PluginMetaError(name, h, f"{msg}")

    try:
        tree = ast.parse(text, filename=f"{name}.py")
    except (SyntaxError, ValueError) as e:
        refuse(f"does not parse: {e}")

    parent_nodes = [n for n in ast.walk(tree) if _assigned(n, "PARENT")]
    parent = None
    if len(parent_nodes) > 1:
        refuse("PARENT is assigned more than once")
    if parent_nodes:
        n = parent_nodes[0]
        if n not in tree.body:
            refuse("PARENT must be a top-level statement")
        v = n.value if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name) \
            else (n.value if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) else None)
        if not (isinstance(v, ast.Constant) and isinstance(v.value, str)):
            refuse("PARENT must be a single string literal")
        parent = v.value or None

    classes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and any(map(_is_strategy_base, n.bases))]
    if len(classes) != 1:
        refuse(f"expected exactly one Strategy subclass, found {len(classes)}")
    meta = {}
    for key, default in _CLASS_DEFAULTS.items():
        nodes = [n for n in classes[0].body if _assigned(n, key)]
        if len(nodes) > 1:
            refuse(f"{key} is assigned more than once")
        if not nodes:
            meta[key] = default.copy()
            continue
        try:
            meta[key] = ast.literal_eval(nodes[0].value) if nodes[0].value is not None else refuse(f"{key} has no value")
        except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
            refuse(f"{key} must be a literal (computed values are not read)")
        if not isinstance(meta[key], list if key == "timeframes" else dict) or (
                key == "timeframes" and not all(isinstance(t, str) for t in meta[key])):
            refuse(f"{key} must be a literal {'list of strings' if key == 'timeframes' else 'dict'}")

    try:
        check_session(meta["session"])
    except ValueError as e:
        refuse(str(e))

    doc = ast.get_docstring(tree, clean=False) or ""
    hypothesis = doc.strip().splitlines()[0].strip() if doc.strip() else ""
    return PluginMeta(parent, hypothesis, meta["params"], meta["grid"], meta["timeframes"], meta["session"])


def names_group(src: PluginSource, group) -> list[str]:
    """Where `src` names a column group (a `columns.ColumnGroup`), from the text with `ast`; no plugin
    code runs. It counts a string constant or f-string part containing one of the group's columns or its
    prefix, an attribute named like one (`bars.btc_close`), and a call to one of its helpers
    (`reference_frame(bars)`, `ind.reference_frame(bars)`). Each name is listed once, in source order:
    the column, or the prefix for a string that holds no whole column, or the helper. Comments aren't in
    the tree, so they never count. Docstrings are string constants, so they do: a plugin can read its
    own back, and a false positive only costs a refused paper run."""
    hits = []                                       # (line, column, name)
    for node in ast.walk(ast.parse(src.text)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            s = node.value
            names = [c for c in group.columns if c in s] or ([group.prefix] if group.prefix in s else [])
        elif isinstance(node, ast.Attribute) and (node.attr in group.columns or node.attr.startswith(group.prefix)):
            names = [node.attr]
        elif isinstance(node, ast.Call) and _called(node) in group.helpers:
            names = [_called(node)]
        else:
            continue
        hits += [(node.lineno, node.col_offset, n) for n in names]
    return list(dict.fromkeys(n for *_, n in sorted(hits)))


def _called(call: ast.Call) -> str | None:
    """The called name: `f` for `f(...)` and `m.f(...)`; None for any other callee."""
    f = call.func
    return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None


def reads_columns(cfg, src: PluginSource, group, params: dict | None = None) -> bool:
    """Whether `src` reads `group`: it names it (`names_group`), or its decisions change when only the
    group's stream does. The dynamic half runs as the lookahead check does, on its synthetic bars as coin
    "X" at 1h, each run in a fresh sandbox worker: at the default params (and at `params`, when given),
    once with `group.synthetic(bars, 1)` and once with seed 2. Any difference in entries or exits counts.
    Features don't, so a plugin that passes every column through `prepare` and decides on close alone
    does not read the group. A run's PluginError propagates."""
    if names_group(src, group):
        return True
    from perp_lab.engine.backtest import EngineParams
    from perp_lab.sandbox import client             # client imports this module
    from perp_lab.validate import causality

    bars = causality.with_moving_columns(causality.synthetic_bars(seed=1))
    streams = [bars.assign(**group.synthetic(bars, seed)) for seed in (1, 2)]
    variants = [{}] + ([dict(params)] if params else [])
    with ThreadPoolExecutor(client.max_workers(cfg)) as pool:
        runs = [[pool.submit(client.fresh_run, cfg, src, "X", "1h", b, kw, EngineParams()) for b in streams]
                for kw in variants]
        return any(not a.result().decisions.same(b.result().decisions) for a, b in runs)

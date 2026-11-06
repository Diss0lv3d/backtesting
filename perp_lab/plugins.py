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
    """Where `src` names a column group (a `columns.ColumnGroup`), read from the text with `ast`; no
    plugin code runs. Each name is listed once, in source order. It counts:
    - a string constant or f-string part that holds one of the group's columns (listed); else its prefix
      (`f"btc_{k}"` lists "btc_"); else, outside a docstring, the prefix's stem (`filter(like="btc")`
      lists "btc");
    - an attribute named like a column or a helper, or starting with the prefix (`bars.btc_close`,
      `ind.reference_frame`), called or not;
    - a name bound to a helper, called or not, listed as the helper: `reference_frame(bars)`, `rf(bars)`
      after `import reference_frame as rf`, `f = reference_frame`. A bare import is not a read.
    Comments aren't in the tree, so they never count. Docstrings count by prefixed column or prefix (a plugin
    can read its own back, and a false positive only costs a refused paper run), not by the bare stem, which
    prose uses for the coin, nor by an unprefixed column (`basis`), which is an ordinary word."""
    tree = ast.parse(src.text)
    helper = {h: h for h in group.helpers} | {
        a.asname or a.name: a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
        for a in n.names if a.name in group.helpers}
    docs = {d for n in ast.walk(tree) if (d := _docstring(n)) is not None}
    stem = group.prefix.rstrip("_")
    hits = []                                       # (line, column, name)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            names = _named_in(node.value, group, node in docs, stem)
        elif isinstance(node, ast.Attribute) and (node.attr in group.columns or node.attr in group.helpers
                                                  or node.attr.startswith(group.prefix)):
            names = [node.attr]
        elif isinstance(node, ast.Name) and node.id in helper:
            names = [helper[node.id]]
        else:
            continue
        hits += [(node.lineno, node.col_offset, n) for n in names]
    return list(dict.fromkeys(n for *_, n in sorted(hits)))


def _docstring(node: ast.AST) -> ast.Constant | None:
    """The docstring's constant of a module, class or function; None for any other node or none there."""
    if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
        first = node.body[0].value if isinstance(node.body[0], ast.Expr) else None
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first
    return None


def _named_in(s: str, group, docstring: bool, stem: str) -> list[str]:
    """The group's columns that `s` holds; else its prefix, if `s` holds it; else, outside a docstring, `stem`,
    if held. In a docstring only the prefixed columns count: an unprefixed one (`basis`) is a word."""
    cols = [c for c in group.columns if c in s and not (docstring and not c.startswith(group.prefix))]
    if cols:
        return cols
    for name in (group.prefix, None if docstring else stem):
        if name and name in s:
            return [name]
    return []


# A run's failure that is the machine's or the pipe's, not the plugin's: a worker lost twice (`fresh_run`
# retries a WorkerLost once, then raises PluginError with its kind) or a protocol error.
MACHINE_KINDS = ("died", "timeout", "protocol")


def reads_columns(cfg, src: PluginSource, group, params: dict | None = None) -> bool:
    """Whether `src` reads `group`: it names it (`names_group`), or the dynamic half can't rule it out.

    That half runs the plugin as the lookahead check does: on the check's own bars (`causality.check_bars()`,
    whose group stream the check verified), as coin "X" at 1h, each run in a fresh sandbox worker, at the
    default params and at `params` when given. For each params, one run is on those bars and one has only
    the group's stream replaced, by `group.synthetic(bars, 1)`. The plugin reads the group when, for either
    params:
    - the two runs' decisions differ: entries and exits with their stop and target levels. Features don't
      count, so a plugin that passes every column through `prepare` and decides on close alone doesn't read it;
    - neither run makes a decision, so the runs can't tell;
    - the plugin fails a run (a PluginError). A worker lost twice and a protocol error are the machine's
      failures, not the plugin's, and are raised, as is any other error."""
    if names_group(src, group):
        return True
    from perp_lab.engine.backtest import EngineParams
    from perp_lab.sandbox import client             # client imports this module
    from perp_lab.sandbox.errors import PluginError
    from perp_lab.validate import causality

    checked = causality.check_bars()
    sides = (checked, checked.assign(**group.synthetic(checked, 1)))
    variants = [{}] + ([dict(params)] if params else [])
    with ThreadPoolExecutor(client.max_workers(cfg)) as pool:
        runs = [[pool.submit(client.fresh_run, cfg, src, "X", "1h", b, kw, EngineParams()) for b in sides]
                for kw in variants]
        for pair in runs:
            try:
                a, b = (f.result().decisions for f in pair)
            except PluginError as e:
                if e.kind in MACHINE_KINDS:
                    raise
                return True
            if a.n_actions() == b.n_actions() == 0 or not a.same(b):
                return True
    return False

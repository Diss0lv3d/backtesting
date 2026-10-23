"""The proposal contract: what the model returns, as one set of dataclasses.

The JSON schema handed to `claude -p --json-schema`, the prompt's description of the reply and the
orchestrator's own validation are all generated from the fields below (ADR 0007), so they can't drift.
`parse` does its own checks rather than pulling in a schema library: it covers exactly what `schema` emits.
"""

from __future__ import annotations

import math
import types
import typing
from dataclasses import MISSING, dataclass, fields, is_dataclass
from typing import Any, Literal, Union

HYPOTHESIS_MAX_CHARS = 200
SOURCE_MAX_BYTES = 65_536

FIELD_DOCS: dict[str, str] = {
    "IdeaCard.title": "A short name for the idea, not already used by an idea in the record.",
    "IdeaCard.mechanism": "What happens in the market, and who pays.",
    "IdeaCard.why_it_persists": "Why the effect should keep paying instead of being arbitraged away.",
    "IdeaCard.signal": "The signal, in words.",
    "IdeaCard.trades_per_coin_per_year": "An estimate of how many trades a coin gets in a year.",
    "IdeaCard.likely_failure": "How the idea is most likely to fail.",
    "IdeaCard.columns": "The bar columns the plugin reads.",
    "Refines.idea_id": "The id of the existing idea this refines.",
    "Refines.change": "What changes and why, citing the diagnostics behind it.",
    "Ports.idea_id": "The id of the existing idea this ports to another timeframe.",
    "Ports.why": "Why the other timeframe should keep the effect and lower its cost, citing the "
    "diagnostics behind it. The port inherits the idea's card.",
    "Proposal.kind": "new (a new idea), refinement (a change to an existing idea) or port "
    "(an existing idea at another timeframe).",
    "Proposal.timeframe": "The one timeframe the plugin trades.",
    "Proposal.hypothesis": f"One line, at most {HYPOTHESIS_MAX_CHARS} characters.",
    "Proposal.source": f"The plugin module, with no PARENT. At most {SOURCE_MAX_BYTES} bytes.",
    "Proposal.idea": "The idea card. Required when kind is new, otherwise omitted.",
    "Proposal.refines": "Required when kind is refinement, otherwise omitted.",
    "Proposal.ports": "Required when kind is port, otherwise omitted.",
}


@dataclass(frozen=True)
class IdeaCard:
    title: str
    mechanism: str
    why_it_persists: str
    signal: str
    trades_per_coin_per_year: float
    likely_failure: str
    columns: tuple[str, ...]


@dataclass(frozen=True)
class Refines:
    idea_id: int
    change: str


@dataclass(frozen=True)
class Ports:
    idea_id: int
    why: str


@dataclass(frozen=True)
class Proposal:
    kind: Literal["new", "refinement", "port"]
    timeframe: str
    hypothesis: str
    source: str
    idea: IdeaCard | None = None
    refines: Refines | None = None
    ports: Ports | None = None


# kind -> the block it requires
BLOCK_FOR_KIND = {"new": "idea", "refinement": "refines", "port": "ports"}


class ContractError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


def _unwrap(tp):
    """(inner type, optional?) for `X | None` / Optional[X]."""
    if typing.get_origin(tp) in (Union, types.UnionType):
        args = [a for a in typing.get_args(tp) if a is not type(None)]
        if len(args) == 1:
            return args[0], True
    return tp, False


def _hints(cls):
    return typing.get_type_hints(cls)


def _field_schema(tp, timeframes, doc, name):
    if is_dataclass(tp):
        s = _object_schema(tp, timeframes)
    elif typing.get_origin(tp) is Literal:
        s = {"type": "string", "enum": list(typing.get_args(tp))}
    elif tp is str:
        s = {"type": "string", "minLength": 1}
    elif tp is float:
        s = {"type": "number"}
    elif tp is int:
        s = {"type": "integer"}
    elif typing.get_origin(tp) is tuple and typing.get_args(tp)[1:] == (Ellipsis,):
        s = {"type": "array", "items": {"type": "string", "minLength": 1}}
    else:
        raise TypeError(f"no schema for {tp!r}")
    if name == "timeframe":
        s = {"type": "string", "enum": list(timeframes)}
    if name == "hypothesis":
        s = {**s, "maxLength": HYPOTHESIS_MAX_CHARS, "pattern": "^[^\\r\\n]+$"}
    if name == "source":
        s = {**s, "maxLength": SOURCE_MAX_BYTES}  # characters; parse enforces bytes
    return {**s, "description": doc} if doc else s


def _object_schema(cls, timeframes):
    hints = _hints(cls)
    props, required = {}, []
    for f in fields(cls):
        inner, optional = _unwrap(hints[f.name])
        props[f.name] = _field_schema(inner, timeframes, FIELD_DOCS.get(f"{cls.__name__}.{f.name}"), f.name)
        if not optional and f.default is MISSING:
            required.append(f.name)
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


def schema(timeframes) -> dict:
    s = _object_schema(Proposal, tuple(timeframes))
    s["allOf"] = [
        {
            "if": {"properties": {"kind": {"const": kind}}, "required": ["kind"]},
            "then": {"required": [block]},
        }
        for kind, block in BLOCK_FOR_KIND.items()
    ]
    return s


def _type_words(tp, timeframes, name):
    if is_dataclass(tp):
        return "object"
    if typing.get_origin(tp) is Literal:
        return "one of " + ", ".join(typing.get_args(tp))
    if name == "timeframe":
        return "one of " + ", ".join(timeframes)
    if tp is str:
        return "string"
    if tp is float:
        return "number"
    if tp is int:
        return "integer"
    return "array of strings"


def _describe_class(cls, timeframes, indent, out):
    hints = _hints(cls)
    for f in fields(cls):
        inner, optional = _unwrap(hints[f.name])
        req = "optional" if optional or f.default is not MISSING else "required"
        doc = FIELD_DOCS.get(f"{cls.__name__}.{f.name}", "")
        out.append(f"{'  ' * indent}- {f.name} ({_type_words(inner, timeframes, f.name)}; {req}): {doc}")
        if is_dataclass(inner):
            _describe_class(inner, timeframes, indent + 1, out)


def describe(timeframes) -> str:
    timeframes = tuple(timeframes)
    out = ["Reply with one JSON object and nothing else. Unknown keys are refused.", ""]
    _describe_class(Proposal, timeframes, 0, out)
    out.append("")
    for kind, block in BLOCK_FOR_KIND.items():
        out.append(f"When kind is {kind}, {block} is required.")
    return "\n".join(out)


def _check_value(tp, v, path, timeframes, name, errors):
    if is_dataclass(tp):
        return _check_object(tp, v, path, timeframes, errors)
    if typing.get_origin(tp) is Literal:
        ok = isinstance(v, str) and v in typing.get_args(tp)
        if not ok:
            errors.append(f"{path}: must be one of {list(typing.get_args(tp))}, got {v!r}")
        return v if ok else None
    if tp is str:
        if not isinstance(v, str) or not v:
            errors.append(f"{path}: must be a non-empty string")
            return None
        if name == "timeframe" and v not in timeframes:
            errors.append(f"{path}: must be one of {list(timeframes)}, got {v!r}")
            return None
        if name == "hypothesis":
            if "\n" in v or "\r" in v:
                errors.append(f"{path}: must be one line")
            if len(v) > HYPOTHESIS_MAX_CHARS:
                errors.append(f"{path}: {len(v)} characters, at most {HYPOTHESIS_MAX_CHARS}")
        if name == "source" and len(v.encode("utf-8")) > SOURCE_MAX_BYTES:
            errors.append(f"{path}: more than {SOURCE_MAX_BYTES} bytes")
        return v
    if tp is float:
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            errors.append(f"{path}: must be a finite number")
            return None
        return float(v)
    if tp is int:
        if isinstance(v, bool) or not isinstance(v, int):
            errors.append(f"{path}: must be an integer")
            return None
        return v
    # tuple[str, ...]
    if not isinstance(v, (list, tuple)) or not all(isinstance(x, str) and x for x in v):
        errors.append(f"{path}: must be an array of non-empty strings")
        return None
    return tuple(v)


def _check_object(cls, obj, path, timeframes, errors):
    if not isinstance(obj, dict):
        errors.append(f"{path}: must be an object")
        return None
    hints = _hints(cls)
    names = {f.name for f in fields(cls)}
    for k in sorted(set(obj) - names, key=str):
        errors.append(f"{path}.{k}: unknown field" if path else f"{k}: unknown field")
    kwargs, bad = {}, False
    for f in fields(cls):
        inner, optional = _unwrap(hints[f.name])
        p = f"{path}.{f.name}" if path else f.name
        if f.name not in obj or (optional and obj[f.name] is None):
            if not optional and f.default is MISSING:
                errors.append(f"{p}: required")
                bad = True
            continue
        val = _check_value(inner, obj[f.name], p, timeframes, f.name, errors)
        if val is None:
            bad = True
        else:
            kwargs[f.name] = val
    return None if bad else cls(**kwargs)


def parse(obj: dict, timeframes) -> Proposal:
    timeframes = tuple(timeframes)
    errors: list[str] = []
    prop = _check_object(Proposal, obj, "", timeframes, errors)
    kind = obj.get("kind") if isinstance(obj, dict) else None
    block = BLOCK_FOR_KIND.get(kind) if isinstance(kind, str) else None
    if block and isinstance(obj, dict) and obj.get(block) is None:
        errors.append(f"{block}: required when kind is {kind}")
    if errors or prop is None:
        raise ContractError(errors)
    return prop

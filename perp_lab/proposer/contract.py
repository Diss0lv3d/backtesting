"""The proposal and research contracts: what the model returns, as dataclasses.

The JSON schema handed to `claude -p --json-schema`, the prompt's description of the reply and the
orchestrator's own validation are all generated from the fields below (ADR 0007), so they can't drift.
`parse` does its own checks rather than pulling in a schema library: it covers exactly what `schema` emits,
plus one rule the schema can't carry. The CLI sends the schema as a tool's input_schema, and the API refuses
oneOf, allOf or anyOf at its top level (R42), so "kind -> its block" is stated by `describe` and enforced by
`parse` alone.

A research round's reply, `ResearchNote`, comes from the same machinery (`research_schema`, `describe_research`,
`parse_research`); its ideas' record rules (a new title, a proposer timeframe, readable columns) are research.py's.
"""

from __future__ import annotations

import math
import types
import typing
from dataclasses import MISSING, dataclass, fields, is_dataclass
from typing import Any, Literal, Union

HYPOTHESIS_MAX_CHARS = 200
SOURCE_MAX_BYTES = 65_536
BUILDS_ON_MAX_CHARS = 300
STOP_MAX_ITEMS = 5
STOP_MAX_CHARS = 200
SUMMARY_MAX_CHARS = 2_000

# field -> (at most this many characters, one line?): the schema states it and parse enforces it, on each item
# of an array of strings
TEXT_LIMITS = {
    "hypothesis": (HYPOTHESIS_MAX_CHARS, True),
    "builds_on": (BUILDS_ON_MAX_CHARS, True),
    "stop": (STOP_MAX_CHARS, True),
    "summary": (SUMMARY_MAX_CHARS, False),
}
ONE_LINE = "^[^\\r\\n]+$"

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
    "ResearchIdea.title": "A short name for the idea, on one line, not already used by an idea in the record or "
    "by another idea in this reply.",
    "ResearchIdea.timeframe": "The one timeframe the idea's plugin should trade.",
    "ResearchIdea.builds_on": f"One line, at most {BUILDS_ON_MAX_CHARS} characters: the lesson or result in the "
    "record the idea builds on.",
    "ResearchNote.ideas": "New idea cards that build on what the record has taught rather than repeat it. May be "
    "empty.",
    "ResearchNote.stop": f"Directions the builder should stop trying, each one line of at most {STOP_MAX_CHARS} "
    "characters. May be empty.",
    "ResearchNote.summary": f"For the human: what the record has taught and why these ideas. At most "
    f"{SUMMARY_MAX_CHARS} characters.",
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


@dataclass(frozen=True)
class ResearchIdea(IdeaCard):
    timeframe: str
    builds_on: str


@dataclass(frozen=True)
class ResearchNote:
    ideas: tuple[ResearchIdea, ...]
    stop: tuple[str, ...]
    summary: str


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


class _Ctx(typing.NamedTuple):
    """What a contract's limits depend on at run time."""
    timeframes: tuple[str, ...]
    max_items: dict[str, int]       # array field -> at most this many items


def _hints(cls):
    return typing.get_type_hints(cls)


def _doc(cls, name):
    """The field's description; a subclass inherits its base's unless it states its own."""
    for c in cls.__mro__:
        if (d := FIELD_DOCS.get(f"{c.__name__}.{name}")) is not None:
            return d
    return None


def _item(tp):
    """The item type of `tuple[X, ...]`, else None."""
    if typing.get_origin(tp) is tuple and typing.get_args(tp)[1:] == (Ellipsis,):
        return typing.get_args(tp)[0]
    return None


def _text_schema(name):
    s = {"type": "string", "minLength": 1}
    if name in TEXT_LIMITS:
        most, one_line = TEXT_LIMITS[name]
        s = {**s, "maxLength": most, **({"pattern": ONE_LINE} if one_line else {})}
    return s


def _field_schema(tp, ctx, doc, name):
    item = _item(tp)
    if is_dataclass(tp):
        s = _object_schema(tp, ctx)
    elif typing.get_origin(tp) is Literal:
        s = {"type": "string", "enum": list(typing.get_args(tp))}
    elif tp is str:
        s = _text_schema(name)
    elif tp is float:
        s = {"type": "number"}
    elif tp is int:
        s = {"type": "integer"}
    elif item is str or is_dataclass(item):
        s = {"type": "array", "items": _object_schema(item, ctx) if is_dataclass(item) else _text_schema(name)}
        if name in ctx.max_items:
            s["maxItems"] = ctx.max_items[name]
    else:
        raise TypeError(f"no schema for {tp!r}")
    if name == "timeframe":
        s = {"type": "string", "enum": list(ctx.timeframes)}
    if name == "source":
        s = {**s, "maxLength": SOURCE_MAX_BYTES}  # characters; parse enforces bytes
    return {**s, "description": doc} if doc else s


def _object_schema(cls, ctx):
    hints = _hints(cls)
    props, required = {}, []
    for f in fields(cls):
        inner, optional = _unwrap(hints[f.name])
        props[f.name] = _field_schema(inner, ctx, _doc(cls, f.name), f.name)
        if not optional and f.default is MISSING:
            required.append(f.name)
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


def _proposal_ctx(timeframes) -> _Ctx:
    return _Ctx(tuple(timeframes), {})


def _research_ctx(timeframes, max_ideas) -> _Ctx:
    return _Ctx(tuple(timeframes), {"ideas": max_ideas, "stop": STOP_MAX_ITEMS})


def schema(timeframes) -> dict:
    # No oneOf/allOf/anyOf at the top level: the API refuses the whole call (R42). parse() checks kind -> block.
    return _object_schema(Proposal, _proposal_ctx(timeframes))


def research_schema(timeframes, max_ideas: int) -> dict:
    return _object_schema(ResearchNote, _research_ctx(timeframes, max_ideas))     # no combinator, as schema()


def _type_words(tp, ctx, name):
    if is_dataclass(tp):
        return "object"
    if typing.get_origin(tp) is Literal:
        return "one of " + ", ".join(typing.get_args(tp))
    if name == "timeframe":
        return "one of " + ", ".join(ctx.timeframes)
    if tp is str:
        return "string"
    if tp is float:
        return "number"
    if tp is int:
        return "integer"
    words = "array of objects" if is_dataclass(_item(tp)) else "array of strings"
    return f"{words}, at most {ctx.max_items[name]}" if name in ctx.max_items else words


def _describe_class(cls, ctx, indent, out):
    hints = _hints(cls)
    for f in fields(cls):
        inner, optional = _unwrap(hints[f.name])
        req = "optional" if optional or f.default is not MISSING else "required"
        doc = _doc(cls, f.name) or ""
        out.append(f"{'  ' * indent}- {f.name} ({_type_words(inner, ctx, f.name)}; {req}): {doc}")
        sub = inner if is_dataclass(inner) else _item(inner)
        if is_dataclass(sub):
            _describe_class(sub, ctx, indent + 1, out)


def describe(timeframes) -> str:
    out = ["Reply with one JSON object and nothing else. Unknown keys are refused.", ""]
    _describe_class(Proposal, _proposal_ctx(timeframes), 0, out)
    out.append("")
    for kind, block in BLOCK_FOR_KIND.items():
        out.append(f"When kind is {kind}, {block} is required.")
    return "\n".join(out)


def describe_research(timeframes, max_ideas: int) -> str:
    out = ["Reply with one JSON object and nothing else. Unknown keys are refused.", ""]
    _describe_class(ResearchNote, _research_ctx(timeframes, max_ideas), 0, out)
    return "\n".join(out)


def _text_errors(v, path, name, errors):
    if name in TEXT_LIMITS:
        most, one_line = TEXT_LIMITS[name]
        if one_line and ("\n" in v or "\r" in v):
            errors.append(f"{path}: must be one line")
        if len(v) > most:
            errors.append(f"{path}: {len(v)} characters, at most {most}")


def _check_items(v, path, ctx, name, errors):
    if name in ctx.max_items and len(v) > ctx.max_items[name]:
        errors.append(f"{path}: {len(v)} items, at most {ctx.max_items[name]}")
        return False
    return True


def _check_value(tp, v, path, ctx, name, errors):
    if is_dataclass(tp):
        return _check_object(tp, v, path, ctx, errors)
    if typing.get_origin(tp) is Literal:
        ok = isinstance(v, str) and v in typing.get_args(tp)
        if not ok:
            errors.append(f"{path}: must be one of {list(typing.get_args(tp))}, got {v!r}")
        return v if ok else None
    if tp is str:
        if not isinstance(v, str) or not v:
            errors.append(f"{path}: must be a non-empty string")
            return None
        if name == "timeframe" and v not in ctx.timeframes:
            errors.append(f"{path}: must be one of {list(ctx.timeframes)}, got {v!r}")
            return None
        _text_errors(v, path, name, errors)
        if name == "source" and len(v.encode("utf-8")) > SOURCE_MAX_BYTES:
            errors.append(f"{path}: more than {SOURCE_MAX_BYTES} bytes")
        return v
    if tp is float:
        try:  # an int past ~1e308 overflows float(), and isfinite() on it
            ok = not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v)
            out = float(v) if ok else None
        except OverflowError:
            ok = False
        if not ok:
            errors.append(f"{path}: must be a finite number")
            return None
        return out
    if tp is int:
        if isinstance(v, bool) or not isinstance(v, int):
            errors.append(f"{path}: must be an integer")
            return None
        return v
    item = _item(tp)
    if is_dataclass(item):
        if not isinstance(v, (list, tuple)):
            errors.append(f"{path}: must be an array of objects")
            return None
        if not _check_items(v, path, ctx, name, errors):
            return None
        out = [_check_object(item, x, f"{path}[{n}]", ctx, errors) for n, x in enumerate(v)]
        return None if any(o is None for o in out) else tuple(out)
    # tuple[str, ...]
    if not isinstance(v, (list, tuple)) or not all(isinstance(x, str) and x for x in v):
        errors.append(f"{path}: must be an array of non-empty strings")
        return None
    if not _check_items(v, path, ctx, name, errors):
        return None
    for n, x in enumerate(v):
        _text_errors(x, f"{path}[{n}]", name, errors)
    return tuple(v)


def _check_object(cls, obj, path, ctx, errors):
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
        if f.name not in obj:
            if not optional and f.default is MISSING:
                errors.append(f"{p}: required")
                bad = True
            continue
        val = _check_value(inner, obj[f.name], p, ctx, f.name, errors)
        if val is None:
            bad = True
        else:
            kwargs[f.name] = val
    return None if bad else cls(**kwargs)


def parse_card(obj: dict) -> IdeaCard:
    """An idea card alone, by the checks `parse` gives a new idea's card: the human's own cards meet the model's
    rules (human.py)."""
    errors: list[str] = []
    card = _check_object(IdeaCard, obj, "", _proposal_ctx(()), errors)
    if errors or card is None:
        raise ContractError(errors)
    return card


def parse(obj: dict, timeframes) -> Proposal:
    errors: list[str] = []
    prop = _check_object(Proposal, obj, "", _proposal_ctx(timeframes), errors)
    kind = obj.get("kind") if isinstance(obj, dict) else None
    block = BLOCK_FOR_KIND.get(kind) if isinstance(kind, str) else None
    if block and isinstance(obj, dict) and block not in obj:
        errors.append(f"{block}: required when kind is {kind}")
    if errors or prop is None:
        raise ContractError(errors)
    return prop


def parse_research(obj: dict, timeframes, max_ideas: int) -> ResearchNote:
    """The research reply by the contract alone; research.check_note adds the record's rules for its ideas."""
    errors: list[str] = []
    note = _check_object(ResearchNote, obj, "", _research_ctx(timeframes, max_ideas), errors)
    if errors or note is None:
        raise ContractError(errors)
    return note

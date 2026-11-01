"""The proposal contract: one set of dataclasses yields the schema, the prompt text and the validation."""

from __future__ import annotations

import pytest

from perp_lab.proposer.contract import (
    BLOCK_FOR_KIND, FIELD_DOCS, ContractError, IdeaCard, Ports, Proposal, Refines, ResearchIdea, ResearchNote,
    describe, describe_research, parse, parse_card, parse_research, research_schema, schema,
)

TFS = ("15m", "1h", "4h")
CARD = {
    "title": "t", "mechanism": "m", "why_it_persists": "w", "signal": "s",
    "trades_per_coin_per_year": 12, "likely_failure": "f", "columns": ["close", "volume"],
}


def good(**over):
    base = {"kind": "new", "timeframe": "1h", "hypothesis": "one line", "source": "x = 1", "idea": CARD}
    return {**base, **over}


def _names(s):
    if isinstance(s, dict):
        for k, v in s.get("properties", {}).items():
            yield k
            yield from _names(v)
        yield from _names(s.get("items"))


def test_schema_and_description_come_from_the_same_fields():
    text = describe(TFS)
    names = set(_names(schema(TFS)))
    assert {"kind", "idea", "title", "columns", "refines", "change", "ports", "why", "idea_id"} <= names
    for n in names:
        assert n in text
    for key in FIELD_DOCS:
        if not key.startswith("Research"):
            assert FIELD_DOCS[key] in text


def test_parse_accepts_a_new_idea():
    p = parse(good(), TFS)
    assert isinstance(p, Proposal) and isinstance(p.idea, IdeaCard)
    assert p.idea.columns == ("close", "volume") and p.idea.trades_per_coin_per_year == 12.0
    assert p.refines is None and p.ports is None


def test_parse_accepts_refinement_and_port():
    r = parse({"kind": "refinement", "timeframe": "4h", "hypothesis": "h", "source": "s",
               "refines": {"idea_id": 3, "change": "c"}}, TFS)
    assert r.refines == Refines(3, "c")
    p = parse({"kind": "port", "timeframe": "4h", "hypothesis": "h", "source": "s",
               "ports": {"idea_id": 3, "why": "y"}}, TFS)
    assert p.ports == Ports(3, "y")


def test_parse_lists_every_problem():
    with pytest.raises(ContractError) as e:
        parse({"kind": "new", "timeframe": "5m", "hypothesis": "a\nb", "source": "x"}, TFS)
    assert len(e.value.errors) == 3


@pytest.mark.parametrize("kind,block", [("new", "idea"), ("refinement", "refines"), ("port", "ports")])
def test_a_kind_requires_its_block(kind, block):
    with pytest.raises(ContractError) as e:
        parse({"kind": kind, "timeframe": "1h", "hypothesis": "h", "source": "s"}, TFS)
    assert any(block in m for m in e.value.errors)
    assert f"When kind is {kind}, {block} is required." in describe(TFS)     # the model is told the rule


def test_the_schema_has_no_combinator_at_its_top_level():
    """The CLI turns --json-schema into a tool's input_schema. With the kind -> block rule as a top-level allOf
    (if/then on kind), a real call on 2026-11-01 got, as a result with is_error true, api_error_status 400 and
    terminal_reason api_error:

        API Error: 400 tools.1.custom.input_schema: input_schema does not support oneOf, allOf, or anyOf at the top level

    so every proposal call failed (R42). The rule is parse()'s alone now, and describe() states it."""
    s = schema(TFS)
    assert not {"oneOf", "allOf", "anyOf"} & set(s)
    assert set(s) == {"type", "properties", "required", "additionalProperties"}


def test_the_schema_is_closed():
    s = schema(TFS)
    assert s["additionalProperties"] is False
    assert {"kind", "timeframe", "hypothesis", "source"} <= set(s["required"])
    assert "idea" not in s["required"]
    for sub in ("idea", "refines", "ports"):
        assert s["properties"][sub]["additionalProperties"] is False
    assert s["properties"]["timeframe"]["enum"] == list(TFS)
    assert s["properties"]["kind"]["enum"] == ["new", "refinement", "port"]
    assert s["properties"]["idea"]["properties"]["columns"]["type"] == "array"


def test_parse_rechecks_types_unknown_keys_and_limits():
    with pytest.raises(ContractError) as e:
        parse(good(extra=1, hypothesis="x" * 201, source="é" * 40_000,
                   idea={**CARD, "trades_per_coin_per_year": True, "columns": "close"}), TFS)
    errs = " | ".join(e.value.errors)
    for frag in ("extra", "hypothesis", "source", "trades_per_coin_per_year", "columns"):
        assert frag in errs
    with pytest.raises(ContractError):
        parse(good(refines={"idea_id": "3", "change": "c"}), TFS)
    with pytest.raises(ContractError):
        parse([], TFS)


def test_a_huge_int_is_a_contract_error_not_an_overflow():
    with pytest.raises(ContractError) as e:
        parse(good(idea={**CARD, "trades_per_coin_per_year": 10**400}), TFS)
    assert any("trades_per_coin_per_year" in m for m in e.value.errors)


def test_an_explicit_null_block_is_refused():
    with pytest.raises(ContractError):
        parse(good(ports=None), TFS)


def _case(**over):
    base = {"kind": "new", "timeframe": "1h", "hypothesis": "h", "source": "s", "idea": CARD}
    out = {**base, **over}
    return {k: v for k, v in out.items() if v is not ...}


# Known, accepted disagreements, left out of the table: integral floats like 3.0 for integer fields (the
# schema accepts them, parse refuses), and the source limit (bytes in parse, characters in the schema). The one
# rule parse adds on purpose, kind -> its block, is `_kind_rule` below.
AGREEMENT = [
    _case(),
    _case(kind="refinement", idea=..., refines={"idea_id": 1, "change": "c"}),
    _case(kind="port", idea=..., ports={"idea_id": 1, "why": "y"}),
    _case(kind="refinement", idea=...),
    _case(idea=...),
    _case(ports=None),
    _case(ports={"idea_id": 1, "why": "y"}),
    _case(hypothesis="x" * 200),
    _case(hypothesis="x" * 201),
    _case(hypothesis="a\nb"),
    _case(hypothesis=""),
    _case(source=""),
    _case(timeframe="5m"),
    _case(kind="other"),
    _case(extra=1),
    _case(idea={**CARD, "extra": 1}),
    _case(idea={**CARD, "trades_per_coin_per_year": True}),
    _case(idea={**CARD, "trades_per_coin_per_year": "12"}),
    _case(idea={**CARD, "trades_per_coin_per_year": 12.5}),
    _case(idea={**CARD, "columns": "close"}),
    _case(idea={**CARD, "columns": [""]}),
    _case(idea={**CARD, "title": ""}),
    _case(kind="refinement", idea=..., refines={"idea_id": True, "change": "c"}),
    {"kind": "new", "timeframe": "5m", "hypothesis": "a\nb", "source": "x"},
]


def _kind_rule(obj) -> bool:
    """The rule the schema can't carry (the API refuses a top-level allOf, R42): parse requires the kind's block."""
    block = BLOCK_FOR_KIND.get(obj.get("kind")) if isinstance(obj.get("kind"), str) else None
    return block is None or block in obj


@pytest.mark.parametrize("obj", AGREEMENT)
def test_parse_accepts_exactly_what_the_schema_and_the_kind_rule_accept(obj):
    jsonschema = pytest.importorskip("jsonschema")
    try:
        jsonschema.validate(obj, schema(TFS))
        by_schema = _kind_rule(obj)
    except jsonschema.ValidationError:
        by_schema = False
    try:
        parse(obj, TFS)
        by_parse = True
    except ContractError:
        by_parse = False
    assert by_parse == by_schema, obj


@pytest.mark.parametrize("obj", [_case(kind="refinement", idea=...), _case(kind="port", idea=...), _case(idea=...)])
def test_the_kind_rule_is_the_only_thing_parse_adds(obj):
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.validate(obj, schema(TFS))                # the schema lets a kind without its block through
    with pytest.raises(ContractError, match="required when kind is"):
        parse(obj, TFS)


def test_parse_card_checks_a_card_as_parse_checks_a_new_ideas():
    """The human's cards (human.py) pass the model's own card checks: the same errors, without the idea. path."""
    assert parse_card(CARD) == parse(good(), TFS).idea
    bad = {**CARD, "mechanism": "", "trades_per_coin_per_year": "many", "columns": "close", "extra": 1}
    del bad["signal"]
    with pytest.raises(ContractError) as as_card:
        parse_card(bad)
    with pytest.raises(ContractError) as in_proposal:
        parse(good(idea=bad), TFS)
    assert in_proposal.value.errors == ["idea." + e for e in as_card.value.errors]
    assert len(as_card.value.errors) == 5
    with pytest.raises(ContractError, match="must be an object"):
        parse_card(["not", "a", "card"])


# The research note: the reply of a research round, by the same machinery.

MAX = 3
IDEA = {**CARD, "timeframe": "4h", "builds_on": "the funding fades all closed"}


def note(**over):
    out = {"ideas": [IDEA], "stop": ["sign flips of closed ideas"], "summary": "what the record taught\nand why"}
    out = {**out, **over}
    return {k: v for k, v in out.items() if v is not ...}


def test_the_research_schema_and_description_come_from_the_same_fields():
    text = describe_research(TFS, MAX)
    names = set(_names(research_schema(TFS, MAX)))
    assert {"ideas", "stop", "summary", "title", "columns", "timeframe", "builds_on"} <= names
    for n in names:
        assert n in text
    for key, doc in FIELD_DOCS.items():
        if key.startswith("Research") or (key.startswith("IdeaCard.") and key != "IdeaCard.title"):
            assert doc in text, key                              # an idea's card fields keep the card's words
    assert FIELD_DOCS["IdeaCard.title"] not in text              # a research idea's title says more
    assert f"ideas (array of objects, at most {MAX}; required)" in text
    assert "stop (array of strings, at most 5; required)" in text
    assert "  - builds_on (string; required)" in text and "  - timeframe (one of 15m, 1h, 4h; required)" in text


def test_the_research_schema_has_no_combinator_at_its_top_level():
    """The API refuses oneOf, allOf or anyOf at an input_schema's top level (R42): see the proposal's test."""
    s = research_schema(TFS, MAX)
    assert not {"oneOf", "allOf", "anyOf"} & set(s)
    assert set(s) == {"type", "properties", "required", "additionalProperties"}


def test_the_research_schema_is_closed_and_carries_the_limits():
    jsonschema = pytest.importorskip("jsonschema")
    s = research_schema(TFS, 2)
    jsonschema.Draft202012Validator.check_schema(s)
    assert s["additionalProperties"] is False and s["required"] == ["ideas", "stop", "summary"]
    ideas, stop, summary = (s["properties"][k] for k in ("ideas", "stop", "summary"))
    assert (ideas["type"], ideas["maxItems"]) == ("array", 2)
    item = ideas["items"]
    assert item["additionalProperties"] is False and item["required"] == [
        "title", "mechanism", "why_it_persists", "signal", "trades_per_coin_per_year", "likely_failure", "columns",
        "timeframe", "builds_on"]
    assert item["properties"]["timeframe"]["enum"] == list(TFS)
    assert (item["properties"]["builds_on"]["maxLength"], item["properties"]["builds_on"]["pattern"]) == (
        300, "^[^\\r\\n]+$")
    assert (stop["maxItems"], stop["items"]["maxLength"], stop["items"]["pattern"]) == (5, 200, "^[^\\r\\n]+$")
    assert summary["maxLength"] == 2000 and "pattern" not in summary


def test_parse_research_accepts_a_note():
    n = parse_research(note(ideas=[IDEA, {**IDEA, "title": "u", "timeframe": "15m"}]), TFS, MAX)
    assert isinstance(n, ResearchNote) and all(isinstance(i, ResearchIdea) for i in n.ideas)
    assert [(i.title, i.timeframe) for i in n.ideas] == [("t", "4h"), ("u", "15m")]
    assert n.ideas[0].columns == ("close", "volume") and n.ideas[0].trades_per_coin_per_year == 12.0
    assert n.ideas[0].builds_on == "the funding fades all closed"
    assert n.stop == ("sign flips of closed ideas",) and n.summary == "what the record taught\nand why"


def test_a_note_may_hold_no_idea_and_no_stop():
    n = parse_research(note(ideas=[], stop=[]), TFS, MAX)
    assert n.ideas == () and n.stop == ()


@pytest.mark.parametrize("over,error", [
    ({"ideas": [IDEA] * 4}, "ideas: 4 items, at most 3"),
    ({"stop": ["s"] * 6}, "stop: 6 items, at most 5"),
    ({"stop": ["ok", "a\nb"]}, "stop[1]: must be one line"),
    ({"stop": ["x" * 201]}, "stop[0]: 201 characters, at most 200"),
    ({"stop": ["ok", ""]}, "stop: must be an array of non-empty strings"),
    ({"summary": "x" * 2001}, "summary: 2001 characters, at most 2000"),
    ({"summary": ""}, "summary: must be a non-empty string"),
    ({"ideas": [IDEA, {**IDEA, "builds_on": "a\rb"}]}, "ideas[1].builds_on: must be one line"),
    ({"ideas": [{**IDEA, "builds_on": "x" * 301}]}, "ideas[0].builds_on: 301 characters, at most 300"),
    ({"ideas": [{**IDEA, "timeframe": "5m"}]}, "ideas[0].timeframe: must be one of ['15m', '1h', '4h'], got '5m'"),
    ({"ideas": [{**IDEA, "columns": "close"}]}, "ideas[0].columns: must be an array of non-empty strings"),
    ({"ideas": [{**IDEA, "extra": 1}]}, "ideas[0].extra: unknown field"),
    ({"ideas": [{k: v for k, v in IDEA.items() if k != "builds_on"}]}, "ideas[0].builds_on: required"),
    ({"ideas": ["card"]}, "ideas[0]: must be an object"),
    ({"ideas": IDEA}, "ideas: must be an array of objects"),
    ({"stop": ...}, "stop: required"),
    ({"extra": 1}, "extra: unknown field"),
])
def test_parse_research_refuses_each_limit_with_a_clear_error(over, error):
    with pytest.raises(ContractError) as e:
        parse_research(note(**over), TFS, MAX)
    assert e.value.errors == [error]


def test_parse_research_takes_its_most_ideas_from_the_caller():
    assert len(parse_research(note(ideas=[IDEA] * 4), TFS, 4).ideas) == 4
    with pytest.raises(ContractError, match="ideas: 2 items, at most 1"):
        parse_research(note(ideas=[IDEA] * 2), TFS, 1)


def test_parse_research_lists_every_problem_and_refuses_a_non_object():
    with pytest.raises(ContractError) as e:
        parse_research({"ideas": [{**IDEA, "timeframe": "5m"}, {**IDEA, "signal": ""}], "stop": ["a\nb"]},
                       TFS, MAX)
    assert e.value.errors == ["ideas[0].timeframe: must be one of ['15m', '1h', '4h'], got '5m'",
                              "ideas[1].signal: must be a non-empty string", "stop[0]: must be one line",
                              "summary: required"]
    with pytest.raises(ContractError):
        parse_research([], TFS, MAX)


# Known, accepted disagreement, left out of the table: a trailing newline on a one-line field ("a\n"). Python's
# re, which jsonschema uses, lets `$` match before it, so the schema passes it; JSON Schema's own regex (ECMA 262)
# doesn't, nor does parse. The proposal's hypothesis has the same pattern.
RESEARCH_AGREEMENT = [
    note(),
    note(ideas=[], stop=[]),
    note(ideas=[IDEA] * 3),
    note(ideas=[IDEA] * 4),
    note(ideas=IDEA),
    note(ideas=["x"]),
    note(ideas=...),
    note(stop=["s"] * 5),
    note(stop=["s"] * 6),
    note(stop=["x" * 200]),
    note(stop=["x" * 201]),
    note(stop=["a\nb"]),
    note(stop=[""]),
    note(stop="one"),
    note(stop=[1]),
    note(summary="x" * 2000),
    note(summary="x" * 2001),
    note(summary="multi\nline"),
    note(summary=""),
    note(summary=...),
    note(extra=1),
    note(ideas=[{**IDEA, "extra": 1}]),
    note(ideas=[{**IDEA, "timeframe": "5m"}]),
    note(ideas=[{**IDEA, "builds_on": "x" * 300}]),
    note(ideas=[{**IDEA, "builds_on": "x" * 301}]),
    note(ideas=[{**IDEA, "builds_on": "a\nb"}]),
    note(ideas=[{**IDEA, "builds_on": ""}]),
    note(ideas=[{k: v for k, v in IDEA.items() if k != "timeframe"}]),
    note(ideas=[{**IDEA, "trades_per_coin_per_year": True}]),
    note(ideas=[{**IDEA, "columns": [""]}]),
    note(ideas=[IDEA, {**IDEA, "title": ""}]),
]


@pytest.mark.parametrize("obj", RESEARCH_AGREEMENT)
def test_parse_research_accepts_exactly_what_the_research_schema_accepts(obj):
    jsonschema = pytest.importorskip("jsonschema")
    try:
        jsonschema.validate(obj, research_schema(TFS, MAX))
        by_schema = True
    except jsonschema.ValidationError:
        by_schema = False
    try:
        parse_research(obj, TFS, MAX)
        by_parse = True
    except ContractError:
        by_parse = False
    assert by_parse == by_schema, obj


def test_the_proposal_contract_is_unchanged_by_the_research_one():
    """describe() still states only the proposal's fields: no research doc leaks into the proposal prompt."""
    text = describe(TFS)
    for key, doc in FIELD_DOCS.items():
        if key.startswith("Research"):
            assert doc not in text, key
    assert "at most" not in text.replace(FIELD_DOCS["Proposal.hypothesis"], "").replace(
        FIELD_DOCS["Proposal.source"], "")

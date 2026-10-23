"""The proposal contract: one set of dataclasses yields the schema, the prompt text and the validation."""

from __future__ import annotations

import pytest

from perp_lab.proposer.contract import (
    FIELD_DOCS, ContractError, IdeaCard, Ports, Proposal, Refines, describe, parse, schema,
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


def test_schema_and_description_come_from_the_same_fields():
    text = describe(TFS)
    names = set(_names(schema(TFS)))
    assert {"kind", "idea", "title", "columns", "refines", "change", "ports", "why", "idea_id"} <= names
    for n in names:
        assert n in text
    for key in FIELD_DOCS:
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
    then = [a["then"]["required"] for a in schema(TFS)["allOf"] if a["if"]["properties"]["kind"]["const"] == kind]
    assert then == [[block]]


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

"""The human's own idea cards: the model's card checks, a new title, a proposer timeframe, and a seeded record."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from perp_lab.proposer import context, contract, human, piles
from perp_lab.proposer.record import Record

PC = SimpleNamespace(timeframes=("15m", "1h", "4h"))     # proposer.timeframes


def entry(**over):
    e = {"title": "Basis squeeze", "mechanism": "m", "why_it_persists": "w", "signal": "s",
         "trades_per_coin_per_year": 12, "likely_failure": "f", "columns": ["close", "funding_last"],
         "timeframe": "1h"}
    return {**e, **over}


@pytest.fixture
def rec(tmp_path):
    r = Record(tmp_path / "state")
    r.add_idea("Seeded", {}, "seed", "4h")
    return r


def test_a_valid_card_is_added_as_an_open_human_idea(rec):
    assert human.check(rec, PC, entry()) == []
    i = human.add(rec, PC, entry(title="  Basis squeeze  "))
    x = rec.idea(i)
    assert (x["title"], x["origin"], x["status"], x["timeframe"], x["next_up"]) == \
        ("Basis squeeze", "human", "open", "1h", None)
    assert x["card"] == {"title": "Basis squeeze", "mechanism": "m", "why_it_persists": "w", "signal": "s",
                         "trades_per_coin_per_year": 12.0, "likely_failure": "f", "columns": ["close", "funding_last"]}


def test_a_bad_card_gets_the_contracts_own_errors(rec):
    bad = entry(mechanism="", trades_per_coin_per_year=float("nan"), extra="x")
    del bad["signal"]
    with pytest.raises(contract.ContractError) as want:
        contract.parse_card({k: v for k, v in bad.items() if k != "timeframe"})
    assert human.check(rec, PC, bad) == want.value.errors
    with pytest.raises(human.Refused) as refused:
        human.add(rec, PC, bad)
    assert refused.value.errors == want.value.errors and len(rec.ideas()) == 1


def test_a_blank_field_is_an_empty_one(rec):
    assert human.check(rec, PC, entry(mechanism="  \n ")) == ["mechanism: must be a non-empty string"]


@pytest.mark.parametrize("title", ["Seeded", "seeded", "  SEEDED "])
def test_a_title_the_record_has_is_refused_whatever_the_case(rec, title):
    assert human.check(rec, PC, entry(title=title)) == [
        f"title: an idea titled {title.strip()!r} is already in the record"]


@pytest.mark.parametrize("tf", ["5m", "2h", None, 1])
def test_a_timeframe_outside_proposer_timeframes_is_refused(rec, tf):
    assert human.check(rec, PC, entry(timeframe=tf)) == [f"timeframe: must be one of ['15m', '1h', '4h'], got {tf!r}"]


def test_a_missing_timeframe_is_refused(rec):
    e = entry()
    del e["timeframe"]
    assert human.check(rec, PC, e) == ["timeframe: required"]


def test_a_column_no_plugin_may_read_is_refused(rec):
    (err,) = human.check(rec, PC, entry(columns=["close", "slippage", "atr"]))
    assert err.startswith("columns: 'slippage', 'atr' are not among the bar columns a plugin may read: ")
    assert err.endswith(", ".join(context.PLUGIN_COLUMNS))
    assert human.check(rec, PC, entry(columns=["slippage"]))[0].startswith("columns: 'slippage' is not among ")


def test_every_error_is_reported_at_once(rec):
    assert human.check(rec, PC, entry(title="seeded", timeframe="5m", likely_failure="")) == [
        "likely_failure: must be a non-empty string", "title: an idea titled 'seeded' is already in the record",
        "timeframe: must be one of ['15m', '1h', '4h'], got '5m'"]


@pytest.mark.parametrize("raw", [None, ["title"], "Basis squeeze"])
def test_an_entry_that_is_no_mapping_is_refused(rec, raw):
    assert human.check(rec, PC, raw) == ["an idea is a mapping of the card's fields and its timeframe"]


def test_an_unseeded_record_takes_no_human_idea(tmp_path):
    """seed() is a no-op once the record holds any idea, so a human idea in an empty record would block the seed."""
    r = Record(tmp_path / "state")
    assert not r.seeded() and human.check(r, PC, entry()) == [human.NOT_SEEDED]
    r.add_idea("Mine", {}, "proposer", "1h")
    assert not r.seeded() and human.check(r, PC, entry()) == [human.NOT_SEEDED]
    with pytest.raises(human.Refused) as refused:
        human.add(r, PC, entry())
    assert refused.value.errors == [human.NOT_SEEDED] and len(r.ideas()) == 1
    assert "`lab propose --trials 0`" in human.NOT_SEEDED
    r.add_idea("Seeded", {}, "seed", "4h")
    assert r.seeded() and human.check(r, PC, entry()) == []


def test_a_human_idea_is_never_filed_as_a_duplicate():
    """The duplicate status is for the model's own new ideas; a human idea, like a seeded one, keeps its own."""
    dup = {"judge_exit": 0, "verdict": {"decision": "kill"}, "duplicate_of": 3}
    assert piles.idea_status({"origin": "proposer"}, [dup], None, 4) == "duplicate"
    assert piles.idea_status({"origin": "human"}, [dup], None, 4) == "open"

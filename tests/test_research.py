"""A research note's ideas meet the human cards' rules (human.check), and their titles differ within the note."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from perp_lab.proposer import context, human, research
from perp_lab.proposer.contract import ResearchIdea, ResearchNote, parse_research
from perp_lab.proposer.record import Record

PC = SimpleNamespace(timeframes=("15m", "1h", "4h"), proposals_per_idea=4)
CARD = {"title": "Basis squeeze", "mechanism": "m", "why_it_persists": "w", "signal": "s",
        "trades_per_coin_per_year": 12, "likely_failure": "f", "columns": ["close", "funding_last"]}


def idea(**over) -> ResearchIdea:
    return parse_research({"ideas": [{**CARD, "timeframe": "1h", "builds_on": "b", **over}], "stop": [],
                           "summary": "s"}, ("5m", *PC.timeframes), 1).ideas[0]


def note(*ideas: ResearchIdea) -> ResearchNote:
    return ResearchNote(ideas=ideas, stop=("x",), summary="s")


@pytest.fixture
def rec(tmp_path):
    r = Record(tmp_path / "state")
    r.add_idea("Seeded", {}, "seed", "4h")
    return r


def test_a_good_note_passes(rec):
    assert research.check_note(note(idea(), idea(title="Carry unwind", timeframe="4h")), rec, PC) == []
    assert research.check_note(note(), rec, PC) == []


def test_an_idea_is_a_human_card_with_its_timeframe():
    """builds_on is for the human: it is no card field, so human.check would refuse it as unknown."""
    assert research.entry(idea(timeframe="4h")) == {**CARD, "trades_per_coin_per_year": 12.0, "timeframe": "4h"}


def test_a_bad_card_gets_the_human_checks_errors(rec):
    bad = idea(title="Wide", columns=["close", "slippage"], mechanism=" \n")
    want = human.check(rec, PC, research.entry(bad))
    assert len(want) == 2
    assert research.check_note(note(idea(), bad), rec, PC) == [f"idea #2 (Wide): {e}" for e in want]
    assert want[0] == "mechanism: must be a non-empty string"
    assert want[1].startswith("columns: 'slippage' is not among the bar columns a plugin may read: ")
    assert want[1].endswith(", ".join(context.PLUGIN_COLUMNS))


@pytest.mark.parametrize("title,label", [("Seeded", "Seeded"), (" SEEDED ", "SEEDED")])
def test_a_title_the_record_has_is_refused(rec, title, label):
    assert research.check_note(note(idea(title=title)), rec, PC) == [
        f"idea #1 ({label}): title: an idea titled {label!r} is already in the record"]


def test_titles_differ_within_the_note(rec):
    """The note's ideas are added all or none, in one transaction: two of the same title would fail it whole."""
    errors = research.check_note(note(idea(title="One"), idea(title="Two"), idea(title=" one ")), rec, PC)
    assert errors == ["idea #3 (one): title: the same as idea #1's"]


def test_a_timeframe_outside_the_proposers_is_refused(rec):
    assert research.check_note(note(idea(timeframe="5m")), rec, PC) == [
        "idea #1 (Basis squeeze): timeframe: must be one of ['15m', '1h', '4h'], got '5m'"]


def test_a_multi_line_title_is_refused_and_labelled_on_one_line(rec):
    assert research.check_note(note(idea(title="Basis\nsqueeze")), rec, PC) == [
        "idea #1 (Basis squeeze): title: must be one line"]


def test_every_idea_is_checked_and_every_error_reported(rec):
    errors = research.check_note(note(idea(title="Seeded", timeframe="5m"), idea(likely_failure=" "),
                                      idea()), rec, PC)
    assert errors == [
        "idea #1 (Seeded): title: an idea titled 'Seeded' is already in the record",
        "idea #1 (Seeded): timeframe: must be one of ['15m', '1h', '4h'], got '5m'",
        "idea #2 (Basis squeeze): likely_failure: must be a non-empty string",
        "idea #3 (Basis squeeze): title: the same as idea #2's"]


def test_an_unseeded_record_takes_no_research_idea(tmp_path):
    assert research.check_note(note(idea()), Record(tmp_path / "state"), PC) == [
        f"idea #1 (Basis squeeze): {human.NOT_SEEDED}"]

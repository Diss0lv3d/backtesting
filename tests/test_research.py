"""A research note's ideas meet the human cards' rules (human.check), and their titles differ within the note."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from perp_lab.proposer import context, human, research
from perp_lab.proposer.contract import RefereeReport, ResearchIdea, ResearchNote, Verdict, parse_research
from perp_lab.proposer.record import Record

DOMAINS = ("arxiv.org", "www.nber.org")
PC = SimpleNamespace(timeframes=("15m", "1h", "4h"), proposals_per_idea=4, referee_domains=DOMAINS)
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


def test_an_idea_is_kept_as_the_human_path_keeps_the_same_card(rec):
    """research.row and human.add must not drift: the record holds one card shape, whoever wrote the card."""
    i = idea(title=" Basis squeeze ", mechanism=" m ", trades_per_coin_per_year=12)
    kept = rec.idea(human.add(rec, PC, research.entry(i)))
    assert research.row(i) == {"title": kept["title"], "card": kept["card"], "origin": "research",
                               "timeframe": kept["timeframe"]}
    assert kept["title"] == "Basis squeeze" and kept["card"]["mechanism"] == "m"
    assert isinstance(research.row(i)["card"]["trades_per_coin_per_year"], float) and "builds_on" not in kept["card"]


# -- the referee's report on a note's cards (referee-and-human-stops spec)

def verdict(title="Basis squeeze", decision="accept", revised=None, sources=(), reason="r") -> Verdict:
    return Verdict(title=title, decision=decision, reason=reason, sources=tuple(sources), revised=revised)


def report(*verdicts: Verdict) -> RefereeReport:
    return RefereeReport(verdicts=verdicts)


A, B, C = idea(title="Basis squeeze"), idea(title="Carry unwind", timeframe="4h"), idea(title="Round numbers")
ABC = note(A, B, C)


def check(rep, n=ABC, web=True, rec=None):
    return research.check_report(rep, n, rec, PC, web=web)


def test_a_report_that_matches_the_note_passes_and_adds_the_accepted_and_revised_cards(rec):
    fixed = idea(title="Carry unwind", timeframe="1h", signal="sharper")
    rep = report(verdict("Basis squeeze", sources=["https://arxiv.org/abs/2101.00001"]),
                 verdict("Carry unwind", "revise", fixed, ["https://www.nber.org/papers/w1"]),
                 verdict("Round numbers", "reject"))
    assert research.check_report(rep, ABC, rec, PC, web=True) == []
    assert research.referee_result(rep, ABC) == [A, fixed]
    assert research.referee_result(rep, ABC)[0] is A                # accepted as the research step wrote it


def test_every_card_rejected_adds_nothing_and_no_card_needs_no_verdict(rec):
    rep = report(*(verdict(i.title, "reject") for i in ABC.ideas))
    assert research.check_report(rep, ABC, rec, PC, web=True) == [] and research.referee_result(rep, ABC) == []
    assert research.check_report(report(), note(), rec, PC, web=True) == []


@pytest.mark.parametrize("verdicts,error", [
    ((verdict("Basis squeeze"), verdict("Carry unwind")),
     "verdicts: 2 for 3 cards; one verdict per card, in the cards' order"),
    ((verdict("Basis squeeze"), verdict("Carry unwind"), verdict("Round numbers"), verdict("Round numbers")),
     "verdicts: 4 for 3 cards; one verdict per card, in the cards' order"),
])
def test_one_verdict_per_card(rec, verdicts, error):
    assert error in check(report(*verdicts), rec=rec)


def test_titles_match_the_cards_in_order(rec):
    errors = check(report(verdict("Carry unwind"), verdict("Basis squeeze"), verdict("Round numbers")), rec=rec)
    assert errors == [
        "verdict #1 (Carry unwind): title: card #1 is 'Basis squeeze'; one verdict per card, in the cards' order",
        "verdict #2 (Basis squeeze): title: card #2 is 'Carry unwind'; one verdict per card, in the cards' order"]
    assert check(report(verdict(" Basis squeeze "), verdict("Carry unwind"), verdict("Round numbers")),
                 rec=rec) == []                                       # surrounding blanks don't count


def test_an_unknown_title_is_refused(rec):
    assert check(report(verdict("Basis squeeze"), verdict("Funding fade"), verdict("Round numbers")), rec=rec) == [
        "verdict #2 (Funding fade): title: card #2 is 'Carry unwind'; one verdict per card, in the cards' order"]


def test_revised_is_present_exactly_when_the_decision_is_revise(rec):
    card = idea(title="Basis squeeze", timeframe="4h")
    errors = check(report(verdict("Basis squeeze", "revise"), verdict("Carry unwind", "accept", card),
                          verdict("Round numbers", "reject", card)), rec=rec)
    assert errors == ["verdict #1 (Basis squeeze): revised: required when decision is revise",
                      "verdict #2 (Carry unwind): revised: must be null when decision is accept",
                      "verdict #3 (Round numbers): revised: must be null when decision is reject"]


def test_a_revised_card_meets_the_human_checks(rec):
    bad = idea(title="Basis squeeze", columns=["close", "slippage"], mechanism=" \n")
    want = human.check(rec, PC, research.entry(bad))
    assert len(want) == 2
    errors = check(report(verdict("Basis squeeze", "revise", bad), verdict("Carry unwind"),
                          verdict("Round numbers")), rec=rec)
    assert errors == [f"verdict #1 (Basis squeeze): revised: {e}" for e in want]
    assert check(report(verdict("Basis squeeze", "revise", idea(timeframe="5m")), verdict("Carry unwind"),
                        verdict("Round numbers")), rec=rec) == [
        "verdict #1 (Basis squeeze): revised: timeframe: must be one of ['15m', '1h', '4h'], got '5m'"]


def test_a_revised_card_keeps_its_title_or_takes_an_unused_one(rec):
    def revise_b(title):
        return check(report(verdict("Basis squeeze"), verdict("Carry unwind", "revise", idea(title=title)),
                            verdict("Round numbers")), rec=rec)

    assert revise_b("Carry unwind") == [] and revise_b(" CARRY unwind ") == [] and revise_b("Carry decay") == []
    assert revise_b("Seeded") == [
        "verdict #2 (Carry unwind): revised: title: an idea titled 'Seeded' is already in the record"]
    assert revise_b("round numbers") == [
        "verdict #2 (Carry unwind): revised: title: card #3's; a revised card keeps its own title or takes a new one"]


def test_the_cards_to_add_have_distinct_titles(rec):
    errors = check(report(verdict("Basis squeeze", "revise", idea(title="Carry decay")),
                          verdict("Carry unwind", "revise", idea(title=" carry DECAY")),
                          verdict("Round numbers")), rec=rec)
    assert errors == ["verdict #2 (Carry unwind): revised: title: the same as verdict #1's revised card"]
    assert check(report(verdict("Basis squeeze", "revise", idea(title="Carry unwind")),
                        verdict("Carry unwind", "revise", idea(title="Carry unwind")),
                        verdict("Round numbers")), rec=rec) == [   # its own card's title stays its own
        "verdict #1 (Basis squeeze): revised: title: card #2's; a revised card keeps its own title or takes a new one"]


def test_a_source_must_be_on_the_allowlist(rec):
    ok = ["https://arxiv.org/abs/2101.00001", "https://export.arxiv.org/api/query?id_list=1",
          "http://nber.org/papers/w1", "https://WWW.NBER.ORG/papers/w2"]
    assert check(report(verdict("Basis squeeze", sources=ok), verdict("Carry unwind"), verdict("Round numbers")),
                 rec=rec) == []
    bad = ["https://www.coingecko.com/en/coins/bitcoin", "arxiv.org/abs/1", "https://arxiv.org.evil.com/x"]
    errors = check(report(verdict("Basis squeeze", sources=[ok[0], *bad]), verdict("Carry unwind"),
                          verdict("Round numbers")), rec=rec)
    assert errors == [f"verdict #1 (Basis squeeze): sources[{n}]: {u!r} is not on the allowed academic hosts "
                      f"(arxiv.org, www.nber.org)" for n, u in enumerate(bad, 1)]


def test_a_referee_without_the_web_names_no_source(rec):
    """No fetch could have backed a source: a tool-less referee's sources would be from memory, not checked."""
    rep = report(verdict("Basis squeeze", sources=["https://arxiv.org/abs/2101.00001"]), verdict("Carry unwind"),
                 verdict("Round numbers"))
    assert check(rep, web=False, rec=rec) == [
        "verdict #1 (Basis squeeze): sources: must be empty: this referee had no web access"]
    assert check(report(*(verdict(i.title) for i in ABC.ideas)), web=False, rec=rec) == []


def test_every_problem_is_reported_at_once(rec):
    errors = check(report(verdict("Carry unwind", "revise", sources=["https://example.com/"]),
                          verdict("Carry unwind", "reject", idea(title="Seeded"))), rec=rec)
    assert errors == [
        "verdicts: 2 for 3 cards; one verdict per card, in the cards' order",
        "verdict #1 (Carry unwind): title: card #1 is 'Basis squeeze'; one verdict per card, in the cards' order",
        "verdict #1 (Carry unwind): revised: required when decision is revise",
        "verdict #1 (Carry unwind): sources[0]: 'https://example.com/' is not on the allowed academic hosts "
        "(arxiv.org, www.nber.org)",
        "verdict #2 (Carry unwind): revised: must be null when decision is reject"]


def test_a_multi_line_verdict_title_is_labelled_on_one_line_and_matches_no_card(rec):
    assert check(report(verdict("Basis\nsqueeze"), verdict("Carry unwind"), verdict("Round numbers")),
                 rec=rec) == [
        "verdict #1 (Basis squeeze): title: card #1 is 'Basis squeeze'; one verdict per card, in the cards' order"]


def test_referee_result_refuses_a_report_that_does_not_pair_with_the_note():
    with pytest.raises(ValueError):
        research.referee_result(report(verdict("Basis squeeze")), ABC)

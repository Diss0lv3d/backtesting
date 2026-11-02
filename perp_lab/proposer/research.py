"""A research round's ideas enter the record by the rules a human card meets.

The contract (contract.parse_research) checks the reply's shape. Each idea must then pass human.check, as a human
card with its timeframe: the card checks, a title no idea in the record has, a proposer timeframe, columns a plugin
may read, a one-line title. The note's ideas are added all or none, in one transaction, so their titles must differ
from each other's too, and one bad idea fails the whole reply (a fix-up): the record never takes half a note.

The referee's report (contract.parse_referee) is checked against the note it reviewed: one verdict per card, in the
cards' order; a corrected card exactly when the decision is revise, and that card meets the same human.check, under
its card's title or one no other card has; the cards to add differ in title; and every source is on the allowlist
when the referee had the web (model.on_allowlist, the rule the call's fetch audit uses), none when it hadn't, since
no fetch could have backed it. Any error fails the whole report (a fix-up): a verdict is never half-applied.
"""

from __future__ import annotations

from dataclasses import asdict

from perp_lab.proposer import human, model
from perp_lab.proposer.contract import RefereeReport, ResearchIdea, ResearchNote
from perp_lab.proposer.record import Record

# An idea's web_reviewed mark, for the human about to spend a holdout read on it: the Ideas page's review packet and
# `lab exam`'s awaiting-review line say it in these words.
WEB_REVIEWED_CAVEAT = ("A web-informed referee reviewed this card: academic papers published after the model's "
                       "training cutoff may discuss the holdout period, so treat its exam as weaker evidence.")


def entry(idea: ResearchIdea) -> dict:
    """The idea as a human card entry: the card's fields and its timeframe. builds_on is for the human, no card
    field."""
    e = {k: v for k, v in asdict(idea).items() if k != "builds_on"}
    return {**e, "columns": list(idea.columns)}


def row(idea: ResearchIdea) -> dict:
    """The idea as Record.add_ideas takes it: the card in the shape human.add keeps a human card (its fields
    stripped, trades a float, columns a list), origin research. builds_on stays in the research row's reply."""
    e = entry(idea)
    tf = e.pop("timeframe")
    card = {k: v.strip() if isinstance(v, str) else v for k, v in e.items()}
    card["trades_per_coin_per_year"] = float(card["trades_per_coin_per_year"])
    return {"title": card["title"], "card": card, "origin": "research", "timeframe": tf}


def check_note(note: ResearchNote, record: Record, pc) -> list[str]:
    """Every reason the note's ideas can't all be added, each prefixed `idea #k (title): `; [] when they can."""
    errors, seen = [], {}
    for k, idea in enumerate(note.ideas, 1):
        errs = human.check(record, pc, entry(idea))
        key = idea.title.strip().casefold()
        if key in seen:
            errs.append(f"title: the same as idea #{seen[key]}'s")
        seen.setdefault(key, k)
        errors += [f"idea #{k} ({' '.join(idea.title.split())}): {e}" for e in errs]
    return errors


def _key(title: str) -> str:
    return title.strip().casefold()


def check_report(report: RefereeReport, note: ResearchNote, record: Record, pc, *, web: bool) -> list[str]:
    """Every reason the report can't be applied to the note, the count's first, then each prefixed
    `verdict #k (title): `; [] when it can. `web`: whether the referee could fetch (pc.referee_domains)."""
    cards = note.ideas
    errors = []
    if len(report.verdicts) != len(cards):
        errors.append(f"verdicts: {len(report.verdicts)} for {len(cards)} cards; one verdict per card, in the cards' "
                      "order")
    titles = {_key(c.title): j for j, c in enumerate(cards, 1)}
    # The cards to add must differ in title. The note's do (check_note), and a revised card may take only its own
    # card's title or one no card has, so only two new titles can clash.
    new = {}
    for k, v in enumerate(report.verdicts, 1):
        errs = []
        card = cards[k - 1] if k <= len(cards) else None
        if card is not None and v.title.strip() != card.title.strip():
            errs.append(f"title: card #{k} is {' '.join(card.title.split())!r}; one verdict per card, in the cards' "
                        "order")
        if v.decision == "revise" and v.revised is None:
            errs.append("revised: required when decision is revise")
        elif v.decision != "revise" and v.revised is not None:
            errs.append(f"revised: must be null when decision is {v.decision}")
        elif v.revised is not None:
            errs += [f"revised: {e}" for e in human.check(record, pc, entry(v.revised))]
            key = _key(v.revised.title)
            j = titles.get(key)
            if j is not None and j != k:
                errs.append(f"revised: title: card #{j}'s; a revised card keeps its own title or takes a new one")
            elif j is None and key in new:
                errs.append(f"revised: title: the same as verdict #{new[key]}'s revised card")
            if j is None:
                new.setdefault(key, k)
        if not web and v.sources:
            errs.append("sources: must be empty: this referee had no web access")
        elif web:
            errs += [f"sources[{n}]: {u!r} is not on the allowed academic hosts ({', '.join(pc.referee_domains)})"
                     for n, u in enumerate(v.sources) if not model.on_allowlist(u, pc.referee_domains)]
        errors += [f"verdict #{k} ({' '.join(v.title.split())}): {e}" for e in errs]
    return errors


def referee_result(report: RefereeReport, note: ResearchNote) -> list[ResearchIdea]:
    """The cards to add, in the note's order: an accepted card as the research step wrote it, a revised one as the
    referee corrected it, a rejected one not at all. For a report check_report passed; ValueError if it doesn't
    pair with the note."""
    out = []
    for v, card in zip(report.verdicts, note.ideas, strict=True):
        if v.decision == "accept":
            out.append(card)
        elif v.decision == "revise":
            out.append(v.revised)
    return out

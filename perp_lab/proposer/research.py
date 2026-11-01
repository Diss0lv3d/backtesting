"""A research round's ideas enter the record by the rules a human card meets.

The contract (contract.parse_research) checks the reply's shape. Each idea must then pass human.check, as a human
card with its timeframe: the card checks, a title no idea in the record has, a proposer timeframe, columns a plugin
may read, a one-line title. The note's ideas are added all or none, in one transaction, so their titles must differ
from each other's too, and one bad idea fails the whole reply (a fix-up): the record never takes half a note.
"""

from __future__ import annotations

from dataclasses import asdict

from perp_lab.proposer import human
from perp_lab.proposer.contract import ResearchIdea, ResearchNote
from perp_lab.proposer.record import Record


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

"""The human's own idea cards, added from the Ideas page or `lab idea add` beside the seed's and the model's.

A human card meets the rules a model's new idea meets, in code: the contract's card checks (contract.parse_card), a
title no idea in the record has, whatever the case, and a timeframe a plugin may trade. Its columns must be ones the
bar table lets a plugin read, the list the page's form offers. The model reads the card as it reads any open
idea's, so the card must carry no result from real data: WARNING says so wherever the human writes one.

A human idea waits for the seed: seed() adds nothing to a record that holds any idea, so one added first would keep
the seed out for good.

The human's "try next" goes only to an open idea some proposal could work on now (assemble.answerable).
"""

from __future__ import annotations

from dataclasses import asdict

from perp_lab.proposer.assemble import answerable
from perp_lab.proposer.context import PLUGIN_COLUMNS
from perp_lab.proposer.contract import ContractError, parse_card
from perp_lab.proposer.record import PINNABLE, Record

WARNING = ("The builder reads this text. Describe the mechanism and the rule only, with no results from real data "
           "(Sharpe ratios, returns, backtest figures, dates of runs): the model must not see real-data statistics "
           "outside its own verdicts.")
NOT_SEEDED = ("The record is not seeded yet, and an idea added now would keep the seed out for good. Run "
              "`lab propose --trials 0` first.")


class Refused(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


def _parse(record: Record, pc, entry) -> tuple[dict | None, str | None, list[str]]:
    """(the card as the record keeps it, the timeframe, []) or (None, None, every error)."""
    if not isinstance(entry, dict):
        return None, None, ["an idea is a mapping of the card's fields and its timeframe"]
    if not record.seeded():
        return None, None, [NOT_SEEDED]
    raw = {k: v.strip() if isinstance(v, str) else v for k, v in entry.items() if k != "timeframe"}
    errors: list[str] = []
    try:
        card = parse_card(raw)
    except ContractError as e:
        card, errors = None, list(e.errors)
    title = raw.get("title")
    if isinstance(title, str) and len(title.splitlines()) > 1:      # the prompt's heading and closed entry are one
        errors.append("title: must be one line")                     # line each (prompt.render_record)
    cols = raw.get("columns")
    bad = [c for c in cols if isinstance(c, str) and c and c not in PLUGIN_COLUMNS] \
        if isinstance(cols, (list, tuple)) else []
    if bad:
        errors.append(f"columns: {', '.join(map(repr, bad))} {'is' if len(bad) == 1 else 'are'} not among the bar "
                      f"columns a plugin may read: {', '.join(PLUGIN_COLUMNS)}")
    if isinstance(title, str) and title.casefold() in {i["title"].strip().casefold() for i in record.ideas()}:
        errors.append(f"title: an idea titled {title!r} is already in the record")
    tf = entry.get("timeframe")
    if "timeframe" not in entry:
        errors.append("timeframe: required")
    elif tf not in pc.timeframes:
        errors.append(f"timeframe: must be one of {list(pc.timeframes)}, got {tf!r}")
    if errors or card is None:
        return None, None, errors
    return {**asdict(card), "trades_per_coin_per_year": float(card.trades_per_coin_per_year),
            "columns": list(card.columns)}, tf, []


def check(record: Record, pc, entry) -> list[str]:
    """Every reason the entry (the card's fields and `timeframe`) can't be added; [] when it can."""
    return _parse(record, pc, entry)[2]


def add(record: Record, pc, entry) -> int:
    """Add the entry as an open idea of origin "human" and return its id, or raise Refused with every error."""
    card, tf, errors = _parse(record, pc, entry)
    if errors:
        raise Refused(errors)
    try:
        return record.add_idea(card["title"], card, "human", tf)
    except ValueError as e:                     # the same title, added since the check
        raise Refused([f"title: {e}"]) from e


def add_all(record: Record, pc, entries: list) -> list[int]:
    """Add every entry, in one transaction, and return their ids; or add none and raise Refused with each failing
    entry's errors, prefixed by its number (from 1) and title. Titles within the list must differ too."""
    if not record.seeded():
        raise Refused([NOT_SEEDED])
    if not entries:
        raise Refused(["the file holds no idea"])
    rows, errors, seen = [], [], {}
    for n, entry in enumerate(entries, 1):
        card, tf, errs = _parse(record, pc, entry)
        title = entry.get("title") if isinstance(entry, dict) else None
        label = f"card #{n} ({' '.join(title.split())})" if isinstance(title, str) else f"card #{n}"
        if card is not None:
            key = card["title"].casefold()
            if key in seen:
                errs = [f"title: the same as card #{seen[key]}'s"]
            seen.setdefault(key, n)
            rows.append({"title": card["title"], "card": card, "origin": "human", "timeframe": tf})
        errors += [f"{label}: {e}" for e in errs]
    if errors:
        raise Refused(errors)
    try:
        return record.add_ideas(rows)
    except ValueError as e:                     # a title added since the check
        raise Refused([f"title: {e}"]) from e


def why_not_next(record: Record, pc, idea: dict) -> str | None:
    """Why the idea can't be tried next, or None: it must be open or exhausted, and some proposal must be able to
    work on it now (an exhausted idea takes no refinement, so a port)."""
    if idea["status"] not in PINNABLE:
        return f"idea #{idea['id']} is {idea['status']}; only an open or exhausted idea can be tried next"
    return answerable(idea, record, pc)


def flag_next(record: Record, pc, id: int, by: str = "human") -> None:
    """Flag the idea to be tried next, or raise ValueError with the reason it can't be. `by`: who asks, the human
    or a research round (whose request never replaces the human's: Record.flag_next)."""
    if not -(2 ** 63) <= id < 2 ** 63:          # past SQLite's integers: no idea has it (and SQLite would overflow)
        raise ValueError(f"there is no idea #{id}")
    try:
        idea = record.idea(id)
    except KeyError:
        raise ValueError(f"there is no idea #{id}") from None
    if why := why_not_next(record, pc, idea):
        raise ValueError(why)
    record.flag_next(id, by=by)                 # checks the status again, in its own write

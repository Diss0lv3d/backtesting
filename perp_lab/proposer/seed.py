"""The seed: the record's starting ideas, so the proposer knows what the human already tried.

The spec's Seed (The record): six ideas from the seven judged human trials, the LCE idea of the two trials
since (ruling R18), the research note's untested candidates B-D, and candidate E, excluded. The ideas and their
cards are data (seed.yaml). What a verdict decides (pile, family, status) is computed here in code (ADR 0006),
by the same functions that file the proposer's own trials.

The seeded trials enter as judged proposals of origin "seed", with judge exit 0 and no judge time: the record
shows them like any judged trial, but they never count toward trials_total or a day's quota.

Most of the human verdicts predate the judge's diagnostics, so their piles stay "unknown" until `lab
verdict-refresh` recomputes them on today's data. check_seed refuses to start until then.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import yaml

from perp_lab.plugins import blob_hash
from perp_lab.proposer import piles, settings
from perp_lab.proposer.contract import IdeaCard
from perp_lab.proposer.record import Record
from perp_lab.proposer.settings import StartRefused

SEED = Path(__file__).with_name("seed.yaml")
# Where the judge reads plugins (perp_lab.strategy.STRATEGY_DIR, which the proposer may not import). The seed
# must hash the bytes the judge hashed, or it looks for the verdicts under the wrong names.
STRATEGIES = Path("strategies")
REFRESH = "lab verdict-refresh {plugin} --tf {timeframe}"


def verdict_path(cfg, plugin: str, tf: str, hash_: str) -> Path:
    return Path(cfg["judge"]["verdicts_dir"]) / f"{plugin}@{tf}@{hash_[:10]}.json"


def read_verdict(path: Path) -> dict | None:
    """The verdict file's contents; None when it is missing, unreadable or not a JSON object."""
    try:
        v = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return v if isinstance(v, dict) else None


def _has_before_costs(verdict: dict | None) -> bool:
    diag = (verdict or {}).get("diagnostics")
    return isinstance(diag, dict) and isinstance(diag.get("before_costs"), dict) and bool(diag["before_costs"])


def _card(raw: dict) -> dict:
    card = IdeaCard(**raw)      # exactly the contract's fields: a missing or unknown one is a TypeError
    return {**asdict(card), "trades_per_coin_per_year": float(card.trades_per_coin_per_year),
            "columns": list(card.columns)}


def load_seed(path: Path = SEED) -> list[dict]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["ideas"]


def _rows(cfg, strategies: Path, per_idea: int) -> tuple[list[dict], list[dict]]:
    """The seed's idea and proposal rows. Ideas are numbered from 1, since they go into a record with none."""
    entries = load_seed()
    ids = {e["card"]["title"]: n for n, e in enumerate(entries, 1)}
    ideas, props = [], []
    for n, e in enumerate(entries, 1):
        tf, plugins = e["timeframe"], e.get("plugins") or []
        mine = []
        for k, plugin in enumerate(plugins):
            path = Path(strategies) / f"{plugin}.py"
            try:
                text = path.read_bytes()
                source = text.decode("utf-8")
            except (OSError, UnicodeDecodeError) as err:
                raise StartRefused(f"the seed records the human trial {plugin} at {tf}, but can't read {path}: "
                                   f"{err}") from err
            h = blob_hash(text)
            verdict = read_verdict(verdict_path(cfg, plugin, tf, h))
            kind = "refinement" if k else "port" if e.get("port_of") else "new"
            mine.append({"seq": len(props) + len(mine) + 1, "idea_id": n, "origin": "seed", "kind": kind,
                         "plugin": plugin, "timeframe": tf, "hash": h, "source": source,
                         "judge_exit": 0, "judged_at": None, "verdict": verdict, "pile": piles.pile(verdict),
                         "state": "judged"})
        props += mine
        family = next((p["verdict"].get("lineage_id") for p in mine if p["verdict"] is not None), None)
        ideas.append({"id": n, "title": e["card"]["title"], "card": _card(e["card"]), "origin": "seed",
                      "timeframe": tf, "port_of": ids[e["port_of"]] if e.get("port_of") else None,
                      "family": family, "root_plugin": plugins[0] if plugins else None,
                      "excluded_reason": e.get("excluded_reason")})
    # Statuses last: a family's spent exam closes every idea in it, the seeded ports included.
    outcomes = piles.family_outcomes([p["verdict"] for p in props if p["verdict"] is not None])
    for idea in ideas:
        idea["status"] = piles.idea_status(idea, [p for p in props if p["idea_id"] == idea["id"]],
                                           outcomes.get(idea["family"]), per_idea)
    return ideas, props


def seed(record: Record, cfg, *, strategies: Path = STRATEGIES) -> None:
    """Seed a record that has no ideas, in one transaction; a record with any idea is left as it is."""
    if record.ideas():
        return
    record.add_seed(*_rows(cfg, strategies, settings.load(cfg).proposals_per_idea))


def check_seed(record: Record, cfg) -> None:
    """Refuse to start while a seeded trial's verdict file is missing or has no diagnostics.before_costs.

    The files are read, not the record: once refreshed, the record catches up at the loop's snapshot.
    """
    seeded = [p for p in record.proposals() if p["origin"] == "seed"]
    stale = [p for p in seeded
             if not _has_before_costs(read_verdict(verdict_path(cfg, p["plugin"], p["timeframe"], p["hash"])))]
    if stale:
        raise StartRefused("\n".join([
            f"Seeded verdicts missing or without diagnostics.before_costs: {len(stale)} of {len(seeded)}, so "
            "their piles are unknown. Recompute them from walk-forward runs on today's data (no trial is added), "
            "then start again:",
            *(REFRESH.format(**p) for p in stale),
        ]))

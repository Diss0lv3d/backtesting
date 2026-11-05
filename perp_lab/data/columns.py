"""Column groups: extra plugin-facing columns attached to every coin's bars (X1-X3 of
docs/superpowers/specs/2026-11-05-cross-coin-reference-design.md).

A group's `rule` is part of the data identity once it is above 1, so a later change to its
alignment, NaN or definition rule moves data_id. `column_group_problems` is the readiness check:
every research month of every source must be complete in its manifest before a run reads it.

store imports this module inside `load_bars` only, never at module level: this module imports
perp_lab.data.reference, which imports store.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable

import pandas as pd

from perp_lab.data import reference


@dataclass(frozen=True)
class ColumnGroup:
    name: str
    columns: tuple[str, ...]
    prefix: str
    rule: int
    synthetic_id: int
    exam_required: str                  # "always" | "if_read"
    helpers: tuple[str, ...]
    paper_ready: bool
    load_source: Callable | None        # (cfg, tf, start, end, holdout) -> source shared by every coin
    attach: Callable                    # (bars, symbol, tf, source) -> DataFrame
    synthetic: Callable                 # (bars, seed) -> {column: float64 array}; rng (seed, synthetic_id)
    sources: Callable                   # (cfg, symbols, tf) -> [(symbol, dataset directory name)]


GROUPS: tuple[ColumnGroup, ...] = (
    ColumnGroup(name="reference", columns=reference.COLUMNS, prefix=reference.PREFIX, rule=1,
                synthetic_id=reference.SYNTHETIC_ID,
                exam_required="always", helpers=("reference_frame",), paper_ready=False,
                load_source=reference.load_source, attach=reference.attach, synthetic=reference.synthetic,
                sources=reference.sources),
)


def register(group: ColumnGroup) -> None:
    """Tests only: production code registers by literal in GROUPS above."""
    global GROUPS
    GROUPS = GROUPS + (group,)


@contextmanager
def registered(*groups: ColumnGroup):
    """Register groups for the duration of a `with` block and restore the registry after."""
    global GROUPS
    saved = GROUPS
    try:
        for g in groups:
            register(g)
        yield
    finally:
        GROUPS = saved


def identity_block() -> dict:
    """`{"columns": {name: rule}}` for groups past rule 1; empty while every group is at 1."""
    cols = {g.name: g.rule for g in GROUPS if g.rule > 1}
    return {"columns": cols} if cols else {}


def _research_months(cfg) -> list[str]:
    first = cfg.history_start.tz_localize(None).to_period("M") if cfg.history_start.tzinfo else cfg.history_start.to_period("M")
    h = cfg.holdout_start
    last = (h.tz_localize(None) if h.tzinfo else h).to_period("M") - 1
    return [str(m) for m in pd.period_range(first, last, freq="M")]


def _is_real(entry) -> bool:
    return bool(entry) and entry.get("source") not in (None, "absent")


def column_group_problems(cfg, symbols: list[str], tf: str) -> list[str]:
    """One string per research month a group's source data is missing or incomplete. Months at
    or after holdout_start are never looked at."""
    from perp_lab.data.vision import Manifest

    months = _research_months(cfg)
    settings = cfg.get("column_groups", {}) or {}
    problems: list[str] = []
    for g in GROUPS:
        gs = settings.get(g.name, {}) or {}
        excluded = set(gs.get("exclusions", []) or [])
        gaps = {(k["symbol"], k["dataset"], k["month"]) for k in gs.get("known_gaps", []) or []}
        for symbol, dataset in g.sources(cfg, symbols, tf):
            if symbol in excluded or symbol.removesuffix(cfg.get("quote", "USDT")) in excluded:
                continue
            data = Manifest(cfg.data_dir / symbol / dataset).data
            seen_real = False
            for m in months:
                if (symbol, dataset, m) in gaps:
                    seen_real = seen_real or _is_real(data.get(m))
                    continue
                e = data.get(m)
                where = f"column group {g.name}: {symbol}/{dataset} {m}"
                if e is None:
                    problems.append(f"{where} has no manifest entry")
                elif not e.get("complete"):
                    problems.append(f"{where} is incomplete")
                elif e.get("source") == "absent":
                    if seen_real:
                        problems.append(f"{where} is absent after the first real month")
                else:
                    seen_real = True
            if not seen_real:
                problems.append(f"column group {g.name}: {symbol}/{dataset} has no real month in the research span")
    return problems

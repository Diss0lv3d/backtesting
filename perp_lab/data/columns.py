"""Column groups: extra plugin-facing columns attached to every coin's bars (X1-X3 of
docs/superpowers/specs/2026-11-05-cross-coin-reference-design.md).

A group's `rule` is part of the data identity once it is above 1, so a later change to its
alignment, NaN or definition rule moves data_id. `column_group_problems` is the readiness check:
every research month of every source must be complete in its manifest before a run reads it.

store imports this module inside `load_bars` only, never at module level: this module imports the
groups' modules, perp_lab.data.reference and perp_lab.data.spot, which import store.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable

import pandas as pd

from perp_lab.data import reference, spot


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
    symbol_check: Callable | None = None    # (cfg, tf) -> [problem]: the source's own data, beyond its manifests


class ReadinessRefused(ValueError):
    """X3 refused a run: a column group's research data is missing or incomplete, so nothing ran and no
    trial counts. `tf` names the timeframe checked (for `lab propose`, its timeframes), `problems` holds
    `column_group_problems`' lines."""

    def __init__(self, tf: str, problems: list[str]):
        self.tf, self.problems = tf, list(problems)
        super().__init__(f"column group data not ready at {tf}; nothing ran:\n" + "\n".join(self.problems))


GROUPS: tuple[ColumnGroup, ...] = (
    ColumnGroup(name="reference", columns=reference.COLUMNS, prefix=reference.PREFIX, rule=1,
                synthetic_id=reference.SYNTHETIC_ID,
                exam_required="always", helpers=("reference_frame",), paper_ready=False,
                load_source=reference.load_source, attach=reference.attach, synthetic=reference.synthetic,
                sources=reference.sources, symbol_check=reference.symbol_check),
    ColumnGroup(name="spot", columns=spot.COLUMNS, prefix=spot.PREFIX, rule=1, synthetic_id=spot.SYNTHETIC_ID,
                exam_required="if_read", helpers=(), paper_ready=False,
                load_source=spot.load_source, attach=spot.attach, synthetic=spot.synthetic, sources=spot.sources,
                symbol_check=spot.symbol_check),
)


def check_source_fits(group: str, source_tf: str, source_end: pd.Timestamp, bars: pd.DataFrame, symbol: str,
                      tf: str) -> None:
    """Refuse (ValueError) a group's shared source that doesn't fit `bars`: one at another timeframe,
    whose rows would join at the wrong open times, and one cut at or before the bars' last close, whose
    columns past its cut would be wrong with no sign of it. Each group's `attach` calls this first."""
    if source_tf != tf:
        raise ValueError(f"a {source_tf} {group} source given to {symbol} {tf} bars")
    if len(bars):
        last_close = int(bars.close_time.max())
        if int(source_end.value // 10**6) <= last_close:
            raise ValueError(f"the {group} source was cut at {source_end}, before {symbol} {tf}'s last bar "
                             f"closed at {pd.Timestamp(last_close, unit='ms', tz='UTC')}")


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
    """One string per research month a group's source data is missing or incomplete, then the
    group's `symbol_check` lines (once per group). Months at or after holdout_start are never
    looked at."""
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
        if g.symbol_check is not None:
            problems += g.symbol_check(cfg, tf)
    return problems


def require_ready(cfg, symbols: list[str], tf: str) -> None:
    """X3 before a run: ReadinessRefused with every problem, or nothing."""
    problems = column_group_problems(cfg, symbols, tf)
    if problems:
        raise ReadinessRefused(tf, problems)

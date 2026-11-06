"""Holdout exam: one run with frozen parameters on the locked window, in the judge's process.

The token is checked by object identity and is invalid in any other process, so the exam never
uses the walk-forward worker pool, and the holdout bars are loaded here. The plugin is streamed
(`perp_lab.sandbox.stream`): per symbol, a fresh sandbox worker is sent the research bars, then one
holdout bar at a time, and the trusted engine runs on the plugin's decisions; symbols go in batches
of at most `client.max_workers`, each batch in lockstep. Indicators warm up on research data (each
run starts at history_start); only returns and trades inside [holdout_start, end) are kept.

`cost_hours` estimates an exam's CPU time per symbol before any read is spent, from research data
only, so a plugin too slow to stream is blocked without one.

After each batch, whatever its outcome, and before the next batch's workers start, the files the
batch's workers mapped (their libraries) are evicted from the page cache: a worker can fault in
chosen pages of them, which a later batch's workers, holding earlier bars, or a later exam could
read back (spec R7). `stream.run_batch` does it, and before a rerun too.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from perp_lab.config import TF_MS
from perp_lab.data import columns
from perp_lab.data.archive import (  # noqa: F401  (the constants stay importable from here)
    CCXT, DAY_MS, DEFAULT_FUNDING_H, EXCHANGE_SOURCES, REGIME_SOURCES, data_end, missing_days, month_problems,
    window_months)
from perp_lab.data.store import load_bars
from perp_lab.engine.backtest import EngineParams
from perp_lab.engine.metrics import daily_returns, portfolio_returns
from perp_lab.holdout import check_token
from perp_lab.plugins import PluginSource, reads_columns
from perp_lab.sandbox import client, stream

COST_PROBES = 3                         # timed `prepare` calls; the estimate uses their median


@dataclass
class ExamResult:
    start: pd.Timestamp
    end: pd.Timestamp
    params: dict
    daily: dict[str, pd.Series]
    portfolio: pd.Series
    trades: pd.DataFrame


def _checked(groups) -> tuple[columns.ColumnGroup, ...]:
    """`groups` as a tuple. ValueError when one's `exam_required` is neither "always" nor "if_read": the
    exam can't tell whether to cover it, so it refuses rather than leave the group out."""
    groups = tuple(groups)
    bad = [f"{g.name} ({g.exam_required!r})" for g in groups if g.exam_required not in ("always", "if_read")]
    if bad:
        raise ValueError(f"column group exam_required must be 'always' or 'if_read': {', '.join(bad)}")
    return groups


def always_groups() -> tuple[columns.ColumnGroup, ...]:
    """The registered `always` groups (the reference): what every exam covers, whatever the plugin reads."""
    return tuple(g for g in _checked(columns.GROUPS) if g.exam_required == "always")


def exam_groups(cfg, src: PluginSource, params: dict) -> tuple[columns.ColumnGroup, ...]:
    """The column groups `src`'s exam covers, in `columns.GROUPS` order: every `always` group (the
    reference), and each `if_read` group `plugins.reads_columns` says it reads at `params` (or can't
    rule out). That may run the plugin in sandbox workers, on the lookahead check's synthetic bars
    only, never on holdout data. Every group's `exam_required` is checked before any run."""
    return tuple(g for g in _checked(columns.GROUPS)
                 if g.exam_required == "always" or reads_columns(cfg, src, g, params))


def _covered(groups) -> tuple[columns.ColumnGroup, ...]:
    """`groups`, or when None the `always` groups, each `exam_required` checked."""
    return always_groups() if groups is None else _checked(groups)


def window_end(cfg, symbols: list[str], tf: str, groups=None) -> pd.Timestamp:
    """The last 00:00 UTC up to which every symbol has closed candles, at `tf` and at the regime
    timeframe (`load_bars` builds `regime` from those: past their last one it would carry a single
    label forward), and funding.

    Each of the exam's column groups (`groups`; None: the `always` ones, the reference) counts too,
    through its `exam_end`: where its data for these coins stops, since every coin's bars carry its
    columns. The reference's is BTC's, an exam coin or not (`reference.exam_end`). A group with no
    `exam_end`, or one that returns None, sets no end.

    Reads timestamps only (open_time, funding_time, interval_hours, create_time), never prices."""
    tfs = tuple(dict.fromkeys((tf, cfg["regime"]["timeframe"])))
    ends = [data_end(cfg, s, tfs) for s in symbols]
    for g in _covered(groups):
        e = g.exam_end(cfg, symbols, tf) if g.exam_end is not None else None
        if e is not None:
            ends.append(e)
    return pd.Timestamp(min(ends), unit="ms", tz="UTC").floor("D")


def group_ends(cfg, symbols: list[str], tf: str, groups) -> list[tuple[str, str, pd.Timestamp]]:
    """(group name, coin, end) for each of `groups` and each exam coin: where that group's data for that coin
    alone stops, from its `exam_end` on the one coin. A group with no `exam_end`, or a coin it sets no end
    for (an excluded coin), is left out. Timestamps only, as `window_end` reads them."""
    out = []
    for g in _covered(groups):
        if g.exam_end is None:
            continue
        for s in symbols:
            e = g.exam_end(cfg, [s], tf)
            if e is not None:
                out.append((g.name, s, pd.Timestamp(e, unit="ms", tz="UTC")))
    return out


def window_data_problems(cfg, symbols: list[str], tf: str, end: pd.Timestamp, groups=None) -> list[str]:
    """Where the exam window's candles didn't come from exchange files, as "SYMBOL tf YYYY-MM: why",
    from the klines manifests (`lab backfill` and `lab tail` write them); no candle is read.

    For every month overlapping [holdout_start, end), each symbol's manifest must name a
    data.binance.vision source (`monthly`, or `daily` for a month rebuilt from daily files). A month
    `lab tail` stitched is `ccxt`: its candles carry no taker, trade-count or quote-volume columns,
    so a plugin that reads them sits flat through the exam and spends its family's one read on a
    data gap. A month the window covers to its end must also be complete (otherwise days of it are
    missing); the month the window ends in may still be open, since `window_end` stops at the last
    candle. A missing or unreadable manifest, a month without an entry or an unknown source counts
    too: what isn't known to be exchange data is refused.

    The exam also reads the regime timeframe's candles (`regime`, built from them by `load_bars`),
    so its months are checked as well, ccxt allowed there: the labels need OHLC only.

    Last, at both timeframes, every UTC day of the window must hold at least one candle (from
    `open_time` alone, as `window_end` reads it; never a price). That catches a day missing from
    the open month, which `complete` can't show, while a short exchange outage, which monthly files
    carry too, doesn't hold an exam back for good.

    Each of the exam's column groups (`groups`; None: the `always` ones, the reference) adds its
    `exam_problems` lines after these, in order; a group with none, or one that returns None, adds
    nothing. The reference's (`reference.exam_problems`) check BTC's data, an exam coin or not, each
    line ending in " (BTC reference)"."""
    start = cfg.holdout_start
    if end <= start:
        return []
    months = window_months(start, end)
    rtf = cfg["regime"]["timeframe"]
    out = []
    for s in symbols:
        out += month_problems(cfg, s, tf, months, end, EXCHANGE_SOURCES, "")
        if rtf != tf:
            out += month_problems(cfg, s, rtf, months, end, REGIME_SOURCES, " (for the regime labels)")
        for t in dict.fromkeys((tf, rtf)):
            out += missing_days(cfg, s, t, start, end)
    for g in _covered(groups):
        if g.exam_problems is not None:
            out += g.exam_problems(cfg, symbols, tf, end) or []
    return out


def cost_hours(cfg, src: PluginSource, tf: str, symbols: list[str], params: dict, end: pd.Timestamp) -> float:
    """Estimated CPU hours of one symbol's streamed exam on [holdout_start, end): the median CPU time
    of COST_PROBES `prepare` calls on the first symbol's full research bars, in a worker of its own,
    times the window's bar count. Each stream step runs one `prepare` over everything held, so this
    is a slight underestimate. The bar count comes from the window's length: no holdout data is read."""
    sym = symbols[0]
    bars = load_bars(cfg, sym, tf)                         # research bars only: no token
    n = max(0, (end - cfg.holdout_start) // pd.Timedelta(milliseconds=TF_MS[tf]))

    def probe() -> list[float]:
        with client.spawn(cfg, src) as w:
            w.bars(sym, tf, bars)
            return [w.prepare(params) for _ in range(COST_PROBES)]
    return float(np.median(client._retry_once(probe))) * n / 3600


def run_exam(cfg, src: PluginSource, tf: str, symbols: list[str], params: dict, token, end: pd.Timestamp,
             stderr_dir: Path | None = None) -> ExamResult:
    """The streamed exam of `src` with frozen `params` on [holdout_start, end). A plugin failure is
    a PluginError; each worker's stderr goes to `stderr_dir` when it closes. After each batch,
    whatever the outcome, the files its workers mapped are evicted from the page cache.

    Each column group's source (BTC's, for the reference) is built once here, with the token, and
    shared by every coin's bars: never the walk-forward's research cache, which is cut at
    holdout_start. Its columns sit on the coin's own rows, so at step t a worker holds BTC's rows up
    to t only, as BTC's own worker does."""
    check_token(token)
    start = cfg.holdout_start
    start_ms = start.value // 10**6
    sources = {g.name: g.load_source(cfg, tf, end=end, holdout=token)
               for g in columns.GROUPS if g.load_source is not None}

    def load(s: str):
        bars = load_bars(cfg, s, tf, end=end, holdout=token, sources=sources)
        k0 = int(np.searchsorted(bars["open_time"].to_numpy(), start_ms))    # the first holdout bar
        return bars, k0, EngineParams.from_config(cfg, s)

    runs = {}
    per = client.max_workers(cfg)
    for b in range(0, len(symbols), per):
        runs.update(stream.run_batch(cfg, src, tf, params, symbols[b:b + per], load, stderr_dir))   # evicts its files
    daily, parts = {}, []
    for s in symbols:
        res = runs[s]
        dr = daily_returns(res.equity, res.time)
        daily[s] = dr[(dr.index >= start) & (dr.index < end)] if len(dr) else dr
        t = pd.DataFrame(res.trades)
        if len(t):
            st = pd.to_datetime(t.signal_time, unit="ms", utc=True)
            parts.append(t[(st >= start) & (st < end)])
    trades = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    return ExamResult(start, end, dict(params), daily, portfolio_returns(daily), trades)

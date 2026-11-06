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

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from perp_lab.config import TF_MS
from perp_lab.data import columns, reference
from perp_lab.data.store import OI_PUBLISH_LAG_MS, load_bars
from perp_lab.engine.backtest import EngineParams
from perp_lab.engine.metrics import daily_returns, portfolio_returns
from perp_lab.holdout import check_token
from perp_lab.plugins import PluginSource
from perp_lab.sandbox import client, stream

DEFAULT_FUNDING_H = 8.0
COST_PROBES = 3                         # timed `prepare` calls; the estimate uses their median


@dataclass
class ExamResult:
    start: pd.Timestamp
    end: pd.Timestamp
    params: dict
    daily: dict[str, pd.Series]
    portfolio: pd.Series
    trades: pd.DataFrame


def _files(d: Path) -> list[Path]:
    files = sorted(d.glob("*.parquet"))
    if not files:
        raise FileNotFoundError(f"no parquet files in {d}; run `lab backfill`")
    return files


def _stamps(cfg, s: str, dataset: str) -> np.ndarray:
    """Every row's time stamp in `s`'s `dataset` files as int64 ms: `open_time` for candles, `create_time`
    for metrics (scaled from seconds where written so, as `store.load_metrics` does). That column only,
    never a price. FileNotFoundError when there are no files."""
    col = "create_time" if dataset == "metrics" else "open_time"
    t = np.concatenate([pd.read_parquet(f, columns=[col])[col].to_numpy("int64")
                        for f in _files(cfg.data_dir / s / dataset)])
    return np.where(t >= 10**11, t, t * 1000) if dataset == "metrics" else t


def _data_end(cfg, s: str, tfs) -> int:
    """Where `s`'s data stops, in ms: its last candle's close at each of `tfs`, and its funding."""
    closes = [int(_stamps(cfg, s, t).max()) + TF_MS[t] for t in tfs]
    fund = (pd.concat([pd.read_parquet(f, columns=["funding_time", "interval_hours"])
                       for f in _files(cfg.data_dir / s / "funding")])
            .sort_values("funding_time").iloc[-1])
    ih = fund.interval_hours if pd.notna(fund.interval_hours) else DEFAULT_FUNDING_H
    return min(*closes, int(fund.funding_time) + int(ih * 3_600_000))


def window_end(cfg, symbols: list[str], tf: str) -> pd.Timestamp:
    """The last 00:00 UTC up to which every symbol has closed candles, at `tf` and at the regime
    timeframe (`load_bars` builds `regime` from those: past their last one it would carry a single
    label forward), and funding.

    The reference coin (`reference.SYMBOL`) counts too, an exam coin or not, since every coin's bars
    carry its `btc_` columns: its candles at `tf`, its funding, and its last metrics snapshot plus the
    publish lag, past which `btc_oi_value` would carry that snapshot forward.

    Reads timestamps only (open_time, funding_time, interval_hours, create_time), never prices."""
    tfs = tuple(dict.fromkeys((tf, cfg["regime"]["timeframe"])))
    ends = [_data_end(cfg, s, tfs) for s in symbols]
    b = reference.SYMBOL
    ends.append(min(_data_end(cfg, b, (tf,)), int(_stamps(cfg, b, "metrics").max()) + OI_PUBLISH_LAG_MS))
    return pd.Timestamp(min(ends), unit="ms", tz="UTC").floor("D")


EXCHANGE_SOURCES = ("monthly", "daily")    # data.binance.vision files: every column, taker flow included
REGIME_SOURCES = (*EXCHANGE_SOURCES, "ccxt")  # the regime labels need OHLC only, which ccxt candles have
CCXT = "stitched from ccxt (no taker, trade-count or quote-volume columns)"


def window_data_problems(cfg, symbols: list[str], tf: str, end: pd.Timestamp) -> list[str]:
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

    The reference coin's data is checked as well, an exam coin or not, each line ending in
    " (BTC reference)": its months at `tf` with the exchange sources (its `btc_` volume and taker
    columns would be NaN through a ccxt month), its days at `tf` with no candle, and its days with no
    metrics snapshot (`btc_oi_value` would carry the last one over the whole day). When it is an exam
    coin its candles were checked above, so only the metrics are."""
    start = cfg.holdout_start
    if end <= start:
        return []
    months = pd.period_range(start.tz_localize(None), (end - pd.Timedelta(milliseconds=1)).tz_localize(None),
                             freq="M")
    rtf = cfg["regime"]["timeframe"]
    out = []
    for s in symbols:
        out += _month_problems(cfg, s, tf, months, end, EXCHANGE_SOURCES, "")
        if rtf != tf:
            out += _month_problems(cfg, s, rtf, months, end, REGIME_SOURCES, " (for the regime labels)")
        for t in dict.fromkeys((tf, rtf)):
            out += _missing_days(cfg, s, t, start, end)
    b, note = reference.SYMBOL, " (BTC reference)"
    if b not in symbols:
        out += _month_problems(cfg, b, tf, months, end, EXCHANGE_SOURCES, note)
        out += _missing_days(cfg, b, tf, start, end, note=note)
    return out + _missing_days(cfg, b, "metrics", start, end, "metrics snapshots", note)


def _month_problems(cfg, s: str, tf: str, months, end: pd.Timestamp, sources: tuple, note: str) -> list[str]:
    """`window_data_problems` for one symbol at one timeframe, each line ending in `note`."""
    p = cfg.data_dir / s / tf / "_manifest.json"
    try:
        man = json.loads(p.read_text())
        if not isinstance(man, dict):
            raise ValueError("not an object")
    except FileNotFoundError:
        return [f"{s} {tf} {m}: no manifest{note}" for m in months]
    except (OSError, ValueError):
        return [f"{s} {tf} {m}: manifest unreadable{note}" for m in months]
    out = []
    for m in months:
        e = man.get(str(m))
        covered = (m + 1).start_time.tz_localize("UTC") <= end     # the window runs to its end
        if not isinstance(e, dict):
            why = "no manifest entry"
        elif e.get("source") not in sources:
            why = CCXT if e.get("source") == "ccxt" else f"unknown source {e.get('source')!r}"
        elif covered and e.get("source") != "ccxt" and e.get("complete") is not True:
            why = "incomplete (days missing from data.binance.vision)"
        else:
            continue
        out.append(f"{s} {tf} {m}: {why}{note}")
    return out


DAY_MS = 86_400_000


def _missing_days(cfg, s: str, dataset: str, start: pd.Timestamp, end: pd.Timestamp, what: str = "candles",
                  note: str = "") -> list[str]:
    """The UTC days in [start, end) without a single row of `s`'s `dataset` (a timeframe's candles, or
    "metrics"), as runs ("SYMBOL tf 2026-09-10 to 2026-09-12: no candles (3 days)"), each line ending in
    `note`; reads the time stamps only (`_stamps`), never a price."""
    lo, hi = start.value // 10**6, end.value // 10**6
    try:
        t = _stamps(cfg, s, dataset)
    except FileNotFoundError:
        t = np.empty(0, dtype="int64")
    t = t[(t >= lo) & (t < hi)]
    missing = np.setdiff1d(np.arange(lo // DAY_MS, -(-hi // DAY_MS)), t // DAY_MS)
    out = []
    for run in (np.split(missing, np.flatnonzero(np.diff(missing) != 1) + 1) if len(missing) else []):
        a, b = (pd.Timestamp(int(d) * DAY_MS, unit="ms").strftime("%Y-%m-%d") for d in (run[0], run[-1]))
        out.append(f"{s} {dataset} {a}: no {what}{note}" if len(run) == 1
                   else f"{s} {dataset} {a} to {b}: no {what} ({len(run)} days){note}")
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

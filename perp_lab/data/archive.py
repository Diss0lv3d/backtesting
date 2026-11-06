"""The exchange archive on disk, as `lab backfill` and `lab tail` lay it out: each dataset's manifest and its rows'
time stamps under data/<SYMBOL>/<dataset>/, read without a price.

The holdout exam (perp_lab.judge.exam) and each column group's exam checks (`ColumnGroup.exam_end` and
`exam_problems`) use these. They live here, not in the judge, so a group's module can use them without importing
the judge. This module imports neither the judge nor the exam.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from perp_lab.config import TF_MS

DEFAULT_FUNDING_H = 8.0
EXCHANGE_SOURCES = ("monthly", "daily")    # data.binance.vision files: every column, taker flow included
REGIME_SOURCES = (*EXCHANGE_SOURCES, "ccxt")  # the regime labels need OHLC only, which ccxt candles have
CCXT = "stitched from ccxt (no taker, trade-count or quote-volume columns)"
DAY_MS = 86_400_000


def files(d: Path) -> list[Path]:
    """The parquet files in `d`, sorted. FileNotFoundError when there are none."""
    found = sorted(d.glob("*.parquet"))
    if not found:
        raise FileNotFoundError(f"no parquet files in {d}; run `lab backfill`")
    return found


def stamps(cfg, symbol: str, dataset: str, traded: bool = False) -> np.ndarray:
    """Every row's time stamp in `symbol`'s `dataset` files as int64 ms: `open_time` for candles, `create_time`
    for metrics (scaled from seconds where written so, as `store.load_metrics` does). With `traded`, the
    candles with `trades` above 0 only (X6: a zero-trade candle, or one with NaN trades, is no traded bar),
    reading that column too. Never a price. FileNotFoundError when there are no files."""
    col = "create_time" if dataset == "metrics" else "open_time"
    parts = []
    for f in files(cfg.data_dir / symbol / dataset):
        df = pd.read_parquet(f, columns=[col, "trades"] if traded else [col])
        t = df[col].to_numpy("int64")
        parts.append(t[df.trades.to_numpy("float64") > 0] if traded else t)
    t = np.concatenate(parts)
    return np.where(t >= 10**11, t, t * 1000) if dataset == "metrics" else t


def data_end(cfg, symbol: str, tfs) -> int:
    """Where `symbol`'s data stops, in ms: its last candle's close at each of `tfs`, and its funding."""
    closes = [int(stamps(cfg, symbol, t).max()) + TF_MS[t] for t in tfs]
    fund = (pd.concat([pd.read_parquet(f, columns=["funding_time", "interval_hours"])
                       for f in files(cfg.data_dir / symbol / "funding")])
            .sort_values("funding_time").iloc[-1])
    ih = fund.interval_hours if pd.notna(fund.interval_hours) else DEFAULT_FUNDING_H
    return min(*closes, int(fund.funding_time) + int(ih * 3_600_000))


def window_months(start: pd.Timestamp, end: pd.Timestamp) -> pd.PeriodIndex:
    """The calendar months overlapping [start, end)."""
    return pd.period_range(start.tz_localize(None), (end - pd.Timedelta(milliseconds=1)).tz_localize(None), freq="M")


def month_problems(cfg, symbol: str, dataset: str, months, end: pd.Timestamp, sources: tuple,
                   note: str) -> list[str]:
    """For each of `months`, why `symbol`'s `dataset` manifest doesn't show it as exchange data from `sources`
    ("SYMBOL dataset YYYY-MM: why", each line ending in `note`); no row is read. A month the window covers to
    its end (`end`) must also be complete. A missing or unreadable manifest, a month without an entry or an
    unknown source counts too: what isn't known to be exchange data is refused."""
    p = cfg.data_dir / symbol / dataset / "_manifest.json"
    try:
        man = json.loads(p.read_text())
        if not isinstance(man, dict):
            raise ValueError("not an object")
    except FileNotFoundError:
        return [f"{symbol} {dataset} {m}: no manifest{note}" for m in months]
    except (OSError, ValueError):
        return [f"{symbol} {dataset} {m}: manifest unreadable{note}" for m in months]
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
        out.append(f"{symbol} {dataset} {m}: {why}{note}")
    return out


def missing_days(cfg, symbol: str, dataset: str, start: pd.Timestamp, end: pd.Timestamp, what: str = "candles",
                 note: str = "", traded: bool = False) -> list[str]:
    """The UTC days in [start, end) without a single row of `symbol`'s `dataset` (a timeframe's candles, or
    "metrics"), as runs ("SYMBOL tf 2026-09-10 to 2026-09-12: no candles (3 days)"), each line ending in
    `note`. With `traded`, a day needs a candle with trades: a day of zero-trade candles counts as missing.
    Reads the time stamps (and with `traded`, `trades`) only (`stamps`), never a price."""
    lo, hi = start.value // 10**6, end.value // 10**6
    try:
        t = stamps(cfg, symbol, dataset, traded)
    except FileNotFoundError:
        t = np.empty(0, dtype="int64")
    t = t[(t >= lo) & (t < hi)]
    missing = np.setdiff1d(np.arange(lo // DAY_MS, -(-hi // DAY_MS)), t // DAY_MS)
    out = []
    for run in (np.split(missing, np.flatnonzero(np.diff(missing) != 1) + 1) if len(missing) else []):
        a, b = (pd.Timestamp(int(d) * DAY_MS, unit="ms").strftime("%Y-%m-%d") for d in (run[0], run[-1]))
        out.append(f"{symbol} {dataset} {a}: no {what}{note}" if len(run) == 1
                   else f"{symbol} {dataset} {a} to {b}: no {what} ({len(run)} days){note}")
    return out

"""The reference group: BTC's bar on every coin's bars, as eight `btc_` columns (spec
docs/superpowers/specs/2026-11-05-cross-coin-reference-design.md, "Plugin-facing interface").

The group itself is registered in perp_lab.data.columns. Its columns come from an explicit whitelist
over BTC's candles, funding and metrics, never from BTC's `load_bars` frame. BTC's bar t joins the
coin's bar t on `open_time` exactly and is NaN where BTC has no bar or a bar with no trades (X6); no
forward fill. The as-of two follow the coin's own `funding_last` and `oi` rules at the coin's close.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd

from perp_lab.data import store

log = logging.getLogger(__name__)

COIN = "BTC"
SYMBOL = "BTCUSDT"
PREFIX = "btc_"
BAR_COLUMNS = ("open", "high", "low", "close", "quote_volume", "taker_buy_quote_volume")
ASOF_COLUMNS = ("funding_last", "oi_value")
COLUMNS = tuple(PREFIX + c for c in BAR_COLUMNS + ASOF_COLUMNS)

_CANDLES = ("open_time", "trades", *BAR_COLUMNS)
_FUNDING = ("funding_time", "funding_rate", "interval_hours")
_METRICS = ("create_time", "oi", "oi_value")
_missing_warned: set[str] = set()      # timeframes warned about: one warning per process each


@dataclass(frozen=True)
class ReferenceSource:
    """BTC's data at one timeframe, shared by every coin, holding nothing at or after its end:
    `candles` (open_time, trades and BAR_COLUMNS), `funding` (funding_time, funding_rate,
    interval_hours) and `metrics` (create_time, oi, oi_value); empty frames where data is missing."""
    candles: pd.DataFrame
    funding: pd.DataFrame
    metrics: pd.DataFrame


def _empty(cols: tuple[str, ...], time_col: str) -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype="int64" if c == time_col else "float64") for c in cols})


def load_source(cfg, tf: str, start=None, end=None, holdout=None) -> ReferenceSource:
    """BTC's candles in [start, end) through `store.load_candles` (so `_resolve_end` guards them),
    and its funding and metrics before the same resolved end."""
    end = store._resolve_end(cfg, end, holdout)
    e_ms = int(end.value // 10**6)
    missing = []
    try:
        candles = store.load_candles(cfg, SYMBOL, tf, start, end, holdout)[list(_CANDLES)].reset_index(drop=True)
    except FileNotFoundError:
        candles = _empty(_CANDLES, "open_time")
        missing.append(tf)
    funding = store.load_funding(cfg, SYMBOL)
    if funding.empty:
        funding = _empty(_FUNDING, "funding_time")
        missing.append("funding")
    metrics = store.load_metrics(cfg, SYMBOL)
    if metrics is None:
        metrics = _empty(_METRICS, "create_time")
        missing.append("metrics")
    if missing and tf not in _missing_warned:
        _missing_warned.add(tf)
        log.warning("column group reference %s: no %s %s data; the btc_ columns it feeds are NaN",
                    tf, SYMBOL, ", ".join(missing))
    funding = funding.loc[funding.funding_time < e_ms, list(_FUNDING)].reset_index(drop=True)
    metrics = metrics.loc[metrics.create_time < e_ms, list(_METRICS)].reset_index(drop=True)
    return ReferenceSource(candles, funding, metrics)


def attach(bars: pd.DataFrame, symbol: str, tf: str, source: ReferenceSource) -> pd.DataFrame:
    """`bars` with COLUMNS appended, all float64, on the same rows and index. Every coin, BTC
    included, gets the same BTC values, so `symbol` and `tf` are unused (the source is per tf)."""
    out = bars.copy()
    c = source.candles.drop_duplicates("open_time").set_index("open_time")
    c = c[c.trades != 0]                      # X6; NaN trades (ccxt-stitched) are not zero
    joined = c.reindex(out.open_time.to_numpy())
    for col in BAR_COLUMNS:
        out[PREFIX + col] = joined[col].to_numpy(dtype="float64")
    close = out.close_time.to_numpy(dtype="int64")
    out[PREFIX + "funding_last"] = store.funding_last_at(close, source.funding)[0].astype("float64")
    out[PREFIX + "oi_value"] = store.metrics_at(close, source.metrics)[1].astype("float64")
    return out


def synthetic(bars: pd.DataFrame, seed: int):
    """The synthetic reference for the lookahead check (X5)."""
    raise NotImplementedError("the synthetic reference is not built yet")


def sources(cfg, symbols: list[str], tf: str) -> list[tuple[str, str]]:
    """The (symbol, dataset) directories the readiness check reads: BTC's, whatever the coins."""
    return [(SYMBOL, tf), (SYMBOL, "funding"), (SYMBOL, "metrics")]

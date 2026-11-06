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

import numpy as np
import pandas as pd

from perp_lab.config import TF_MS
from perp_lab.data import store

log = logging.getLogger(__name__)

COIN = "BTC"
SYMBOL = "BTCUSDT"
PREFIX = "btc_"
BAR_COLUMNS = ("open", "high", "low", "close", "quote_volume", "taker_buy_quote_volume")
ASOF_COLUMNS = ("funding_last", "oi_value")
COLUMNS = tuple(PREFIX + c for c in BAR_COLUMNS + ASOF_COLUMNS)

SYNTHETIC_ID = 1                       # the group's generator stream (X5): default_rng((seed, SYNTHETIC_ID))

_CANDLES = ("open_time", "trades", *BAR_COLUMNS)
_FUNDING = ("funding_time", "funding_rate", "interval_hours")
_METRICS = ("create_time", "oi", "oi_value")
_missing_warned: set[str] = set()      # timeframes warned about: one warning per process each

# the synthetic reference (spec "The lookahead check and the synthetic bars")
_BASE_PRICE = 20_000.0
_SIGMA_BARS = 100                       # the coin's trailing volatility window
_SIGMA_FLOOR = 1e-4
_DRIFT_BARS = 300                       # BTC's own drift is held this long
_OI_FROM_ROW = 300                      # synthetic OI is NaN before this row; the first cut (400) is past it
_FUNDING_MS = 8 * 3_600_000


@dataclass(frozen=True)
class ReferenceSource:
    """BTC's data at one timeframe, shared by every coin, holding nothing at or after its end:
    `candles` (open_time, trades and BAR_COLUMNS), `funding` (funding_time, funding_rate,
    interval_hours) and `metrics` (create_time, oi, oi_value); empty frames where data is missing.
    `tf` is the candles' timeframe and `end` the resolved end it was cut at, so `attach` can refuse
    a source that doesn't fit the bars."""
    candles: pd.DataFrame
    funding: pd.DataFrame
    metrics: pd.DataFrame
    tf: str
    end: pd.Timestamp


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
    return ReferenceSource(candles, funding, metrics, tf, end)


def attach(bars: pd.DataFrame, symbol: str, tf: str, source: ReferenceSource) -> pd.DataFrame:
    """`bars` with COLUMNS appended, all float64, on the same rows and index. Every coin, BTC
    included, gets the same BTC values.

    Refuses (ValueError) a source at another timeframe, whose candles would join at the wrong open
    times, and one cut before the bars' last close, which would carry btc_funding_last and
    btc_oi_value forward past its cut with no NaN to show it."""
    if source.tf != tf:
        raise ValueError(f"a {source.tf} reference source given to {symbol} {tf} bars")
    if len(bars):
        last_close = int(bars.close_time.max())
        if int(source.end.value // 10**6) <= last_close:
            raise ValueError(f"the reference source was cut at {source.end}, before {symbol} {tf}'s last bar "
                             f"closed at {pd.Timestamp(last_close, unit='ms', tz='UTC')}")
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


def synthetic(bars: pd.DataFrame, seed: int) -> dict[str, np.ndarray]:
    """The synthetic reference for the lookahead check (X5): {column: float64 array} for COLUMNS on
    bars' rows, drawn from `default_rng((seed, SYNTHETIC_ID))`. Each value at row t depends on bars' rows
    up to t, but the stream as a whole depends on the frame's length (how many drift blocks are drawn, which
    rows are NaN) and its last `open_time` (how many funding periods are drawn), so drawn on bars[:k] it
    isn't the full frame's stream cut at k. That is harmless: the lookahead check fills once, on the whole
    frame, before cutting.

    BTC's log-return is r_b = 0.7·r_x + 0.71·σ·z + d: r_x the coin's, σ its trailing 100-bar standard
    deviation (floored), z standard normal and d a drift of ±0.3·σ held for 300-bar blocks, so BTC trends
    apart from the coin and trend filters switch. Quote volume is heavier on big moves, funding changes
    every 8 hours of bar time with the odd spike, and OI is NaN on the first 300 rows, as real BTC OI
    is before 2020-09-01. The six bar columns are NaN on the row before each of the lookahead check's
    cuts, as real BTC's are on a zero-trade bar (X6): the cut run ends on that NaN and the full run has
    BTC's next bar after it, so a fill from later bars (`bfill`, `interpolate`) is caught."""
    from perp_lab.validate.causality import CUTS      # causality imports this module
    rng = np.random.default_rng((seed, SYNTHETIC_ID))
    n = len(bars)
    log_close = np.log(bars["close"].to_numpy(dtype="float64"))
    r_x = np.diff(log_close, prepend=log_close[:1])
    sigma = pd.Series(r_x).rolling(_SIGMA_BARS, min_periods=2).std().to_numpy()
    sigma = np.maximum(np.nan_to_num(sigma), _SIGMA_FLOOR)
    drift = np.repeat(rng.choice([-0.3, 0.3], n // _DRIFT_BARS + 1), _DRIFT_BARS)[:n] * sigma
    r_b = 0.7 * r_x + 0.71 * sigma * rng.standard_normal(n) + drift
    close = _BASE_PRICE * np.exp(np.cumsum(r_b))
    open_ = np.r_[_BASE_PRICE, close][:n]
    wick = np.abs(rng.normal(0.0, 0.4 * sigma, (2, n)))
    noise = rng.normal(0.0, 0.1, n)
    ar = np.empty(n)
    level = 0.0
    for i in range(n):                        # AR(1), so busy and quiet stretches last
        level = 0.95 * level + noise[i]
        ar[i] = level
    quote = 1e9 * np.exp(ar) * (1 + 30 * np.abs(r_b))
    buy_share = np.clip(0.5 + 0.15 * np.tanh(r_b / sigma) + rng.normal(0.0, 0.03, n), 0.05, 0.95)
    oi_value = 1e5 * np.exp(np.cumsum(rng.normal(0.0, 0.01, n))) * close
    oi_value[:_OI_FROM_ROW] = np.nan
    # last, so only the funding values depend on the bars' times: one per 8-hour period from the first bar's
    period = bars["open_time"].to_numpy(dtype="int64") // _FUNDING_MS
    period = period - period[0] if n else period
    m = int(period[-1]) + 1 if n else 0
    rate = np.where(rng.random(m) < 1 / 15, rng.choice([-0.001, 0.001], m), 0.0001 + rng.normal(0.0, 0.0001, m))
    out = {"open": open_, "high": np.maximum(open_, close) * (1 + wick[0]),
           "low": np.minimum(open_, close) * (1 - wick[1]), "close": close, "quote_volume": quote,
           "taker_buy_quote_volume": quote * buy_share, "funding_last": rate[period], "oi_value": oi_value}
    out = {PREFIX + c: out[c].astype("float64") for c in BAR_COLUMNS + ASOF_COLUMNS}
    gaps = [k - 1 for k in CUTS if k < n]     # after every draw, so no other value changes
    for c in BAR_COLUMNS:
        out[PREFIX + c][gaps] = np.nan
    return out


def sources(cfg, symbols: list[str], tf: str) -> list[tuple[str, str]]:
    """The (symbol, dataset) directories the readiness check reads: BTC's, whatever the coins."""
    return [(SYMBOL, tf), (SYMBOL, "funding"), (SYMBOL, "metrics")]


def symbol_check(cfg, tf: str) -> list[str]:
    """X3's symbol check: BTC's candles in its `tf` folder over the last research month (the month
    before holdout_start's), read for `open_time` only, must be exactly `tf` apart. That names a folder
    holding another timeframe's candles, which the manifests can't show, and a gap inside the month.
    Nothing at or after holdout_start is read."""
    h = cfg.holdout_start
    month = (h.tz_localize(None) if h.tzinfo else h).to_period("M") - 1
    lo, hi = (int(m.start_time.tz_localize("UTC").value // 10**6) for m in (month, month + 1))
    where = f"column group reference: {SYMBOL}/{tf} {month}"
    inside = [("open_time", ">=", lo), ("open_time", "<", hi)]
    t = [pd.read_parquet(f, columns=["open_time"], filters=inside).open_time.to_numpy("int64")
         for f in sorted((cfg.data_dir / SYMBOL / tf).glob("*.parquet"))]
    t = np.unique(np.concatenate(t)) if t else np.empty(0, "int64")   # monthly and daily files may overlap
    if not len(t):
        return [f"{where} has no candles"]
    off = sorted(set(np.diff(t).tolist()) - {TF_MS[tf]})
    if off:
        return [f"{where} has open times {', '.join(_span(ms) for ms in off)} apart, not {tf}"]
    return []


def _span(ms: int) -> str:
    """A step in the timeframes' own units: 8h, 15m, 2d."""
    for unit, size in (("d", 86_400_000), ("h", 3_600_000), ("m", 60_000), ("s", 1000)):
        if ms % size == 0:
            return f"{ms // size}{unit}"
    return f"{ms}ms"

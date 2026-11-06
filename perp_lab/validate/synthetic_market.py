"""One fixed synthetic market that plugins at every timeframe trade, for `lab preflight`'s overlap.

Each symbol is a single 5m path from `synthetic_bars` (fixed seeds and start date, never the
day-pinned holdout span, so the market is the same every day), resampled to each coarser timeframe.
A 1h plugin and its 4h twin then see the same prices, and their positions on the hourly grid can be
compared. The path's per-bar volatility is scaled by sqrt(5m/15m) so 15m and coarser bars keep the
volatility they had at a 15m base. Positions measured here are cached by MARKET_VERSION: bump it whenever
the bars change.

Version 3: the first coin is BTCUSDT, the reference coin, with version 2's SYN1 path (seed 100). SYN2 to SYN4
mix their 5m log-returns with its returns at rho = 0.55, 0.7 and 0.85 (r' = rho*r_ref + sqrt(1-rho^2)*r, the
same variance) before close, open, high and low are rebuilt, so a correlation-gated plugin holds something.
Every frame carries the `btc_` columns, taken from BTCUSDT's frame by open time (a bar with no trades is
NaN, X6); BTCUSDT's equal its own.

Version 4: every frame carries the spot group's five columns, drawn by `spot.synthetic` on each coin's own 5m
path with the path's seed, before the `btc_` columns. Resampled, spot's quote volumes and trades add up (NaN
for a bucket with no spot bar), `spot_close` is the bucket's last and `basis` is recomputed from the bucket's
`close` and `spot_close`, as a coarser kline's would be. Spot's NaN stretches lie on multiples of 48 rows, so
they are whole buckets at 15m, 1h and 4h. No other column changed.

Version 5: the spot stream's constants are calibrated to the real-data inventory (docs/research/spot-inventory,
plan Task 11): its basis tails and spot share move; no other column changed.
"""

from __future__ import annotations

from functools import lru_cache

import numpy as np
import pandas as pd

from perp_lab.config import TF_MS
from perp_lab.data import reference, spot
from perp_lab.validate.causality import synthetic_bars, with_moving_columns

MARKET_VERSION = 5
SYMBOLS = (reference.SYMBOL, "SYN2", "SYN3", "SYN4")
RHO = (None, 0.55, 0.7, 0.85)           # each coin's return correlation with the reference; None for itself
DAYS = 180
BASE_MS = TF_MS["5m"]
# synthetic_bars' noise is per bar whatever the timeframe; scaling it by sqrt(5m / 15m) keeps the 15m and
# coarser bars about as volatile as they were when the path was 15m.
VOL_SCALE = (BASE_MS / TF_MS["15m"]) ** 0.5
_SUMMED = ("volume", "quote_volume", "trades", "taker_buy_volume", "taker_buy_quote_volume", "funding_charge")
# summed with min_count=1, so a bucket with no spot bar (all NaN) stays NaN rather than 0
_SPOT_SUMMED = ("spot_quote_volume", "spot_taker_buy_quote_volume", "spot_trades")


class NoMarket(ValueError):
    """A timeframe the 5m path can't be resampled to."""


def _raw(i: int) -> pd.DataFrame:
    return synthetic_bars(n=DAYS * 86_400_000 // BASE_MS, seed=100 + i, start="2023-01-01", funding_every=96,
                          tf_ms=BASE_MS, vol_scale=VOL_SCALE)          # funding every 8 hours


def _log_returns(bars: pd.DataFrame) -> np.ndarray:
    return np.diff(np.log(bars["close"].to_numpy()), prepend=np.log(bars["open"].iloc[0]))


def _mixed(bars: pd.DataFrame, ref: pd.DataFrame, rho: float) -> pd.DataFrame:
    """bars with its 5m log-returns mixed with ref's at correlation rho (same variance), close and open
    rebuilt from the start price, high and low keeping their distance from the larger or smaller of open
    and close."""
    r = rho * _log_returns(ref) + (1 - rho * rho) ** 0.5 * _log_returns(bars)
    close = bars["open"].iloc[0] * np.exp(np.cumsum(r))
    open_ = np.r_[bars["open"].iloc[0], close[:-1]]
    hi_up = bars["high"] / np.maximum(bars["open"], bars["close"])
    lo_dn = bars["low"] / np.minimum(bars["open"], bars["close"])
    out = bars.copy()
    out["close"], out["open"] = close, open_
    out["high"] = np.maximum(open_, close) * hi_up.to_numpy()
    out["low"] = np.minimum(open_, close) * lo_dn.to_numpy()
    return out


def _path(i: int) -> pd.DataFrame:
    """Coin i's 5m path with every column moving, its own spot stream included; the `btc_` columns are
    dropped, to be joined from BTCUSDT's frame after resampling."""
    bars = _raw(i)
    if RHO[i] is not None:
        bars = _mixed(bars, _raw(0), RHO[i])
    return with_moving_columns(bars, seed=200 + i).drop(columns=list(reference.COLUMNS))   # joined after resampling


def _with_reference(bars: pd.DataFrame, ref: pd.DataFrame) -> pd.DataFrame:
    """bars with the `btc_` columns from ref (BTCUSDT's frame at the same timeframe) by open time. A BTC
    bar with no trades gives NaN bar columns (X6); the as-of two are BTC's own at the bar."""
    ref = ref.set_index("open_time")
    joined = ref.reindex(bars["open_time"].to_numpy())
    live = (joined["trades"] != 0).to_numpy()
    out = bars.copy()
    for c in reference.BAR_COLUMNS:
        out[reference.PREFIX + c] = np.where(live, joined[c].to_numpy(dtype="float64"), np.nan)
    out[reference.PREFIX + "funding_last"] = joined["funding_last"].to_numpy(dtype="float64")
    out[reference.PREFIX + "oi_value"] = joined["oi_value"].to_numpy(dtype="float64")
    return out


def _resample(bars: pd.DataFrame, ms: int) -> pd.DataFrame:
    """OHLC as candles combine; volumes, trade counts, taker flow and funding charged add up; the
    bar's own open time; every other column (funding rate and interval, open interest, regime, the
    close time, `spot_close`) is its state at the bar's close. Spot's volumes and trades add up too,
    NaN where the bucket has no spot bar; `basis` is recomputed from the bar's `close` and `spot_close`."""
    how = {c: "last" for c in bars.columns if c not in _SPOT_SUMMED}
    how.update({c: "sum" for c in _SUMMED})
    how.update(open_time="first", open="first", high="max", low="min", close="last")
    buckets = bars.resample(pd.Timedelta(milliseconds=ms), label="left", closed="left")
    out = buckets.agg(how)
    out[list(_SPOT_SUMMED)] = buckets[list(_SPOT_SUMMED)].sum(min_count=1)
    out["basis"] = out["close"] / out["spot_close"] - 1
    return out[bars.columns]


@lru_cache(maxsize=None)
def market(tf: str) -> dict[str, pd.DataFrame]:
    """{symbol: bars at tf}. Built once per timeframe and shared: callers must not change the frames."""
    ms = TF_MS.get(tf)
    if ms is None or ms < BASE_MS or ms % BASE_MS:
        raise NoMarket(f"no synthetic market at {tf}: its bars are 5m, resampled to multiples of 5m")
    paths = {}
    for i, s in enumerate(SYMBOLS):
        bars = _path(i)
        paths[s] = bars if ms == BASE_MS else _resample(bars, ms)
    ref = paths[reference.SYMBOL]
    return {s: _with_reference(b, ref) for s, b in paths.items()}

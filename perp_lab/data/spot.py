"""The spot group: each coin's own Binance spot bar on its perp bars, as four `spot_` columns and `basis`
(spec docs/superpowers/specs/2026-11-05-spot-basis-design.md, "Alignment, publish lag, gaps", "Holdout" and
"Plugin-facing interface").

Each perp bar takes the spot row with the same `open_time`, so the two cover the same interval and close at
the same instant; there is no publish lag. `basis = close / spot_close − 1` from those two rows. X6: the four
`spot_` columns are NaN where the spot bar is missing or has no trades, and `basis` is NaN then too and where
the perp bar has no trades; NaN perp trades (ccxt-stitched) are not zero. No forward fill.

Unlike the reference, whose one coin's data every coin shares, each coin reads its own spot. So the group's
source is a lazy `SpotSource` handle holding no rows: `attach` reads that coin's spot through it, cut at the
end resolved (and holdout-guarded) when the handle was built.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

from perp_lab.data import store

log = logging.getLogger(__name__)

PREFIX = "spot_"
COLUMNS = ("spot_close", "spot_quote_volume", "spot_taker_buy_quote_volume", "spot_trades", "basis")
SYNTHETIC_ID = 2                       # the group's generator stream (X5): default_rng((seed, SYNTHETIC_ID))

_FIELDS = ("close", "quote_volume", "taker_buy_quote_volume", "trades")   # each spot_ column's candle field
_missing_warned: set[tuple[str, str]] = set()   # (symbol, tf) warned about: one warning per process each


def dataset(tf: str) -> str:
    """The spot folder under a symbol's data directory, as readiness and `known_gaps` name it."""
    return f"spot/{tf}"


def excluded(cfg, symbol: str) -> bool:
    """Whether `column_groups.spot.exclusions` names `symbol`, as the symbol (FILUSDT) or its coin (FIL)."""
    names = ((cfg.get("column_groups") or {}).get("spot") or {}).get("exclusions") or []
    return symbol in names or symbol.removesuffix(cfg.get("quote", "USDT")) in names


def load_spot(cfg, symbol: str, tf: str, start=None, end=None, holdout=None) -> pd.DataFrame:
    """`symbol`'s spot candles with open_time in [start, end): start defaults to history_start, and `end`
    goes through `store._resolve_end`, so without a token an end past holdout_start raises. Duplicate open
    times (a daily file over a monthly one) are dropped; sorted, CANDLE_SCHEMA columns, a RangeIndex.
    FileNotFoundError when the folder holds no parquet."""
    from perp_lab.data.vision import CANDLE_SCHEMA     # not at module level: vision imports requests

    end = store._resolve_end(cfg, end, holdout)
    start = start or cfg.history_start
    df = store._read_dir(cfg.data_dir / symbol / dataset(tf))
    if df is None:
        raise FileNotFoundError(f"no spot candles for {symbol} {tf}; run `lab backfill --datasets spotKlines`")
    s_ms, e_ms = int(start.value // 10**6), int(end.value // 10**6)
    df = df.drop_duplicates("open_time").sort_values("open_time")
    return df.loc[(df.open_time >= s_ms) & (df.open_time < e_ms), CANDLE_SCHEMA].reset_index(drop=True)


def attach_spot(bars: pd.DataFrame, spot: pd.DataFrame | None) -> pd.DataFrame:
    """`bars` with COLUMNS appended, all float64, on the same rows and index: each bar joined to the spot
    row with its exact `open_time` (spot rows deduplicated first, spot rows with no bar dropped), under X6.
    `spot` None (no spot to read) gives all NaN. Pure: each row's values depend on that row's two bars alone."""
    out = bars.copy()
    if spot is None:
        for c in COLUMNS:
            out[c] = np.full(len(out), np.nan)
        return out
    s = spot.drop_duplicates("open_time").set_index("open_time")
    s = s[s.trades != 0]                      # X6: a zero-trade spot bar is no bar
    joined = s.reindex(out.open_time.to_numpy())
    for col, field in zip(COLUMNS, _FIELDS):
        out[col] = joined[field].to_numpy(dtype="float64")
    traded = out.trades.to_numpy(dtype="float64") != 0          # NaN perp trades (ccxt-stitched) are not zero
    basis = out.close.to_numpy(dtype="float64") / out.spot_close.to_numpy() - 1
    out["basis"] = np.where(traded, basis, np.nan)
    return out


@dataclass(frozen=True)
class SpotSource:
    """Every coin's spot at one timeframe, as a handle with no rows: `attach` reads a coin's spot with this
    `cfg` and `holdout` token, cut at `end`. `end` was resolved by `store._resolve_end` when the handle was
    built, so a request past holdout_start without a token raised then; `tf` lets `attach` refuse a handle
    that doesn't fit the bars."""
    cfg: object
    tf: str
    end: pd.Timestamp
    holdout: object


def load_source(cfg, tf: str, start=None, end=None, holdout=None) -> SpotSource:
    """The handle for `tf`, reading no rows. `start` is unused: `attach` reads from the bars' own first open."""
    return SpotSource(cfg, tf, store._resolve_end(cfg, end, holdout), holdout)


def attach(bars: pd.DataFrame, symbol: str, tf: str, source: SpotSource) -> pd.DataFrame:
    """`attach_spot` on `symbol`'s own spot from the bars' first open to the source's end.

    Refuses (ValueError) a source at another timeframe, whose rows would join at the wrong open times, and
    one cut at or before the bars' last close, whose missing tail would pass for a spot gap. An excluded
    coin gets all NaN without a read. A coin with no spot candles gets all NaN and one warning per process
    per (symbol, tf)."""
    if source.tf != tf:
        raise ValueError(f"a {source.tf} spot source given to {symbol} {tf} bars")
    if len(bars):
        last_close = int(bars.close_time.max())
        if int(source.end.value // 10**6) <= last_close:
            raise ValueError(f"the spot source was cut at {source.end}, before {symbol} {tf}'s last bar "
                             f"closed at {pd.Timestamp(last_close, unit='ms', tz='UTC')}")
    if excluded(source.cfg, symbol) or not len(bars):
        return attach_spot(bars, None)
    first = pd.Timestamp(int(bars.open_time.min()), unit="ms", tz="UTC")
    try:
        spot = load_spot(source.cfg, symbol, tf, first, source.end, source.holdout)
    except FileNotFoundError:
        if (symbol, tf) not in _missing_warned:
            _missing_warned.add((symbol, tf))
            log.warning("column group spot: no %s %s spot candles; its spot_ columns and basis are NaN",
                        symbol, tf)
        spot = None
    return attach_spot(bars, spot)


def sources(cfg, symbols: list[str], tf: str) -> list[tuple[str, str]]:
    """The (symbol, dataset) directories the readiness check reads: each coin's own spot folder at `tf`."""
    return [(s, dataset(tf)) for s in symbols]

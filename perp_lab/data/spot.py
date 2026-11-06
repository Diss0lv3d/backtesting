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

# the synthetic spot stream (spec "The lookahead check and the synthetic bars"); the calibration rule there
# sets BASIS_C, BASIS_SLOW_SD, BASIS_FAST_SD, BASIS_JUMP and SPOT_SHARE from real 1h bars
BASIS_SLOW_PHI = 0.995                 # basis = slow + fast; slow: half-life about 140 bars
BASIS_SLOW_SD = 0.0010                 # its stationary sd
BASIS_C = 0.0                          # its mean
BASIS_FAST_PHI = 0.9
BASIS_FAST_SD = 0.0005                 # stationary sd, before jumps
BASIS_JUMP_P = 0.01                    # the share of rows a jump enters the fast state on
BASIS_JUMP = (0.0020, 0.0060)          # its size range, either sign
SPOT_SHARE = 0.3                       # spot's median share of perp quote volume, and of trades
SHARE_PHI = 0.99                       # log share = log SPOT_SHARE + an AR(1) + iid noise
SHARE_SD = 0.3                         # the AR(1)'s stationary sd
SHARE_NOISE_SD = 0.3
TAKER_SLOPE = 0.6                      # spot taker share = 0.5 + slope·(perp taker share − 0.5) + noise
TAKER_NOISE_SD = 0.05
MIN_BARS = 100                         # bars traded on both sides that make a month the symbol check's
BASIS_LIMIT = 0.01                     # the symbol check's bound on a month's median basis
# rows [start, end): spot not listed yet, then a halt just after the first NaN cut row (399), so a rolling
# window over spot clears both by the later cuts (ruling S5); on multiples of 48, so resampled buckets are whole
NAN_STRETCHES = ((0, 288), (432, 480))


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

    df = store._read_window(cfg, cfg.data_dir / symbol / dataset(tf), start, end, holdout)
    if df is None:
        raise FileNotFoundError(f"no spot candles for {symbol} {tf}; run `lab backfill --datasets spotKlines`")
    return df[CANDLE_SCHEMA]


def attach_spot(bars: pd.DataFrame, spot: pd.DataFrame | None) -> pd.DataFrame:
    """`bars` with COLUMNS appended, all float64, on the same rows and index: each bar joined to the spot
    row with its exact `open_time` (spot rows deduplicated first, spot rows with no bar dropped), under X6.
    `spot` None (no spot to read) gives all NaN. Pure: each row's values depend on that row's two bars alone."""
    out = bars.copy()
    if spot is None:
        for c in COLUMNS:
            out[c] = np.full(len(out), np.nan)
        return out
    joined = store.exact_join(out, spot, _FIELDS)                 # X6: a zero-trade spot bar is no bar
    for col, field in zip(COLUMNS, _FIELDS):
        out[col] = joined[field].to_numpy()
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
    from perp_lab.data import columns          # not at module level: columns imports the groups' modules
    columns.check_source_fits("spot", source.tf, source.end, bars, symbol, tf)
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


def _ar1(phi: float, sd: float, z: np.ndarray, kicks: np.ndarray | None = None) -> np.ndarray:
    """An AR(1) with coefficient `phi` and stationary sd `sd`, started from its stationary distribution:
    `z` holds one standard normal per row, and `kicks` (when given) enter the state and decay with it."""
    shocks = sd * np.sqrt(1 - phi * phi) * z
    if len(z):
        shocks[0] = sd * z[0]
    if kicks is not None:
        shocks = shocks + kicks
    out, x = [], 0.0
    for e in shocks.tolist():
        x = phi * x + e
        out.append(x)
    return np.array(out, dtype="float64")


def _share(z: np.ndarray, noise: np.ndarray) -> np.ndarray:
    """A moving share of the perp's: SPOT_SHARE times the exp of an AR(1) in logs plus iid noise."""
    return SPOT_SHARE * np.exp(_ar1(SHARE_PHI, SHARE_SD, z) + SHARE_NOISE_SD * noise)


def synthetic(bars: pd.DataFrame, seed: int) -> dict[str, np.ndarray]:
    """The synthetic spot stream for the lookahead check (X5): {column: float64 array} for COLUMNS on bars'
    rows, drawn from `default_rng((seed, SYNTHETIC_ID))`. It reads the filled `close`, `quote_volume`,
    `trades` and `taker_buy_quote_volume`, and each value at row t depends on bars' rows up to t. Every draw
    is made up front, in a fixed order, one per row, so the draws depend on the frame's length alone; drawn
    on bars[:k], the stream isn't the full frame's cut at k. That is harmless: the lookahead check fills
    once, on the whole frame, before cutting.

    `basis` is a slow AR(1) around BASIS_C, persistent for days at 1h so a threshold on its 24-bar mean
    fires, plus a fast AR(1) that the odd jump enters and decays with. `spot_close = close / (1 + basis)`,
    and `basis` is then recomputed as `close / spot_close − 1`, so the identity is exact. Spot quote volume
    and trades are the perp's times a moving share each; spot's taker share follows the perp's, clipped to
    [0.05, 0.95]. A plugin that crashes on NaN spot fails the check, not a trial.

    X6 holds by construction, as `attach_spot`'s does: the five are NaN where the drawn spot trades round
    to 0 (a perp bar with few or no trades), and `basis` is NaN where the perp bar has no trades.

    The five are NaN on NAN_STRETCHES, as real spot's are before a listing and in a halt, and on the row
    before the lookahead check's first cut only (`causality.NAN_CUTS`), as on a zero-trade bar (X6): the cut
    run ends on that NaN and the full run has the next bar after it, so a fill from later bars (`bfill`,
    `interpolate`) is caught. Only the first, because a next-bar peek shows only on the cut run's last row:
    at the later cuts spot is set there, so a peek gated on a spot column can show. A rolling window over
    them is NaN until it clears the NaN rows, the last of which is the halt's, just after row 399: a gate on
    one is seen at 1100 up to 620 bars and at 2500 up to 2020; a longer window is seen only by the dated
    check."""
    from perp_lab.validate.causality import NAN_CUTS  # causality imports the groups' modules
    rng = np.random.default_rng((seed, SYNTHETIC_ID))
    n = len(bars)
    z_slow, z_fast = rng.standard_normal(n), rng.standard_normal(n)
    jump_on, jump_size = rng.random(n) < BASIS_JUMP_P, rng.uniform(*BASIS_JUMP, n)
    jump_sign = rng.choice([-1.0, 1.0], n)
    z_volume, noise_volume = rng.standard_normal(n), rng.standard_normal(n)
    noise_taker = rng.normal(0.0, TAKER_NOISE_SD, n)
    z_trades, noise_trades = rng.standard_normal(n), rng.standard_normal(n)

    jumps = np.where(jump_on, jump_size * jump_sign, 0.0)
    raw = BASIS_C + _ar1(BASIS_SLOW_PHI, BASIS_SLOW_SD, z_slow) + _ar1(BASIS_FAST_PHI, BASIS_FAST_SD, z_fast, jumps)
    close, quote = (bars[c].to_numpy(dtype="float64") for c in ("close", "quote_volume"))
    spot_close = close / (1 + raw)
    spot_quote = quote * _share(z_volume, noise_volume)
    with np.errstate(divide="ignore", invalid="ignore"):   # a zero-volume perp bar has no taker share
        perp_taker = bars["taker_buy_quote_volume"].to_numpy(dtype="float64") / quote
    taker = np.clip(0.5 + TAKER_SLOPE * (perp_taker - 0.5) + noise_taker, 0.05, 0.95)
    trades = bars["trades"].to_numpy(dtype="float64")
    out = {"spot_close": spot_close, "spot_quote_volume": spot_quote,
           "spot_taker_buy_quote_volume": spot_quote * taker,
           "spot_trades": np.round(trades * _share(z_trades, noise_trades)),
           "basis": close / spot_close - 1}
    # last, after every draw, so no other value changes. X6: a spot bar drawn with no trades is no bar, and
    # basis is NaN where the perp didn't trade (NaN perp trades are not zero)
    quiet = np.flatnonzero(out["spot_trades"] == 0).tolist()
    gaps = [r for a, b in NAN_STRETCHES for r in range(a, min(b, n))] + [k - 1 for k in NAN_CUTS if k < n]
    for c in COLUMNS:
        out[c][gaps + quiet] = np.nan
    out["basis"][trades == 0] = np.nan
    return out


def sources(cfg, symbols: list[str], tf: str) -> list[tuple[str, str]]:
    """The (symbol, dataset) directories the readiness check reads: each coin's own spot folder at `tf`."""
    return [(s, dataset(tf)) for s in symbols]


def symbol_check(cfg, tf: str) -> list[str]:
    """The symbol check: each coin of `cfg.symbols` that isn't excluded must have its spot pair be its perp's
    market. On the last research month with at least 100 bars traded on both sides (perp and spot `trades`
    above 0, joined on `open_time`) the median of `close / spot_close − 1` must be within ±1%; a coin with
    no such month is a problem too. Reads research data only (both loaders' default end)."""
    problems: list[str] = []
    for sym in cfg.symbols:
        if excluded(cfg, sym):
            continue
        where = f"column group spot: {sym}/{dataset(tf)}"
        try:
            perp = store.load_candles(cfg, sym, tf)[["open_time", "close", "trades"]]
            spot = load_spot(cfg, sym, tf)[["open_time", "close", "trades"]]
        except FileNotFoundError:
            perp = spot = None
        both = None if perp is None else perp.merge(spot, on="open_time", suffixes=("", "_spot"))
        if both is not None:
            both = both[(both.trades > 0) & (both.trades_spot > 0)]    # NaN trades are not traded
            month = pd.to_datetime(both.open_time, unit="ms", utc=True).dt.tz_localize(None).dt.to_period("M")
            counts = month.value_counts()
            full = counts[counts >= MIN_BARS]
        if both is None or full.empty:
            problems.append(f"{where} has no research month with {MIN_BARS} bars traded on both perp and spot; "
                            f"exclude the coin in column_groups.spot.exclusions")
            continue
        last = full.index.max()
        rows = both[(month == last).to_numpy()]
        m = float((rows.close / rows.close_spot - 1).median())
        if abs(m) > BASIS_LIMIT:
            problems.append(f"{where} {last}: median basis {m:+.2%}, outside ±1%; another pair? "
                            f"exclude the coin in column_groups.spot.exclusions")
    return problems

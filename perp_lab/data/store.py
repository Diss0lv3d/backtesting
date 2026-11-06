"""Parquet readers + the holdout guard + bar assembly (candles, funding, OI, column groups, regime).

Every research read goes through here. Reads at/after `holdout_start` raise
unless a valid HoldoutToken is passed.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd

from perp_lab.config import TF_MS, Config
from perp_lab.holdout import HoldoutViolation, check_token

log = logging.getLogger(__name__)
OI_PUBLISH_LAG_MS = 300_000            # a metrics snapshot stamped T is known at T + this
_gap_warned: set[tuple[str, str]] = set()
_tail_warned: set[tuple[str, str]] = set()


def _read_dir(d: Path) -> pd.DataFrame | None:
    files = sorted(d.glob("*.parquet"))
    if not files:
        return None
    return pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)


def _resolve_end(cfg: Config, end: pd.Timestamp | None, holdout) -> pd.Timestamp:
    hs = cfg.holdout_start
    if holdout is not None:
        check_token(holdout)
        return end or pd.Timestamp.now(tz="UTC")
    if end is None:
        return hs
    if end > hs:
        raise HoldoutViolation(f"requested end {end} is past holdout_start {hs}; holdout is locked")
    return end


def _drop_dead_tail(df: pd.DataFrame, symbol: str, tf: str) -> pd.DataFrame:
    """Drop the run after the last traded candle when all of it has no trades and no volume.

    A delisted coin's archive keeps flat zero-trade candles; they are absent, not data.
    Zero-trade candles between real trades stay, and NaN trades (ccxt-stitched) never drop.
    """
    traded = np.flatnonzero((df.trades > 0).values)
    if not len(traded):
        return df
    tail = df.iloc[traded[-1] + 1:]
    if len(tail) and (tail.trades == 0).all() and (tail.volume == 0).all():
        if (symbol, tf) not in _tail_warned:
            _tail_warned.add((symbol, tf))
            log.warning("%s %s: dropped %d zero-trade candles after the last trade (%s)", symbol, tf,
                        len(tail), pd.to_datetime(tail.open_time.iloc[0], unit="ms", utc=True))
        return df.iloc[: traded[-1] + 1].reset_index(drop=True)
    return df


def load_candles(cfg: Config, symbol: str, tf: str, start: pd.Timestamp | None = None,
                 end: pd.Timestamp | None = None, holdout=None) -> pd.DataFrame:
    """Closed candles with open_time in [start, end). end defaults to holdout_start."""
    end = _resolve_end(cfg, end, holdout)
    start = start or cfg.history_start
    df = _read_dir(cfg.data_dir / symbol / tf)
    if df is None:
        raise FileNotFoundError(f"no candles for {symbol} {tf}; run `lab backfill`")
    df = df.drop_duplicates("open_time").sort_values("open_time")
    s_ms, e_ms = int(start.value // 10**6), int(end.value // 10**6)
    df = df[(df.open_time >= s_ms) & (df.open_time < e_ms)].reset_index(drop=True)
    df = _drop_dead_tail(df, symbol, tf)
    step = TF_MS[tf]
    gaps = np.flatnonzero(np.diff(df.open_time.values) != step)
    if len(gaps) and (symbol, tf) not in _gap_warned:
        _gap_warned.add((symbol, tf))
        log.warning("%s %s: %d gaps in candle series (first at %s)", symbol, tf, len(gaps),
                    pd.to_datetime(df.open_time.iloc[gaps[0]], unit="ms", utc=True))
    df.index = pd.to_datetime(df.open_time, unit="ms", utc=True)
    df.index.name = "time"
    return df


def research_symbols(cfg: Config, symbols: list[str], tf: str) -> list[str]:
    """`symbols` in order, minus each coin with no `tf` candle in [history_start, holdout_start).

    A coin listed late still qualifies; only a coin with nothing to research on is dropped.
    Reads the `open_time` column only.
    """
    s_ms, e_ms = int(cfg.history_start.value // 10**6), int(cfg.holdout_start.value // 10**6)
    keep = []
    for sym in symbols:
        files = sorted((cfg.data_dir / sym / tf).glob("*.parquet"))
        if any(pd.read_parquet(f, columns=["open_time"]).open_time.between(s_ms, e_ms, inclusive="left").any()
               for f in files):
            keep.append(sym)
        else:
            log.warning("%s %s: no research bars before %s; skipped", sym, tf, cfg.holdout_start.date())
    return keep


def load_funding(cfg: Config, symbol: str) -> pd.DataFrame:
    df = _read_dir(cfg.data_dir / symbol / "funding")
    if df is None:
        return pd.DataFrame(columns=["funding_time", "funding_rate", "interval_hours"])
    return df.drop_duplicates("funding_time").sort_values("funding_time").reset_index(drop=True)


def load_metrics(cfg: Config, symbol: str) -> pd.DataFrame | None:
    df = _read_dir(cfg.data_dir / symbol / "metrics")
    if df is None:
        return None
    ct = df.create_time.astype("int64")
    df["create_time"] = ct.where(ct >= 10**11, ct * 1000)   # guard: files written with second resolution
    return df.drop_duplicates("create_time").sort_values("create_time").reset_index(drop=True)


def funding_last_at(close_times: np.ndarray, funding: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """Last settled funding rate and its interval hours as of each close time.

    A settlement is known once `funding_time <= close_time`. NaN before the first one
    (and everywhere when `funding` is empty). Pure: the same rule for a coin's own
    funding and for a reference coin's.
    """
    close_times = np.asarray(close_times)
    if funding is None or funding.empty:
        return np.full(len(close_times), np.nan), np.full(len(close_times), np.nan)
    ft = funding.funding_time.values.astype("int64")
    fr = funding.funding_rate.values.astype("float64")
    ih = funding.interval_hours.values.astype("float64")
    j = np.searchsorted(ft, close_times, side="right") - 1
    jc = np.clip(j, 0, len(fr) - 1)
    return np.where(j >= 0, fr[jc], np.nan), np.where(j >= 0, ih[jc], np.nan)


def metrics_at(close_times: np.ndarray, metrics: pd.DataFrame | None,
               publish_lag_ms: int = OI_PUBLISH_LAG_MS) -> tuple[np.ndarray, np.ndarray]:
    """`oi` and `oi_value` as of each close time. A snapshot stamped T is known at T + lag.

    NaN before the first known snapshot, and everywhere when `metrics` is None or empty.
    """
    close_times = np.asarray(close_times)
    if metrics is None or metrics.empty:
        return np.full(len(close_times), np.nan), np.full(len(close_times), np.nan)
    known = metrics.create_time.values.astype("int64") + publish_lag_ms
    j = np.searchsorted(known, close_times, side="right") - 1
    valid = j >= 0
    jj = np.clip(j, 0, len(known) - 1)
    return (np.where(valid, metrics.oi.values[jj], np.nan),
            np.where(valid, metrics.oi_value.values[jj], np.nan))


def align_funding(candles: pd.DataFrame, funding: pd.DataFrame, tf: str) -> pd.DataFrame:
    """Attach funding to bars.

    funding_charge: rate settled inside [open_time, open_time + tf). The engine
        charges it at that bar's open, to the position held *before* the open's fills.
        (Exit at the settlement open pays; entry at it does not.)
    funding_last:   last settled rate known by the bar's close — the only funding
        value a strategy may look at on that bar (see `funding_last_at`).
    """
    out = candles.copy()
    n = len(out)
    out["funding_charge"] = 0.0
    out["funding_last"] = np.nan
    out["funding_interval_h"] = np.nan
    if n == 0 or funding.empty:
        return out
    ot = out.open_time.values
    ft = funding.funding_time.values.astype("int64")
    fr = funding.funding_rate.values.astype("float64")
    idx = np.searchsorted(ot, ft, side="right") - 1          # bar containing settlement
    ok = (idx >= 0) & (ft < ot[np.clip(idx, 0, n - 1)] + TF_MS[tf])
    charge = np.zeros(n)
    np.add.at(charge, idx[ok], fr[ok])
    out["funding_charge"] = charge
    out["funding_last"], out["funding_interval_h"] = funding_last_at(out.close_time.values, funding)
    return out


def align_metrics(candles: pd.DataFrame, metrics: pd.DataFrame | None,
                  publish_lag_ms: int = OI_PUBLISH_LAG_MS) -> pd.DataFrame:
    """As-of join OI onto bars (see `metrics_at`)."""
    out = candles.copy()
    out["oi"], out["oi_value"] = metrics_at(out.close_time.values, metrics, publish_lag_ms)
    return out


def attach_slippage(cfg: Config, candles: pd.DataFrame) -> pd.DataFrame:
    """Add `slippage` (a fraction): each bar's point-in-time cost, from the volume before its day."""
    from perp_lab.engine.slippage import slippage_bps, trailing_adv

    costs = cfg["costs"]
    model = costs["slippage_model"]
    adv = trailing_adv(candles.open_time.to_numpy(), candles.quote_volume.to_numpy(dtype="float64"),
                       model["adv_days"])
    out = candles.copy()
    out["slippage"] = slippage_bps(adv, costs["slippage_bps"], model) / 1e4
    return out


def load_bars(cfg: Config, symbol: str, tf: str, start=None, end=None, holdout=None,
              with_regime: bool = True, with_metrics: bool = True, groups: bool = True,
              sources: dict[str, object] | None = None) -> pd.DataFrame:
    """Candles + funding + OI + each column group's columns + regime label, all strictly causal.

    `sources`: a group's shared source by group name; a group without one builds it here with the
    same start, end and token.
    """
    from perp_lab.data import columns          # not at module level: columns -> reference -> store
    from perp_lab.validate.regime import attach_regime

    lead = (cfg["costs"]["slippage_model"]["adv_days"] + 1) * 86_400_000
    c = load_candles(cfg, symbol, tf, None if start is None else start - pd.Timedelta(milliseconds=lead),
                     end, holdout)
    c = attach_slippage(cfg, c)
    if start is not None:
        c = c[c.open_time >= int(start.value // 10**6)]
    c = align_funding(c, load_funding(cfg, symbol), tf)
    if with_metrics:
        c = align_metrics(c, load_metrics(cfg, symbol))
    if groups:
        for g in columns.GROUPS:
            src = (sources or {}).get(g.name)
            if src is None and g.load_source is not None:
                src = g.load_source(cfg, tf, start, end, holdout)
            c = g.attach(c, symbol, tf, src)
    if with_regime:
        c = attach_regime(cfg, c, symbol, start, end, holdout)
    return c

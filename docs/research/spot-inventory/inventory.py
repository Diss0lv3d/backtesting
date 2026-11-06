"""Real-data inventory for the spot column group (spot-basis spec, X7; plan Task 2).

Run from the main checkout's root, where data/ lives, with the worktree's code:
    PYTHONPATH=<worktree> .venv/bin/python <worktree>/docs/research/spot-inventory/inventory.py \
        --config <worktree>/config/lab.yaml --out <worktree>/docs/research/spot-inventory

Research span only: perp candles through store.load_candles (default end = holdout_start), spot rows with
open_time < holdout_start, manifest months before holdout_start. Writes only into --out. No forward returns.

Definitions (Task 11 relies on them):
- basis  = perp close / spot close - 1, on bars with the same open_time where both have trades > 0
- mean24 = per coin, basis.rolling(24, min_periods=24).mean() over the perp's 1h bars
- share  = spot quote_volume / perp quote_volume where both traded
- pooled = every non-excluded coin's 1h values, concatenated; percentiles by np.nanpercentile (linear)
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from perp_lab.config import TF_MS, load_config
from perp_lab.data.store import attach_slippage, load_candles

TFS = ("15m", "1h", "4h")
DAY_MS = 86_400_000


def research_months(cfg) -> list[str]:
    return [str(m) for m in pd.period_range(cfg.history_start, cfg.holdout_start - pd.Timedelta(days=1), freq="M")]


def excluded(cfg) -> set[str]:
    quote = cfg.get("quote", "USDT")
    ex = (cfg.raw.get("column_groups", {}).get("spot", {}) or {}).get("exclusions", []) or []
    return {e if e.endswith(quote) else e + quote for e in ex}


def read_spot(cfg, sym: str, tf: str) -> pd.DataFrame | None:
    d = cfg.data_dir / sym / "spot" / tf
    files = sorted(d.glob("*.parquet"))
    if not files:
        return None
    df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    hs = int(cfg.holdout_start.value // 10**6)
    df = df[df.open_time < hs].drop_duplicates("open_time").sort_values("open_time").reset_index(drop=True)
    return df


def manifest_summary(cfg, sym: str, tf: str) -> dict:
    p = cfg.data_dir / sym / "spot" / tf / "_manifest.json"
    out = {"monthly_months": 0, "daily_months": 0, "absent_months": 0, "manifest_problems": 0}
    if not p.exists():
        out["manifest_problems"] = len(research_months(cfg))
        return out
    m = json.loads(p.read_text())
    seen_real = False
    for k in research_months(cfg):
        e = m.get(k)
        if e is None or not e.get("complete"):
            out["manifest_problems"] += 1
            continue
        src = e.get("source")
        if src == "absent":
            out["absent_months"] += 1
            if seen_real:
                out["manifest_problems"] += 1
        else:
            seen_real = True
            out["monthly_months" if src == "monthly" else "daily_months"] += 1
    return out


def runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """(start, end_inclusive) index pairs of True runs."""
    if not mask.any():
        return []
    d = np.diff(np.concatenate([[0], mask.astype(np.int8), [0]]))
    return list(zip(np.flatnonzero(d == 1), np.flatnonzero(d == -1) - 1))


def pct(x, q) -> float:
    x = np.asarray(x, dtype="float64")
    x = x[np.isfinite(x)]
    return float(np.nanpercentile(x, q)) if len(x) else float("nan")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    cfg = load_config(a.config)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    quote = cfg.get("quote", "USDT")
    syms = [c + quote for c in cfg["universe"]]
    ex = excluded(cfg)

    coverage, nan_rows, zero_rows, share_rows = [], [], [], []
    pooled_level, pooled_mean24, pooled_share = [], [], []
    btc_monthly = None

    for sym in syms:
        for tf in TFS:
            step = TF_MS[tf]
            perp = load_candles(cfg, sym, tf)
            spot = read_spot(cfg, sym, tf)
            row = {"symbol": sym, "tf": tf, "excluded": sym in ex, "perp_bars": len(perp),
                   "first_perp_day": str(perp.index[0].date())}
            row.update(manifest_summary(cfg, sym, tf))
            if spot is None or spot.empty:
                row.update({"first_spot_month": None, "first_spot_day": None, "off_grid_rows": 0,
                            "spot_nan_share": 1.0, "nan_runs_over_1d": None, "zero_trade_spot_bars": 0,
                            "symbol_check_month": None, "symbol_check_median": None, "abs_basis_p50": None,
                            "half_spread_proxy": None})
                coverage.append(row)
                continue
            first = pd.Timestamp(int(spot.open_time.iloc[0]), unit="ms", tz="UTC")
            row["first_spot_month"] = str(first.to_period("M"))
            row["first_spot_day"] = str(first.date())
            row["off_grid_rows"] = int((spot.open_time % step != 0).sum())
            zt = spot[spot.trades == 0]
            row["zero_trade_spot_bars"] = len(zt)
            zero_rows += [{"symbol": sym, "tf": tf,
                           "open_time": str(pd.Timestamp(int(t), unit="ms", tz="UTC"))} for t in zt.open_time]

            s = spot.set_index("open_time")
            j = perp[["open_time", "close", "quote_volume", "trades"]].copy()
            j["spot_close"] = j.open_time.map(s.close)
            j["spot_qv"] = j.open_time.map(s.quote_volume)
            j["spot_trades"] = j.open_time.map(s.trades)
            spot_ok = j.spot_trades.fillna(0).to_numpy() > 0
            row["spot_nan_share"] = round(float(1 - spot_ok.mean()), 6)
            long_runs = [(i, k) for i, k in runs(~spot_ok) if (k - i + 1) * step > DAY_MS]
            row["nan_runs_over_1d"] = len(long_runs)
            nan_rows += [{"symbol": sym, "tf": tf,
                          "first_open": str(j.index[i]), "last_open": str(j.index[k]), "bars": int(k - i + 1)}
                         for i, k in long_runs]

            both = spot_ok & (j.trades.fillna(1).to_numpy() > 0)
            basis = pd.Series(np.where(both, j.close / j.spot_close - 1, np.nan), index=j.index)
            # symbol check: the last research month with >= 100 bars traded on both sides
            bm = basis.groupby(basis.index.to_period("M")).agg(["count", "median"])
            ok = bm[bm["count"] >= 100]
            if len(ok):
                row["symbol_check_month"] = str(ok.index[-1])
                row["symbol_check_median"] = round(float(ok["median"].iloc[-1]), 6)
            else:
                row["symbol_check_month"], row["symbol_check_median"] = None, None
            if tf == "1h":
                row["abs_basis_p50"] = round(pct(basis.abs(), 50), 7)
                slip = attach_slippage(cfg, perp)["slippage"]
                row["half_spread_proxy"] = round(float(np.nanmedian(slip)), 7)
                share = pd.Series(np.where(both & (j.quote_volume.to_numpy() > 0),
                                           j.spot_qv / j.quote_volume, np.nan), index=j.index)
                for y, v in share.groupby(share.index.year):
                    share_rows.append({"symbol": sym, "year": int(y), "share_p50": round(pct(v, 50), 5)})
                if sym not in ex:
                    pooled_level.append(basis.to_numpy())
                    pooled_mean24.append(basis.rolling(24, min_periods=24).mean().to_numpy())
                    pooled_share.append(share.to_numpy())
                if sym == "BTCUSDT":
                    sp = pd.Series(np.where(spot_ok, j.spot_qv, np.nan), index=j.index)
                    tr = pd.Series(np.where(spot_ok, j.spot_trades, np.nan), index=j.index)
                    pq = j.quote_volume
                    g = pd.DataFrame({"spot_qv": sp, "perp_qv": pq, "spot_trades": tr})
                    g = g.groupby(g.index.to_period("M")).sum(min_count=1)
                    g = g.loc["2021-07":"2024-06"]
                    btc_monthly = pd.DataFrame({"month": g.index.astype(str),
                                                "spot_share": (g.spot_qv / g.perp_qv).round(4),
                                                "spot_trades_m": (g.spot_trades / 1e6).round(2)})
            else:
                row["abs_basis_p50"], row["half_spread_proxy"] = None, None
            coverage.append(row)
            print(sym, tf, "done", flush=True)

    pd.DataFrame(coverage).to_csv(out / "coverage.csv", index=False)
    pd.DataFrame(nan_rows, columns=["symbol", "tf", "first_open", "last_open", "bars"]).to_csv(
        out / "nan_runs.csv", index=False)
    pd.DataFrame(zero_rows, columns=["symbol", "tf", "open_time"]).to_csv(out / "zero_trade.csv", index=False)
    pd.DataFrame(share_rows, columns=["symbol", "year", "share_p50"]).to_csv(out / "volume_share.csv", index=False)
    if btc_monthly is not None:
        btc_monthly.to_csv(out / "btc_monthly_spot.csv", index=False)
    lv, m24, sh = (np.concatenate(x) if x else np.array([]) for x in (pooled_level, pooled_mean24, pooled_share))
    targets = [("level_p1", pct(lv, 1)), ("level_p50", pct(lv, 50)), ("level_p99", pct(lv, 99)),
               ("mean24_p1", pct(m24, 1)), ("mean24_p50", pct(m24, 50)), ("mean24_p99", pct(m24, 99)),
               ("share_p50", pct(sh, 50))]
    pd.DataFrame(targets, columns=["statistic", "value"]).to_csv(out / "calibration_targets.csv", index=False)
    print("excluded:", sorted(ex))
    print("targets:", {k: round(v, 6) for k, v in targets})


if __name__ == "__main__":
    main()

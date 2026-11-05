"""Real-data inventory for the BTC reference group (cross-coin spec, X7).

Run from the main checkout's root, where data/ lives:
    .venv/bin/python <this file> > docs/research/reference-inventory/inventory.json

Research span only: every candle read goes through store.load_candles with its default end
(holdout_start), and manifest months at or after holdout_start are skipped. Nothing is written
under data/.
"""
from __future__ import annotations

import json
import sys

import numpy as np
import pandas as pd

from perp_lab.config import load_config
from perp_lab.data.store import TF_MS, load_candles

REF = "BTCUSDT"
TFS = ("15m", "1h", "4h")
DATASETS = TFS + ("funding", "metrics")


def research_months(cfg) -> list[str]:
    months = pd.period_range(cfg.history_start, cfg.holdout_start - pd.Timedelta(days=1), freq="M")
    return [str(m) for m in months]


def manifest_summary(cfg, symbol: str, dataset: str) -> dict:
    path = cfg.data_dir / symbol / dataset / "_manifest.json"
    if not path.exists():
        return {"manifest": False}
    m = json.loads(path.read_text())
    months = research_months(cfg)
    entries = {k: m.get(k) for k in months}
    real = [k for k in months if entries[k] and entries[k].get("source") not in (None, "absent")]
    first = real[0] if real else None
    problems = []
    for k in months:
        e = entries[k]
        if e is None:
            problems.append(f"{k}: no manifest entry")
        elif not e.get("complete"):
            problems.append(f"{k}: complete=False ({e.get('source')}, {e.get('rows')} rows)")
        elif e.get("source") == "absent" and first and k > first:
            problems.append(f"{k}: absent after the first real month")
    return {"manifest": True, "first_month": first, "problems": problems}


def candle_gaps(df: pd.DataFrame, tf: str) -> dict:
    step = TF_MS[tf]
    ot = df.open_time.to_numpy()
    d = np.diff(ot)
    idx = np.flatnonzero(d != step)
    gaps = [{"after": str(pd.to_datetime(ot[i], unit="ms", utc=True)), "missing_bars": int(d[i] // step - 1)}
            for i in idx]
    zero = df.index[df.trades.to_numpy() == 0]
    return {"bars": int(len(df)), "first": str(df.index[0]), "last": str(df.index[-1]),
            "gaps": gaps, "zero_trade_bars": [str(t) for t in zero],
            "symbol_check_steps_ok": bool(len(idx) == 0 or all(d[i] > step for i in idx))}


def calibration(cfg, tf: str) -> dict:
    btc = load_candles(cfg, REF, tf)
    rb = np.log(btc.close.where(btc.close > 0)).diff()
    out = {}
    missing_btc = {}
    for coin in cfg["universe"]:
        sym = coin + cfg.get("quote", "USDT")
        if sym == REF:
            continue
        c = load_candles(cfg, sym, tf)
        rc = np.log(c.close.where(c.close > 0)).diff()
        j = pd.concat([rc.rename("c"), rb.rename("b")], axis=1, join="inner").dropna()
        out[coin] = round(float(j.c.corr(j.b)), 3)
        missing_btc[coin] = int((~c.open_time.isin(btc.open_time)).sum())
    lv = np.log(btc.quote_volume.where(btc.quote_volume > 0))
    share = (btc.taker_buy_quote_volume / btc.quote_volume.where(btc.quote_volume > 0)).dropna()
    vals = list(out.values())
    return {
        "corr_with_btc": out,
        "corr_median": round(float(np.median(vals)), 3),
        "corr_min": round(float(np.min(vals)), 3),
        "corr_max": round(float(np.max(vals)), 3),
        "coin_bars_without_btc_bar": missing_btc,
        "btc_logvol_vs_absret_corr": round(float(lv.corr(rb.abs())), 3),
        "btc_taker_share": {"mean": round(float(share.mean()), 4), "sd": round(float(share.std()), 4),
                            "p01": round(float(share.quantile(0.01)), 4),
                            "p99": round(float(share.quantile(0.99)), 4)},
    }


def main() -> None:
    cfg = load_config()
    report = {"span": [str(cfg.history_start.date()), str(cfg.holdout_start.date())],
              "manifests": {d: manifest_summary(cfg, REF, d) for d in DATASETS},
              "candles": {tf: candle_gaps(load_candles(cfg, REF, tf), tf) for tf in TFS},
              "calibration": {tf: calibration(cfg, tf) for tf in ("1h", "4h")}}
    json.dump(report, sys.stdout, indent=1)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()

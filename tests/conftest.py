import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from perp_lab.config import TF_MS, load_config
from perp_lab.strategy import HOLD, Signal, Strategy
from perp_lab.validate.causality import synthetic_bars as make_bars  # noqa: F401  (tests import it from here)

H = 3_600_000

# The repo's real plugins, found from this file rather than the working directory, so a test
# run from elsewhere fails loudly instead of collecting an empty parameter set.
REPO = Path(__file__).resolve().parents[1]
REAL_PLUGINS = sorted(p.stem for p in (REPO / "strategies").glob("*.py") if not p.stem.startswith("_"))


def flat_bars(n=50, price=100.0, tf_ms=H, start="2023-01-01"):
    t0 = int(pd.Timestamp(start, tz="UTC").value // 10**6)
    ot = t0 + np.arange(n) * tf_ms
    df = pd.DataFrame({"open_time": ot, "open": price, "high": price, "low": price, "close": price,
                       "volume": 1.0, "close_time": ot + tf_ms - 1, "funding_charge": 0.0,
                       "regime": "ranging"})
    df.index = pd.to_datetime(df.open_time, unit="ms", utc=True)
    return df


class Scripted(Strategy):
    """Test helper: emits signals from a {bar_index: Signal} schedule."""
    name = "scripted"
    params = {}

    def __init__(self, schedule):
        self.p = {}
        self.schedule = schedule

    def prepare(self, bars):
        return bars[[]]

    def on_bar(self, ctx):
        return self.schedule.get(ctx.i, HOLD)


@pytest.fixture(autouse=True)
def _sandbox(request, monkeypatch):
    """Tests marked `sandbox` use real workers, and skip where this machine can't confine one; no
    worker may outlive its test. Every other test gets the in-process twin as `client.spawn` and a
    `client.check` that passes."""
    from perp_lab.sandbox import client, confine
    from tests import sandbox_fixtures

    if request.node.get_closest_marker("sandbox") is None:
        monkeypatch.setattr(client, "spawn", sandbox_fixtures.InProcessWorker)
        monkeypatch.setattr(client, "check", lambda cfg: 8)
        yield
        return
    reason = confine.probe()
    if reason is not None:
        pytest.skip(f"sandbox unavailable: {reason}")
    yield
    left = sandbox_fixtures.running_workers()
    if left:
        sandbox_fixtures.kill_workers(left)
        pytest.fail(f"sandbox workers left running: {left}")


@pytest.fixture
def bars():
    return make_bars()


CANDLE_COLS = ["open_time", "open", "high", "low", "close", "volume", "quote_volume", "trades",
               "taker_buy_volume", "taker_buy_quote_volume", "close_time"]


def _write_manifest(d: Path, entries: dict) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / "_manifest.json").write_text(json.dumps(entries, indent=1, sort_keys=True))


def write_market(data_dir, symbols, candles_until="2026-10-15", funding_until="2026-09-30 16:00", tfs=("4h",)):
    """Synthetic candles from 2025-01-01 (4h, or each of `tfs`), 8h funding and hourly OI metrics
    (to `candles_until`), laid out like `lab backfill` output, with its manifests: closed months
    from the monthly files (metrics from daily ones: they have no monthly archive), the last one
    (still open on `candles_until`) from daily files and incomplete. Funding has no daily files, so
    its manifest stops at the last closed month. Every symbol's data starts in the first month, so a
    research span from 2025-01 passes the readiness check (X3) for the reference coin, BTCUSDT."""
    t0, t1 = pd.Timestamp("2025-01-01", tz="UTC"), pd.Timestamp(candles_until, tz="UTC")
    months = pd.period_range(t0.tz_localize(None), t1.tz_localize(None), freq="M")
    manifest = {str(m): {"complete": m != months[-1], "source": "monthly" if m != months[-1] else "daily",
                         "rows": 0} for m in months}
    last_funding = pd.Timestamp(funding_until).to_period("M")
    funding_manifest = {str(m): {"complete": True, "source": "monthly", "rows": 0}
                        for m in months[:-1] if m <= last_funding}
    metrics_manifest = {str(m): {"complete": m != months[-1], "source": "daily", "rows": 0} for m in months}
    for i, s in enumerate(symbols):
        for k, tf in enumerate(tfs):
            n = int((t1 - t0) / pd.Timedelta(milliseconds=TF_MS[tf]))
            b = make_bars(n=n, seed=10 + i + 10 * k, start="2025-01-01", tf_ms=TF_MS[tf])
            d = Path(data_dir) / s / tf
            d.mkdir(parents=True, exist_ok=True)
            b[CANDLE_COLS].reset_index(drop=True).to_parquet(d / "all.parquet")
            _write_manifest(d, manifest)
        ms = pd.date_range("2025-01-01", funding_until, freq="8h", tz="UTC").as_unit("ms").asi8
        fd = Path(data_dir) / s / "funding"
        fd.mkdir(parents=True, exist_ok=True)
        pd.DataFrame({"calc_time": ms, "interval_hours": 8.0, "funding_rate": 0.0001,
                      "funding_time": ms}).to_parquet(fd / "all.parquet")
        _write_manifest(fd, funding_manifest)
        ct = pd.date_range(t0, t1, freq="1h").as_unit("ms").asi8
        oi = 1e6 * np.exp(np.cumsum(np.random.default_rng(100 + i).normal(0.0, 0.002, len(ct))))
        md = Path(data_dir) / s / "metrics"
        md.mkdir(parents=True, exist_ok=True)
        pd.DataFrame({"create_time": ct, "oi": oi, "oi_value": oi * 100.0}).to_parquet(md / "all.parquet")
        _write_manifest(md, metrics_manifest)


def spot_candles(candles, basis=0.0003):
    """Spot candles made from perp `candles`: prices ÷ (1 + basis), so close / spot close − 1 is `basis`
    up to rounding; volumes × 0.3, taker buys half of that; trades halved and rounded."""
    s = candles[CANDLE_COLS].reset_index(drop=True).copy()
    for c in ("open", "high", "low", "close"):
        s[c] = s[c] / (1 + basis)
    for c in ("volume", "quote_volume"):
        s[c] = s[c] * 0.3
        s["taker_buy_" + c] = s[c] / 2
    s["trades"] = np.round(0.5 * s["trades"].astype("float64"))
    return s


def write_spot(data_dir, symbol, tf, candles, manifest, basis=0.0003):
    """`spot_candles(candles, basis)` as data/<symbol>/spot/<tf>/all.parquet, with `manifest` as the
    folder's _manifest.json, laid out like `lab backfill`'s spotKlines output."""
    d = Path(data_dir) / symbol / "spot" / tf
    d.mkdir(parents=True, exist_ok=True)
    spot_candles(candles, basis).to_parquet(d / "all.parquet")
    _write_manifest(d, manifest)


def write_ready_reference(cfg, tfs=("4h",)):
    """The least BTCUSDT data under cfg.data_dir that passes the readiness check (X3) at each of `tfs`:
    a complete manifest entry for every research month of its candles, funding and metrics, and
    candles in the symbol check's month (the last research month) only. For tests whose runs are
    stand-ins and read no bars."""
    from perp_lab.data import columns
    months = columns._research_months(cfg)
    for dataset in (*tfs, "funding", "metrics"):
        _write_manifest(Path(cfg.data_dir) / "BTCUSDT" / dataset,
                        {m: {"complete": True, "source": "monthly", "rows": 0} for m in months})
    last = pd.Period(months[-1], "M")
    for tf in tfs:
        b = make_bars(n=last.days_in_month * 86_400_000 // TF_MS[tf], start=str(last.start_time.date()),
                      tf_ms=TF_MS[tf])
        b[CANDLE_COLS].reset_index(drop=True).to_parquet(Path(cfg.data_dir) / "BTCUSDT" / tf / f"{last}.parquet")


def market_config(tmp_path, **sections):
    """lab.yaml copy on disk over a synthetic 2-symbol market: research 2025-01-01..2026-07-01,
    holdout from 2026-07-01, store and judge files under tmp_path. Dict values update sections."""
    raw = yaml.safe_load(open("config/lab.yaml"))
    raw.update(data_dir=str(tmp_path / "data"), runs_dir=str(tmp_path / "runs"),
               store={"url": f"sqlite:///{tmp_path}/lab.db"}, history_start="2025-01-01",
               universe=["BTC", "ETH"])
    raw["judge"] = {**raw["judge"], "holdout_log": str(tmp_path / "runs" / "holdout_log.jsonl"),
                    "verdicts_dir": str(tmp_path / "runs" / "verdicts")}
    for k, v in sections.items():
        raw[k] = {**raw.get(k, {}), **v} if isinstance(v, dict) else v
    p = tmp_path / "lab.yaml"
    p.write_text(yaml.safe_dump(raw))
    cfg = load_config(p)
    write_market(cfg.data_dir, cfg.symbols)
    return cfg

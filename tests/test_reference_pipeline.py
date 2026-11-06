"""BTC sources in the walk-forward, readiness before any trial, and the market diagnostic (spec
docs/superpowers/specs/2026-11-05-cross-coin-reference-design.md: X3, "How the worker gets it", "Judging";
Testing 10, 12 and 13; Review Focus 4 of the cross-coin plan)."""
import dataclasses
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from perp_lab.data import columns, reference
from perp_lab.data.store import load_bars, load_candles
from perp_lab.engine.metrics import sharpe
from perp_lab.judge.judge import judge
from perp_lab.plugins import PluginSource
from perp_lab.store import db
from perp_lab.validate import benchmarks, pipeline
from perp_lab.validate import walkforward as wfm
from tests.conftest import REPO, market_config, write_market
from tests.judge_fixtures import STRONG, StubValidate, plugins  # noqa: F401

T = lambda s: pd.Timestamp(s, tz="UTC")  # noqa: E731
DONCHIAN = PluginSource.from_text("donchian_atr", (REPO / "strategies" / "donchian_atr.py").read_text())

PEEK = PluginSource.from_text("peek", '''"""Prints what it was sent of BTC's columns, and never trades."""
from perp_lab.strategy import HOLD, Strategy


class Peek(Strategy):
    params = {"n": 1}
    grid = {"n": [1, 2]}

    def prepare(self, bars):
        print("columns", ",".join(sorted(c for c in bars.columns if c.startswith("btc_"))))
        print("close", repr(float(bars.btc_close.sum())), int(bars.btc_close.notna().sum()), len(bars))
        return bars[[]]

    def entry(self, ctx):
        return HOLD
''')


@pytest.fixture
def fresh_wf_caches():
    caches = (wfm._cfg, wfm._bars, wfm._sources)
    for c in caches:
        c.cache_clear()
    yield
    for c in caches:
        c.cache_clear()


def btc_close_at(cfg, tf, open_time) -> np.ndarray:
    """BTC's own close at `tf` on each open time, from its candles."""
    return load_candles(cfg, "BTCUSDT", tf).set_index("open_time").close.reindex(open_time).to_numpy()


# ------------------------------------------------------------------ readiness (X3)

def incomplete_metrics_month(cfg):
    p = cfg.data_dir / "BTCUSDT" / "metrics" / "_manifest.json"
    m = json.loads(p.read_text())
    m["2025-06"]["complete"] = False
    p.write_text(json.dumps(m))


def no_funding_manifest(cfg):
    (cfg.data_dir / "BTCUSDT" / "funding" / "_manifest.json").unlink()


@pytest.mark.parametrize("break_it, problem", [
    (incomplete_metrics_month, "BTCUSDT/metrics 2025-06 is incomplete"),
    (no_funding_manifest, "BTCUSDT/funding 2025-01 has no manifest entry"),
])
def test_validate_refuses_before_register_trial_on_problem(tmp_path, monkeypatch, break_it, problem):
    cfg = market_config(tmp_path)
    break_it(cfg)
    monkeypatch.setattr(pipeline, "walkforward", lambda *a, **k: pytest.fail("the walk-forward ran"))
    with pytest.raises(columns.ReadinessRefused, match=problem):
        pipeline.validate_strategy(cfg, "donchian_atr", "4h", src=DONCHIAN)
    assert db.trial_stats(cfg)[0] == 0 and db.read_table(cfg, db.trials).empty      # N unchanged


def test_a_market_config_passes_readiness_and_its_trial_counts(tmp_path, monkeypatch):
    cfg = market_config(tmp_path)
    assert columns.column_group_problems(cfg, cfg.symbols, "4h") == []
    monkeypatch.setattr(pipeline, "walkforward", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("ran")))
    with pytest.raises(RuntimeError, match="ran"):
        pipeline.validate_strategy(cfg, "donchian_atr", "4h", src=DONCHIAN)
    assert db.trial_stats(cfg)[0] == 1


# ------------------------------------------------------------------ the walk-forward's sources

# workers=2 forks a pool from this multi-threaded test process, as test_run_many_matches_a_direct_backtest does
@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")
@pytest.mark.parametrize("workers", [1, 2])
def test_chunk_worker_receives_btc_columns(tmp_path, fresh_wf_caches, workers):
    cfg = market_config(tmp_path)
    jobs = [(c, "ETHUSDT", "4h", None, None, None) for c in wfm.grid_combos(PEEK.meta.grid)]
    wfm.run_many(cfg, PEEK, jobs, workers, stderr_dir=tmp_path / "err")
    lines = [ln.split() for f in sorted((tmp_path / "err").glob("ETHUSDT-*.txt")) for ln in f.read_text().splitlines()]
    eth = load_candles(cfg, "ETHUSDT", "4h")
    want = btc_close_at(cfg, "4h", eth.open_time)
    assert not np.isnan(want).any()
    sent = [ln for ln in lines if ln[0] == "columns"]
    assert len(sent) == len(jobs) and all(ln[1].split(",") == sorted(reference.COLUMNS) for ln in sent)
    closes = [ln for ln in lines if ln[0] == "close"]
    assert len(closes) == len(jobs)
    for _, total, notna, n in closes:
        assert float(total) == pytest.approx(want.sum(), rel=1e-12) and int(notna) == int(n) == len(eth)


def counting_groups(built: list):
    """GROUPS with each source loader logging the timeframe it builds."""
    def counted(g):
        def load_source(cfg, tf, *a, **k):
            built.append(tf)
            return g.load_source(cfg, tf, *a, **k)
        return dataclasses.replace(g, load_source=load_source)
    return tuple(counted(g) if g.load_source is not None else g for g in columns.GROUPS)


def test_sources_built_once_per_process_per_tf(tmp_path, monkeypatch, fresh_wf_caches):
    cfg = market_config(tmp_path)
    write_market(cfg.data_dir, cfg.symbols, tfs=("4h", "1h"))
    built = []
    monkeypatch.setattr(columns, "GROUPS", counting_groups(built))
    jobs = lambda tf: [(c, s, tf, None, None, None) for c in wfm.grid_combos(PEEK.meta.grid) for s in cfg.symbols]
    wfm.run_many(cfg, PEEK, jobs("4h"), 1)
    assert built == ["4h"]                                  # two coins, two combos: one BTC load
    wfm.run_many(cfg, PEEK, jobs("1h"), 1)
    wfm.run_many(cfg, PEEK, jobs("4h"), 1)
    assert built == ["4h", "1h"]


def test_sources_keyed_by_tf(tmp_path, fresh_wf_caches):
    cfg = market_config(tmp_path)
    write_market(cfg.data_dir, cfg.symbols, tfs=("4h", "1h"))
    wfm._init_worker(str(cfg.path))
    one = wfm._sources("1h")["reference"]                  # a 1h source is cached first
    four = wfm._sources("4h")["reference"]
    assert (one.tf, four.tf) == ("1h", "4h") and wfm._sources("1h")["reference"] is one
    assert one.end == four.end == cfg.holdout_start         # research only: built with no token
    bars = wfm._bars("ETHUSDT", "4h")
    want = btc_close_at(cfg, "4h", bars.open_time)
    np.testing.assert_array_equal(bars.btc_close.to_numpy(), want)
    assert not np.array_equal(btc_close_at(cfg, "1h", bars.open_time), want)     # 1h values would show
    with pytest.raises(ValueError, match="1h"):
        load_bars(cfg, "ETHUSDT", "4h", sources={"reference": one})


# ------------------------------------------------------------------ the market diagnostic

def daily_btc(cfg, a, b) -> pd.Series:
    c = load_candles(cfg, "BTCUSDT", "4h").close.resample("1D").last()
    r = (c / c.shift(1) - 1).dropna()
    return r[(r.index >= a) & (r.index < b)]


def test_btc_exposure_recovers_beta(tmp_path):
    cfg = market_config(tmp_path)
    a, b = T("2025-07-01"), T("2026-07-01")
    r_btc = daily_btc(cfg, a, b)
    noise = pd.Series(np.random.default_rng(3).normal(0.001, 0.01, len(r_btc)), index=r_btc.index)
    inside = 1.5 * r_btc + noise
    oos = pd.concat([pd.Series(0.5, index=[T("2025-06-30")]), inside])    # a day outside [a, b) is ignored
    got = benchmarks.btc_exposure(cfg, oos, "4h", a, b)
    assert set(got) == {"beta", "corr", "hedged_sharpe"}
    assert got["beta"] == pytest.approx(1.5, abs=0.1) and 0.9 < got["corr"] < 1.0
    # √365, beta from the same span; then near the noise's: beta's error times BTC's fixture drift
    # (-0.36%/day) moves it ~0.14 per standard error, while unhedged BTC's fall swamps it
    assert got["hedged_sharpe"] == pytest.approx(sharpe(inside - got["beta"] * r_btc), rel=1e-12)
    assert got["hedged_sharpe"] == pytest.approx(sharpe(noise), abs=0.25)
    assert sharpe(inside) < 0 < sharpe(noise) and abs(sharpe(inside) - sharpe(noise)) > 2


def test_btc_exposure_is_none_without_btc(tmp_path):
    cfg = market_config(tmp_path, universe=["ETH"])
    oos = pd.Series(0.001, index=pd.date_range("2025-07-01", "2026-06-30", freq="D", tz="UTC"))
    assert benchmarks.btc_exposure(cfg, oos, "4h", T("2025-07-01"), T("2026-07-01")) is None


@pytest.mark.filterwarnings("error::RuntimeWarning")
def test_btc_exposure_of_a_portfolio_that_never_traded(tmp_path):
    cfg = market_config(tmp_path)
    flat = pd.Series(0.0, index=pd.date_range("2025-07-01", "2026-06-30", freq="D", tz="UTC"))
    assert benchmarks.btc_exposure(cfg, flat, "4h", T("2025-07-01"), T("2026-07-01")) == {
        "beta": 0.0, "corr": None, "hedged_sharpe": 0.0}


def test_validate_records_btc_exposure_in_the_run_summary(tmp_path, fresh_wf_caches):
    cfg = market_config(tmp_path, validation={"random_seeds": 2, "mc_iterations": 50})
    rec, _ = pipeline.validate_strategy(cfg, "donchian_atr", "4h", workers=1, src=DONCHIAN)
    d = Path(rec["run_dir"])
    port = pd.read_parquet(d / "oos_daily.parquet")["PORTFOLIO"].dropna()
    want = benchmarks.btc_exposure(cfg, port, "4h", T(rec["oos_start"]), T(rec["date_end"]))
    assert want is not None and all(np.isfinite(v) for v in want.values())
    assert json.loads((d / "summary.json").read_text())["btc_exposure"] == pytest.approx(want)


ALL_BTC = {"beta": 1.5, "corr": 0.97, "hedged_sharpe": -2.0}      # "this OOS result is just BTC"
SAME = ("decision", "reasons", "dsr", "sr0", "n_trials", "parent_passed", "beats_parent_frac", "holdout_status",
        "holdout_reason")


def test_market_diagnostic_in_verdict_and_read_by_no_gate(tmp_path, plugins):
    outcomes = {"donchian_atr": ("candidate", STRONG), "donchian_atr_wide": ("candidate", STRONG + 0.001)}
    got = {}
    for store, extra in (("older", {}), ("with_market", {"btc_exposure": ALL_BTC})):
        (tmp_path / store).mkdir()
        cfg = market_config(tmp_path / store)
        v = StubValidate(outcomes, extra=extra)
        js = [judge(cfg, name, "4h", validate=v) for name in ("donchian_atr", "donchian_atr_wide")]
        got[store] = [(j, json.loads(Path(j["verdict_path"]).read_text())) for j in js]
    assert got["older"][0][0]["decision"] == "promote" and got["older"][1][0]["parent_passed"] is True
    for (j0, v0), (j1, v1) in zip(got["older"], got["with_market"]):
        assert v0["diagnostics"]["market"] is None and v1["diagnostics"]["market"] == ALL_BTC
        assert {k: j0[k] for k in SAME} == {k: j1[k] for k in SAME}
        assert {k: v0[k] for k in ("decision", "reasons", "dsr", "n_trials")} == {
            k: v1[k] for k in ("decision", "reasons", "dsr", "n_trials")}
        rest = lambda v: {k: x for k, x in v["diagnostics"].items() if k != "market"}  # noqa: E731
        assert rest(v0) == rest(v1)

"""The spot group's loading and its exact join (spec docs/superpowers/specs/2026-11-05-spot-basis-design.md,
"Alignment, publish lag, gaps", "Holdout" and "Plugin-facing interface"; Testing strategy; Review Focus 1, 2
and 5 of the spot-basis plan)."""
import dataclasses
import logging
import shutil

import numpy as np
import pandas as pd
import pyarrow
import pytest

from perp_lab.config import TF_MS
from perp_lab.data import spot, vision
from perp_lab.data.store import load_bars
from perp_lab.holdout import HoldoutViolation, issue_holdout_token
from tests.conftest import CANDLE_COLS, market_config, spot_candles, write_spot

T = lambda s: pd.Timestamp(s, tz="UTC")  # noqa: E731
H = 3_600_000
FIVE = list(spot.COLUMNS)
SPOT = FIVE[:4]
FROM = ["close", "quote_volume", "taker_buy_quote_volume", "trades"]    # each spot_ column's spot field
HOLDOUT_PRICE = 1e12
MANIFEST = {str(m): {"complete": True, "source": "monthly", "rows": 0}
            for m in pd.period_range("2025-01", "2026-10", freq="M")}


def ms(t) -> int:
    return int((t if isinstance(t, pd.Timestamp) else T(t)).value // 10**6)


@pytest.fixture(autouse=True)
def fresh_warnings(monkeypatch):
    monkeypatch.setattr(spot, "_missing_warned", set())


def perp(open_time, tf="1h"):
    """Perp candles that traded on every bar, whose prices, volumes and trades differ from bar to bar
    and follow from the open time, indexed like `load_bars`' frame."""
    ot = np.asarray(open_time, dtype="int64")
    px = ot / 1e9
    df = pd.DataFrame({"open_time": ot, "open": px, "high": px + 2, "low": px - 2, "close": px + 1,
                       "volume": 10.0, "quote_volume": px * 10, "trades": 100.0 + ot // TF_MS[tf] % 50,
                       "taker_buy_volume": 5.0, "taker_buy_quote_volume": px * 4,
                       "close_time": ot + TF_MS[tf] - 1})
    df.index = pd.to_datetime(df.open_time, unit="ms", utc=True)
    df.index.name = "time"
    return df


def hourly(n=48, start="2025-03-01"):
    return perp(ms(start) + np.arange(n, dtype="int64") * H)


def holed():
    """Hourly bars and their spot with one of each hole: no spot bar at row 8, a zero-trade spot bar at
    12, a zero-trade perp bar at 15 and a perp bar with NaN trades (ccxt-stitched) at 19."""
    bars, s = hourly(), spot_candles(hourly())
    s = s.drop(index=8)
    s.loc[12, "trades"] = 0.0
    bars.loc[bars.index[15], "trades"] = 0.0
    bars.loc[bars.index[19], "trades"] = np.nan
    return bars, s


def spot_at(s, open_time) -> pd.DataFrame:
    """The spot fields behind the four spot_ columns, looked up at each open time (a KeyError if one is missing)."""
    return s.set_index("open_time").loc[np.asarray(open_time), FROM]


# ---- the pure join


def test_spot_names_are_the_spec_values():
    assert (spot.PREFIX, spot.SYNTHETIC_ID) == ("spot_", 2)
    assert spot.COLUMNS == ("spot_close", "spot_quote_volume", "spot_taker_buy_quote_volume", "spot_trades",
                            "basis")


def test_exact_join_on_open_time():
    bars = hourly()
    s = spot_candles(bars)
    out = spot.attach_spot(bars, s.sample(frac=1, random_state=0))       # joined by open time, not position
    np.testing.assert_array_equal(out[SPOT].to_numpy(), spot_at(s, bars.open_time).to_numpy())
    assert np.allclose(out.basis, 0.0003, rtol=0, atol=1e-12)
    assert list(out.columns) == [*bars.columns, *FIVE]
    pd.testing.assert_frame_equal(out[list(bars.columns)], bars)


def test_off_grid_spot_row_is_not_attached():
    bars = hourly()
    s = spot_candles(bars)
    s.loc[10, "open_time"] += 60_000                                     # opens a minute late
    out = spot.attach_spot(bars, s)
    assert len(out) == len(bars) and out.index.equals(bars.index)
    assert out[FIVE].iloc[10].isna().all()
    assert out[FIVE].drop(index=out.index[10]).notna().all().all()


def test_spot_rows_without_a_perp_bar_are_dropped():
    every = hourly(60, start="2025-02-28 18:00")
    bars = every.iloc[6:54].drop(index=every.index[20:23])              # starts later, ends earlier, a gap
    out = spot.attach_spot(bars, spot_candles(every))
    assert out.index.equals(bars.index)
    assert out[FIVE].notna().all().all()
    np.testing.assert_array_equal(out[SPOT].to_numpy(), spot_at(spot_candles(every), bars.open_time).to_numpy())


def test_missing_spot_bar_gives_five_nan():
    bars = hourly()
    out = spot.attach_spot(bars, spot_candles(bars).drop(index=[10, 30]))
    nan = out[FIVE].isna()
    assert nan.iloc[[10, 30]].all().all()
    assert not nan.drop(index=out.index[[10, 30]]).any().any()


def test_zero_trade_spot_bar_gives_five_nan():
    bars = hourly()
    s = spot_candles(bars)
    s.loc[[10, 30], "trades"] = 0.0                                      # the file keeps the last close
    out = spot.attach_spot(bars, s)
    nan = out[FIVE].isna()
    assert nan.iloc[[10, 30]].all().all()
    assert not nan.drop(index=out.index[[10, 30]]).any().any()


def test_zero_trade_perp_bar_gives_nan_basis_keeps_spot():
    bars = hourly()
    s = spot_candles(bars)
    bars.loc[bars.index[10], "trades"] = 0.0
    out = spot.attach_spot(bars, s)
    assert np.isnan(out.basis.iloc[10]) and out.basis.drop(index=out.index[10]).notna().all()
    np.testing.assert_array_equal(out[SPOT].iloc[10].to_numpy(), s.loc[10, FROM].to_numpy(dtype="float64"))


def test_nan_perp_trades_still_get_basis():
    bars = hourly()
    s = spot_candles(bars)
    bars["trades"] = np.nan                                              # ccxt-stitched: not zero
    out = spot.attach_spot(bars, s)
    assert out[FIVE].notna().all().all()
    np.testing.assert_array_equal(out.basis.to_numpy(), (out.close / out.spot_close - 1).to_numpy())


def test_bar_after_a_gap_has_its_own_values():
    bars = hourly()
    s = spot_candles(bars)
    out = spot.attach_spot(bars, s.drop(index=[10, 11, 12]))             # a spot gap
    assert out[FIVE].iloc[10:13].isna().all().all()
    assert out.spot_close.iloc[13] == s.close.iloc[13] != s.close.iloc[9]
    out = spot.attach_spot(bars.drop(index=bars.index[10:13]), s)        # a perp gap
    assert out.spot_close.iloc[10] == s.close.iloc[13]


def test_basis_identity_is_exact():
    bars, s = holed()
    s["close"] = s.close * (1 + np.random.default_rng(3).normal(0.0, 0.002, len(s)))  # basis moves bar to bar
    out = spot.attach_spot(bars, s)
    have = out.basis.notna().to_numpy()
    assert have.sum() == len(bars) - 3 and np.isnan(out.basis.iloc[[8, 12, 15]]).all()
    assert out.basis.nunique() == have.sum()
    np.testing.assert_array_equal(out.basis[have].to_numpy(), (out.close / out.spot_close - 1)[have].to_numpy())


# ---- causality of the join


def test_attach_on_a_prefix_equals_the_prefix():
    bars, s = holed()
    full = spot.attach_spot(bars, s)
    for k in (0, 1, 8, 9, 13, 16, 20, len(bars) - 1):
        pd.testing.assert_frame_equal(spot.attach_spot(bars.iloc[:k], s), full.iloc[:k])


def test_truncating_spot_after_t_changes_nothing_up_to_t():
    bars, s = holed()
    full = spot.attach_spot(bars, s)
    for t in (5, 7, 11, 15, 30):
        cut = spot.attach_spot(bars, s[s.open_time <= bars.open_time.iloc[t]])
        pd.testing.assert_frame_equal(cut.iloc[: t + 1], full.iloc[: t + 1])
        assert cut[FIVE].iloc[t + 1:].isna().all().all()


def test_zero_trade_bar_at_t_plus_1_leaves_t_unchanged():
    bars, s = holed()
    full = spot.attach_spot(bars, s)
    t = 25
    quiet_spot = s.copy()
    quiet_spot.loc[quiet_spot.open_time == bars.open_time.iloc[t + 1], "trades"] = 0.0
    quiet_perp = bars.copy()
    quiet_perp.loc[quiet_perp.index[t + 1], "trades"] = 0.0
    for b, sp in ((bars, quiet_spot), (quiet_perp, s), (quiet_perp, quiet_spot)):
        out = spot.attach_spot(b, sp)
        pd.testing.assert_frame_equal(out.iloc[: t + 1], full.iloc[: t + 1])
        assert np.isnan(out.basis.iloc[t + 1])


def test_duplicate_spot_open_time_keeps_row_count(cfg):
    bars = hourly()
    s = spot_candles(bars)
    overlap = pd.concat([s, s.iloc[10:20]]).iloc[::-1]                   # a daily file over a monthly one
    pd.testing.assert_frame_equal(spot.attach_spot(bars, overlap), spot.attach_spot(bars, s))
    whole = spot.load_spot(cfg, "ETHUSDT", "4h")
    monthly = pd.read_parquet(spot_dir(cfg) / "all.parquet")
    monthly[monthly.open_time >= ms("2025-03-01")].iloc[:18].to_parquet(spot_dir(cfg) / "2025-03-01.parquet")
    pd.testing.assert_frame_equal(spot.load_spot(cfg, "ETHUSDT", "4h"), whole)
    b = research_bars(cfg)
    out = spot.attach(b, "ETHUSDT", "4h", spot.load_source(cfg, "4h"))
    assert out.index.equals(b.index) and out[FIVE].notna().all().all()


def test_spot_columns_float64_even_all_nan():
    bars = hourly()
    s = spot_candles(bars)
    for c in FROM:
        s[c] = s[c].round().astype("int64")
    cases = {"int64 spot": spot.attach_spot(bars, s), "none": spot.attach_spot(bars, None),
             "no rows": spot.attach_spot(bars, s.iloc[:0]),
             "columns only": spot.attach_spot(bars, pd.DataFrame(columns=CANDLE_COLS)),
             "no bars": spot.attach_spot(bars.iloc[:0], s)}
    for name, out in cases.items():
        assert [out[c].dtype for c in FIVE] == [np.dtype("float64")] * 5, name
        n = 0 if name == "no bars" else len(bars)
        assert len(out) == n and out.index.equals(bars.index[:n]), name
    assert cases["int64 spot"][FIVE].notna().all().all()
    for name in ("none", "no rows", "columns only"):
        assert cases[name][FIVE].isna().all().all(), name


def test_spot_shorter_than_perp_gives_nan_tail():
    bars = hourly()
    out = spot.attach_spot(bars, spot_candles(bars).iloc[5:30])          # spot starts later and ends earlier
    inside = np.zeros(len(bars), dtype=bool)
    inside[5:30] = True
    assert out[FIVE][~inside].isna().all().all() and out[FIVE][inside].notna().all().all()
    assert out.index.equals(bars.index)


# ---- loading, the handle and the holdout


@pytest.fixture
def cfg(tmp_path):
    """BTC and ETH at 4h (market_config: 2025-01-01 to 2026-10-15, research to 2026-07-01), ETH's perp
    candles replaced by ones that traded on every bar, and ETH's spot made from them by `write_spot`."""
    c = market_config(tmp_path)
    p = c.data_dir / "ETHUSDT" / "4h" / "all.parquet"
    eth = perp(pd.read_parquet(p).open_time.to_numpy(), "4h")
    eth.reset_index(drop=True).to_parquet(p)
    write_spot(c.data_dir, "ETHUSDT", "4h", eth, MANIFEST)
    return c


def spot_dir(cfg, symbol="ETHUSDT", tf="4h"):
    return cfg.data_dir / symbol / "spot" / tf


def research_bars(cfg, symbol="ETHUSDT", **kw):
    return load_bars(cfg, symbol, "4h", groups=False, with_regime=False, **kw)


def plant_holdout(cfg):
    """An absurd ETH spot close on every bar at or after holdout_start."""
    p = spot_dir(cfg) / "all.parquet"
    s = pd.read_parquet(p)
    s.loc[s.open_time >= ms(cfg.holdout_start), "close"] = HOLDOUT_PRICE
    s.to_parquet(p)


def test_load_spot_past_holdout_without_token_raises(cfg):
    for end in (cfg.holdout_start + pd.Timedelta(milliseconds=1), T("2026-08-01")):
        with pytest.raises(HoldoutViolation):
            spot.load_spot(cfg, "ETHUSDT", "4h", end=end)
    assert spot.load_spot(cfg, "ETHUSDT", "4h", end=cfg.holdout_start).open_time.max() == ms("2026-06-30 20:00")


def test_load_spot_keeps_start_to_end(cfg, tmp_path):
    early = spot_candles(perp(ms("2024-12-01") + np.arange(31 * 6, dtype="int64") * 4 * H, "4h"))
    early.assign(ignore=0)[early.columns[::-1].tolist() + ["ignore"]].to_parquet(spot_dir(cfg) / "2024-12.parquet")
    d = spot.load_spot(cfg, "ETHUSDT", "4h")
    assert list(d.columns) == vision.CANDLE_SCHEMA and d.index.equals(pd.RangeIndex(len(d)))
    assert d.open_time.is_monotonic_increasing and d.open_time.is_unique
    assert (d.open_time.iloc[0], d.open_time.iloc[-1]) == (ms("2025-01-01"), ms("2026-06-30 20:00"))
    d = spot.load_spot(cfg, "ETHUSDT", "4h", start=T("2025-03-01"), end=T("2025-04-01"))
    assert (d.open_time.iloc[0], d.open_time.iloc[-1], len(d)) == (ms("2025-03-01"), ms("2025-03-31 20:00"), 31 * 6)
    tok = issue_holdout_token("lineage", "test", tmp_path / "holdout.jsonl")
    d = spot.load_spot(cfg, "ETHUSDT", "4h", start=T("2024-12-01"), end=T("2026-10-01"), holdout=tok)
    assert (d.open_time.iloc[0], d.open_time.iloc[-1]) == (ms("2024-12-01"), ms("2026-09-30 20:00"))
    assert d.open_time.is_unique and list(d.columns) == vision.CANDLE_SCHEMA


def test_load_source_resolves_end_and_guards(cfg, tmp_path):
    assert [f.name for f in dataclasses.fields(spot.SpotSource)] == ["cfg", "tf", "end", "holdout"]
    assert spot.load_source(cfg, "4h") == spot.SpotSource(cfg, "4h", cfg.holdout_start, None)
    assert spot.load_source(cfg, "4h", start=T("2025-06-01"), end=T("2026-01-01")).end == T("2026-01-01")
    shutil.rmtree(spot_dir(cfg))                                         # building reads no rows
    assert spot.load_source(cfg, "4h").end == cfg.holdout_start
    for token in (None, "not a token"):
        with pytest.raises(HoldoutViolation):
            spot.load_source(cfg, "4h", end=T("2026-08-01"), holdout=token)
    tok = issue_holdout_token("lineage", "test", tmp_path / "holdout.jsonl")
    held = spot.load_source(cfg, "4h", end=T("2026-10-01"), holdout=tok)
    assert (held.cfg, held.tf, held.end, held.holdout) == (cfg, "4h", T("2026-10-01"), tok)


def test_no_holdout_spot_row_reaches_a_research_bar(cfg):
    plant_holdout(cfg)
    bars = research_bars(cfg)
    out = spot.attach(bars, "ETHUSDT", "4h", spot.load_source(cfg, "4h"))
    assert out.index.max() < cfg.holdout_start
    assert out.spot_close.notna().all() and (out.spot_close < HOLDOUT_PRICE).all()
    p = spot_dir(cfg) / "all.parquet"
    s = pd.read_parquet(p)
    s[s.open_time < ms(cfg.holdout_start)].to_parquet(p)                # the holdout rows deleted
    pd.testing.assert_frame_equal(spot.attach(bars, "ETHUSDT", "4h", spot.load_source(cfg, "4h")), out)
    forged = dataclasses.replace(spot.load_source(cfg, "4h"), end=T("2026-10-01"))   # no token: refused at the read
    with pytest.raises(HoldoutViolation):
        spot.attach(bars, "ETHUSDT", "4h", forged)


def test_a_token_built_handle_reads_the_holdout(cfg, tmp_path):
    plant_holdout(cfg)
    tok = issue_holdout_token("lineage", "test", tmp_path / "holdout.jsonl")
    end = T("2026-10-01")
    bars = research_bars(cfg, end=end, holdout=tok)
    out = spot.attach(bars, "ETHUSDT", "4h", spot.load_source(cfg, "4h", end=end, holdout=tok))
    assert out.index.max() == T("2026-09-30 20:00")
    held = out.index >= cfg.holdout_start
    assert held.any() and (out.spot_close[held] == HOLDOUT_PRICE).all()
    assert (out.spot_close[~held] < HOLDOUT_PRICE).all() and out.spot_close.notna().all()
    with pytest.raises(ValueError, match="cut at"):                      # a research handle can't feed them
        spot.attach(bars, "ETHUSDT", "4h", spot.load_source(cfg, "4h"))


def test_missing_spot_folder_gives_nan_and_one_warning_per_coin_tf(cfg, caplog):
    (spot_dir(cfg) / "all.parquet").unlink()                             # the folder keeps its manifest only
    assert not (spot_dir(cfg, "BTCUSDT")).exists()
    for symbol in ("ETHUSDT", "BTCUSDT"):
        with pytest.raises(FileNotFoundError):
            spot.load_spot(cfg, symbol, "4h")
    eth, btc, eth_1h = research_bars(cfg), research_bars(cfg, "BTCUSDT"), hourly()
    four, one = spot.load_source(cfg, "4h"), spot.load_source(cfg, "1h")
    with caplog.at_level(logging.WARNING):
        a = spot.attach(eth, "ETHUSDT", "4h", four)
        assert len([r for r in caplog.records if r.name == "perp_lab.data.spot"]) == 1
        spot.attach(eth, "ETHUSDT", "4h", four)                          # the same coin and tf: no second
        spot.attach(btc, "BTCUSDT", "4h", four)
        spot.attach(eth_1h, "ETHUSDT", "1h", one)
        spot.attach(btc, "BTCUSDT", "4h", four)
    assert a.index.equals(eth.index) and a[FIVE].isna().all().all()
    assert all(a[c].dtype == "float64" for c in FIVE)
    warned = [r.getMessage() for r in caplog.records if r.name == "perp_lab.data.spot"]
    assert len(warned) == 3
    for msg, (symbol, tf) in zip(warned, (("ETHUSDT", "4h"), ("BTCUSDT", "4h"), ("ETHUSDT", "1h"))):
        assert f"{symbol} {tf}" in msg and "spot" in msg


def test_excluded_coin_is_all_nan_and_reads_nothing(cfg, caplog):
    (spot_dir(cfg) / "planted.parquet").write_bytes(b"not a parquet file")
    bars = research_bars(cfg)
    src = spot.load_source(cfg, "4h")
    with pytest.raises(pyarrow.ArrowInvalid):                            # reading the folder would fail
        spot.attach(bars, "ETHUSDT", "4h", src)
    for written in ("ETH", "ETHUSDT"):
        cfg.raw["column_groups"]["spot"]["exclusions"] = [written]
        assert spot.excluded(cfg, "ETHUSDT") and not spot.excluded(cfg, "BTCUSDT")
        with caplog.at_level(logging.WARNING):
            out = spot.attach(bars, "ETHUSDT", "4h", src)
        assert out.index.equals(bars.index) and out[FIVE].isna().all().all()
        assert all(out[c].dtype == "float64" for c in FIVE)
    assert not [r for r in caplog.records if r.name == "perp_lab.data.spot"]
    cfg.raw["column_groups"]["spot"] = {"known_gaps": []}
    assert not spot.excluded(cfg, "ETHUSDT")
    del cfg.raw["column_groups"]
    assert not spot.excluded(cfg, "ETHUSDT")


def test_attach_refuses_other_tf_or_short_source(cfg):
    bars = research_bars(cfg)
    with pytest.raises(ValueError, match="a 1h spot source given to ETHUSDT 4h bars"):
        spot.attach(bars, "ETHUSDT", "4h", spot.load_source(cfg, "1h"))
    last_close = pd.Timestamp(int(bars.close_time.max()), unit="ms", tz="UTC")
    for end in (last_close, T("2026-01-01")):
        with pytest.raises(ValueError, match="cut at"):
            spot.attach(bars, "ETHUSDT", "4h", spot.load_source(cfg, "4h", end=end))
    just_past = spot.load_source(cfg, "4h", end=last_close + pd.Timedelta(milliseconds=1))
    whole = spot.attach(bars, "ETHUSDT", "4h", just_past)
    cut = bars[bars.open_time < ms("2026-01-01")]                       # bars that end where the source does
    early = spot.load_source(cfg, "4h", end=T("2026-01-01"))
    pd.testing.assert_frame_equal(spot.attach(cut, "ETHUSDT", "4h", early), whole.loc[cut.index])
    cfg.raw["column_groups"]["spot"]["exclusions"] = ["ETH"]               # excluded, still refused
    with pytest.raises(ValueError, match="a 1h spot source"):
        spot.attach(bars, "ETHUSDT", "4h", spot.load_source(cfg, "1h"))


def test_sources_name_spot_tf_dirs(cfg):
    assert spot.dataset("1h") == "spot/1h"
    assert spot.sources(cfg, ["BTCUSDT", "ETHUSDT"], "15m") == [("BTCUSDT", "spot/15m"), ("ETHUSDT", "spot/15m")]
    for symbol, d in spot.sources(cfg, cfg.symbols, "4h"):
        assert cfg.data_dir / symbol / d == vision.dataset_dir(cfg.data_dir, symbol, "spotKlines", "4h")

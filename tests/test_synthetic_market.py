import numpy as np
import pandas as pd
import pytest

from perp_lab.config import TF_MS
from perp_lab.data import reference, spot
from perp_lab.validate.causality import NAN_CUTS, synthetic_bars, with_moving_columns
from perp_lab.validate.synthetic_market import DAYS, MARKET_VERSION, SYMBOLS, VOL_SCALE, market


def test_the_market_is_one_path_at_every_timeframe():
    m5, m4h = market("5m")["BTCUSDT"], market("4h")["BTCUSDT"]
    assert m4h.close.iloc[0] == m5.close.iloc[47] and len(m4h) * 48 == len(m5)


def test_market_5m_exists():
    m = market("5m")
    assert tuple(m) == SYMBOLS
    assert all(len(df) == 180 * 288 for df in m.values())


def test_15m_is_resampled_from_5m():
    m5, m15 = market("5m")["BTCUSDT"], market("15m")["BTCUSDT"]
    assert len(m15) * 3 == len(m5)
    assert m15.close.iloc[0] == m5.close.iloc[2] and m15.open.iloc[1] == m5.open.iloc[3]
    assert m15.high.iloc[1] == m5.high.iloc[3:6].max() and m15.volume.iloc[1] == pytest.approx(m5.volume.iloc[3:6].sum())


def test_the_market_is_fixed():
    assert (MARKET_VERSION, SYMBOLS, DAYS) == (5, ("BTCUSDT", "SYN2", "SYN3", "SYN4"), 180)
    m = market("5m")
    assert tuple(m) == SYMBOLS and market("5m") is m                 # built once per timeframe
    groups = list(reference.COLUMNS + spot.COLUMNS)
    base = with_moving_columns(synthetic_bars(n=DAYS * 288, seed=102, start="2023-01-01", funding_every=96,
                                              tf_ms=300_000, vol_scale=VOL_SCALE), seed=202)
    base = base.drop(columns=groups)
    pd.testing.assert_frame_equal(m["BTCUSDT"].drop(columns=groups), with_moving_columns(
        synthetic_bars(n=DAYS * 288, seed=100, start="2023-01-01", funding_every=96, tf_ms=300_000,
                       vol_scale=VOL_SCALE), seed=200).drop(columns=groups))
    assert m["SYN3"].drop(columns=groups).shape == base.shape       # mixed, so not the seed-102 path
    assert m["BTCUSDT"].index[0] == pd.Timestamp("2023-01-01", tz="UTC")
    assert not m["BTCUSDT"].close.equals(m["SYN2"].close)                # one path per symbol


def test_resampling_aggregates_each_column_by_its_meaning():
    b, h = market("5m")["SYN2"].iloc[48:96], market("4h")["SYN2"].iloc[1]
    assert (h.open, h.high, h.low, h.close) == (b.open.iloc[0], b.high.max(), b.low.min(), b.close.iloc[-1])
    for c in ("volume", "quote_volume", "trades", "taker_buy_volume", "taker_buy_quote_volume", "funding_charge"):
        assert h[c] == pytest.approx(b[c].sum(), rel=1e-12), c
    for c in ("funding_last", "funding_interval_h", "oi", "oi_value", "regime", "regime_adx", "close_time"):
        assert h[c] == b[c].iloc[-1], c
    assert h.open_time == b.open_time.iloc[0] == market("4h")["SYN2"].index[1].value // 10**6
    assert h.close_time - h.open_time == TF_MS["4h"] - 1


def test_every_timeframe_has_every_column_with_no_gaps():
    cols = list(market("5m")["BTCUSDT"].columns)
    # spot is drawn on each coin's 5m path; the btc_ columns are joined after resampling, so they stay last
    assert cols[-len(reference.COLUMNS) - len(spot.COLUMNS):] == list(spot.COLUMNS + reference.COLUMNS)
    for tf in ("15m", "1h", "4h", "1d"):
        for s, df in market(tf).items():
            assert list(df.columns) == cols and len(df) == DAYS * 86_400_000 // TF_MS[tf]
            assert df.drop(columns=["regime", *spot.COLUMNS]).notna().all().all()     # spot has its NaN stretches
            assert (np.diff(df.open_time.to_numpy()) == TF_MS[tf]).all()


def test_funding_is_charged_once_whatever_the_timeframe():
    for s in SYMBOLS:
        total = market("5m")[s].funding_charge.sum()
        assert market("1h")[s].funding_charge.sum() == pytest.approx(total)
        assert market("4h")[s].funding_charge.sum() == pytest.approx(total)


@pytest.mark.parametrize("tf", ["1m", "7m", "2w"])
def test_no_market_below_5m(tf):      # finer than 5m, or unknown
    with pytest.raises(ValueError, match="no synthetic market"):
        market(tf)


def test_synthetic_bars_vol_scale_defaults_to_one_and_scales_the_noise():
    a = synthetic_bars(n=4000, seed=3)
    pd.testing.assert_frame_equal(a, synthetic_bars(n=4000, seed=3, vol_scale=1.0))
    half = synthetic_bars(n=4000, seed=3, vol_scale=0.5)               # the same draws, halved
    sd = lambda b: np.log(b.close).diff().std()
    assert sd(half) == pytest.approx(0.5 * sd(a), rel=1e-6)


def test_the_5m_market_is_scaled_to_the_old_15m_volatility():
    assert VOL_SCALE == pytest.approx(3 ** -0.5)


def test_the_reference_coin_keeps_version_2s_syn1_prices():
    assert SYMBOLS[0] == "BTCUSDT"
    m5, h4 = market("5m")["BTCUSDT"], market("4h")["BTCUSDT"]
    got5 = [m5.close.iloc[i] for i in (0, 1, 100, 5000, 51839)]
    assert got5 == [99.14708109951353, 100.05719825457314, 84.41346995329614, 205.3197593235598, 9493.174574954965]
    assert [h4.close.iloc[i] for i in (0, 50, 1079)] == [91.00665693057778, 442.51733171293176, 9493.174574954965]


@pytest.mark.parametrize("symbol, closes_5m, closes_4h", [
    ("SYN2", [99.0648545518708, 99.51802287843947, 84.32424885171137, 86.94158325502612, 6.06493612424289],
     [89.66268779565456, 319.1308622965845, 6.06493612424289]),
    ("SYN3", [100.17823441150033, 101.10839104176925, 89.99760278093159, 435.8064615645195, 110.03039149741292],
     [95.9129409226911, 548.624434058022, 110.03039149741292]),
    ("SYN4", [99.46554577146432, 100.40292992026988, 93.96951781372255, 88.44041644832852, 5389.997531424877],
     [95.9094062364508, 170.92847385308355, 5389.997531424877]),
])
def test_syn2_to_syn4_keep_version_3s_prices(symbol, closes_5m, closes_4h):
    # captured from version 3 as built: a change to the mixing or to the seeds moves these and needs a bump
    m5, h4 = market("5m")[symbol], market("4h")[symbol]
    assert [m5.close.iloc[i] for i in (0, 1, 100, 5000, 51839)] == closes_5m
    assert [h4.close.iloc[i] for i in (0, 50, 1079)] == closes_4h


def test_syn2_to_syn4_follow_the_reference_in_order():
    m = market("5m")
    r = {s: np.log(m[s].close).diff().dropna() for s in SYMBOLS}
    corr = [r["BTCUSDT"].corr(r[s]) for s in SYMBOLS[1:]]
    assert corr == sorted(corr) and len(set(corr)) == 3
    for c, rho in zip(corr, (0.55, 0.7, 0.85)):
        assert c == pytest.approx(rho, abs=0.02)
    assert r["SYN4"].std() == pytest.approx(r["BTCUSDT"].std(), rel=0.03)         # same variance


@pytest.mark.parametrize("tf", ["5m", "1h", "4h"])
def test_every_frames_btc_columns_are_the_reference_coins_by_open_time(tf):
    m = market(tf)
    btc = m["BTCUSDT"].set_index("open_time")
    own = {"btc_" + c: c for c in reference.BAR_COLUMNS + reference.ASOF_COLUMNS}
    for s, df in m.items():
        d = df.set_index("open_time")
        for col, src in own.items():
            np.testing.assert_array_equal(d[col].to_numpy(), btc[src].to_numpy(), err_msg=f"{s} {col}")


def test_reference_symbol_is_the_configs_btc():
    from perp_lab.config import load_config
    cfg = load_config()
    assert reference.SYMBOL == "BTC" + cfg["quote"] and "BTC" in cfg["universe"]


# ------------------------------------------------------------------ the spot group (version 4)

FIVE = list(spot.COLUMNS)
SPOT_SUMMED = ["spot_quote_volume", "spot_taker_buy_quote_volume", "spot_trades"]


def test_market_version_is_5():
    assert MARKET_VERSION == 5   # 5: spot calibrated (plan Task 11)


@pytest.mark.parametrize("tf", ["15m", "1h", "4h"])
def test_market_spot_resampling(tf):
    k = TF_MS[tf] // TF_MS["5m"]
    for s in SYMBOLS:
        m5, m = market("5m")[s], market(tf)[s]
        bucket = m5.groupby(np.arange(len(m5)) // k)
        for c in SPOT_SUMMED:                         # min_count=1: a bucket with no spot bar is NaN, not 0
            want = bucket[c].sum(min_count=1).to_numpy()
            assert np.isnan(want).any() and not (want == 0).any()
            np.testing.assert_allclose(m[c].to_numpy(), want, rtol=1e-12, err_msg=f"{s} {c}")
        last = m5.iloc[k - 1::k]                      # the bucket's last 5m bar: never a NaN row unless all are
        np.testing.assert_array_equal(m.spot_close.to_numpy(), last.spot_close.to_numpy(), err_msg=s)
        np.testing.assert_array_equal(m.basis.to_numpy(), (m.close / m.spot_close - 1).to_numpy(), err_msg=s)
        np.testing.assert_array_equal(m.basis.to_numpy(), last.basis.to_numpy(), err_msg=s)


@pytest.mark.parametrize("tf", ["5m", "15m", "1h", "4h"])
def test_market_spot_nan_stretches_whole(tf):
    # ruling S5: at 15m, 1h and 4h the NaN bars are exactly the buckets inside the 5m stretches; at 5m the
    # row before the lookahead check's first cut is NaN too, and every coarser bucket holding it is not
    k = TF_MS[tf] // TF_MS["5m"]
    want = [i for a, b in spot.NAN_STRETCHES for i in range(a // k, b // k)]
    if tf == "5m":
        want = sorted(want + [cut - 1 for cut in NAN_CUTS])
    for s, df in market(tf).items():
        for c in FIVE:
            assert np.flatnonzero(df[c].isna().to_numpy()).tolist() == want, (s, c)


# hash_pandas_object sums of each frame without the spot columns, captured from version 3 before spot was
# registered: the spot columns change no other column, at any timeframe
PRE_SPOT_HASHES = {
    "5m": [17969628142834356824, 14986892942727429196, 7851917052932312749, 10407428671937934215],
    "15m": [8524389208028684765, 3518741898660839041, 4904552601250995757, 17006380583731575392],
    "1h": [15142064789493710281, 13300092846650806367, 4286491101074938586, 5373710348511338589],
    "4h": [13023897928200926206, 2164061988692270364, 11915280444696585250, 823188312973466877],
    "1d": [2382705713924388717, 2057505535612692657, 4345179035570960741, 12010016461512015824],
}


@pytest.mark.parametrize("tf", list(PRE_SPOT_HASHES))
def test_market_pre_existing_columns_unchanged(tf):
    m = market(tf)
    got = [int(pd.util.hash_pandas_object(df.drop(columns=FIVE)).sum()) for df in m.values()]
    assert got == PRE_SPOT_HASHES[tf]

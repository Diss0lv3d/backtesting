import numpy as np
import pandas as pd

from perp_lab.indicators import reference_frame, rolling_beta, volume_vs_same_hour

_BAR = ("open", "high", "low", "close", "quote_volume", "taker_buy_quote_volume")
_HOUR = 3_600_000


def _bars(n=24 * 7 * 6, seed=0):
    rng = np.random.default_rng(seed)
    ot = np.arange(n, dtype=np.int64) * _HOUR + 1_700_000_000_000 // _HOUR * _HOUR
    df = pd.DataFrame({"open_time": ot, "close_time": ot + _HOUR - 1,
                       "volume": rng.uniform(1, 2, n), "close": rng.uniform(1, 2, n)},
                      index=pd.RangeIndex(100, 100 + n))
    for c in _BAR:
        df["btc_" + c] = rng.uniform(1, 100, n)
    return df


def test_rolling_beta_of_x_on_itself_is_exactly_one():
    x = pd.Series(np.random.default_rng(1).normal(size=500))
    b = rolling_beta(x, x, 20).dropna()
    assert len(b) > 0
    assert (b == 1.0).all()


def test_residual_against_itself_is_exactly_zero():
    x = pd.Series(np.random.default_rng(2).normal(size=500))
    r = (x - rolling_beta(x, x, 20) * x).dropna()
    assert len(r) > 0 and (r == 0.0).all()


def test_rolling_beta_nan_on_zero_variance():
    x = pd.Series([1.0] * 30)
    y = pd.Series(np.random.default_rng(3).normal(size=30))
    assert rolling_beta(y, x, 5).isna().all()


def test_rolling_beta_recovers_slope():
    x = pd.Series(np.random.default_rng(4).normal(size=200))
    assert np.allclose(rolling_beta(3 * x, x, 20).dropna(), 3.0)


def test_volume_vs_same_hour_on_reference_frame():
    bars = _bars()
    hand = pd.DataFrame({"open_time": bars.open_time, "close_time": bars.close_time,
                         **{c: bars["btc_" + c] for c in _BAR}}, index=bars.index)
    got = volume_vs_same_hour(reference_frame(bars), 4, col="quote_volume")
    pd.testing.assert_series_equal(got, volume_vs_same_hour(hand, 4, col="quote_volume"))


def test_reference_frame_keeps_index_and_times():
    bars = _bars(50)
    ref = reference_frame(bars)
    assert list(ref.columns) == ["open_time", "close_time", *_BAR]
    assert ref.index.equals(bars.index)
    assert (ref.open_time == bars.open_time).all() and (ref.close_time == bars.close_time).all()
    assert (ref.close == bars.btc_close).all()
    assert list(reference_frame(bars.rename(columns=lambda c: c.replace("btc_", "x_")), "x_").columns) == list(ref.columns)

"""The bar-range volatility and the same-hour volume ratio: exact on hand-built bars, causal on random ones."""

import math

import numpy as np
import pandas as pd
import pytest

from perp_lab.indicators import parkinson_vol
from tests.conftest import flat_bars, make_bars

K = 1 / math.sqrt(4 * math.log(2))      # Parkinson's factor on ln(high/low), ~0.601


def _ranges(highs, lows):
    b = flat_bars(n=len(highs))
    b["high"], b["low"] = np.asarray(highs, float), np.asarray(lows, float)
    return b


# ------------------------------------------------------------------ parkinson_vol

def test_parkinson_of_one_bar_is_0_601_times_its_log_range():
    v = parkinson_vol(_ranges([110.0, 100.0], [100.0, 100.0]))
    assert K == pytest.approx(0.601, abs=5e-4)
    assert v.iloc[0] == pytest.approx(K * math.log(1.1), rel=1e-12)
    assert v.iloc[1] == 0.0                                  # high == low: no range, no volatility
    assert v.index.equals(flat_bars(n=2).index)


def test_parkinson_over_n_bars_is_the_root_of_the_mean_variance_up_to_t():
    v = parkinson_vol(_ranges([110.0, 105.0, 120.0], [100.0, 100.0, 100.0]), n=2)
    var = [(K * math.log(r)) ** 2 for r in (1.1, 1.05, 1.2)]
    assert math.isnan(v.iloc[0])                             # one bar is not two
    assert v.iloc[1] == pytest.approx(math.sqrt((var[0] + var[1]) / 2), rel=1e-12)
    assert v.iloc[2] == pytest.approx(math.sqrt((var[1] + var[2]) / 2), rel=1e-12)


def test_parkinson_is_nan_where_high_or_low_is_not_positive():
    b = _ranges([110.0, 0.0, 110.0, -1.0, 110.0, 110.0], [100.0, 100.0, 0.0, -2.0, 100.0, 100.0])
    assert parkinson_vol(b).isna().tolist() == [False, True, True, True, False, False]
    assert parkinson_vol(b, n=2).isna().tolist() == [True, True, True, True, True, False]


def test_parkinson_estimates_sigma_and_beats_close_to_close():
    """On a fine-grained random walk with per-bar sigma 1%: the mean Parkinson variance is about sigma^2
    (a discrete path's range is a little short), and it scatters ~5x less than the squared return."""
    rng = np.random.default_rng(0)
    steps = rng.normal(0, 0.01 / math.sqrt(2000), (3000, 2000))
    path = np.cumsum(steps, axis=1)
    hi, lo = np.maximum(path.max(axis=1), 0), np.minimum(path.min(axis=1), 0)
    b = _ranges(100 * np.exp(hi), 100 * np.exp(lo))
    pv2 = parkinson_vol(b).to_numpy() ** 2
    r2 = path[:, -1] ** 2
    assert 0.92 < pv2.mean() / 0.01 ** 2 < 1.03
    assert 4.0 < r2.var() / pv2.var() < 6.5


# ------------------------------------------------------------------ causality on random bars

CUTS = (1, 337, 1100, 2001, 2999)
SCRAMBLED = ("open", "high", "low", "close", "volume", "quote_volume", "trades", "taker_buy_volume")


def _scrambled_from(bars, k, seed):
    """A copy of bars in which every row from k on has new prices, ranges and volumes."""
    out = bars.copy()
    rng = np.random.default_rng(seed)
    for c in SCRAMBLED:
        out[c] = out[c].astype(float)
        out.iloc[k:, out.columns.get_loc(c)] = rng.uniform(1, 1000, len(out) - k)
    return out


def assert_causal(fn, bars):
    """The value at t is the same when the bars after t are removed or altered."""
    full = fn(bars)
    assert full.notna().sum() > len(bars) // 2              # it says something, so the check means something
    for k in CUTS:
        pd.testing.assert_series_equal(fn(bars.iloc[:k]), full.iloc[:k], check_exact=True)
        pd.testing.assert_series_equal(fn(_scrambled_from(bars, k, seed=k)).iloc[:k], full.iloc[:k],
                                       check_exact=True)


@pytest.mark.parametrize("n", [1, 24])
def test_parkinson_is_causal(n):
    assert_causal(lambda b: parkinson_vol(b, n), make_bars(seed=5))

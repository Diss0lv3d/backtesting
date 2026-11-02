"""The bar-range volatility and the same-hour volume ratio: exact on hand-built bars, causal on random ones."""

import math

import numpy as np
import pandas as pd
import pytest

from perp_lab.indicators import parkinson_vol, volume_vs_same_hour
from perp_lab.plugins import PluginSource
from perp_lab.validate.causality import causality_failures, with_moving_columns
from tests.conftest import flat_bars, make_bars
from tests.test_store import cfg  # noqa: F401

K = 1 / math.sqrt(4 * math.log(2))      # Parkinson's factor on ln(high/low), ~0.601
M15, H, H4 = 900_000, 3_600_000, 14_400_000
WEEK = 7 * 24                           # hourly bars in a week; 2024-01-01 00:00 UTC is a Monday


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


# ------------------------------------------------------------------ volume_vs_same_hour

def _weeks_of(tf_ms, weeks, volume):
    """Bars from Monday 2024-01-01 00:00 UTC, `weeks` weeks long, volume from volume(week, bar_of_week)."""
    per_week = 7 * 86_400_000 // tf_ms
    b = flat_bars(n=weeks * per_week, tf_ms=tf_ms, start="2024-01-01")
    i = np.arange(len(b))
    b["volume"] = [float(volume(w, j)) for w, j in zip(i // per_week, i % per_week)]
    return b


def test_a_monday_spike_is_read_against_earlier_mondays_not_quiet_sundays():
    mondays_13h = {0: 80, 1: 100, 2: 120, 3: 200, 4: 330}
    b = _weeks_of(H, 5, lambda w, j: mondays_13h[w] if j == 13 else 1.0)
    v = volume_vs_same_hour(b)                                         # weeks=4: needs 2 earlier values
    at = [w * WEEK + 13 for w in range(5)]
    assert np.isnan(v.iloc[at[0]]) and np.isnan(v.iloc[at[1]])           # 0 and 1 earlier Mondays
    assert v.iloc[at[2]] == pytest.approx(120 / 90, rel=1e-12)             # median of 80, 100
    assert v.iloc[at[3]] == pytest.approx(200 / 100, rel=1e-12)            # median of 80, 100, 120
    assert v.iloc[at[4]] == pytest.approx(3.0, rel=1e-12)                  # 330 / median(80, 100, 120, 200)
    assert b.volume.iloc[at[4]] / b.volume.iloc[:at[4]].mean() > 100      # an all-hours mean reads the clock
    sunday_13h = 4 * WEEK + 6 * 24 + 13
    assert b.index[sunday_13h].day_name() == "Sunday" and v.iloc[sunday_13h] == 1.0
    assert volume_vs_same_hour(b, weeks=2).iloc[at[4]] == pytest.approx(330 / 160, rel=1e-12)  # weeks 2 and 3


def test_on_15m_bars_the_hour_holds_every_quarter_of_earlier_weeks_but_not_this_one():
    monday_13h = {(0, j): 10.0 * (j + 1) for j in range(4)} | {(1, j): 10.0 * (j + 5) for j in range(4)}
    monday_13h |= {(2, 0): 1000.0, (2, 1): 1000.0, (2, 2): 90.0}       # this hour's earlier quarters are huge
    b = _weeks_of(M15, 3, lambda w, j: monday_13h.get((w, j - 52), 1.0))    # Monday 13:00 is quarter 52
    v = volume_vs_same_hour(b, weeks=2)
    q = 7 * 96
    assert v.iloc[q + 52] == pytest.approx(50 / 25, rel=1e-12)             # week 0's 10, 20, 30, 40
    assert v.iloc[q + 53] == pytest.approx(60 / 25, rel=1e-12)
    assert v.iloc[2 * q + 54] == pytest.approx(90 / 45, rel=1e-12)         # weeks 0 and 1: 10..80, no 1000s


@pytest.mark.parametrize("tf_ms", [M15, H, H4])
def test_every_bar_of_a_doubled_week_reads_2_on_any_timeframe(tf_ms):
    b = _weeks_of(tf_ms, 5, lambda w, j: w + 1)                        # weeks 0..3 hold 1..4, week 4 holds 5
    v = volume_vs_same_hour(b)
    per_week = len(b) // 5
    assert v.iloc[:per_week].isna().all()                              # nothing earlier in week 0
    assert (v.iloc[4 * per_week:] == 2.0).all()                        # 5 / median(1, 2, 3, 4)


def test_it_is_nan_until_max_2_weeks_halved_earlier_values():
    b = _weeks_of(H, 5, lambda w, j: w + 1)
    v = volume_vs_same_hour(b, weeks=6)                                # needs 3 earlier values
    assert v.iloc[:3 * WEEK].isna().all()
    assert (v.iloc[3 * WEEK:4 * WEEK] == 4 / 2).all()                  # 4 / median(1, 2, 3)
    assert volume_vs_same_hour(b, weeks=1).isna().all()                # one earlier value is never 2


def test_a_zero_median_or_a_missing_value_is_nan():
    b = _weeks_of(H, 3, lambda w, j: 5.0 if w == 2 else (0.0 if j == 7 else 1.0))
    b.loc[b.index[2 * WEEK + 9], "volume"] = np.nan
    v = volume_vs_same_hour(b, weeks=2)
    assert np.isnan(v.iloc[2 * WEEK + 7]) and np.isnan(v.iloc[2 * WEEK + 9])
    assert v.iloc[2 * WEEK + 8] == 5.0


def test_it_reads_the_column_named_by_col():
    b = with_moving_columns(make_bars(seed=3))
    for c in ("trades", "quote_volume", "taker_buy_volume"):
        got = volume_vs_same_hour(b, col=c)
        assert got.notna().sum() > 2000 and not got.equals(volume_vs_same_hour(b))
        assert got.equals(volume_vs_same_hour(b.assign(volume=b[c])))


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


@pytest.mark.parametrize("tf_ms", [M15, H, H4])
@pytest.mark.parametrize("weeks", [2, 4])
def test_volume_vs_same_hour_is_causal(tf_ms, weeks):
    assert_causal(lambda b: volume_vs_same_hour(b, weeks), make_bars(seed=6, tf_ms=tf_ms))


PROBE_TEXT = '''"""Buys a volume spike for its hour of the week, bracketed by bar-range volatility."""
from perp_lab.indicators import parkinson_vol, volume_vs_same_hour
from perp_lab.strategy import HOLD, Strategy, enter


class Probe(Strategy):
    params = {}

    def prepare(self, bars):
        return bars[[]].assign(pv=parkinson_vol(bars, 24), vr=volume_vs_same_hour(bars))

    def entry(self, ctx):
        if not (ctx["vr"] > 1.5 and ctx["pv"] > 0):
            return HOLD
        c = ctx["close"]
        return enter(1, c * (1 - 3 * ctx["pv"]), c * (1 + 3 * ctx["pv"]))
'''
PROBE = PluginSource.from_text("probe", PROBE_TEXT)


def test_the_labs_lookahead_check_passes_a_plugin_built_on_both(cfg):
    assert causality_failures(cfg, PROBE, make_bars(seed=1)) == []

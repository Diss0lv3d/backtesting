"""Pure as-of helpers: one rule for a coin's own funding/OI and for BTC reference columns."""
import numpy as np
import pandas as pd

from perp_lab.data import store

H = 3_600_000
T0 = 1_700_000_000_000 - (1_700_000_000_000 % H)


def _candles(n=6):
    ot = T0 + np.arange(n, dtype="int64") * H
    return pd.DataFrame({"open_time": ot, "close_time": ot + H - 1})


def _funding():
    ft = np.array([T0 - H, T0 + 2 * H, T0 + 2 * H + 1 + H, T0 + 4 * H - 1], dtype="int64")
    return pd.DataFrame({"funding_time": ft,
                         "funding_rate": [0.0001, -0.0002, 0.0003, 0.0004],
                         "interval_hours": [8.0, 8.0, 4.0, 4.0]})


def _metrics():
    ct = np.array([T0 - 10 * H, T0 + H - 300_000, T0 + 2 * H - 299_999, T0 + 4 * H], dtype="int64")
    return pd.DataFrame({"create_time": ct, "oi": [10.0, 11.0, 12.0, 13.0],
                         "oi_value": [100.0, 110.0, 120.0, 130.0]})

# Captured by running the pre-refactor align_funding / align_metrics on the fixtures above.
PIN_CHARGE = np.array([0.0, 0.0, -0.0002, 0.0007, 0.0, 0.0])
PIN_LAST = np.array([0.0001, 0.0001, -0.0002, 0.0004, 0.0004, 0.0004])
PIN_IH = np.array([8.0, 8.0, 8.0, 4.0, 4.0, 4.0])
PIN_OI = np.array([10.0, 11.0, 12.0, 12.0, 13.0, 13.0])
PIN_OIV = np.array([100.0, 110.0, 120.0, 120.0, 130.0, 130.0])


def test_align_funding_unchanged():
    out = store.align_funding(_candles(), _funding(), "1h")
    np.testing.assert_array_equal(out.funding_charge.values, PIN_CHARGE)
    np.testing.assert_array_equal(out.funding_last.values, PIN_LAST)
    np.testing.assert_array_equal(out.funding_interval_h.values, PIN_IH)


def test_align_metrics_unchanged():
    out = store.align_metrics(_candles(), _metrics())
    np.testing.assert_array_equal(out.oi.values, PIN_OI)
    np.testing.assert_array_equal(out.oi_value.values, PIN_OIV)


def test_funding_last_at_boundary():
    f = pd.DataFrame({"funding_time": [1000], "funding_rate": [0.5], "interval_hours": [8.0]})
    rate, ih = store.funding_last_at(np.array([1000, 999], dtype="int64"), f)
    assert rate[0] == 0.5 and ih[0] == 8.0
    assert np.isnan(rate[1]) and np.isnan(ih[1])
    f2 = pd.DataFrame({"funding_time": [1001], "funding_rate": [0.5], "interval_hours": [8.0]})
    rate, _ = store.funding_last_at(np.array([1000], dtype="int64"), f2)
    assert np.isnan(rate[0])


def test_metrics_at_lag_boundary():
    close = 10_000_000
    m = pd.DataFrame({"create_time": [close - 300_000], "oi": [1.0], "oi_value": [2.0]})
    oi, oiv = store.metrics_at(np.array([close], dtype="int64"), m)
    assert oi[0] == 1.0 and oiv[0] == 2.0
    m = pd.DataFrame({"create_time": [close - 300_000 + 1], "oi": [1.0], "oi_value": [2.0]})
    oi, oiv = store.metrics_at(np.array([close], dtype="int64"), m)
    assert np.isnan(oi[0]) and np.isnan(oiv[0])
    oi, oiv = store.metrics_at(np.array([close], dtype="int64"), None)
    assert np.isnan(oi[0]) and np.isnan(oiv[0])

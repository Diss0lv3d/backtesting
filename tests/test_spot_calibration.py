"""The synthetic spot stream is calibrated to the real-data inventory (spot-basis spec, X5; plan Task 11).

T: docs/research/spot-inventory/calibration_targets.csv (pooled real 1h statistics, research span).
S: the same statistics on the lookahead check's bars, with the inventory's definitions.
Rule: each tail within 25% of its distance from the target median.
"""
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from perp_lab.data import spot
from perp_lab.validate import causality, synthetic_market

TARGETS = Path(__file__).resolve().parents[1] / "docs" / "research" / "spot-inventory" / "calibration_targets.csv"


def _targets() -> dict[str, float]:
    return pd.read_csv(TARGETS).set_index("statistic")["value"].to_dict()


def _synthetic() -> dict[str, float]:
    b = causality.check_bars()
    both = (b.spot_trades.fillna(0) > 0) & (b.trades.fillna(1) > 0)
    basis = b.basis.where(both)
    m24 = basis.rolling(24, min_periods=24).mean()
    pct = lambda x, q: float(np.nanpercentile(x.to_numpy(dtype="float64"), q))
    return {"level_p1": pct(basis, 1), "level_p99": pct(basis, 99),
            "mean24_p1": pct(m24, 1), "mean24_p99": pct(m24, 99)}


@pytest.mark.parametrize("stat", ["level_p1", "level_p99", "mean24_p1", "mean24_p99"])
def test_synthetic_tails_within_25pct_of_targets(stat):
    t, s = _targets(), _synthetic()
    median = t[stat.split("_")[0] + "_p50"]
    assert abs(s[stat] - t[stat]) <= 0.25 * abs(t[stat] - median), (stat, s[stat], t[stat])


def test_spot_share_is_the_target_median():
    assert spot.SPOT_SHARE == pytest.approx(_targets()["share_p50"], abs=5e-4)


def test_market_version_is_5():
    assert synthetic_market.MARKET_VERSION == 5

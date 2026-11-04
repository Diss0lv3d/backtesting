"""The trade statistics a walk-forward's OOS metrics carry, and the builder reads in every verdict."""

from __future__ import annotations

import math

import pandas as pd

from perp_lab.engine.metrics import trade_stats

PAYOFF = ("avg_win_r", "avg_loss_r", "payoff_ratio")


def _trades(pnl: list[float], r: list[float]) -> pd.DataFrame:
    n = len(pnl)
    return pd.DataFrame({"pnl": pnl, "r_multiple": r, "bars_held": [1] * n, "exit_reason": ["signal"] * n,
                         "fees": [0.0] * n, "funding": [0.0] * n})


def test_the_payoff_is_the_average_winner_against_the_average_loser_in_r():
    """A 1:4 design wins a quarter of the time and still pays: one +4R winner against three -1R losers."""
    s = trade_stats(_trades([400.0, -100.0, -100.0, -100.0], [4.0, -1.0, -1.0, -1.0]))
    assert (s["win_rate"], s["avg_win_r"], s["avg_loss_r"], s["payoff_ratio"]) == (0.25, 4.0, -1.0, 4.0)
    assert s["avg_r"] == 0.25


def test_winners_and_losers_are_the_trades_the_win_rate_counts():
    """Split by net pnl (R is net of costs too, so the signs agree): a small loss is a loser, and a flat trade is in
    neither."""
    s = trade_stats(_trades([300.0, -20.0, 0.0, -100.0], [3.0, -0.2, 0.0, -1.0]))
    assert s["win_rate"] == 0.25 and s["avg_win_r"] == 3.0
    assert math.isclose(s["avg_loss_r"], -0.6) and math.isclose(s["payoff_ratio"], 5.0)


def test_a_payoff_needs_winners_and_losers():
    s = trade_stats(_trades([100.0, 50.0], [1.0, 0.5]))
    assert (s["avg_win_r"], s["avg_loss_r"]) == (0.75, 0.0) and math.isinf(s["payoff_ratio"])
    assert {k: trade_stats(_trades([-100.0], [-1.0]))[k] for k in PAYOFF} == {
        "avg_win_r": 0.0, "avg_loss_r": -1.0, "payoff_ratio": 0.0}
    assert {k: trade_stats(None)[k] for k in PAYOFF} == dict.fromkeys(PAYOFF, 0.0)

"""The static context: built from the repo's files and config, under the command-line cap."""

from __future__ import annotations

import re

import pytest

from perp_lab.config import load_config
from perp_lab.dsr import sharpe_bar
from perp_lab.proposer import context
from perp_lab.proposer.assemble import ALLOWED_IMPORTS
from perp_lab.proposer.contract import describe
from perp_lab.proposer.settings import load
from tests.conftest import REPO

LESSONS = REPO / "perp_lab/proposer/context/lessons.md"
OPENING = ("Human notes from the first six trials: OOS 2022-07 to 2026-07, 8 coins, flat 2 bp slippage. "
           "Today's lab: 17 coins from 2020, per-coin slippage.")


@pytest.fixture
def cfg():
    return load_config(REPO / "config/lab.yaml")


def test_it_fits_under_the_cap_and_holds_the_api_and_the_bar(cfg):
    text = context.static_context(cfg, load(cfg))
    assert len(text.encode("utf-8")) < context.STATIC_MAX_BYTES == 100_000
    assert (REPO / "perp_lab/strategy.py").read_text() in text
    assert "1.24" in text


def test_it_holds_every_source_in_order(cfg):
    pc = load(cfg)
    text = context.static_context(cfg, pc)
    parts = [(REPO / "perp_lab/strategy.py").read_text(), (REPO / "perp_lab/indicators.py").read_text(),
             ", ".join(ALLOWED_IMPORTS),
             (REPO / "strategies/flow_shock_fade.py").read_text(), (REPO / "strategies/donchian_atr.py").read_text(),
             LESSONS.read_text(), describe(pc.timeframes)]
    at = [text.find(p) for p in parts]
    assert -1 not in at and at == sorted(at)
    assert text.rstrip().endswith(describe(pc.timeframes).rstrip())


def test_the_bar_table_is_computed_for_each_listed_n(cfg):
    text = context.static_context(cfg, load(cfg))
    row = next(line for line in text.splitlines() if line.startswith("| bar |"))
    assert row == "| bar | " + " | ".join(f"{sharpe_bar(n):.2f}" for n in context.DSR_TABLE_N) + " |"
    assert context.DSR_TABLE_N == (7, 10, 20, 30, 50, 100, 200, 500, 1000)
    assert f"{sharpe_bar(7):.2f}" == "1.24"


def test_the_numbers_come_from_the_config(cfg):
    base = context.static_context(cfg, load(cfg))
    assert "- 17 coins," in base and "OOS from 2020-07 in 36 two-month folds" in base
    assert "2.05 × (ADV / 100M USDT)^−0.63" in base and "taker 5 bp" in base and "maker 2 bp" in base
    cfg.raw["universe"] = ["BTC", "ETH", "SOL"]
    cfg.raw["history_start"] = "2021-01-01"
    cfg.raw["costs"] = {**cfg.raw["costs"], "taker": 0.0007,
                        "slippage_model": {**cfg.raw["costs"]["slippage_model"], "alpha": 0.77}}
    cfg.raw["proposer"] = {**cfg.raw["proposer"], "max_grid_combos": 23}
    text = context.static_context(cfg, load(cfg))
    assert "- 3 coins," in text and "- 17 coins," not in text
    assert "OOS from 2021-07 in 30 two-month folds" in text
    assert "taker 7 bp" in text and "^−0.77" in text and "^−0.63" not in text
    assert "23 grid combinations" in text


@pytest.mark.parametrize("start", ["2020-01-01", "2021-01-01", "2020-03-15"])
def test_the_oos_span_matches_the_walk_forward_folds(cfg, start):
    from perp_lab.validate.walkforward import make_folds
    cfg.raw["history_start"] = start
    wf = cfg["walkforward"]
    folds = make_folds(cfg.history_start, cfg.holdout_start, wf["train_months"], wf["test_months"])
    assert context.oos_span(cfg) == (folds[0]["test_start"], len(folds),
                                     (folds[-1]["test_end"] - folds[0]["test_start"]).days)


def _row(text: str, column: str) -> str:
    return next(line for line in text.splitlines() if line.startswith(f"| {column} |"))


def test_slippage_is_an_engine_cost_and_no_column_a_plugin_may_read(cfg):
    text = context.static_context(cfg, load(cfg))
    assert "not available to plugins" in _row(text, "slippage")
    costs = text[text.index("## The cost model"):text.index("## The universe and spans")]
    assert "the engine charges it" in costs and "slippage` column" not in costs
    # Why: the bars of the lookahead check and of preflight have no slippage column (Ruling R24). When they
    # get one, this fails, and the bar table may offer the column again.
    from perp_lab.validate.causality import synthetic_bars, with_moving_columns
    assert "slippage" not in with_moving_columns(synthetic_bars(n=50, seed=1)).columns


def test_open_interest_is_nan_on_every_real_bar(cfg):
    """Fixed text (Ruling R23): the proposer may not read data/, and no open-interest history is stored."""
    text = context.static_context(cfg, load(cfg))
    for column in ("oi", "oi_value"):
        assert "NaN on every real bar" in _row(text, column), column
    assert "a plugin must not depend on them" in text
    assert "open-interest snapshot" not in text          # it never becomes known later


def test_over_the_cap_is_refused(cfg, monkeypatch):
    monkeypatch.setattr(context, "STATIC_MAX_BYTES", 1_000)
    with pytest.raises(ValueError, match="1000"):
        context.static_context(cfg, load(cfg))


def test_lessons_open_with_the_label_and_drop_lesson_six_and_section_references():
    text = LESSONS.read_text()
    assert text.splitlines()[0] == OPENING
    for gone in ("1.52", "0.59", "section", "The unit that matters is R", "In dollars, fees were",
                 "Section 5 says why", "the same exits", "constraint 5", "plan's"):
        assert gone.casefold() not in text.casefold(), gone
    for kept in ("| donchian_atr_wide | 4h |", "1. **Costs per timeframe.**", "2. **Profit came from the exit",
                 "3. **Fading a stretched price", "4. **Regime behaviour.**", "5. **In-sample overfitting.**",
                 "no target and a time exit. Its OOS Sharpe sat at p68", "on top of a 21k gross loss."):
        assert kept in text, kept
    assert not re.search(r"^6\. ", text, re.M)

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


def test_open_interest_names_when_it_starts(cfg):
    """Fixed text (Ruling R23: the proposer may not read data/). Binance's metrics archive, backfilled
    2026-11-04, starts 2021-12-01 for every coin but BTC, which starts 2020-09-01."""
    text = context.static_context(cfg, load(cfg))
    for column in ("oi", "oi_value"):
        row = _row(text, column)
        assert "NaN on every real bar" not in row, column
        assert "2021-12-01" in row and "BTC" in row and "2020-09-01" in row, column
    assert "a plugin must not depend on them" not in text
    assert "5-minute" in text                             # what a value is, and when it is known
    assert "flat days" in text                            # a plugin idle before the start is diluted
    assert "must handle NaN" in text


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


def test_the_columns_a_plugin_may_read_are_the_bar_tables_but_slippage():
    assert context.PLUGIN_COLUMNS == (
        "open_time", "close_time", "open", "high", "low", "close", "volume", "quote_volume", "trades",
        "taker_buy_volume", "taker_buy_quote_volume", "funding_charge", "funding_last", "funding_interval_h", "oi",
        "oi_value", "spot_close", "spot_quote_volume", "spot_taker_buy_quote_volume", "spot_trades", "basis",
        "regime", "regime_adx", "btc_open", "btc_high", "btc_low", "btc_close", "btc_quote_volume",
        "btc_taker_buy_quote_volume", "btc_funding_last", "btc_oi_value")
    from perp_lab.proposer.seed import load_seed
    assert {c for e in load_seed() for c in e["card"]["columns"]} <= set(context.PLUGIN_COLUMNS)


def test_the_builder_is_told_about_the_btc_columns(cfg):
    text = context.static_context(cfg, load(cfg))
    for row in ("| btc_open, btc_high, btc_low, btc_close |", "| btc_quote_volume |",
                "| btc_taker_buy_quote_volume |", "| btc_funding_last |", "| btc_oi_value |"):
        assert row in text, row
    for phrase in ("BTCUSDT", "nothing is filled forward", "diagnostics.market", "reference_frame", "rolling_beta",
                   "as BTCUSDT"):
        assert phrase in text, phrase
    assert len(text.encode("utf-8")) < context.STATIC_MAX_BYTES


SPOT_ROWS = (
    "| spot_close | the last trade on Binance spot (`{COIN}USDT`) in the bar; NaN where spot didn't trade in the bar "
    "| USDT |",
    "| spot_quote_volume | volume traded on spot in the bar; NaN where spot didn't trade | USDT |",
    "| spot_taker_buy_quote_volume | spot volume bought by takers (the rest was sold by takers); NaN where spot "
    "didn't trade | USDT |",
    "| spot_trades | number of spot trades in the bar; NaN where spot didn't trade | count |",
    "| basis | `close / spot_close − 1`, the perp's premium over spot at the bar's close; NaN where spot or the "
    "perp didn't trade | fraction |",
)


def test_spot_rows_in_bar_table(cfg):
    text = context.static_context(cfg, load(cfg))
    for row in SPOT_ROWS:
        assert row in text.splitlines(), row
    lines = text.splitlines()
    assert lines.index(SPOT_ROWS[0]) == lines.index(_row(text, "oi_value")) + 1


def test_clamp_sentence_exact(cfg):
    assert "F = P + clamp(0.01% − P, −0.05%, +0.05%)" in context.static_context(cfg, load(cfg))


def test_band_phrases(cfg):
    text = " ".join(context.static_context(cfg, load(cfg)).split())
    for phrase in ("inside that band funding says nothing about the premium",
                   "Outside the band F = P − 0.05% (above) or P + 0.05% (below)",
                   "an idea built on that average is a funding idea"):
        assert phrase in text, phrase


def test_spot_exclusions_line_from_config(cfg):
    assert "for: none." in context.static_context(cfg, load(cfg))
    cfg.raw["column_groups"]["spot"]["exclusions"] = ["FIL", "AAVEUSDT"]
    text = context.static_context(cfg, load(cfg))
    assert "for: FILUSDT, AAVEUSDT." in text and "for: none." not in text


def test_perp_only_and_paper_phrases(cfg):
    text = context.static_context(cfg, load(cfg))
    assert "The harness trades the perp only" in text and "can't be paper traded yet" in text


def test_the_spot_paragraph_warns_of_the_promotion_and_the_falling_share(cfg):
    """Fixed text from the real-data inventory (Ruling S2; R23: no number computed from data/)."""
    text = " ".join(context.static_context(cfg, load(cfg)).split())
    assert "BTC's spot volume and trade count are inflated from 2022-07 to 2023-03" in text
    assert "zero-fee BTC spot promotion" in text
    assert "a change against its own recent level, not as a level" in text


def test_plugin_columns_hold_spot():
    for name in ("spot_close", "spot_quote_volume", "spot_taker_buy_quote_volume", "spot_trades", "basis"):
        assert name in context.PLUGIN_COLUMNS, name


def test_piles_mentions_spot_and_basis(cfg):
    text = " ".join(context.static_context(cfg, load(cfg)).split())
    assert ("The synthetic bars' taker, open-interest, volume and spot columns are random walks, their `basis` "
            "has slow and fast mean-reverting parts, their spot columns are NaN over two stretches, their `btc_` "
            "columns are a synthetic BTC that moves with the coin, and they have no `slippage` column.") in text


def test_static_context_under_cap(cfg):
    assert len(context.static_context(cfg, load(cfg)).encode("utf-8")) < context.STATIC_MAX_BYTES

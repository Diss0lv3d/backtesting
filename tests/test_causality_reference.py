"""The synthetic BTC reference and the lookahead check's "as BTC" variants (spec
docs/superpowers/specs/2026-11-05-cross-coin-reference-design.md, "The lookahead check and the
synthetic bars"; Testing 5 and 6)."""

import numpy as np
import pandas as pd
import pytest

from perp_lab.data import reference
from perp_lab.plugins import PluginSource
from perp_lab.validate.causality import (CUTS, EXTRAS_SEED, NAN_CUTS, as_reference_coin, causality_failures,
                                         check_bars, synthetic_bars, with_moving_columns)
from tests.test_causality import peeker
from tests.test_store import cfg  # noqa: F401

MOMENTUM = '''"""Honest one-bar momentum; each fixture below changes one line of it."""
import numpy as np
from perp_lab.indicators import atr
from perp_lab.strategy import HOLD, Strategy, enter


class Momentum(Strategy):
    params = {}

    def prepare(self, bars):
        return bars[[]].assign(sig=np.sign(bars.close - bars.close.shift(1)), atr=atr(bars, 14))

    def bracket(self, ctx, side):
        return ctx["close"] - side * 2 * ctx["atr"], None

    def entry(self, ctx):
        s = ctx["sig"]
        if s != s or s == 0:
            return HOLD
        side = int(s)
        return enter(side, *self.bracket(ctx, side))
'''
SIDE = "        side = int(s)\n"
# on BTC only, the side comes from the series' last close, read straight off the engine's arrays
PEEK_SIDE = '        side = (1 if ctx._a["close"][-1] > ctx["close"] else -1) if WHEN else int(s)\n'


def from_momentum(name: str, text: str) -> PluginSource:
    """A fixture plugin: MOMENTUM with its line(s) changed."""
    assert text != MOMENTUM
    return PluginSource.from_text(name, text)


def labels(fails: list[str]) -> list[str]:
    return [f.split(": ")[0] for f in fails]


# ------------------------------------------------------------------ what the check catches

@pytest.mark.parametrize("col", reference.COLUMNS)
def test_a_plugin_peeking_at_any_btc_column_is_reported(cfg, col):
    # btc_close.shift(-1) and btc_oi_value.shift(-1) among them: row k-1 is NaN cut, set in full
    fails = causality_failures(cfg, peeker(col))
    assert labels(fails) == ["defaults", "defaults as BTC"]
    assert all(": AssertionError: features at cut" in f for f in fails)


BETA_FADE = '''"""Fades the coin's return residual against BTC's."""
import numpy as np
from perp_lab.indicators import atr, rolling_beta
from perp_lab.strategy import HOLD, Strategy, enter


def full_sample_beta(r, rb):
    m = r.notna() & rb.notna()
    return np.polyfit(rb[m], r[m], 1)[0]


class BetaFade(Strategy):
    params = {}

    def prepare(self, bars):
        r, rb = np.log(bars.close).diff(), np.log(bars.btc_close).diff()
        beta = BETA_EXPR
        return bars[[]].assign(beta=beta, resid=r - beta * rb, atr=atr(bars, 14))

    def bracket(self, ctx, side):
        return ctx["close"] - side * 2 * ctx["atr"], None

    def entry(self, ctx):
        e = ctx["resid"]
        if e != e or e == 0:
            return HOLD
        side = -1 if e > 0 else 1
        return enter(side, *self.bracket(ctx, side))
'''


def test_a_full_sample_beta_is_reported(cfg):
    src = PluginSource.from_text("polyfit_beta", BETA_FADE.replace("BETA_EXPR", "full_sample_beta(r, rb)"))
    fails = causality_failures(cfg, src)
    assert fails and fails[0].startswith("defaults: AssertionError: features at cut")


def test_a_peek_gated_on_the_btc_symbol_is_caught_as_btc_only(cfg):
    peek = PEEK_SIDE.replace("WHEN", 'ctx.symbol == "BTCUSDT"')
    src = from_momentum("symbol_peek", MOMENTUM.replace(SIDE, peek))
    fails = causality_failures(cfg, src)
    assert labels(fails) == ["defaults as BTC"]
    assert fails[0].startswith("defaults as BTC: AssertionError: signals diverge before cut")


def test_a_plugin_that_refuses_btcs_own_bars_fails_as_btc_only(cfg):
    raises = ('        if (bars.close == bars.btc_close).any():\n'
              '            raise ValueError("BTC is its own reference")\n'
              '        return bars[[]]')
    src = from_momentum("not_on_itself", MOMENTUM.replace("        return bars[[]]", raises))
    assert causality_failures(cfg, src) == ["defaults as BTC: ValueError: BTC is its own reference"]


def test_a_peek_gated_on_a_grid_value_fails_those_params_as_btc(cfg):
    when = 'ctx.symbol == "BTCUSDT" and self.p["k"] == 2'
    text = (MOMENTUM.replace("    params = {}\n", '    params = {"k": 1}\n    grid = {"k": [1, 3]}\n')
            .replace(SIDE, PEEK_SIDE.replace("WHEN", when)))
    src = from_momentum("grid_peek", text)
    assert causality_failures(cfg, src) == []                      # the corners k=1, k=3 are honest
    fails = causality_failures(cfg, src, params={"k": 2})
    assert labels(fails) == ["k=2 as BTC"]
    assert fails[0].startswith("k=2 as BTC: AssertionError: signals diverge before cut")


# the momentum's signal taken from BTC's close after a fill over its NaN bars
FILLED = ("        b = bars.btc_close.FILL()\n"
          "        return bars[[]].assign(sig=np.sign(b - b.shift(1)), atr=atr(bars, 14))\n")
OWN_SIG = "        return bars[[]].assign(sig=np.sign(bars.close - bars.close.shift(1)), atr=atr(bars, 14))\n"


@pytest.mark.parametrize("fill", ["bfill", "interpolate"])
def test_a_fill_from_later_btc_bars_is_reported(cfg, fill):
    # the synthetic bar columns are NaN on the row before each of NAN_CUTS: the cut run can't fill that row
    # from the bar after it, the full run can
    src = from_momentum(f"{fill}_btc", MOMENTUM.replace(OWN_SIG, FILLED.replace("FILL", fill)))
    fails = causality_failures(cfg, src)
    assert labels(fails) == ["defaults"]                         # as BTC, btc_close is its own close: no NaN
    assert fails[0].startswith(f"defaults: AssertionError: features at cut {CUTS[0]}")


def test_a_forward_fill_over_btc_columns_passes(cfg):
    src = from_momentum("ffill_btc", MOMENTUM.replace(OWN_SIG, FILLED.replace("FILL", "ffill")))
    assert causality_failures(cfg, src) == []


# the momentum's signal from the next close where WHEN holds on BTC's bar, its own momentum elsewhere
GATED_PEEK = ("        own = np.sign(bars.close - bars.close.shift(1))\n"
              "        peek = np.sign(bars.close.shift(-1) - bars.close).where(WHEN, own)\n"
              "        return bars[[]].assign(sig=peek, atr=atr(bars, 14))\n")


def open_cuts(when: str, bars: pd.DataFrame) -> list[int]:
    """The cuts k at whose row k - 1 the gate `when` (a GATED_PEEK condition) holds on `bars`."""
    gate = eval(when, {"bars": bars})                      # the fixture's own expression, so the two can't drift
    return [k for k in CUTS if gate.iloc[k - 1]]


@pytest.mark.parametrize("when", [
    "bars.btc_close > bars.btc_close.shift(1)",
    "bars.btc_close > 0",
    "bars.btc_close < bars.btc_close.rolling(1200).mean()",   # BTC below its 50-day SMA at 1h
], ids=["btc_up", "btc_set", "btc_below_sma_1200"])
def test_a_next_bar_peek_gated_on_a_btc_bar_column_is_reported(cfg, when):
    # the peek shows only on the cut run's last row, k - 1; the btc_ bar columns are NaN there only before
    # NAN_CUTS, and a rolling window over them is set again once it clears that row, so the gate can open at a
    # later cut
    bars = check_bars()
    cut = next((k for k in open_cuts(when, bars) if k not in NAN_CUTS), None)
    assert cut is not None                                   # the gate opens where the default check can see it
    as_btc = ["defaults as BTC"] if open_cuts(when, as_reference_coin(bars)) else []
    src = from_momentum("btc_gated_peek", MOMENTUM.replace(OWN_SIG, GATED_PEEK.replace("WHEN", when)))
    fails = causality_failures(cfg, src)
    assert labels(fails) == ["defaults", *as_btc]
    assert fails[0].startswith(f"defaults: AssertionError: features at cut {cut}")


def test_an_honest_rolling_beta_plugin_passes(cfg):
    # as BTC its beta is exactly 1 and its residual exactly 0, so it never trades: that is no failure
    src = PluginSource.from_text("honest_beta", BETA_FADE.replace("BETA_EXPR", "rolling_beta(r, rb, 100)"))
    assert causality_failures(cfg, src) == []


def test_a_plugin_that_never_trades_btc_passes(cfg):
    skip = 's = 0 if ctx.symbol == "BTCUSDT" else ctx["sig"]'
    src = from_momentum("skips_btc", MOMENTUM.replace('s = ctx["sig"]', skip))
    assert causality_failures(cfg, src) == []


BTC_DATE_PEEK = '''"""Honest momentum on ordinary dates; peeks at BTC's next close from the holdout start."""
import numpy as np
import pandas as pd
from perp_lab.indicators import atr
from perp_lab.strategy import HOLD, Strategy, enter


class BtcDatePeek(Strategy):
    params = {}

    def prepare(self, bars):
        late = bars.index >= pd.Timestamp("HOLDOUT", tz="UTC")
        honest = np.sign(bars.close - bars.close.shift(1))
        peek = np.sign(bars.btc_close.shift(-1) - bars.btc_close)
        return bars[[]].assign(sig=honest.where(~late, peek), atr=atr(bars, 14))

    def bracket(self, ctx, side):
        return ctx["close"] - side * 2 * ctx["atr"], None

    def entry(self, ctx):
        s = ctx["sig"]
        if s != s or s == 0:
            return HOLD
        side = int(s)
        return enter(side, *self.bracket(ctx, side))
'''


def test_a_btc_peek_past_the_holdout_start_is_caught_by_the_dated_check(cfg):
    from perp_lab.judge.judge import dated_lookahead_failures
    day = cfg.holdout_start.strftime("%Y-%m-%d")
    src = PluginSource.from_text("btc_date_peek", BTC_DATE_PEEK.replace("HOLDOUT", day))
    assert causality_failures(cfg, src) == []                     # honest on the undated bars
    fails = dated_lookahead_failures(cfg, src, cfg.holdout_start + pd.Timedelta(days=30))
    assert any(f.startswith("holdout-dated: defaults: AssertionError: features at cut") for f in fails)


# ------------------------------------------------------------------ with_moving_columns and as_reference_coin

def fills_before_the_reference(bars: pd.DataFrame, seed: int = EXTRAS_SEED) -> pd.DataFrame:
    """with_moving_columns exactly as it was before the reference group (819c7cc), frozen here:
    its fills must stay byte-identical."""
    out = bars.copy()
    n = len(out)
    rng = np.random.default_rng(seed)
    close = out["close"].to_numpy(dtype=float)
    volume = out["volume"].to_numpy(dtype=float)
    oi = 1e6 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    taker = volume * rng.uniform(0.3, 0.7, n)
    fills = {
        "oi": oi, "oi_value": oi * close,
        "quote_volume": volume * close, "trades": rng.integers(50, 5000, n).astype(float),
        "taker_buy_volume": taker, "taker_buy_quote_volume": taker * close,
        "regime_adx": np.clip(25 + np.cumsum(rng.normal(0, 1, n)), 5, 80),
        "funding_interval_h": np.where((np.arange(n) // 500) % 2 == 0, 8.0, 4.0),
    }
    for c, v in fills.items():
        if c not in out or out[c].nunique() <= 1:
            out[c] = v
    return out


@pytest.mark.parametrize("bars, seed", [
    (synthetic_bars(), EXTRAS_SEED),
    (synthetic_bars(seed=1), EXTRAS_SEED),
    (synthetic_bars(n=2000, seed=102, funding_every=96, tf_ms=300_000), 202),
], ids=["default", "check_bars", "market_5m"])
def test_existing_fills_byte_identical(bars, seed):
    out, old = with_moving_columns(bars, seed), fills_before_the_reference(bars, seed)
    assert [c for c in out.columns if not c.startswith("btc_")] == list(old.columns)
    pd.testing.assert_frame_equal(out[list(old.columns)], old, check_exact=True)
    assert list(out.columns) == list(old.columns) + list(reference.COLUMNS)


def test_the_group_is_kept_when_it_moves_and_redrawn_whole_when_any_column_is_dead():
    bars = with_moving_columns(synthetic_bars())
    cols = list(reference.COLUMNS)
    pd.testing.assert_frame_equal(with_moving_columns(bars, seed=8)[cols], bars[cols], check_exact=True)
    for dead in (np.nan, 1.0):                    # all-NaN or constant, as real BTC OI is before it exists
        out = with_moving_columns(bars.assign(btc_oi_value=dead), seed=8)
        redrawn = reference.synthetic(bars, 8)
        for c in cols:                            # every column of the group, so OHLC stay consistent
            np.testing.assert_array_equal(out[c].to_numpy(), redrawn[c], err_msg=c)
        assert not np.array_equal(out.btc_close, bars.btc_close, equal_nan=True)


def test_as_reference_coin_carries_its_own_columns_as_btc():
    bars = with_moving_columns(synthetic_bars())
    out = as_reference_coin(bars)
    for c in reference.BAR_COLUMNS + reference.ASOF_COLUMNS:
        assert out[reference.PREFIX + c].dtype == "float64"
        np.testing.assert_array_equal(out[reference.PREFIX + c].to_numpy(), bars[c].to_numpy(dtype="float64"))
    rest = [c for c in bars.columns if c not in reference.COLUMNS]
    pd.testing.assert_frame_equal(out[rest], bars[rest], check_exact=True)
    assert list(out.columns) == list(bars.columns)
    assert not bars.btc_close.eq(bars.close).any()            # the input frame is untouched


# ------------------------------------------------------------------ the synthetic reference itself

@pytest.mark.parametrize("coin_seed, seed", [(0, 7), (1, 7), (2, 3), (3, 11), (4, 202)])
def test_synthetic_btc_returns_correlate_with_the_coins(coin_seed, seed):
    bars = synthetic_bars(seed=coin_seed)
    btc = reference.synthetic(bars, seed)
    r_x, r_b = (pd.Series(np.log(c)).diff() for c in (bars.close.to_numpy(), btc["btc_close"]))
    rho = r_x.corr(r_b)                                       # over the bars where both are set
    assert 0.6 <= rho <= 0.8


def test_every_synthetic_btc_column_is_float64_and_moves():
    bars = synthetic_bars()
    btc = reference.synthetic(bars, EXTRAS_SEED)
    assert tuple(btc) == reference.COLUMNS
    for c, v in btc.items():
        assert v.dtype == np.float64 and len(v) == len(bars) and pd.Series(v).nunique() > 1, c
    live = np.ones(len(bars), dtype=bool)
    live[[k - 1 for k in NAN_CUTS]] = False                   # NaN bar rows, pinned below
    for c in reference.COLUMNS[:-1]:
        assert np.isfinite(btc[c][live]).all(), c
    o, h, low, cl = (btc[reference.PREFIX + c][live] for c in ("open", "high", "low", "close"))
    assert (h >= np.maximum(o, cl)).all() and (low <= np.minimum(o, cl)).all() and (low > 0).all()
    assert (btc["btc_taker_buy_quote_volume"][live] < btc["btc_quote_volume"][live]).all()


@pytest.mark.parametrize("n, gaps", [(3000, [399]), (1778, [399]), (401, [399]), (400, [])])
def test_synthetic_btc_bar_columns_are_nan_on_the_row_before_the_first_cut_only(n, gaps):
    # as real BTC's are on a zero-trade bar (X6): at CUTS[0] the cut run ends on a NaN its full run has a bar
    # after; at the later cuts it ends on a set bar, so a peek gated on it can show
    btc = reference.synthetic(synthetic_bars(n=n), EXTRAS_SEED)
    for c in reference.BAR_COLUMNS:
        assert np.flatnonzero(np.isnan(btc[reference.PREFIX + c])).tolist() == gaps, c
    assert NAN_CUTS == [CUTS[0]] and gaps == [k - 1 for k in NAN_CUTS if k < n]
    assert np.isfinite(btc["btc_funding_last"]).all()          # the as-of two keep their own rule
    assert np.isfinite(btc["btc_oi_value"][300:]).all()


def test_synthetic_btc_oi_is_nan_on_exactly_the_first_300_rows():
    v = reference.synthetic(synthetic_bars(), EXTRAS_SEED)["btc_oi_value"]
    assert np.isnan(v[:300]).all() and np.isfinite(v[300:]).all()


def test_synthetic_btc_draws_from_its_own_seed():
    bars = synthetic_bars()
    a, b, c = (reference.synthetic(bars, s) for s in (7, 7, 8))
    assert all(np.array_equal(a[k], b[k], equal_nan=True) for k in reference.COLUMNS)
    assert not np.array_equal(a["btc_close"], c["btc_close"], equal_nan=True)


@pytest.mark.parametrize("t", [0, 299, 300, 1777])
def test_btc_values_up_to_t_ignore_the_coins_rows_after_t(t):
    bars = synthetic_bars()
    later = bars.copy()
    moved = ["open", "high", "low", "close", "volume", "funding_charge", "funding_last", "regime_adx"]
    idx = [later.columns.get_loc(c) for c in moved]
    factors = np.random.default_rng(t).uniform(0.5, 1.5, (len(later) - t - 1, len(moved)))
    later.iloc[t + 1:, idx] = later.iloc[t + 1:, idx].to_numpy() * factors
    a, b = reference.synthetic(bars, EXTRAS_SEED), reference.synthetic(later, EXTRAS_SEED)
    for c in reference.COLUMNS:
        np.testing.assert_array_equal(a[c][:t + 1], b[c][:t + 1], err_msg=c)
    assert not np.array_equal(a["btc_close"][t + 1:], b["btc_close"][t + 1:], equal_nan=True)   # it reached BTC

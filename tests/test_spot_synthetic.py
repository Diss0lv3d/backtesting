"""The synthetic spot stream and its lookahead coverage (spec docs/superpowers/specs/2026-11-05-spot-basis-design.md,
"The lookahead check and the synthetic bars"; Testing strategy, Lookahead and Synthetic; Review Focus 4 of the
spot-basis plan)."""
import numpy as np
import pandas as pd
import pytest

from perp_lab.data import columns, spot
from perp_lab.data.columns import ColumnGroup
from perp_lab.engine.backtest import EngineParams
from perp_lab.plugins import PluginSource
from perp_lab.sandbox import client
from perp_lab.validate.causality import (CUTS, EXTRAS_SEED, causality_failures, check_bars, synthetic_bars,
                                         with_moving_columns)
from tests.conftest import REAL_PLUGINS, REPO
from tests.test_causality import peeker
from tests.test_causality_reference import MOMENTUM, OWN_SIG, from_momentum, labels
from tests.test_store import cfg  # noqa: F401

SPOT = ColumnGroup(name="spot", columns=spot.COLUMNS, prefix=spot.PREFIX, rule=1, synthetic_id=spot.SYNTHETIC_ID,
                   exam_required="if_read", helpers=(), paper_ready=False, load_source=spot.load_source,
                   attach=spot.attach, synthetic=spot.synthetic, sources=spot.sources)
PREFIXED = [c for c in spot.COLUMNS if c.startswith(spot.PREFIX)]     # the four spot_ columns
# at n = 3000: the two stretches, then the row before each cut
NAN_ROWS = [*range(0, 288), 399, 1099, *range(1536, 1584), 1776, 2499]


@pytest.fixture
def with_spot():
    """The spot group registered for the test, so the lookahead check fills it. Task 5 registers it in
    columns.GROUPS and turns this into a no-op."""
    with columns.registered(SPOT):
        yield


def filled(**kw) -> pd.DataFrame:
    """synthetic_bars with the exchange columns the spot stream reads (quote_volume, trades,
    taker_buy_quote_volume) moving."""
    return with_moving_columns(synthetic_bars(**kw))


def with_stream(bars: pd.DataFrame, seed: int = EXTRAS_SEED) -> pd.DataFrame:
    return bars.assign(**spot.synthetic(bars, seed))


# ------------------------------------------------------------------ the stream itself

def test_draws_from_seed_and_synthetic_id(monkeypatch):
    bars = filled()
    real, seen = np.random.default_rng, []
    monkeypatch.setattr(spot.np.random, "default_rng", lambda *a: seen.append(a) or real(*a))
    a, b = spot.synthetic(bars, 7), spot.synthetic(bars, 7)
    assert seen == [((7, 2),), ((7, 2),)]
    assert all(np.array_equal(a[c], b[c], equal_nan=True) for c in spot.COLUMNS)
    assert not np.array_equal(a["basis"], spot.synthetic(bars, 8)["basis"], equal_nan=True)


def test_all_five_move():
    bars = filled()
    s = spot.synthetic(bars, EXTRAS_SEED)
    assert tuple(s) == spot.COLUMNS
    for c, v in s.items():
        assert v.dtype == np.float64 and len(v) == len(bars) and pd.Series(v).nunique() > 1, c


def test_basis_identity_exact_on_5m_bars():
    bars = filled(n=3000, seed=102, funding_every=96, tf_ms=300_000)
    s = spot.synthetic(bars, 202)
    assert np.isfinite(s["basis"]).sum() == 3000 - len(NAN_ROWS)
    np.testing.assert_array_equal(s["basis"], bars.close.to_numpy() / s["spot_close"] - 1)


def lopsided() -> pd.DataFrame:
    """Bars whose perp flow is all taker buys on even rows and all taker sells on odd ones, so the spot
    taker share's clip binds."""
    bars = filled()
    return bars.assign(taker_buy_quote_volume=bars.quote_volume * (np.arange(len(bars)) % 2 == 0))


@pytest.mark.parametrize("make", [filled, lopsided], ids=["filled", "lopsided"])
def test_spot_taker_share_within_bounds(make):
    s = spot.synthetic(make(), EXTRAS_SEED)
    share = s["spot_taker_buy_quote_volume"] / s["spot_quote_volume"]
    live = share[~np.isnan(share)]
    assert len(live) == 3000 - len(NAN_ROWS)
    assert ((live >= 0.05 - 1e-12) & (live <= 0.95 + 1e-12)).all()       # (q·x)/q can be x off by an ulp


def test_the_clip_binds_on_lopsided_flow():
    s = spot.synthetic(lopsided(), EXTRAS_SEED)
    share = s["spot_taker_buy_quote_volume"] / s["spot_quote_volume"]
    assert np.isclose(share, 0.95).any() and np.isclose(share, 0.05).any()


def test_spot_trades_are_whole():
    v = spot.synthetic(filled(), EXTRAS_SEED)["spot_trades"]
    live = v[~np.isnan(v)]
    assert len(live) == 3000 - len(NAN_ROWS)
    assert (live == np.round(live)).all() and (live >= 0).all()


def test_basis_is_persistent():
    basis = with_stream(check_bars()).basis
    assert basis.autocorr(24) > 0.3
    mean = (basis.rolling(24).mean() - spot.BASIS_C).dropna().to_numpy()
    assert np.count_nonzero(np.sign(mean[1:]) != np.sign(mean[:-1])) >= 3     # crosses BASIS_C


def test_spot_volume_follows_perp_volume():
    bars = with_stream(check_bars())
    live = bars.spot_quote_volume.notna()
    assert np.log(bars.spot_quote_volume[live]).corr(np.log(bars.quote_volume[live])) > 0.5


@pytest.mark.parametrize("n, rows", [
    (3000, NAN_ROWS),
    (1560, [*range(0, 288), 399, 1099, *range(1536, 1560)]),     # the frame ends inside the halt
    (300, list(range(0, 288))),
], ids=["3000", "1560", "300"])
def test_nan_rows_are_the_stretches_and_the_rows_before_each_cut(n, rows):
    s = spot.synthetic(filled(n=n), EXTRAS_SEED)
    for c in spot.COLUMNS:
        assert np.flatnonzero(np.isnan(s[c])).tolist() == rows, c


@pytest.mark.parametrize("t", [0, 288, 1100, 1777, 2998])
def test_values_up_to_t_ignore_later_rows(t):
    bars = filled()
    later = bars.copy()
    moved = ["open", "high", "low", "close", "quote_volume", "trades", "taker_buy_quote_volume"]
    idx = [later.columns.get_loc(c) for c in moved]
    factors = np.random.default_rng(t).uniform(0.5, 1.5, (len(later) - t - 1, len(moved)))
    later.iloc[t + 1:, idx] = later.iloc[t + 1:, idx].to_numpy() * factors
    a, b = spot.synthetic(bars, EXTRAS_SEED), spot.synthetic(later, EXTRAS_SEED)
    for c in spot.COLUMNS:
        np.testing.assert_array_equal(a[c][:t + 1], b[c][:t + 1], err_msg=c)
    for c in PREFIXED:                                            # the change reached the stream
        assert not np.array_equal(a[c][t + 1:], b[c][t + 1:], equal_nan=True), c


# ------------------------------------------------------------------ what the lookahead check catches

@pytest.mark.parametrize("col", spot.COLUMNS)
def test_peek_at_each_spot_column_is_reported(cfg, with_spot, col):
    fails = causality_failures(cfg, peeker(col))
    assert fails[0].startswith("defaults: AssertionError")
    assert labels(fails) == ["defaults", "defaults as BTC"]         # as BTC, the coin's spot is its own


def test_int_spot_trades_is_reported(cfg, with_spot):
    # it crashes on the NaN stretch, as it would on a real spot gap
    gate = '        if int(ctx["spot_trades"]) < 10:\n            return HOLD\n        s = ctx["sig"]\n'
    src = from_momentum("int_spot_trades", MOMENTUM.replace('        s = ctx["sig"]\n', gate))
    fails = causality_failures(cfg, src)
    assert fails and all("ValueError" in f for f in fails)


# Fades a crowded basis: it enters only when the 24-bar mean of basis lies beyond BASIS_C ± 2·BASIS_SLOW_SD.
THRESHOLD = '''"""Fades a crowded basis when its 24-bar mean leaves the band around its centre."""
from perp_lab.indicators import atr
from perp_lab.strategy import HOLD, Strategy, enter

CENTRE, BAND = CENTRE_VALUE, BAND_VALUE


class BasisFade(Strategy):
    params = {}

    def prepare(self, bars):
        return bars[[]].assign(mean=bars.basis.rolling(24).mean(), atr=atr(bars, 14)NEXT)

    def bracket(self, ctx, side):
        return ctx["close"] - side * 2 * ctx["atr"], None

    def entry(self, ctx):
        m = ctx["mean"]
        if not abs(m - CENTRE) > BAND:          # inside the band, or no mean over a spot gap
            return HOLD
        side = SIDE
        return enter(side, *self.bracket(ctx, side))
'''
HONEST = ("", "-1 if m > CENTRE else 1")


def threshold(name: str, nxt: str, side: str) -> PluginSource:
    text = (THRESHOLD.replace("CENTRE_VALUE", repr(spot.BASIS_C)).replace("BAND_VALUE", repr(2 * spot.BASIS_SLOW_SD))
            .replace("NEXT", nxt).replace("SIDE", side))
    return PluginSource.from_text(name, text)


@pytest.mark.parametrize("nxt, side, caught_by", [
    # the next close as a feature, read in the branch only: the feature is caught on the cut's last row,
    # whether or not the branch is taken
    (", nxt=bars.close.shift(-1)", '1 if ctx["nxt"] > ctx["close"] else -1', "features at cut"),
    # the series' last close, read off the engine's arrays in the branch only: no feature shows it, so it is
    # caught only by the branch's own entries before a cut, which needs the stream to leave the band. (A next
    # close kept only where the branch is taken would show on no compared row: it shows on the cut's last row
    # alone, where every spot column, and so the 24-bar mean, is NaN.)
    ("", '1 if ctx._a["close"][-1] > ctx["close"] else -1', "signals diverge before cut"),
], ids=["next_close", "last_close"])
def test_basis_mean_threshold_peek_is_caught(cfg, with_spot, nxt, side, caught_by):
    fails = causality_failures(cfg, threshold("basis_peek", nxt, side))
    assert labels(fails) == ["defaults", "defaults as BTC"]
    assert fails[0].startswith(f"defaults: AssertionError: {caught_by}")


# the momentum's signal taken from a spot column after a fill over its NaN bars
FILLED = ("        b = bars.COL.FILL()\n"
          "        return bars[[]].assign(sig=np.sign(b - b.shift(1)), atr=atr(bars, 14))\n")


@pytest.mark.parametrize("fill, col", [("bfill", c) for c in spot.COLUMNS] + [("interpolate", "basis")])
def test_bfill_of_spot_is_caught(cfg, with_spot, fill, col):
    # the five are NaN on the row before each cut: the cut run can't fill that row from the bar after it,
    # the full run can
    src = from_momentum(f"{fill}_{col}", MOMENTUM.replace(OWN_SIG, FILLED.replace("COL", col).replace("FILL", fill)))
    fails = causality_failures(cfg, src)
    assert labels(fails) == ["defaults", "defaults as BTC"]
    assert fails[0].startswith(f"defaults: AssertionError: features at cut {CUTS[0]}")


@pytest.mark.parametrize("src", [
    # signals only when the stream leaves the band, so it passing (not "no signals") shows the branch is taken
    threshold("basis_fade", *HONEST),
    from_momentum("ffill_basis", MOMENTUM.replace(OWN_SIG, FILLED.replace("COL", "basis").replace("FILL", "ffill"))),
], ids=["threshold_fade", "ffill_momentum"])
def test_honest_basis_plugin_passes(cfg, with_spot, src):
    assert causality_failures(cfg, src) == []


# ------------------------------------------------------------------ the repo's plugins don't see spot

@pytest.mark.parametrize("name", REAL_PLUGINS)
def test_real_plugins_unchanged_by_spot_columns(cfg, name, monkeypatch):
    monkeypatch.setattr("perp_lab.strategy.STRATEGY_DIR", REPO / "strategies")
    src = PluginSource.read(name)
    with_ = with_stream(check_bars())
    without = with_.drop(columns=list(spot.COLUMNS))
    a, b = (client.fresh_run(cfg, src, "X", "1h", bars, {}, EngineParams(), features=True)
            for bars in (with_, without))
    pd.testing.assert_frame_equal(a.features, b.features, check_exact=True)
    assert a.decisions.n_actions() > 0 and a.decisions.same(b.decisions)

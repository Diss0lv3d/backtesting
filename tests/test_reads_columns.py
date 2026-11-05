"""Which plugins read a column group (spec docs/superpowers/specs/2026-11-05-cross-coin-reference-design.md,
X4; Testing 11): `plugins.names_group`, the static scan, and `plugins.reads_columns`, static or dynamic."""

import dataclasses

import pytest

from perp_lab.data import columns
from perp_lab.engine.backtest import EngineParams
from perp_lab.plugins import PluginSource, names_group, reads_columns
from perp_lab.sandbox import client
from perp_lab.validate.causality import synthetic_bars, with_moving_columns
from tests.test_store import cfg  # noqa: F401

REF = next(g for g in columns.GROUPS if g.name == "reference")

# One-bar momentum on the series `ref`; each fixture below changes the REF line, the docstring or prepare.
PLUGIN = '''"""DOC"""
import numpy as np
from perp_lab import indicators as ind
from perp_lab.indicators import atr, reference_frame
from perp_lab.strategy import HOLD, Strategy, enter


class Momentum(Strategy):
    params = {"k": 1}
    grid = {"k": [1, 3]}

    def prepare(self, bars):
        ref = bars.close
        return bars[[]].assign(sig=np.sign(ref - ref.shift(1)), atr=atr(bars, 14))

    def bracket(self, ctx, side):
        return ctx["close"] - side * 2 * ctx["atr"], None

    def entry(self, ctx):
        s = ctx["sig"]
        if s != s or s == 0:
            return HOLD
        side = int(s)
        return enter(side, *self.bracket(ctx, side))
'''
REF_LINE = "        ref = bars.close\n"
DOC = "One-bar momentum on the coin's own close."


def plugin(ref_line: str = REF_LINE, doc: str = DOC) -> PluginSource:
    text = PLUGIN.replace(REF_LINE, ref_line).replace("DOC", doc)
    return PluginSource.from_text("fixture", text)


# ------------------------------------------------------------------ static: names_group

@pytest.mark.parametrize("line, found", [
    ('        ref = bars["btc_close"]\n', ["btc_close"]),
    ("        ref = bars.btc_close\n", ["btc_close"]),
    ('        k = "close"\n        ref = bars[f"btc_{k}"]\n', ["btc_"]),
    ("        ref = reference_frame(bars).close\n", ["reference_frame"]),
    ("        ref = ind.reference_frame(bars).close\n", ["reference_frame"]),
], ids=["string", "attribute", "f-string", "helper", "module_helper"])
def test_each_way_of_naming_the_group_counts(line, found):
    assert names_group(plugin(line), REF) == found


def test_a_plugin_that_never_names_the_group_has_no_matches():
    assert names_group(plugin(), REF) == []


def test_a_comment_does_not_count():
    assert names_group(plugin("        ref = bars.close  # not bars.btc_close, nor reference_frame(bars)\n"),
                       REF) == []


def test_a_docstring_counts():
    # conservative: a docstring is a string constant the plugin can read back (`__doc__`), and a false
    # positive only costs a refused paper run
    assert names_group(plugin(doc="Momentum on close; a btc_close variant is for later."), REF) == ["btc_close"]


def test_each_match_is_listed_once_in_source_order():
    line = '        ref = bars.btc_close + bars["btc_open"] - bars.btc_close\n'
    assert names_group(plugin(line), REF) == ["btc_close", "btc_open"]


def test_the_scan_follows_the_groups_own_names():
    spot = dataclasses.replace(REF, name="spot", columns=("spot_close",), prefix="spot_", helpers=("spot_frame",))
    assert names_group(plugin("        ref = bars.btc_close\n"), spot) == []
    assert names_group(plugin("        ref = bars.spot_close\n"), spot) == ["spot_close"]
    assert names_group(plugin("        ref = spot_frame(bars).close\n"), spot) == ["spot_frame"]


def test_a_static_match_needs_no_run(cfg, monkeypatch):
    def no_run(*a, **kw):
        raise AssertionError("ran the plugin")

    monkeypatch.setattr(client, "fresh_run", no_run)
    assert reads_columns(cfg, plugin("        ref = bars.btc_close\n"), REF) is True


# ------------------------------------------------------------------ dynamic: reads_columns

BUILT = '        col = "bt" + "c_close"\n        ref = bars[col]\n'


def test_a_name_built_at_run_time_is_caught_by_the_runs(cfg):
    src = plugin(BUILT)
    assert names_group(src, REF) == []
    assert reads_columns(cfg, src, REF) is True


def test_a_plugin_that_never_reads_the_group_is_not_caught(cfg):
    src = plugin()
    bars = with_moving_columns(synthetic_bars(seed=1))
    assert client.fresh_run(cfg, src, "X", "1h", bars, {}, EngineParams()).decisions.n_actions() > 0
    assert reads_columns(cfg, src, REF) is False


def test_feature_passthrough_is_not_reading(cfg):
    # every bar column, the btc_ ones too, comes back as a feature; the decisions use close only
    passthrough = plugin().text.decode().replace(
        "        return bars[[]].assign(", "        return bars.copy().assign(")
    src = PluginSource.from_text("passthrough", passthrough)
    assert names_group(src, REF) == []
    bars = with_moving_columns(synthetic_bars(seed=1))
    assert client.fresh_run(cfg, src, "X", "1h", bars, {}, EngineParams()).decisions.n_actions() > 0
    assert reads_columns(cfg, src, REF) is False


def test_given_params_are_run_as_well_as_the_defaults(cfg):
    src = plugin('        col = "bt" + "c_close" if self.p["k"] == 2 else "close"\n        ref = bars[col]\n')
    assert reads_columns(cfg, src, REF) is False
    assert reads_columns(cfg, src, REF, params={"k": 2}) is True

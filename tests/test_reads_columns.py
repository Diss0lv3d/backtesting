"""Which plugins read a column group (spec docs/superpowers/specs/2026-11-05-cross-coin-reference-design.md,
X4; Testing 11): `plugins.names_group`, the static scan, and `plugins.reads_columns`, static or dynamic."""

import dataclasses

import pytest

from perp_lab.data import columns
from perp_lab.engine.backtest import EngineParams
from perp_lab.plugins import PluginSource, names_group, reads_columns
from perp_lab.sandbox import client
from perp_lab.validate.causality import causality_failures, check_bars
from tests.test_store import cfg  # noqa: F401

REF = next(g for g in columns.GROUPS if g.name == "reference")

# One-bar momentum on the series `ref`; each fixture below changes its prepare lines, imports, docstring or
# bracket. It imports reference_frame without using it: a bare import is not a read.
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
        sig = np.sign(ref - ref.shift(1))
        return bars[[]].assign(sig=sig, atr=atr(bars, 14))

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
SIG_LINE = "        sig = np.sign(ref - ref.shift(1))\n"
IMPORT = "from perp_lab.indicators import atr, reference_frame\n"
AS_RF = "from perp_lab.indicators import atr, reference_frame as rf\n"
BRACKET = '        return ctx["close"] - side * 2 * ctx["atr"], None\n'
DOC = "One-bar momentum on the coin's own close."
BUILT_NAME = '"bt" + "c_close"'           # btc_close, out of two strings that name nothing


def plugin(ref_line: str = REF_LINE, doc: str = DOC, *, sig: str = SIG_LINE, imports: str = IMPORT,
           bracket: str = BRACKET) -> PluginSource:
    text = (PLUGIN.replace(REF_LINE + SIG_LINE, ref_line + sig).replace(IMPORT, imports)
            .replace(BRACKET, bracket).replace("DOC", doc))
    return PluginSource.from_text("fixture", text)


def actions(cfg, src, bars) -> int:
    return client.fresh_run(cfg, src, "X", "1h", bars, {}, EngineParams()).decisions.n_actions()


# ------------------------------------------------------------------ static: names_group

@pytest.mark.parametrize("line, imports, found", [
    ('        ref = bars["btc_close"]\n', IMPORT, ["btc_close"]),
    ("        ref = bars.btc_close\n", IMPORT, ["btc_close"]),
    ('        k = "close"\n        ref = bars[f"btc_{k}"]\n', IMPORT, ["btc_"]),
    ("        ref = reference_frame(bars).close\n", IMPORT, ["reference_frame"]),
    ("        ref = ind.reference_frame(bars).close\n", IMPORT, ["reference_frame"]),
    ("        ref = rf(bars).close\n", AS_RF, ["reference_frame"]),
    ("        f = ind.reference_frame\n        ref = f(bars).close\n", IMPORT, ["reference_frame"]),
    ("        f = reference_frame\n        ref = f(bars).close\n", IMPORT, ["reference_frame"]),
    ('        ref = bars.filter(like="btc").iloc[:, 3]\n', IMPORT, ["btc"]),
    ('        ref = bars[[c for c in bars if c.startswith("btc")][3]]\n', IMPORT, ["btc"]),
], ids=["string", "attribute", "f-string", "helper", "module_helper", "aliased_helper", "attribute_alias",
        "name_alias", "filter_like_stem", "startswith_stem"])
def test_each_way_of_naming_the_group_counts(line, imports, found):
    assert names_group(plugin(line, imports=imports), REF) == found


def test_a_plugin_that_never_names_the_group_has_no_matches():
    assert names_group(plugin(), REF) == []


def test_a_bare_import_of_a_helper_is_not_a_read():
    assert names_group(plugin(imports=AS_RF), REF) == []          # PLUGIN imports it unaliased, unused, too


def test_a_comment_does_not_count():
    assert names_group(plugin("        ref = bars.close  # not bars.btc_close, nor reference_frame(bars)\n"),
                       REF) == []


@pytest.mark.parametrize("doc, found", [
    ("Momentum on close; a btc_close variant is for later.", ["btc_close"]),
    ("Momentum on close; the btc_ columns are for later.", ["btc_"]),
    ("Momentum on close; no btc here.", []),
], ids=["column", "prefix", "stem"])
def test_a_docstring_counts_by_column_or_prefix_not_by_stem(doc, found):
    # conservative: a docstring is a string the plugin can read back (`__doc__`), and a false positive only
    # costs a refused paper run; the bare stem is too common in prose to count there
    assert names_group(plugin(doc=doc), REF) == found


def test_each_match_is_listed_once_in_source_order():
    line = '        ref = bars.btc_close + bars["btc_open"] - bars.btc_close\n'
    assert names_group(plugin(line), REF) == ["btc_close", "btc_open"]


def test_the_scan_follows_the_groups_own_names():
    spot = dataclasses.replace(REF, name="spot", columns=("spot_close",), prefix="spot_", helpers=("spot_frame",))
    assert names_group(plugin("        ref = bars.btc_close\n"), spot) == []
    assert names_group(plugin("        ref = bars.spot_close\n"), spot) == ["spot_close"]
    assert names_group(plugin("        ref = spot_frame(bars).close\n"), spot) == ["spot_frame"]
    assert names_group(plugin('        ref = bars.filter(like="spot").iloc[:, 3]\n'), spot) == ["spot"]


def test_a_static_match_needs_no_run(cfg, monkeypatch):
    def no_run(*a, **kw):
        raise AssertionError("ran the plugin")

    monkeypatch.setattr(client, "fresh_run", no_run)
    assert reads_columns(cfg, plugin("        ref = bars.btc_close\n"), REF) is True


# ------------------------------------------------------------------ dynamic: reads_columns

BUILT = f"        col = {BUILT_NAME}\n        ref = bars[col]\n"


def test_a_name_built_at_run_time_is_caught_by_the_runs(cfg):
    src = plugin(BUILT)
    assert names_group(src, REF) == []
    assert reads_columns(cfg, src, REF) is True


def test_a_plugin_that_never_reads_the_group_is_not_caught(cfg):
    src = plugin()
    assert actions(cfg, src, check_bars()) > 0
    assert reads_columns(cfg, src, REF) is False


def test_feature_passthrough_is_not_reading(cfg):
    # every bar column, the btc_ ones too, comes back as a feature; the decisions use close only
    passthrough = plugin().text.decode().replace(
        "        return bars[[]].assign(", "        return bars.copy().assign(")
    src = PluginSource.from_text("passthrough", passthrough)
    assert names_group(src, REF) == []
    assert actions(cfg, src, check_bars()) > 0
    assert reads_columns(cfg, src, REF) is False


def test_given_params_are_run_as_well_as_the_defaults(cfg):
    src = plugin(f'        col = {BUILT_NAME} if self.p["k"] == 2 else "close"\n        ref = bars[col]\n')
    assert reads_columns(cfg, src, REF) is False
    assert reads_columns(cfg, src, REF, params={"k": 2}) is True


# The reviewer's two probes, which read BTC under an alias and were missed: a filter that drops the coin's
# own momentum on a 4-sigma BTC shock, and a follower of such shocks. The check's bars hold one such shock
# (bar 2192); seed 1's and seed 2's streams hold none.
SHOCK_FILTER = ("        b = np.log(READ).diff()\n        z = (b / b.rolling(100).std()).abs()\n"
                "        own = np.sign(bars.close - bars.close.shift(1))\n        sig = own.where(~(z > 4), 0)\n")
SHOCK_FOLLOW = ("        b = np.log(READ).diff()\n        z = b / b.rolling(100).std()\n"
                "        sig = np.sign(z).where(z.abs() > 4.0, 0)\n")


def shock(rule: str, read: str) -> PluginSource:
    return plugin("", sig=rule.replace("READ", read), imports=AS_RF if read.startswith("rf(") else IMPORT)


@pytest.mark.parametrize("rule", [SHOCK_FILTER, SHOCK_FOLLOW], ids=["shock_filter", "shock_follow"])
def test_the_reviewers_aliased_btc_shock_probes_are_caught(cfg, rule):
    src = shock(rule, "rf(bars).close")
    assert causality_failures(cfg, src) == []
    assert names_group(src, REF) == ["reference_frame"]
    assert reads_columns(cfg, src, REF) is True

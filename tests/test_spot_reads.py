"""Which plugins read the spot group (design 2, Task 7; Reconciliations 11, 12 and 14): `names_group` counts an
unprefixed column such as `basis` outside docstrings only, and `reads_columns` and `lab paper` work for it."""
import pytest
from typer.testing import CliRunner

from perp_lab.cli import app
from perp_lab.data import columns
from perp_lab.engine.backtest import EngineParams
from perp_lab.paper.runner import PaperRunner
from perp_lab.plugins import PluginSource, names_group, reads_columns
from perp_lab.sandbox import client
from perp_lab.store import db
from perp_lab.validate.causality import check_bars
from tests.judge_fixtures import add_plugin, plugins  # noqa: F401
from tests.test_reads_columns import actions
from tests.test_reference_judge import approved_paper_run
from tests.test_store import cfg  # noqa: F401

SPOT = next(g for g in columns.GROUPS if g.name == "spot")
runner = CliRunner()

PLUGIN = '''"""DOC"""
from perp_lab.strategy import HOLD, Strategy, enter


class Momentum(Strategy):
    params = {}
    timeframes = ["4h"]

    def prepare(self, bars):
        ref = bars.close.shift(1)
        return bars[[]].assign(sig=(bars.close - ref).apply(lambda x: 0 if x != x else (1 if x > 0 else -1)))

    def entry(self, ctx):
        s = ctx["sig"]
        if s == 0:
            return HOLD
        return enter(s, ctx["close"] * (1 - 0.05 * s), None)
'''
REF_LINE = "        ref = bars.close.shift(1)\n"


def plugin(ref_line: str = REF_LINE, doc: str = "Momentum on the perp close.") -> PluginSource:
    return PluginSource.from_text("fixture", PLUGIN.replace(REF_LINE, ref_line).replace("DOC", doc))


# ------------------------------------------------------------------ static

@pytest.mark.parametrize("line, found", [
    ('        ref = bars["basis"]\n', ["basis"]),
    ("        ref = bars.basis\n", ["basis"]),
    ('        ref = bars[["spot_close", "close"]].iloc[:, 0]\n', ["spot_close"]),
    ('        ref = bars.filter(like="spot_").iloc[:, 0]\n', ["spot_"]),
    ('        ref = bars.filter(like="spot").iloc[:, 0]\n', ["spot"]),       # the stem counts outside docstrings
], ids=["string", "attribute", "list", "prefix", "stem"])
def test_each_way_of_naming_spot_counts(line, found):
    assert names_group(plugin(line), SPOT) == found


def test_a_plugin_that_names_no_spot_has_no_matches():
    assert names_group(plugin(), SPOT) == []


def test_docstring_saying_basis_is_no_hit():
    text = '"""Fades the basis."""\n' + PLUGIN.split("\n", 1)[1].replace(
        "class Momentum(Strategy):\n", 'class Momentum(Strategy):\n    """Not about the basis, nor spot prices."""\n')
    assert names_group(PluginSource.from_text("doc", text), SPOT) == []


@pytest.mark.parametrize("doc, found", [
    ("Fades spot_close against close.", ["spot_close"]),
    ("Reads the spot_ columns.", ["spot_"]),
], ids=["column", "prefix"])
def test_docstring_naming_spot_close_still_counts(doc, found):
    assert names_group(plugin(doc=doc), SPOT) == found


# ------------------------------------------------------------------ dynamic

def test_runtime_spot_name_is_read(cfg):
    src = plugin('        col = "sp" + "ot_close"\n        ref = bars[col]\n')
    assert names_group(src, SPOT) == []
    assert reads_columns(cfg, src, SPOT) is True


def test_plugin_reading_no_spot_is_not_a_reader(cfg):
    src = plugin()
    assert actions(cfg, src, check_bars()) > 0
    assert reads_columns(cfg, src, SPOT) is False


@pytest.mark.parametrize("line", [
    "        ref = bars.dropna().close.shift(1)\n",
    "        ref = bars.close.shift(1).mask(bars.isna().any(axis=1))\n",
    '        ref = bars.close.shift(1).mask(bars["ba" + "sis"].isna())\n',
], ids=["whole_frame_dropna", "any_nan_in_the_row", "gated_on_basis_nan"])
def test_plugin_reading_only_where_spot_is_nan_is_a_reader(cfg, line):
    # the swapped stream keeps spot's NaN rows in place, so only the run on all-NaN spot can tell
    src = plugin(line)
    assert names_group(src, SPOT) == []
    checked = check_bars()
    a, b = (client.fresh_run(cfg, src, "X", "1h", bars, {}, EngineParams()).decisions
            for bars in (checked, checked.assign(**SPOT.synthetic(checked, 1))))
    assert a.n_actions() > 0 and a.same(b)
    assert reads_columns(cfg, src, SPOT) is True


# ------------------------------------------------------------------ paper

READS_SPOT = '''"""Long when the perp trades rich to spot."""
from perp_lab.strategy import HOLD, Strategy, enter


class ReadsSpot(Strategy):
    params = {"n": 20}                         # the fake run's base params: a param the plugin lacks fails a run
    timeframes = ["4h"]

    def prepare(self, bars):
        return bars[[]].assign(rich=bars.basis.apply(lambda x: 0 if x != x else (1 if x > 0 else -1)))

    def entry(self, ctx):
        s = ctx["rich"]
        return HOLD if s == 0 else enter(s, ctx["close"] * (1 - 0.05 * s), None)
'''


def test_paper_refuses_spot_reader_before_recording(tmp_path, monkeypatch, plugins):
    add_plugin(plugins, "reads_spot", READS_SPOT)
    cfg = approved_paper_run(tmp_path, "reads_spot")
    monkeypatch.setattr(client, "max_workers", lambda c: 8)
    monkeypatch.setattr(PaperRunner, "run", lambda self, max_bars=None: pytest.fail("the paper run started"))
    before = sorted(cfg.runs_dir.iterdir())
    r = runner.invoke(app, ["paper", "c1", "--config", str(cfg.path)])
    assert (r.exit_code, type(r.exception)) == (2, SystemExit), r.output
    assert ("reads_spot reads, or can't be shown not to read, the spot column group (spot_ columns), which paper "
            "trading can't feed yet") in r.output
    assert db.read_table(cfg, db.runs, db.runs.c.validation_mode == "paper").empty
    assert sorted(cfg.runs_dir.iterdir()) == before


READS_BOTH = '''"""Long when BTC's close beats the coin's and the perp trades rich to spot."""
from perp_lab.indicators import reference_frame
from perp_lab.strategy import HOLD, Strategy, enter


class ReadsBoth(Strategy):
    params = {"n": 20}
    timeframes = ["4h"]

    def prepare(self, bars):
        return bars[[]].assign(btc=reference_frame(bars).close, rich=bars.basis)

    def entry(self, ctx):
        return enter(1, 0.9 * ctx["close"]) if ctx["btc"] > ctx["close"] and ctx["rich"] > 0 else HOLD
'''


def test_paper_refusal_names_every_unfed_group(tmp_path, monkeypatch, plugins):
    add_plugin(plugins, "reads_both", READS_BOTH)
    cfg = approved_paper_run(tmp_path, "reads_both")
    monkeypatch.setattr(client, "max_workers", lambda c: 8)
    monkeypatch.setattr(PaperRunner, "run", lambda self, max_bars=None: pytest.fail("the paper run started"))
    before = sorted(cfg.runs_dir.iterdir())
    r = runner.invoke(app, ["paper", "c1", "--config", str(cfg.path)])
    assert (r.exit_code, type(r.exception)) == (2, SystemExit), r.output
    assert ("reads_both reads, or can't be shown not to read, the reference and spot column groups (btc_ and "
            "spot_ columns), which paper trading can't feed yet") in r.output
    assert db.read_table(cfg, db.runs, db.runs.c.validation_mode == "paper").empty
    assert sorted(cfg.runs_dir.iterdir()) == before

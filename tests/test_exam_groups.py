"""The exam's column-group coverage through the registry (spot-basis plan, Task 8; design 1's X1 and X4): each
group declares where its exam data ends (`exam_end`) and its exam data problems (`exam_problems`); the exam
covers the `always` groups, plus each `if_read` group the plugin reads (`exam.exam_groups`)."""

import dataclasses
import json
from pathlib import Path

import pandas as pd
import pytest

from perp_lab.data import columns, reference
from perp_lab.judge import budget, exam
from perp_lab.judge.judge import attempt_exam, verdict_path
from perp_lab.plugins import PluginSource
from perp_lab.sandbox.errors import SandboxUnavailable
from perp_lab.store import db
from tests.conftest import market_config, write_market
from tests.judge_fixtures import plugins  # noqa: F401
from tests.test_exam import END, _drop, _manifest
from tests.test_judge_exam import DAY_200, boom, no_cost, promoted, stub_run
from tests.test_reference_judge import btc_ends_early
from tests.test_store import cfg  # noqa: F401

REF = next(g for g in columns.GROUPS if g.name == "reference")
SPOT = next(g for g in columns.GROUPS if g.name == "spot")


def T(s):
    return pd.Timestamp(s, tz="UTC")


def ms(s):
    return T(s).value // 10**6


def fake(name, exam_required, exam_end=None, exam_problems=None):
    """A registered group that covers the exam as told: the reference's entry with its name, requirement and
    exam hooks replaced."""
    return dataclasses.replace(REF, name=name, exam_required=exam_required, exam_end=exam_end,
                               exam_problems=exam_problems)


# ------------------------------------------------------------------ window_end and window_data_problems

def test_window_end_default_covers_always_groups(tmp_path):
    cfg = market_config(tmp_path)
    btc_ends_early("candles")(cfg)                       # BTC's last 4h close: 2026-09-20 16:00
    assert exam.window_end(cfg, ["ETHUSDT"], "4h") == T("2026-09-20")
    assert exam.window_end(cfg, ["ETHUSDT"], "4h", groups=(REF,)) == T("2026-09-20")
    assert exam.window_end(cfg, ["ETHUSDT"], "4h", groups=()) == END          # ETH's own data runs on
    assert exam.window_end(cfg, cfg.symbols, "4h", groups=()) == T("2026-09-20")   # BTC as an exam coin
    early = lambda cfg_, symbols, tf: ms("2026-08-10 13:00")                  # noqa: E731
    with columns.registered(fake("read_early", "if_read", exam_end=early),
                            fake("no_end", "always", exam_end=lambda *a: None), fake("no_hook", "always")):
        assert exam.window_end(cfg, ["ETHUSDT"], "4h") == T("2026-09-20")     # None and if_read add nothing
        assert exam.window_end(cfg, ["ETHUSDT"], "4h", groups=columns.GROUPS) == T("2026-08-10")
    with columns.registered(fake("always_early", "always", exam_end=early)):
        assert exam.window_end(cfg, ["ETHUSDT"], "4h") == T("2026-08-10")


def test_no_groups_means_no_btc_lines(tmp_path):
    cfg = market_config(tmp_path)
    f = cfg.data_dir / "BTCUSDT" / "metrics" / "all.parquet"
    df = pd.read_parquet(f)
    df[(df.create_time < ms("2026-09-06")) | (df.create_time >= ms("2026-09-07"))].to_parquet(f)
    _manifest(cfg, "BTCUSDT", **{"2026_08": {"complete": False, "source": "ccxt"}})
    lines = ["BTCUSDT 4h 2026-08: stitched from ccxt (no taker, trade-count or quote-volume columns) (BTC reference)",
             "BTCUSDT metrics 2026-09-06: no metrics snapshots (BTC reference)"]
    assert exam.window_data_problems(cfg, ["ETHUSDT"], "4h", END) == lines
    assert exam.window_data_problems(cfg, ["ETHUSDT"], "4h", END, groups=(REF,)) == lines
    assert exam.window_data_problems(cfg, ["ETHUSDT"], "4h", END, groups=()) == []
    # BTC an exam coin: its own month line stays, the reference's metrics line goes
    assert exam.window_data_problems(cfg, cfg.symbols, "4h", END, groups=()) == [lines[0].removesuffix(
        " (BTC reference)")]


def test_window_data_problems_default_covers_always_groups(tmp_path):
    cfg = market_config(tmp_path)
    seen = []

    def problems(line):
        def check(cfg_, symbols, tf, end):
            seen.append((line, symbols, tf, end))
            return [line]
        return check
    with columns.registered(fake("read", "if_read", exam_problems=problems("if_read line")),
                            fake("none", "always", exam_problems=lambda *a: None), fake("no_hook", "always"),
                            fake("always", "always", exam_problems=problems("always line"))):
        assert exam.window_data_problems(cfg, ["ETHUSDT"], "4h", END) == ["always line"]
        assert seen == [("always line", ["ETHUSDT"], "4h", END)]
        assert exam.window_data_problems(cfg, ["ETHUSDT"], "4h", END, groups=columns.GROUPS) == [
            "if_read line", "always line"]                               # in the order given
    assert exam.window_data_problems(cfg, ["ETHUSDT"], "4h", cfg.holdout_start, groups=(
        fake("always", "always", exam_problems=boom),)) == []            # an empty window asks no group


@pytest.mark.parametrize("scenario, symbols, expected", [
    ("no BTC manifest", ["ETHUSDT"], [f"BTCUSDT 4h {m}: no manifest (BTC reference)"
                                      for m in ("2026-07", "2026-08", "2026-09")]),
    ("no BTC manifest", ["BTCUSDT", "ETHUSDT"], [f"BTCUSDT 4h {m}: no manifest"
                                                 for m in ("2026-07", "2026-08", "2026-09")]),
    ("1h", ["ETHUSDT"], ["BTCUSDT 1h 2026-08-03 to 2026-08-04: no candles (2 days) (BTC reference)"]),
    ("1h", ["BTCUSDT", "ETHUSDT"], ["BTCUSDT 1h 2026-08-03 to 2026-08-04: no candles (2 days)"]),
])
def test_reference_exam_lines_are_unchanged(tmp_path, scenario, symbols, expected):
    # Pinned before the move to the registry (reference.exam_problems); the reference's entry declares both hooks.
    assert (REF.exam_end, REF.exam_problems) == (reference.exam_end, reference.exam_problems)
    cfg = market_config(tmp_path)
    tf = "4h"
    if scenario == "no BTC manifest":
        (cfg.data_dir / "BTCUSDT" / "4h" / "_manifest.json").unlink()
    else:
        tf = "1h"
        write_market(cfg.data_dir, cfg.symbols, tfs=("4h", "1h"))
        _manifest(cfg, "BTCUSDT", **{"2026_08": {"complete": False, "source": "ccxt"}})   # 4h: regime labels only
        _drop(cfg, "BTCUSDT", "1h", "2026-08-03", "2026-08-05")
    assert exam.window_data_problems(cfg, symbols, tf, END) == expected
    assert reference.exam_problems(cfg, symbols, tf, END) == [x for x in expected if x.endswith("(BTC reference)")]


# ------------------------------------------------------------------ exam_groups

SPOT_READER = PluginSource.from_text("spot_reader", '''"""Long while the perp trades above spot."""
from perp_lab.strategy import EXIT, HOLD, Strategy, enter


class SpotReader(Strategy):
    params = {}
    timeframes = ["4h"]

    def prepare(self, bars):
        return bars[[]].assign(up=bars.basis > 0)

    def entry(self, ctx):
        return enter(1, 0.8 * ctx["close"]) if ctx["up"] else HOLD

    def manage(self, ctx):
        return HOLD if ctx["up"] else EXIT
''')


def test_exam_groups_reference_always_spot_if_read(tmp_path, monkeypatch):
    cfg = market_config(tmp_path)
    asked, real = [], exam.reads_columns

    def spy(cfg_, src, group, params=None):
        asked.append((src.name, group.name, params))
        return real(cfg_, src, group, params)
    monkeypatch.setattr(exam, "reads_columns", spy)
    got = exam.exam_groups(cfg, SPOT_READER, {})
    assert got == (REF, SPOT) and [g.name for g in got] == ["reference", "spot"]
    params = {"n": 20, "atr_mult": 3.0}
    assert exam.exam_groups(cfg, PluginSource.read("donchian_atr"), params) == (REF,)
    assert asked == [("spot_reader", "spot", {}), ("donchian_atr", "spot", params)]   # never the `always` one


# ------------------------------------------------------------------ attempt_exam

PARAMS = {"n": 40, "atr_n": 14, "atr_mult": 3.0}           # the promoted donchian_atr's base params


def test_attempt_passes_its_groups_to_end_and_data(cfg, plugins):
    budget.init_log(cfg)
    j = promoted(cfg)
    groups, seen = (REF, SPOT), []

    def groups_fn(cfg_, src, params):
        seen.append(("groups", src.name, src.hash, params))
        return groups

    def end_fn(cfg_, symbols, tf, groups_):
        seen.append(("end", symbols, tf, groups_))
        return DAY_200

    def data_fn(cfg_, symbols, tf, end, groups_):
        seen.append(("data", symbols, tf, end, groups_))
        return []
    with pytest.raises(RuntimeError, match="exam crashed"):                 # mints its read, then crashes
        attempt_exam(cfg, j["id"], run=stub_run(0.003, fail=True), end_fn=end_fn, data_fn=data_fn,
                     cost_fn=no_cost, groups_fn=groups_fn)
    first = ("groups", "donchian_atr", j["strategy_hash"], PARAMS)
    assert seen == [first, ("end", cfg.symbols, "4h", groups), ("data", cfg.symbols, "4h", DAY_200, groups)]
    seen.clear()
    done = attempt_exam(cfg, j["id"], run=stub_run(0.003), end_fn=boom, data_fn=data_fn, cost_fn=boom,
                        groups_fn=groups_fn)                                   # a retry: the stored end
    assert done["holdout_status"] == "passed"
    assert seen == [first, ("data", cfg.symbols, "4h", DAY_200, groups)]


def test_groups_fn_sandbox_down_is_pending_exam_error(cfg, plugins):
    budget.init_log(cfg)
    j = promoted(cfg)

    def down(*a, **kw):
        raise SandboxUnavailable("cannot start a worker")
    with pytest.raises(SandboxUnavailable):
        attempt_exam(cfg, j["id"], run=boom, end_fn=boom, data_fn=boom, cost_fn=boom, groups_fn=down)
    assert budget.read_log(budget.log_path(cfg)) == [] and budget.reads_used(cfg) == 0
    r = db.get_judgement_by_id(cfg, j["id"])
    assert (r["holdout_status"], r["holdout_reason"]) == ("pending", "exam_error")
    out = json.loads(Path(verdict_path(cfg, r["strategy"], r["timeframe"], r["strategy_hash"])).read_text())
    assert (out["holdout_status"], out["holdout_reason"]) == ("pending", "exam_error")
    # on a retry, no attempt is used either
    with pytest.raises(RuntimeError, match="exam crashed"):
        attempt_exam(cfg, j["id"], run=stub_run(0.003, fail=True), end_fn=lambda *a: DAY_200,
                     data_fn=lambda *a: [], cost_fn=no_cost, groups_fn=lambda *a: ())
    assert budget.attempts(cfg, j["id"]) == 1
    with pytest.raises(SandboxUnavailable):
        attempt_exam(cfg, j["id"], run=boom, end_fn=boom, data_fn=boom, cost_fn=boom, groups_fn=down)
    assert budget.attempts(cfg, j["id"]) == 1 and budget.reads_used(cfg) == 1

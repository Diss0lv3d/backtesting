"""The BTC reference in the judge, the holdout exam and the CLI (spec
docs/superpowers/specs/2026-11-05-cross-coin-reference-design.md: X3, "Judging" (Exam), "Paper and live path"
(Part A); Testing 10, 12 and 14; Review Focus 4 of the cross-coin plan; ledger rulings R6 and R7)."""
import dataclasses
import json

import numpy as np
import pandas as pd
import pytest
import yaml
from typer.testing import CliRunner

from perp_lab.cli import app
from perp_lab.config import TF_MS
from perp_lab.data import columns, reference
from perp_lab.data.store import load_candles
from perp_lab.holdout import issue_holdout_token
from perp_lab.judge import exam
from perp_lab.judge.judge import JudgeRefused, judge
from perp_lab.plugins import PluginSource
from perp_lab.proposer import context
from perp_lab.sandbox import session
from perp_lab.store import db
from perp_lab.validate import walkforward as wfm
from tests.conftest import CANDLE_COLS, REPO, make_bars, market_config
from tests.judge_fixtures import STRONG, StubValidate, plugins  # noqa: F401
from tests.test_exam import END, _drop, _manifest
from tests.test_stream import assert_same_exam, batch_exam

T = lambda s: pd.Timestamp(s, tz="UTC")  # noqa: E731
runner = CliRunner()


# ------------------------------------------------------------------ readiness: the symbol check (R6)

def btc_candles(cfg, tf, frame):
    """Replace BTCUSDT's candles in its `tf` folder with `frame`."""
    frame[CANDLE_COLS].reset_index(drop=True).to_parquet(cfg.data_dir / "BTCUSDT" / tf / "all.parquet")


def test_symbol_check_names_a_month_of_another_timeframe(tmp_path):
    cfg = market_config(tmp_path)
    assert columns.column_group_problems(cfg, cfg.symbols, "4h") == []
    n = int((T("2026-10-15") - T("2025-01-01")) / pd.Timedelta(hours=1))
    btc_candles(cfg, "4h", make_bars(n=n, seed=3, start="2025-01-01", tf_ms=TF_MS["1h"]))   # 1h data, 4h folder
    assert columns.column_group_problems(cfg, cfg.symbols, "4h") == [
        "column group reference: BTCUSDT/4h 2026-06 has open times 1h apart, not 4h"]


def test_symbol_check_allows_no_gap_inside_its_month(tmp_path):
    cfg = market_config(tmp_path)
    _drop(cfg, "BTCUSDT", "4h", "2026-06-10 08:00", "2026-06-10 12:00")         # one bar
    assert columns.column_group_problems(cfg, cfg.symbols, "4h") == [
        "column group reference: BTCUSDT/4h 2026-06 has open times 8h apart, not 4h"]


def test_symbol_check_reads_one_research_month_of_open_times_only(tmp_path, monkeypatch):
    cfg = market_config(tmp_path)
    _drop(cfg, "BTCUSDT", "4h", "2026-05-10 08:00", "2026-05-10 12:00")         # an earlier month: not checked
    _drop(cfg, "BTCUSDT", "4h", "2026-07-10 08:00", "2026-07-10 12:00")         # the holdout: never read
    reads = []
    real = pd.read_parquet
    monkeypatch.setattr(pd, "read_parquet", lambda p, **kw: reads.append((p, kw)) or real(p, **kw))
    assert reference.symbol_check(cfg, "4h") == []
    assert reads and all(kw["columns"] == ["open_time"] for _, kw in reads)
    hs = int(cfg.holdout_start.value // 10**6)
    assert all(("open_time", "<", hs) in kw["filters"] for _, kw in reads)
    assert all(("open_time", ">=", int(T("2026-06-01").value // 10**6)) in kw["filters"] for _, kw in reads)


def test_symbol_check_names_an_empty_month(tmp_path):
    cfg = market_config(tmp_path)
    _drop(cfg, "BTCUSDT", "4h", "2026-06-01", "2026-07-01")
    assert "column group reference: BTCUSDT/4h 2026-06 has no candles" in reference.symbol_check(cfg, "4h")


# ------------------------------------------------------------------ readiness in the judge (R7) and the CLI

def incomplete_metrics_month(cfg):
    p = cfg.data_dir / "BTCUSDT" / "metrics" / "_manifest.json"
    m = json.loads(p.read_text())
    m["2025-06"]["complete"] = False
    p.write_text(json.dumps(m))


METRICS_PROBLEM = "column group reference: BTCUSDT/metrics 2025-06 is incomplete"


@pytest.mark.parametrize("stub", [False, True])
def test_judge_refuses_before_register_trial(tmp_path, plugins, stub):
    cfg = market_config(tmp_path)
    incomplete_metrics_month(cfg)
    v = StubValidate({"donchian_atr": ("candidate", STRONG)})
    with pytest.raises(JudgeRefused, match=METRICS_PROBLEM):
        judge(cfg, "donchian_atr", "4h", **({"validate": v} if stub else {}))
    assert v.calls == []
    assert db.trial_stats(cfg)[0] == 0 and db.read_table(cfg, db.trials).empty      # N unchanged
    assert db.read_table(cfg, db.judgements).empty


def test_judge_refuses_before_the_parent_runs(tmp_path, plugins):
    cfg = market_config(tmp_path)
    incomplete_metrics_month(cfg)
    v = StubValidate({"donchian_atr": ("candidate", STRONG), "donchian_atr_wide": ("candidate", STRONG)})
    with pytest.raises(JudgeRefused, match=METRICS_PROBLEM):
        judge(cfg, "donchian_atr_wide", "4h", validate=v)
    assert v.calls == [] and db.read_table(cfg, db.trials).empty


def test_lab_judge_prints_the_problems_and_exits_2(tmp_path, plugins):
    cfg = market_config(tmp_path)
    incomplete_metrics_month(cfg)
    r = runner.invoke(app, ["judge", "donchian_atr", "--tf", "4h", "--config", str(cfg.path)])
    assert (r.exit_code, type(r.exception)) == (2, SystemExit), r.output
    assert METRICS_PROBLEM in r.output


def proposer_config(tmp_path, timeframes):
    """lab.yaml over a synthetic market from 2025-01, with the proposer's state under tmp_path."""
    cfg = market_config(tmp_path)
    raw = yaml.safe_load(cfg.path.read_text())
    raw["proposer"] = {**raw["proposer"], "state_dir": str(tmp_path / "proposer"), "timeframes": timeframes}
    cfg.path.write_text(yaml.safe_dump(raw))
    return cfg


def test_propose_refuses_on_problem(tmp_path, monkeypatch):
    monkeypatch.chdir(context.ROOT)
    cfg = proposer_config(tmp_path, ["1h", "4h"])                   # the market has 4h candles only
    incomplete_metrics_month(cfg)
    monkeypatch.setattr("perp_lab.proposer.loop.run_session", lambda *a, **k: pytest.fail("the session started"))
    r = runner.invoke(app, ["propose", "--trials", "0", "--config", str(cfg.path)])
    assert (r.exit_code, type(r.exception)) == (2, SystemExit), r.output
    assert "column group reference: BTCUSDT/1h 2025-01 has no manifest entry" in r.output    # each timeframe
    assert r.output.count(METRICS_PROBLEM) == 1                                         # each problem once
    assert "BTCUSDT/4h" not in r.output
    # the check runs in cli.py: perp_lab/proposer/ still never reads data/
    assert not any("column_group_problems" in f.read_text() for f in (REPO / "perp_lab/proposer").rglob("*.py"))


def test_propose_passes_readiness_on_complete_data(tmp_path, monkeypatch):
    monkeypatch.chdir(context.ROOT)
    cfg = proposer_config(tmp_path, ["4h"])
    monkeypatch.setattr("perp_lab.proposer.loop.run_session", lambda *a, **k: "trials")
    r = runner.invoke(app, ["propose", "--trials", "0", "--config", str(cfg.path)])
    assert r.exit_code == 0 and "stopped: trials" in r.output, r.output


def test_walkforward_refuses_on_problem_without_a_traceback(tmp_path, plugins):
    cfg = market_config(tmp_path)
    incomplete_metrics_month(cfg)
    r = runner.invoke(app, ["walkforward", "donchian_atr", "--tf", "4h", "--workers", "1", "--config", str(cfg.path)])
    assert (r.exit_code, type(r.exception)) == (2, SystemExit), r.output
    assert "refused: column group data not ready at 4h" in r.output and METRICS_PROBLEM in r.output
    assert db.read_table(cfg, db.trials).empty


# ------------------------------------------------------------------ the exam's window

def truncate(cfg, symbol, dataset, col, before, extra=None):
    """Keep `symbol`'s `dataset` rows stamped before `before`, plus the rows of `extra` if given."""
    f = cfg.data_dir / symbol / dataset / "all.parquet"
    df = pd.read_parquet(f)
    df = df[df[col] < T(before).value // 10**6]
    if extra is not None:
        df = pd.concat([df, extra], ignore_index=True)
    df.to_parquet(f)


def btc_ends_early(what):
    def cut(cfg):
        if what == "candles":
            truncate(cfg, "BTCUSDT", "4h", "open_time", "2026-09-20 16:00")       # last close 09-20 16:00
        elif what == "funding":
            truncate(cfg, "BTCUSDT", "funding", "funding_time", "2026-09-12 08:00")  # covers to 09-12 08:00
        else:                                   # the last snapshot at 23:55 is known at 00:00: the 15th
            late = pd.DataFrame({"create_time": [T("2026-09-14 23:55").value // 10**6], "oi": [1e6], "oi_value": [1e8]})
            truncate(cfg, "BTCUSDT", "metrics", "create_time", "2026-09-14 23:00", late)
    return cut


@pytest.mark.parametrize("what, end", [("candles", "2026-09-20"), ("funding", "2026-09-12"),
                                       ("metrics", "2026-09-15")])
def test_window_end_stops_at_btc_end_when_btc_not_an_exam_coin(tmp_path, what, end):
    cfg = market_config(tmp_path)
    assert exam.window_end(cfg, ["ETHUSDT"], "4h") == END
    btc_ends_early(what)(cfg)
    assert exam.window_end(cfg, ["ETHUSDT"], "4h") == T(end)
    assert exam.window_end(cfg, cfg.symbols, "4h") == T(end)                 # and when BTC is one


def test_window_end_reads_btc_metrics_stamped_in_seconds(tmp_path):
    cfg = market_config(tmp_path)
    f = cfg.data_dir / "BTCUSDT" / "metrics" / "all.parquet"
    df = pd.read_parquet(f)
    df = df[df.create_time < T("2026-09-14 23:00").value // 10**6]
    df.assign(create_time=df.create_time // 1000).to_parquet(f)                 # as store.load_metrics guards
    assert exam.window_end(cfg, ["ETHUSDT"], "4h") == T("2026-09-14")


CCXT_LINE = "BTCUSDT 4h 2026-08: stitched from ccxt (no taker, trade-count or quote-volume columns) (BTC reference)"


def test_window_data_problems_names_ccxt_btc_month(tmp_path):
    cfg = market_config(tmp_path)
    assert exam.window_data_problems(cfg, ["ETHUSDT"], "4h", END) == []
    _manifest(cfg, "BTCUSDT", **{"2026_08": {"complete": False, "source": "ccxt"}})
    assert exam.window_data_problems(cfg, ["ETHUSDT"], "4h", END) == [CCXT_LINE]
    # BTC an exam coin: its own line says it once, without the note
    assert exam.window_data_problems(cfg, cfg.symbols, "4h", END) == [CCXT_LINE.removesuffix(" (BTC reference)")]


def test_window_data_problems_names_day_without_btc_metrics(tmp_path):
    cfg = market_config(tmp_path)
    f = cfg.data_dir / "BTCUSDT" / "metrics" / "all.parquet"
    df = pd.read_parquet(f)
    lo, hi = (T(x).value // 10**6 for x in ("2026-09-05 03:00", "2026-09-07"))
    df[(df.create_time < lo) | (df.create_time >= hi)].to_parquet(f)       # 09-05 keeps 3 snapshots
    _drop(cfg, "BTCUSDT", "4h", "2026-08-20", "2026-08-21")
    assert exam.window_data_problems(cfg, ["ETHUSDT"], "4h", END) == [
        "BTCUSDT 4h 2026-08-20: no candles (BTC reference)",
        "BTCUSDT metrics 2026-09-06: no metrics snapshots (BTC reference)"]
    assert exam.window_data_problems(cfg, cfg.symbols, "4h", END) == [
        "BTCUSDT 4h 2026-08-20: no candles",
        "BTCUSDT metrics 2026-09-06: no metrics snapshots (BTC reference)"]


# ------------------------------------------------------------------ the exam's sources and stream

BTC_READER = PluginSource.from_text("btc_trend", '''"""Long while BTC closes above its 20-bar mean."""
from perp_lab.strategy import EXIT, HOLD, Strategy, enter


class BtcTrend(Strategy):
    params = {"n": 20}
    timeframes = ["4h"]

    def prepare(self, bars):
        return bars[[]].assign(up=bars.btc_close > bars.btc_close.rolling(self.p["n"]).mean())

    def entry(self, ctx):
        return enter(1, 0.8 * ctx["close"]) if ctx["up"] else HOLD

    def manage(self, ctx):
        return HOLD if ctx["up"] else EXIT
''')


def token(tmp_path):
    return issue_holdout_token("fam", "reference exam test", tmp_path / "test_log.jsonl")


def test_exam_builds_sources_with_token_not_research_cache(tmp_path, monkeypatch):
    cfg = market_config(tmp_path)
    tok = token(tmp_path)
    monkeypatch.setattr(wfm, "_sources", lambda tf: pytest.fail("the exam read the research cache"))
    built = []

    def counted(g):
        def load_source(cfg, tf, start=None, end=None, holdout=None):
            built.append((tf, start, end, holdout))
            return g.load_source(cfg, tf, start, end, holdout)
        return dataclasses.replace(g, load_source=load_source)
    monkeypatch.setattr(columns, "GROUPS", tuple(counted(g) for g in columns.GROUPS))
    given = []
    real = exam.load_bars
    monkeypatch.setattr(exam, "load_bars", lambda *a, **k: given.append(k.get("sources")) or real(*a, **k))
    res = exam.run_exam(cfg, BTC_READER, "4h", ["ETHUSDT", "BTCUSDT"], {"n": 20}, tok, END)
    assert len(built) == 1 and built[0][:3] == ("4h", None, END) and built[0][3] is tok
    assert len(given) == 2 and given[0] is given[1]
    ref = given[0]["reference"]
    assert (ref.tf, ref.end) == ("4h", END)
    holdout = load_candles(cfg, "BTCUSDT", "4h", start=cfg.holdout_start, end=END, holdout=tok)
    np.testing.assert_array_equal(ref.candles.set_index("open_time").close.reindex(holdout.open_time).to_numpy(),
                                  holdout.close.to_numpy())                 # BTC's holdout bars, with the token
    assert len(res.trades) > 0


def test_streamed_equals_batch_for_btc_reader(tmp_path):
    cfg = market_config(tmp_path)
    tok = token(tmp_path)
    res = exam.run_exam(cfg, BTC_READER, "4h", cfg.symbols, {"n": 20}, tok, END)
    daily, port, trades = batch_exam(cfg, session.load_plugin("btc_trend", BTC_READER.text), "4h", cfg.symbols,
                                     {"n": 20}, tok, END)
    assert len(trades) > 10 and set(trades.symbol) == set(cfg.symbols)
    assert_same_exam(res, daily, port, trades)

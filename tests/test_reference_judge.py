"""The BTC reference in the judge, the holdout exam and the CLI (spec
docs/superpowers/specs/2026-11-05-cross-coin-reference-design.md: X3, "Judging" (Exam), "Paper and live path"
(Part A); Testing 10, 12 and 14; Review Focus 4 of the cross-coin plan; ledger rulings R6 and R7)."""
import json

import pandas as pd
import pytest
import yaml
from typer.testing import CliRunner

from perp_lab.cli import app
from perp_lab.config import TF_MS
from perp_lab.data import columns, reference
from perp_lab.judge.judge import JudgeRefused, judge
from perp_lab.proposer import context
from perp_lab.store import db
from tests.conftest import CANDLE_COLS, REPO, make_bars, market_config
from tests.judge_fixtures import STRONG, StubValidate, plugins  # noqa: F401
from tests.test_exam import _drop

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

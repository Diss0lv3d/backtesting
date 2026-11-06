"""The BTC reference in the judge, the holdout exam and the CLI (spec
docs/superpowers/specs/2026-11-05-cross-coin-reference-design.md: X3, "Judging" (Exam), "Paper and live path"
(Part A); Testing 10, 12 and 14; Review Focus 4 of the cross-coin plan; ledger rulings R6 and R7)."""
import pandas as pd

from perp_lab.config import TF_MS
from perp_lab.data import columns, reference
from tests.conftest import CANDLE_COLS, make_bars, market_config
from tests.test_exam import _drop

T = lambda s: pd.Timestamp(s, tz="UTC")  # noqa: E731


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

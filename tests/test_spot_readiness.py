"""The spot group's readiness (X3 for `spot/<tf>`) and its symbol check (spec "Readiness"; Task 6 of the
spot-basis plan): manifests, the +-1% median-basis check on the last month with 100 bars traded on both
sides, and the refusals that follow in the judge, `validate_strategy`, `lab walkforward` and `lab propose`."""
import json

import pandas as pd
import pytest
import yaml
from typer.testing import CliRunner

from perp_lab.cli import app
from perp_lab.data import columns, spot
from perp_lab.judge.judge import JudgeRefused, judge
from perp_lab.proposer import context
from perp_lab.store import db
from perp_lab.validate import pipeline
from tests.conftest import market_config, spot_candles, traded_month
from tests.judge_fixtures import plugins  # noqa: F401
from tests.test_reference_pipeline import DONCHIAN

runner = CliRunner()
PERP, SPOT = "perp", "spot"


def traded_market(cfg, coins=None, months=("2026-05", "2026-06"), basis=0.0003, last_bars=None,
                  last_scale=1.0, holdout_scale=None):
    """Replace each coin's 4h perp and spot candles with `traded_month`s of `months` (every bar traded on
    both sides), spot at `basis`. For `coins` (default all): the last month keeps `last_bars` bars when
    given and its spot prices are scaled by `last_scale`; `holdout_scale` plants a 2026-07 month of
    spot prices scaled by it."""
    for sym in coins or cfg.symbols:
        perp, spots = [], []
        for m in (*months, *(["2026-07"] if holdout_scale else [])):
            p = traded_month(pd.Period(m, "M"), "4h")
            if m == months[-1] and last_bars:
                p = p.head(last_bars)
            s = spot_candles(p, basis)
            scale = last_scale if m == months[-1] else holdout_scale if m == "2026-07" else 1.0
            for c in ("open", "high", "low", "close"):
                s[c] = s[c] * scale
            perp.append(p), spots.append(s)
        pd.concat(perp, ignore_index=True).to_parquet(cfg.data_dir / sym / "4h" / "all.parquet")
        pd.concat(spots, ignore_index=True).to_parquet(cfg.data_dir / sym / "spot" / "4h" / "all.parquet")


def ready(tmp_path, **kw):
    cfg = market_config(tmp_path)
    traded_market(cfg, **kw)
    return cfg


def exclude(cfg, *names):
    cfg.raw.setdefault("column_groups", {}).setdefault("spot", {})["exclusions"] = list(names)


def edit_manifest(cfg, symbol, change):
    p = cfg.data_dir / symbol / "spot" / "4h" / "_manifest.json"
    m = json.loads(p.read_text())
    change(m)
    p.write_text(json.dumps(m))


def spot_problems(cfg):
    return [p for p in columns.column_group_problems(cfg, cfg.symbols, "4h") if p.startswith("column group spot")]


# ------------------------------------------------------------------ the manifests (generic lines)

def test_ready_market_has_no_spot_problem(tmp_path):
    cfg = ready(tmp_path)
    assert columns.column_group_problems(cfg, cfg.symbols, "4h") == []


def test_mid_span_month_without_entry(tmp_path):
    cfg = ready(tmp_path)
    edit_manifest(cfg, "ETHUSDT", lambda m: m.pop("2025-06"))
    assert spot_problems(cfg) == ["column group spot: ETHUSDT/spot/4h 2025-06 has no manifest entry"]


def test_incomplete_month(tmp_path):
    cfg = ready(tmp_path)
    edit_manifest(cfg, "ETHUSDT", lambda m: m["2025-06"].update(complete=False))
    assert spot_problems(cfg) == ["column group spot: ETHUSDT/spot/4h 2025-06 is incomplete"]


def test_no_spot_manifest(tmp_path):
    cfg = ready(tmp_path)
    (cfg.data_dir / "ETHUSDT" / "spot" / "4h" / "_manifest.json").unlink()
    got = spot_problems(cfg)
    assert "column group spot: ETHUSDT/spot/4h 2025-01 has no manifest entry" in got
    assert "column group spot: ETHUSDT/spot/4h has no real month in the research span" in got


def test_absent_after_first_real_month(tmp_path):
    cfg = ready(tmp_path)
    edit_manifest(cfg, "ETHUSDT", lambda m: m["2025-06"].update(source="absent"))
    assert spot_problems(cfg) == ["column group spot: ETHUSDT/spot/4h 2025-06 is absent after the first real month"]


def test_known_gap_month_passes(tmp_path):
    cfg = ready(tmp_path)
    edit_manifest(cfg, "ETHUSDT", lambda m: m["2025-06"].update(complete=False))
    cfg.raw.setdefault("column_groups", {}).setdefault("spot", {})["known_gaps"] = [
        {"symbol": "ETHUSDT", "dataset": "spot/4h", "month": "2025-06"}]
    assert spot_problems(cfg) == []


@pytest.mark.parametrize("name", ["ETH", "ETHUSDT"])
def test_excluded_coin_skipped(tmp_path, name):
    cfg = ready(tmp_path)
    edit_manifest(cfg, "ETHUSDT", lambda m: m.pop("2025-06"))
    traded_market(cfg, coins=["ETHUSDT"], last_scale=1000.0)         # a bad pair as well: still skipped
    exclude(cfg, name)
    assert spot_problems(cfg) == []


# ------------------------------------------------------------------ the symbol check

def test_1000x_spot_price_is_a_problem(tmp_path):
    cfg = ready(tmp_path)
    traded_market(cfg, coins=["ETHUSDT"], last_scale=1000.0)
    got = spot.symbol_check(cfg, "4h")
    assert len(got) == 1 and got[0].startswith("column group spot: ETHUSDT/spot/4h 2026-06: median basis -99.90%, "
                                               "outside ±1%; another pair? exclude the coin in ")
    assert spot_problems(cfg) == got


def test_a_small_basis_passes_and_a_large_one_fails(tmp_path):
    cfg = ready(tmp_path, basis=0.0099)
    assert spot.symbol_check(cfg, "4h") == []
    traded_market(cfg, coins=["BTCUSDT"], basis=0.0101)
    assert [p.split(":")[1].split("/")[0].strip() for p in spot.symbol_check(cfg, "4h")] == ["BTCUSDT"]


def test_no_month_with_100_traded_bars_is_a_problem(tmp_path):
    cfg = ready(tmp_path)
    traded_market(cfg, coins=["ETHUSDT"], months=("2026-06",), last_bars=99)
    assert spot.symbol_check(cfg, "4h") == [
        "column group spot: ETHUSDT/spot/4h has no research month with 100 bars traded on both perp and spot; "
        "exclude the coin in column_groups.spot.exclusions"]


def test_last_month_with_100_bars_is_used(tmp_path):
    cfg = ready(tmp_path)
    traded_market(cfg, coins=["ETHUSDT"], last_bars=50, last_scale=1000.0)    # 50 bars, bad price: not the month
    assert spot.symbol_check(cfg, "4h") == []
    traded_market(cfg, coins=["ETHUSDT"], months=("2026-05", "2026-06"), last_scale=1.0, basis=0.05)
    assert "2026-06: median basis" in spot.symbol_check(cfg, "4h")[0]


def test_holdout_spot_never_read(tmp_path):
    cfg = ready(tmp_path, holdout_scale=1000.0)
    assert spot.symbol_check(cfg, "4h") == []


def test_excluded_coin_is_not_read(tmp_path):
    cfg = ready(tmp_path)
    (cfg.data_dir / "ETHUSDT" / "spot" / "4h" / "all.parquet").unlink()
    assert len(spot.symbol_check(cfg, "4h")) == 1
    exclude(cfg, "ETH")
    assert spot.symbol_check(cfg, "4h") == []


def plant_trades(cfg, side, value, left=99):
    """ETHUSDT's last research month (2026-06) keeps `left` bars traded on both sides: `value` (0 or NaN)
    goes into the `side` ("perp" or "spot") `trades` of the rest."""
    f = cfg.data_dir / "ETHUSDT" / ("4h" if side == PERP else "spot/4h") / "all.parquet"
    df = pd.read_parquet(f)
    june = (df.open_time >= int(pd.Timestamp("2026-06-01", tz="UTC").value // 10**6)).to_numpy()
    df.loc[june & (df.groupby(june).cumcount() >= left).to_numpy(), "trades"] = value
    df.to_parquet(f)


@pytest.mark.parametrize("side, value", [(PERP, 0.0), (SPOT, 0.0), (PERP, float("nan")), (SPOT, float("nan"))])
def test_bars_must_be_traded_on_both_sides(tmp_path, side, value):
    # June holds 180 bars and a 1000x spot price, but fewer than 100 are traded on both sides: the check
    # falls back to May (a good pair), so there is no problem; a filter on one side or one that counts
    # NaN trades as traded would use June and name its -99.90% median
    cfg = ready(tmp_path)
    traded_market(cfg, coins=["ETHUSDT"], last_scale=1000.0)
    assert len(spot.symbol_check(cfg, "4h")) == 1                           # all traded: June is used
    plant_trades(cfg, side, value)
    assert spot.symbol_check(cfg, "4h") == []
    traded_market(cfg, coins=["ETHUSDT"], last_scale=1000.0)                # 100 traded on both: June again
    plant_trades(cfg, side, value, left=100)
    assert "2026-06: median basis -99.90%" in spot.symbol_check(cfg, "4h")[0]


def test_perp_and_spot_with_no_open_time_in_common_is_a_problem(tmp_path):
    cfg = ready(tmp_path)
    f = cfg.data_dir / "ETHUSDT" / "spot" / "4h" / "all.parquet"
    df = pd.read_parquet(f)
    df["open_time"] += 3_600_000                                            # an hour off every perp bar
    df.to_parquet(f)
    assert spot.symbol_check(cfg, "4h") == [
        "column group spot: ETHUSDT/spot/4h has no research month with 100 bars traded on both perp and spot; "
        "exclude the coin in column_groups.spot.exclusions"]


def test_missing_perp_folder_is_the_no_month_problem(tmp_path):
    cfg = ready(tmp_path)
    (cfg.data_dir / "ETHUSDT" / "4h" / "all.parquet").unlink()
    assert spot.symbol_check(cfg, "4h") == [
        "column group spot: ETHUSDT/spot/4h has no research month with 100 bars traded on both perp and spot; "
        "exclude the coin in column_groups.spot.exclusions"]


# ------------------------------------------------------------------ refusals

PROBLEM = "column group spot: ETHUSDT/spot/4h 2026-06: median basis"


def bad_spot(tmp_path):
    cfg = ready(tmp_path)
    traded_market(cfg, coins=["ETHUSDT"], last_scale=1000.0)
    return cfg


def test_judge_refuses_on_spot_problem(tmp_path, plugins):
    cfg = bad_spot(tmp_path)
    with pytest.raises(JudgeRefused, match=PROBLEM):
        judge(cfg, "donchian_atr", "4h")
    assert db.trial_stats(cfg)[0] == 0 and db.read_table(cfg, db.trials).empty
    assert db.read_table(cfg, db.judgements).empty


def test_validate_refuses_on_spot_problem(tmp_path, monkeypatch):
    cfg = bad_spot(tmp_path)
    monkeypatch.setattr(pipeline, "walkforward", lambda *a, **k: pytest.fail("the walk-forward ran"))
    with pytest.raises(columns.ReadinessRefused, match=PROBLEM):
        pipeline.validate_strategy(cfg, "donchian_atr", "4h", src=DONCHIAN)
    assert db.trial_stats(cfg)[0] == 0 and db.read_table(cfg, db.trials).empty


def test_walkforward_refused_on_spot_problem(tmp_path, plugins):
    cfg = bad_spot(tmp_path)
    r = runner.invoke(app, ["walkforward", "donchian_atr", "--tf", "4h", "--workers", "1", "--config", str(cfg.path)])
    assert (r.exit_code, type(r.exception)) == (2, SystemExit), r.output
    assert "refused: column group data not ready at 4h" in r.output and PROBLEM in r.output
    assert db.trial_stats(cfg)[0] == 0 and db.read_table(cfg, db.trials).empty


def test_propose_exits_2_before_any_model_call(tmp_path, monkeypatch):
    monkeypatch.chdir(context.ROOT)
    cfg = bad_spot(tmp_path)
    raw = yaml.safe_load(cfg.path.read_text())
    raw["proposer"] = {**raw["proposer"], "state_dir": str(tmp_path / "proposer"), "timeframes": ["4h"]}
    cfg.path.write_text(yaml.safe_dump(raw))
    monkeypatch.setattr("perp_lab.proposer.loop.run_session", lambda *a, **k: pytest.fail("the session started"))
    r = runner.invoke(app, ["propose", "--trials", "0", "--config", str(cfg.path)])
    assert (r.exit_code, type(r.exception)) == (2, SystemExit), r.output
    assert PROBLEM in r.output

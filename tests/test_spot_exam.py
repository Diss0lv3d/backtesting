"""Spot coverage in the holdout exam (spot-basis plan, Task 9; spec "Judging", `exam_required: if_read`): for a
plugin that reads spot, the window ends at the earliest last spot candle of the exam's non-excluded coins, spot
months come from exchange files, and every UTC day holds a spot candle with trades (X6). A plugin that reads no
spot is unaffected (Review Focus 3)."""

import json
import shutil

import pandas as pd
import pyarrow
import pytest

from perp_lab.data import archive, columns, spot
from perp_lab.judge import exam
from perp_lab.plugins import PluginSource
from tests.conftest import market_config
from tests.test_exam import END
from tests.test_exam_groups import PARAMS, REF, SPOT, SPOT_READER


def T(s):
    return pd.Timestamp(s, tz="UTC")


def ms(s):
    return T(s).value // 10**6


def _spot_dir(cfg, symbol):
    return cfg.data_dir / symbol / "spot" / "4h"


def _edit_spot(cfg, symbol, a, b, edit):
    """Rewrite `symbol`'s 4h spot candles: `edit(df, rows)` on the rows opening in [a, b)."""
    f = _spot_dir(cfg, symbol) / "all.parquet"
    df = pd.read_parquet(f)
    edit(df, (df.open_time >= ms(a)) & (df.open_time < ms(b))).to_parquet(f)


def _drop_spot(cfg, symbol, a, b):
    _edit_spot(cfg, symbol, a, b, lambda df, rows: df[~rows])


def _zero_trades(cfg, symbol, a, b):
    """X6's flat candles (a spot maintenance or a delisting): the rows stay, with no trades."""
    def edit(df, rows):
        df.loc[rows, "trades"] = 0.0
        return df
    _edit_spot(cfg, symbol, a, b, edit)


def _spot_manifest(cfg, symbol, month, entry):
    """Set (or with None, remove) one month of `symbol`'s spot/4h manifest."""
    p =_spot_dir(cfg, symbol) / "_manifest.json"
    m = json.loads(p.read_text())
    m.pop(month) if entry is None else m.__setitem__(month, {"rows": 0, **entry})
    p.write_text(json.dumps(m))


ZERO_DAY = "ETHUSDT spot/4h 2026-08-20: no spot candles with trades"


def test_every_group_declares_exam_coverage():
    assert [g.name for g in columns.GROUPS if g.exam_end is None or g.exam_problems is None] == []
    assert (SPOT.exam_end, SPOT.exam_problems) == (spot.exam_end, spot.exam_problems)


def test_spot_reader_window_ends_at_earliest_spot_end(tmp_path):
    cfg = market_config(tmp_path)
    assert spot.exam_end(cfg, cfg.symbols, "4h") == ms("2026-10-15")         # the last spot close: candles_until
    _drop_spot(cfg, "ETHUSDT", "2026-09-20 16:00", "2027-01-01")           # ETH's last spot 4h close: 16:00
    assert spot.exam_end(cfg, cfg.symbols, "4h") == ms("2026-09-20 16:00")   # BTC's runs on to 2026-10-15
    assert exam.window_end(cfg, cfg.symbols, "4h", groups=columns.GROUPS) == T("2026-09-20")
    assert exam.window_end(cfg, cfg.symbols, "4h") == END                    # the `always` groups: no spot
    assert exam.window_end(cfg, ["BTCUSDT"], "4h", groups=columns.GROUPS) == END   # ETH no exam coin


@pytest.mark.parametrize("entry, why", [
    (None, "no manifest entry"),
    ({"complete": False, "source": "daily"}, "incomplete (days missing from data.binance.vision)"),
])
def test_spot_month_without_entry_named(tmp_path, entry, why):
    cfg = market_config(tmp_path)
    _spot_manifest(cfg, "ETHUSDT", "2026-08", entry)
    line = f"ETHUSDT spot/4h 2026-08: {why}"
    assert exam.window_data_problems(cfg, cfg.symbols, "4h", END, groups=columns.GROUPS) == [line]
    assert spot.exam_problems(cfg, cfg.symbols, "4h", END) == [line]
    assert exam.window_data_problems(cfg, cfg.symbols, "4h", END) == []    # the `always` groups: no spot


def test_ccxt_spot_month_named_for_spot_reader(tmp_path):
    # a complete month still isn't exchange data when ccxt stitched it: no spot taker, trade-count or quote volume
    cfg = market_config(tmp_path)
    _spot_manifest(cfg, "ETHUSDT", "2026-08", {"source": "ccxt", "complete": True})
    reader = exam.exam_groups(cfg, SPOT_READER, {})
    assert reader == (REF, SPOT)
    assert exam.window_data_problems(cfg, cfg.symbols, "4h", END, reader) == [
        "ETHUSDT spot/4h 2026-08: stitched from ccxt (no taker, trade-count or quote-volume columns)"]
    non_reader = exam.exam_groups(cfg, PluginSource.read("donchian_atr"), PARAMS)
    assert exam.window_data_problems(cfg, cfg.symbols, "4h", END, non_reader) == []


@pytest.mark.parametrize("plant", [_zero_trades, _drop_spot], ids=["zero trades", "dropped"])
def test_zero_trade_spot_day_named(tmp_path, plant):
    cfg = market_config(tmp_path)
    assert spot.exam_problems(cfg, cfg.symbols, "4h", END) == []
    every = archive.stamps(cfg, "ETHUSDT", "spot/4h")
    plant(cfg, "ETHUSDT", "2026-08-20", "2026-08-21")
    assert exam.window_data_problems(cfg, cfg.symbols, "4h", END, groups=columns.GROUPS) == [ZERO_DAY]
    assert exam.window_data_problems(cfg, cfg.symbols, "4h", END) == []
    day = (every >= ms("2026-08-20")) & (every < ms("2026-08-21"))
    assert archive.stamps(cfg, "ETHUSDT", "spot/4h", traded=True).tolist() == every[~day].tolist()
    if plant is _zero_trades:                                     # the flat candles are still candles
        assert archive.stamps(cfg, "ETHUSDT", "spot/4h").tolist() == every.tolist()
        assert archive.missing_days(cfg, "ETHUSDT", "spot/4h", cfg.holdout_start, END) == []


def test_one_traded_spot_candle_keeps_its_day(tmp_path):
    cfg = market_config(tmp_path)
    _zero_trades(cfg, "ETHUSDT", "2026-08-20", "2026-08-20 20:00")          # five of the day's six flat
    assert spot.exam_problems(cfg, cfg.symbols, "4h", END) == []
    _zero_trades(cfg, "ETHUSDT", "2026-08-20 20:00", "2026-08-21")
    assert spot.exam_problems(cfg, cfg.symbols, "4h", END) == [ZERO_DAY]


@pytest.mark.parametrize("written", ["ETH", "ETHUSDT"])
def test_excluded_coin_skipped_in_exam(tmp_path, written):
    cfg = market_config(tmp_path)
    d = _spot_dir(cfg, "ETHUSDT")
    (d / "planted.parquet").write_bytes(b"not a parquet file")            # reading the folder would fail
    (d / "_manifest.json").write_text("{not json")
    for hook in (lambda: spot.exam_end(cfg, cfg.symbols, "4h"),
                 lambda: spot.exam_problems(cfg, cfg.symbols, "4h", END)):
        with pytest.raises(pyarrow.ArrowInvalid):                            # not excluded: the plant bites
            hook()
    cfg.raw["column_groups"]["spot"]["exclusions"] = [written]
    assert spot.exam_end(cfg, ["ETHUSDT"], "4h") is None                   # every exam coin excluded
    assert spot.exam_end(cfg, cfg.symbols, "4h") == ms("2026-10-15")        # BTC's alone
    assert spot.exam_problems(cfg, cfg.symbols, "4h", END) == []
    assert exam.window_end(cfg, cfg.symbols, "4h", groups=columns.GROUPS) == END
    assert exam.window_data_problems(cfg, cfg.symbols, "4h", END, groups=columns.GROUPS) == []


def test_non_reader_exam_ignores_spot_gaps(tmp_path):
    cfg = market_config(tmp_path)
    _drop_spot(cfg, "ETHUSDT", "2026-09-20 16:00", "2027-01-01")           # ETH's spot ends early
    _zero_trades(cfg, "ETHUSDT", "2026-08-20", "2026-08-21")                # and has a day without a trade
    non_reader = exam.exam_groups(cfg, PluginSource.read("donchian_atr"), PARAMS)
    assert non_reader == (REF,)
    assert exam.window_end(cfg, cfg.symbols, "4h", non_reader) == END                    # doesn't end early
    assert exam.window_data_problems(cfg, cfg.symbols, "4h", END, non_reader) == []      # doesn't wait
    reader = exam.exam_groups(cfg, SPOT_READER, {})
    assert reader == (REF, SPOT)
    end = exam.window_end(cfg, cfg.symbols, "4h", reader)
    assert end == T("2026-09-20")
    assert exam.window_data_problems(cfg, cfg.symbols, "4h", end, reader) == [ZERO_DAY]


@pytest.mark.parametrize("how", ["no folder", "manifest only"])
def test_missing_spot_files_end_the_window_at_holdout_start(tmp_path, how):
    cfg = market_config(tmp_path)
    d = _spot_dir(cfg, "ETHUSDT")
    shutil.rmtree(d) if how == "no folder" else (d / "all.parquet").unlink()
    assert spot.exam_end(cfg, cfg.symbols, "4h") == cfg.holdout_start.value // 10**6
    assert exam.window_end(cfg, cfg.symbols, "4h", groups=columns.GROUPS) == cfg.holdout_start
    assert exam.window_end(cfg, cfg.symbols, "4h") == END
    no_days = "ETHUSDT spot/4h 2026-07-01 to 2026-09-30: no spot candles with trades (92 days)"
    months = ([f"ETHUSDT spot/4h {m}: no manifest" for m in ("2026-07", "2026-08", "2026-09")]
              if how == "no folder" else [])
    assert spot.exam_problems(cfg, cfg.symbols, "4h", END) == [*months, no_days]

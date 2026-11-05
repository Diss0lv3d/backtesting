"""The reference group: BTC's bar on every coin's bars (spec "Plugin-facing interface"; Testing 1-3;
Review Focus 1-3 of the cross-coin plan)."""
import dataclasses
import logging
import shutil

import numpy as np
import pandas as pd
import pytest

from perp_lab import runs
from perp_lab.config import TF_MS
from perp_lab.data import columns, reference
from perp_lab.data.store import load_bars
from perp_lab.holdout import HoldoutViolation, issue_holdout_token
from perp_lab.validate import benchmarks
from tests.conftest import market_config, write_market
from tests.test_columns import PRE_CHANGE_DATA_ID

T = lambda s: pd.Timestamp(s, tz="UTC")  # noqa: E731
H = 3_600_000
OI_LAG = 300_000
BAR = [reference.PREFIX + c for c in reference.BAR_COLUMNS]
ASOF = [reference.PREFIX + c for c in reference.ASOF_COLUMNS]


def ms(t) -> int:
    return int((t if isinstance(t, pd.Timestamp) else T(t)).value // 10**6)


@pytest.fixture(autouse=True)
def fresh_warnings(monkeypatch):
    monkeypatch.setattr(reference, "_missing_warned", set())


def eth_open_times(cfg, tf):
    return pd.read_parquet(cfg.data_dir / "ETHUSDT" / tf / "all.parquet").open_time.to_numpy()


def btc_frame(open_time, tf, scale=1.0):
    """BTC candles whose values differ on every bar and follow from its open time."""
    ot = np.asarray(open_time, dtype="int64")
    px = scale * ot / 1e9
    return pd.DataFrame({"open_time": ot, "open": px, "high": px + 2, "low": px - 2, "close": px + 1,
                         "volume": 1.0, "quote_volume": px * 10, "trades": 100.0, "taker_buy_volume": 0.5,
                         "taker_buy_quote_volume": px * 4, "close_time": ot + TF_MS[tf] - 1})


def write_btc(cfg, tf, df, name="all.parquet"):
    d = cfg.data_dir / "BTCUSDT" / tf
    d.mkdir(parents=True, exist_ok=True)
    df.to_parquet(d / name)


def write_btc_metrics(cfg, create_time, oi_value):
    d = cfg.data_dir / "BTCUSDT" / "metrics"
    d.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"create_time": np.asarray(create_time, dtype="int64"), "oi": 1.0,
                  "oi_value": oi_value}).to_parquet(d / "all.parquet")


def hourly_btc_metrics(cfg):
    ct = pd.date_range("2024-12-31", "2026-07-01", freq="1h", tz="UTC", inclusive="left").as_unit("ms").asi8
    write_btc_metrics(cfg, ct, np.arange(len(ct), dtype="float64"))


def write_btc_funding(cfg, funding_time, rate):
    pd.DataFrame({"funding_time": np.asarray(funding_time, dtype="int64"), "funding_rate": rate,
                  "interval_hours": 8.0}).to_parquet(cfg.data_dir / "BTCUSDT" / "funding" / "all.parquet")


def expected(bars, btc):
    """BTC's bar columns looked up at each row's open time (a KeyError if one is missing)."""
    return btc.set_index("open_time").loc[bars.open_time.to_numpy(), list(reference.BAR_COLUMNS)]


@pytest.fixture
def cfg(tmp_path):
    """BTC and ETH at 4h (market_config), ETH at 1h too, and hand-built BTC 1h candles on ETH's grid.
    Research runs 2025-01-01 to 2026-07-01; the candles run to 2026-10-15, funding to 2026-09-30."""
    c = market_config(tmp_path)
    write_market(c.data_dir, ["ETHUSDT"], tfs=("1h",))
    write_btc(c, "1h", btc_frame(eth_open_times(c, "1h"), "1h"))
    return c


def test_reference_group_is_registered_with_the_spec_values():
    g = {g.name: g for g in columns.GROUPS}["reference"]
    assert (reference.COIN, reference.SYMBOL, reference.PREFIX) == ("BTC", "BTCUSDT", "btc_")
    assert g.columns == reference.COLUMNS == (
        "btc_open", "btc_high", "btc_low", "btc_close", "btc_quote_volume", "btc_taker_buy_quote_volume",
        "btc_funding_last", "btc_oi_value")
    assert (g.prefix, g.rule, g.synthetic_id, g.exam_required, g.helpers, g.paper_ready) == (
        "btc_", 1, 1, "always", ("reference_frame",), False)
    assert (g.load_source, g.attach, g.synthetic, g.sources) == (
        reference.load_source, reference.attach, reference.synthetic, reference.sources)


def test_data_id_unchanged_with_the_reference_registered(cfg):
    assert columns.identity_block() == {}
    assert runs.data_id(runs.data_identity(cfg, cfg.symbols)) == PRE_CHANGE_DATA_ID


def test_sources_are_btcs_candles_funding_and_metrics(cfg):
    assert reference.sources(cfg, ["ETHUSDT"], "1h") == [
        ("BTCUSDT", "1h"), ("BTCUSDT", "funding"), ("BTCUSDT", "metrics")]


def test_row_t_carries_btc_bar_t(cfg):
    btc = btc_frame(eth_open_times(cfg, "1h"), "1h")
    b = load_bars(cfg, "ETHUSDT", "1h")
    want = expected(b, btc)
    for c in reference.BAR_COLUMNS:
        np.testing.assert_array_equal(b[reference.PREFIX + c].to_numpy(), want[c].to_numpy())
    plain = load_bars(cfg, "ETHUSDT", "1h", groups=False)
    assert b.index.equals(plain.index)
    own = [c for c in plain.columns if c not in ("regime", "regime_adx")]
    assert list(b.columns) == [*own, *reference.COLUMNS, "regime", "regime_adx"]   # groups before regime


def test_missing_btc_bar_gives_nan_on_that_row_only(cfg):
    btc = btc_frame(eth_open_times(cfg, "1h"), "1h")
    gone = T("2025-03-01 05:00")
    write_btc(cfg, "1h", btc[btc.open_time != ms(gone)])
    hourly_btc_metrics(cfg)
    b = load_bars(cfg, "ETHUSDT", "1h")
    assert list(b.index[b[BAR].isna().any(axis=1)]) == [gone]
    assert b.loc[gone, BAR].isna().all() and b.loc[gone, ASOF].notna().all()


def test_zero_trade_btc_bar_gives_nan_bar_columns_asof_still_set(cfg):
    btc = btc_frame(eth_open_times(cfg, "1h"), "1h")
    dead = T("2025-03-01 05:00")
    btc.loc[btc.open_time == ms(dead), "trades"] = 0.0
    write_btc(cfg, "1h", btc)
    hourly_btc_metrics(cfg)
    b = load_bars(cfg, "ETHUSDT", "1h")
    assert list(b.index[b[BAR].isna().any(axis=1)]) == [dead]
    assert b.loc[dead, BAR].isna().all() and b.loc[dead, ASOF].notna().all()


def test_btc_own_bars_equal_own_columns(cfg):
    btc = btc_frame(eth_open_times(cfg, "1h"), "1h")
    dead = ms("2025-03-01 05:00")
    btc.loc[btc.open_time == dead, "trades"] = 0.0
    write_btc(cfg, "1h", btc)
    hourly_btc_metrics(cfg)
    b = load_bars(cfg, "BTCUSDT", "1h")
    zero = (b.open_time == dead).to_numpy()
    assert zero.sum() == 1
    for c in reference.BAR_COLUMNS:
        assert np.isnan(b[reference.PREFIX + c].to_numpy()[zero]).all()
        np.testing.assert_array_equal(b[reference.PREFIX + c].to_numpy()[~zero], b[c].to_numpy()[~zero])
    for c in reference.ASOF_COLUMNS:
        np.testing.assert_array_equal(b[reference.PREFIX + c].to_numpy(), b[c].to_numpy())


def test_1h_never_takes_4h_value(cfg):
    btc1 = btc_frame(eth_open_times(cfg, "1h"), "1h")
    btc4 = btc_frame(eth_open_times(cfg, "4h"), "4h", scale=1000.0)
    write_btc(cfg, "4h", btc4)
    b1 = load_bars(cfg, "ETHUSDT", "1h")
    b4 = load_bars(cfg, "ETHUSDT", "4h")
    for c in reference.BAR_COLUMNS:
        assert not b1[reference.PREFIX + c].isin(btc4[c]).any()
        np.testing.assert_array_equal(b1[reference.PREFIX + c].to_numpy(), expected(b1, btc1)[c].to_numpy())
        np.testing.assert_array_equal(b4[reference.PREFIX + c].to_numpy(), expected(b4, btc4)[c].to_numpy())


def test_oi_boundary(cfg):
    t = T("2025-03-01 05:00")
    close = ms(t) + H - 1
    write_btc_metrics(cfg, [close - OI_LAG, close - OI_LAG + 1], [1.0, 2.0])
    b = load_bars(cfg, "ETHUSDT", "1h")
    k = b.index.get_loc(t)
    assert b.close_time.iloc[k] == close
    assert b.btc_oi_value.iloc[:k].isna().all()
    assert b.btc_oi_value.iloc[k] == 1.0            # known exactly at the close: on bar t
    assert b.btc_oi_value.iloc[k + 1] == 2.0        # 1 ms later: not on bar t, on bar t + 1


def test_funding_settlement_at_bar_open_belongs_to_that_bar(cfg):
    o = T("2025-03-01 08:00")
    write_btc_funding(cfg, [ms(o) - 8 * H, ms(o)], [0.0001, 0.0042])
    b = load_bars(cfg, "ETHUSDT", "1h")
    assert b.btc_funding_last.loc[o - pd.Timedelta(hours=1)] == 0.0001
    assert b.btc_funding_last.loc[o] == 0.0042
    assert b.funding_last.loc[o] == 0.0001          # ETH's own funding: the value above is BTC's


def plant_holdout(cfg):
    """An absurd BTC 4h price at holdout_start, and an absurd funding rate and OI after it."""
    hs = ms("2026-07-01")
    p = cfg.data_dir / "BTCUSDT" / "4h" / "all.parquet"
    c = pd.read_parquet(p)
    c.loc[c.open_time == hs, ["open", "high", "low", "close"]] = 1e12
    c.to_parquet(p)
    p = cfg.data_dir / "BTCUSDT" / "funding" / "all.parquet"
    f = pd.read_parquet(p)
    f.loc[f.funding_time > hs, "funding_rate"] = 9.99
    f.to_parquet(p)
    write_btc_metrics(cfg, [ms("2026-06-01"), hs + H], [5.0, 1e15])


def test_holdout_rows_absent_without_token(cfg):
    plant_holdout(cfg)
    b = load_bars(cfg, "ETHUSDT", "4h")
    assert b.index.max() < T("2026-07-01")
    assert b.btc_close.notna().all() and (b.btc_close < 1e12).all()
    assert not (b.btc_funding_last == 9.99).any()
    assert not (b.btc_oi_value == 1e15).any() and b.btc_oi_value.iloc[-1] == 5.0


def test_source_holds_nothing_at_or_after_its_end(cfg):
    plant_holdout(cfg)
    for end in (None, T("2026-01-01")):
        e = ms(end or "2026-07-01")
        src = reference.load_source(cfg, "4h", end=end)
        assert len(src.candles) and len(src.funding)
        assert (src.candles.open_time < e).all()
        assert (src.funding.funding_time < e).all()
        assert (src.metrics.create_time < e).all()
    assert src.metrics.empty and src.funding.funding_time.max() == ms("2025-12-31 16:00")


def test_end_past_holdout_without_token_raises(cfg):
    with pytest.raises(HoldoutViolation):
        load_bars(cfg, "ETHUSDT", "4h", end=T("2026-08-01"))
    with pytest.raises(HoldoutViolation):
        reference.load_source(cfg, "4h", end=T("2026-08-01"))


def test_with_token_holdout_rows_appear(cfg, tmp_path):
    plant_holdout(cfg)
    tok = issue_holdout_token("lineage", "test", tmp_path / "holdout.jsonl")
    b = load_bars(cfg, "ETHUSDT", "4h", end=T("2026-10-01"), holdout=tok)
    assert b.index.max() == T("2026-09-30 20:00")
    assert b.btc_close.loc[T("2026-07-01")] == 1e12
    assert b.btc_oi_value.loc[T("2026-07-01")] == 1e15                 # stamped 01:00, known 01:05
    assert b.btc_funding_last.loc[T("2026-07-01 04:00")] == 0.0001
    assert b.btc_funding_last.loc[T("2026-07-01 08:00")] == 9.99


def test_no_btc_data_gives_nan_and_one_warning(tmp_path, caplog):
    cfg = market_config(tmp_path, universe=["ETH"])
    assert not (cfg.data_dir / "BTCUSDT").exists()
    with caplog.at_level(logging.WARNING):
        a = load_bars(cfg, "ETHUSDT", "4h")
        load_bars(cfg, "ETHUSDT", "4h")
    assert a[list(reference.COLUMNS)].isna().all().all() and len(a)
    warned = [r.getMessage() for r in caplog.records if r.name == "perp_lab.data.reference"]
    assert len(warned) == 1 and "reference" in warned[0] and "4h" in warned[0]


def test_duplicate_btc_open_time_keeps_row_count(cfg):
    btc = btc_frame(eth_open_times(cfg, "1h"), "1h")
    overlap = btc[btc.open_time >= ms("2025-03-01")].iloc[:72]        # a daily file over a monthly one
    write_btc(cfg, "1h", overlap, "overlap.parquet")
    plain = load_bars(cfg, "ETHUSDT", "1h", groups=False)
    b = load_bars(cfg, "ETHUSDT", "1h")
    assert b.index.equals(plain.index)
    np.testing.assert_array_equal(b.btc_close.to_numpy(), expected(b, btc).close.to_numpy())
    src = reference.load_source(cfg, "1h")
    dup = reference.ReferenceSource(pd.concat([src.candles, src.candles.iloc[100:200]]).iloc[::-1],
                                    src.funding, src.metrics)
    got = load_bars(cfg, "ETHUSDT", "1h", sources={"reference": dup})
    pd.testing.assert_frame_equal(got, b)


def test_btc_shorter_than_coin_gives_nan_tail(cfg):
    btc = btc_frame(eth_open_times(cfg, "1h"), "1h")
    a, z = ms("2025-03-01"), ms("2026-01-01")
    write_btc(cfg, "1h", btc[(btc.open_time >= a) & (btc.open_time < z)])
    b = load_bars(cfg, "ETHUSDT", "1h")
    inside = ((b.open_time >= a) & (b.open_time < z)).to_numpy()
    assert inside.any() and (~inside).any()
    assert b.loc[~inside, BAR].isna().all().all() and b.loc[inside, BAR].notna().all().all()
    assert b.index.equals(load_bars(cfg, "ETHUSDT", "1h", groups=False).index)


def test_btc_columns_are_float64_even_all_nan(cfg):
    btc = btc_frame(eth_open_times(cfg, "1h"), "1h")
    for c in [*reference.BAR_COLUMNS, "trades"]:
        btc[c] = btc[c].round().astype("int64")
    write_btc(cfg, "1h", btc)
    write_btc_metrics(cfg, [ms("2024-12-01")], np.array([7], dtype="int64"))
    b = load_bars(cfg, "ETHUSDT", "1h")
    assert b[list(reference.COLUMNS)].notna().all().all()
    assert all(b[c].dtype == "float64" for c in reference.COLUMNS)
    empty = reference.attach(b.iloc[:0], "ETHUSDT", "1h", reference.load_source(cfg, "1h"))
    assert len(empty) == 0 and all(empty[c].dtype == "float64" for c in reference.COLUMNS)
    shutil.rmtree(cfg.data_dir / "BTCUSDT")
    b = load_bars(cfg, "ETHUSDT", "1h")
    assert b[list(reference.COLUMNS)].isna().all().all()
    assert all(b[c].dtype == "float64" for c in reference.COLUMNS)


def test_a_given_source_is_used_instead_of_a_load(cfg):
    src = reference.load_source(cfg, "1h")
    want = load_bars(cfg, "ETHUSDT", "1h")
    shutil.rmtree(cfg.data_dir / "BTCUSDT")
    pd.testing.assert_frame_equal(load_bars(cfg, "ETHUSDT", "1h", sources={"reference": src}), want)


def test_buy_and_hold_unchanged(cfg):
    a, z = T("2025-03-01"), T("2026-07-01")
    want = benchmarks.buy_and_hold(cfg, ["ETHUSDT"], "4h", a, z)

    def boom(*args, **kwargs):
        raise AssertionError("buy_and_hold loaded a column group")

    ref = {g.name: g for g in columns.GROUPS}["reference"]
    with columns.registered(dataclasses.replace(ref, name="boom", load_source=boom)):
        got = benchmarks.buy_and_hold(cfg, ["ETHUSDT"], "4h", a, z)
    pd.testing.assert_series_equal(got["daily"], want["daily"])
    assert got["portfolio"] == want["portfolio"]

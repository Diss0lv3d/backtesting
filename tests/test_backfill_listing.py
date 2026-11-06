import json
import shutil

import numpy as np
import pandas as pd

from perp_lab.data import vision
from perp_lab.data.store import load_candles
from tests.conftest import market_config

T = lambda s: pd.Timestamp(s, tz="UTC")  # noqa: E731
XML = ("<ListBucketResult>"
       "<Contents><Key>data/futures/um/monthly/klines/SOLUSDT/1h/SOLUSDT-1h-2020-09.zip</Key></Contents>"
       "<Contents><Key>data/futures/um/monthly/klines/SOLUSDT/1h/SOLUSDT-1h-2020-09.zip.CHECKSUM</Key></Contents>"
       "<Contents><Key>data/futures/um/monthly/klines/SOLUSDT/1h/SOLUSDT-1h-2020-10.zip</Key></Contents>"
       "</ListBucketResult>")


def stub(monkeypatch, first):
    """The listing says `first`; every fetch 404s. Returns (lookups, fetched urls)."""
    looks, urls = [], []
    monkeypatch.setattr(vision, "first_archive_month", lambda *a, **kw: looks.append(a[:3]) or first)
    monkeypatch.setattr(vision, "_fetch_zip_csv", lambda url, *a, **kw: urls.append(url) or None)
    return looks, urls


def test_months_before_the_first_archive_file_are_absent_and_never_fetched(tmp_path, monkeypatch):
    cfg = market_config(tmp_path)
    looks, urls = stub(monkeypatch, pd.Period("2025-03", "M"))
    run = lambda: vision.backfill(cfg, ["SOLUSDT"], ["1h"], ["klines"], start=T("2025-01-01"),  # noqa: E731
                                  end=T("2025-03-31"), workers=1)
    assert run()["absent"] == 2 and len(looks) == 1
    assert not [u for u in urls if "2025-01" in u or "2025-02" in u]
    man = json.loads((cfg.data_dir / "SOLUSDT" / "1h" / "_manifest.json").read_text())
    assert man["2025-01"] == man["2025-02"] == {"complete": True, "source": "absent", "rows": 0}
    urls.clear()
    again = run()
    assert again["absent"] == 0 and again["skip"] == 2
    assert not [u for u in urls if "2025-01" in u or "2025-02" in u]


def test_a_complete_group_needs_no_listing(tmp_path, monkeypatch):
    cfg = market_config(tmp_path)
    looks, _ = stub(monkeypatch, pd.Period("2025-03", "M"))
    m = vision.Manifest(cfg.data_dir / "SOLUSDT" / "1h")
    for k in ("2025-01", "2025-02"):
        m.mark(k, complete=True, source="monthly", rows=1)
    vision.backfill(cfg, ["SOLUSDT"], ["1h"], ["klines"], start=T("2025-01-01"), end=T("2025-02-28"), workers=1)
    assert looks == []


def test_metrics_months_before_the_first_daily_file_are_absent(tmp_path, monkeypatch):
    cfg = market_config(tmp_path)
    _, urls = stub(monkeypatch, pd.Period("2025-02", "M"))
    s = vision.backfill(cfg, ["SOLUSDT"], [], ["metrics"], start=T("2025-01-01"), end=T("2025-02-28"), workers=1)
    assert s["absent"] == 1 and not [u for u in urls if "2025-01-" in u]


def test_listing_lookups_use_a_small_retry_budget_of_their_own(tmp_path, monkeypatch):
    cfg = market_config(tmp_path)
    seen = []
    monkeypatch.setattr(vision, "first_archive_month", lambda *a, **kw: seen.append(a[3]) or None)
    monkeypatch.setattr(vision, "_fetch_zip_csv", lambda url, *a, **kw: None)
    vision.backfill(cfg, ["SOLUSDT", "ETHUSDT"], ["1h"], ["klines"], start=T("2025-01-01"),
                    end=T("2025-01-31"), workers=2)
    assert len(seen) == 2 and max(seen) <= 2


def test_an_unreadable_listing_marks_nothing_absent(tmp_path, monkeypatch):
    cfg = market_config(tmp_path)
    _, urls = stub(monkeypatch, None)
    s = vision.backfill(cfg, ["SOLUSDT"], ["1h"], ["klines"], start=T("2025-01-01"), end=T("2025-01-31"), workers=1)
    assert s["absent"] == 0 and s["missing"] == 1 and any("2025-01" in u for u in urls)   # today's behaviour


def test_first_archive_month_reads_the_earliest_zip(monkeypatch):
    seen = []
    monkeypatch.setattr(vision, "_get", lambda url, r=6: seen.append(url) or XML.encode())
    assert vision.first_archive_month("SOLUSDT", "klines", "1h", 1) == pd.Period("2020-09", "M")
    assert "prefix=data/futures/um/monthly/klines/SOLUSDT/1h/" in seen[0]
    vision.first_archive_month("SOLUSDT", "metrics", None, 1)
    assert "prefix=data/futures/um/daily/metrics/SOLUSDT/" in seen[1]


def test_first_archive_month_is_none_when_the_listing_cant_be_read(monkeypatch):
    for body in (None, b"<ListBucketResult></ListBucketResult>", b"not xml"):
        monkeypatch.setattr(vision, "_get", lambda url, r=6, b=body: b)
        assert vision.first_archive_month("SOLUSDT", "klines", "1h", 1) is None

    bad = XML.replace("2020-09", "2020-13").replace("2020-10", "2020-13")
    monkeypatch.setattr(vision, "_get", lambda url, r=6: bad.encode())
    assert vision.first_archive_month("SOLUSDT", "klines", "1h", 1) is None   # no ValueError

    def down(url, r=6):
        raise vision.FetchError("HTTP 503")
    monkeypatch.setattr(vision, "_get", down)
    assert vision.first_archive_month("SOLUSDT", "klines", "1h", 1) is None


def write_1h(cfg, trades):
    t0 = T("2025-01-01").value // 10**6
    ot = t0 + np.arange(len(trades)) * 3_600_000
    tr = np.array(trades, dtype=float)
    d = cfg.data_dir / "BTCUSDT" / "1h"
    d.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"open_time": ot, "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0,
                  "volume": np.where(tr == 0, 0.0, 1.0), "quote_volume": np.where(tr == 0, 0.0, 100.0),
                  "trades": tr, "taker_buy_volume": 0.0, "taker_buy_quote_volume": 0.0,
                  "close_time": ot + 3_599_999}).to_parquet(d / "all.parquet")


def test_zero_trade_candles_after_the_last_trade_load_as_absent(tmp_path):
    cfg = market_config(tmp_path)
    write_1h(cfg, [5, 5, 0, 0, 5, 5, 0, 0, 0])
    assert load_candles(cfg, "BTCUSDT", "1h").trades.tolist() == [5, 5, 0, 0, 5, 5]   # the halt stays


def test_candles_without_a_trade_count_are_never_dropped(tmp_path):
    cfg = market_config(tmp_path)
    write_1h(cfg, [5, 5, np.nan, np.nan])                  # ccxt-stitched
    assert len(load_candles(cfg, "BTCUSDT", "1h")) == 4


# ---- spotKlines -----------------------------------------------------------------------------------------------

def _csv(rows):
    return pd.DataFrame(rows, dtype=str)


def _spot_rows(t0_ms, n=3, step=3_600_000, mult=1):
    return [[(t0_ms + i * step) * mult, "1", "2", "0.5", "1.5", "10", (t0_ms + i * step + step - 1) * mult,
             "15", "7", "4", "6", "0"] for i in range(n)]


def test_spot_dataset_dir(tmp_path):
    assert vision.dataset_dir(tmp_path, "ETHUSDT", "spotKlines", "1h") == tmp_path / "ETHUSDT" / "spot" / "1h"


def test_spot_monthly_and_daily_urls():
    job = vision.Job("ETHUSDT", "spotKlines", "1h", pd.Period("2025-03", "M"))
    assert vision._monthly_url(job) == \
        "https://data.binance.vision/data/spot/monthly/klines/ETHUSDT/1h/ETHUSDT-1h-2025-03.zip"
    assert vision._daily_url(job, T("2025-03-05")) == \
        "https://data.binance.vision/data/spot/daily/klines/ETHUSDT/1h/ETHUSDT-1h-2025-03-05.zip"


def test_spot_listing_prefix(monkeypatch):
    seen = []
    monkeypatch.setattr(vision, "_get", lambda url, r=6: seen.append(url) or XML.encode())
    vision.first_archive_month("ETHUSDT", "spotKlines", "1h", 1)
    assert "prefix=data/spot/monthly/klines/ETHUSDT/1h/" in seen[0]


def test_spot_jobs_per_timeframe_skip_5m(tmp_path, monkeypatch):
    cfg = market_config(tmp_path)
    shutil.rmtree(cfg.data_dir / "ETHUSDT" / "spot")                 # write_market's 4h spot: no month to skip
    looks, _ = stub(monkeypatch, None)
    vision.backfill(cfg, ["ETHUSDT"], ["5m", "1h", "4h"], ["spotKlines"], start=T("2025-01-01"),
                    end=T("2025-01-31"), workers=1)
    assert sorted(looks) == [("ETHUSDT", "spotKlines", "1h"), ("ETHUSDT", "spotKlines", "4h")]


def test_spot_months_before_the_first_file_are_absent(tmp_path, monkeypatch):
    cfg = market_config(tmp_path)
    looks, urls = stub(monkeypatch, pd.Period("2025-03", "M"))
    run = lambda: vision.backfill(cfg, ["ETHUSDT"], ["1h"], ["spotKlines"], start=T("2025-01-01"),  # noqa: E731
                                  end=T("2025-03-31"), workers=1)
    assert run()["absent"] == 2 and len(looks) == 1
    assert not [u for u in urls if "2025-01" in u or "2025-02" in u]
    man = json.loads((cfg.data_dir / "ETHUSDT" / "spot" / "1h" / "_manifest.json").read_text())
    assert man["2025-01"] == man["2025-02"] == {"complete": True, "source": "absent", "rows": 0}
    urls.clear()
    again = run()
    assert again["absent"] == 0 and again["skip"] == 2
    assert not [u for u in urls if "2025-01" in u or "2025-02" in u]


def test_spot_month_written_with_the_candle_schema(tmp_path, monkeypatch):
    cfg = market_config(tmp_path)
    t0 = T("2025-03-01").value // 10**6
    monkeypatch.setattr(vision, "first_archive_month", lambda *a, **kw: None)
    monkeypatch.setattr(vision, "_fetch_zip_csv", lambda url, *a, **kw: _csv(_spot_rows(t0)))
    s = vision.backfill(cfg, ["ETHUSDT"], ["1h"], ["spotKlines"], start=T("2025-03-01"), end=T("2025-03-31"),
                        workers=1)
    assert s["ok"] == 1
    d = cfg.data_dir / "ETHUSDT" / "spot" / "1h"
    assert list(pd.read_parquet(d / "2025-03.parquet").columns) == vision.CANDLE_SCHEMA
    man = json.loads((d / "_manifest.json").read_text())
    assert man["2025-03"] == {"complete": True, "source": "monthly", "rows": 3}


def test_spot_microsecond_file_parses_to_ms(tmp_path, monkeypatch):
    import hashlib
    import io
    import zipfile
    cfg = market_config(tmp_path)
    t0 = T("2025-03-01").value // 10**6
    rows = _spot_rows(t0, n=2, mult=1000)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("x.csv", "\n".join(",".join(map(str, r)) for r in rows))
    blob = buf.getvalue()

    def get(url, r=6):
        return (hashlib.sha256(blob).hexdigest() + "  x.zip").encode() if url.endswith(".CHECKSUM") else blob
    monkeypatch.setattr(vision, "_get", get)
    monkeypatch.setattr(vision, "first_archive_month", lambda *a, **kw: None)
    vision.backfill(cfg, ["ETHUSDT"], ["1h"], ["spotKlines"], start=T("2025-03-01"), end=T("2025-03-31"),
                    workers=1)
    df = pd.read_parquet(cfg.data_dir / "ETHUSDT" / "spot" / "1h" / "2025-03.parquet")
    assert df.open_time.tolist() == [t0, t0 + 3_600_000]
    assert (df.close_time == df.open_time + 3_599_999).all()


def test_config_backfills_spot():
    import yaml
    from pathlib import Path
    raw = yaml.safe_load((Path(__file__).resolve().parents[1] / "config" / "lab.yaml").read_text())
    assert "spotKlines" in raw["backfill"]["datasets"]

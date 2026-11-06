# BTC reference inventory (cross-coin spec, X7)

Run 2026-11-05 by the lead, from the main checkout's `data/`, over the research span 2020-01-01 to 2026-07-01
(exclusive). The script is `inventory.py` here and the raw output is `inventory.json`. It reads candles through
`store.load_candles` with its default end (`holdout_start`) and skips manifest months at or after it; nothing in the
holdout is read. This is the first build task of `docs/superpowers/specs/2026-11-05-cross-coin-reference-design.md`
and comes before any trial reads `btc_`.

## Findings

| dataset | first month | manifest problems in the research span |
|---|---|---|
| BTCUSDT 15m | 2020-01 | none |
| BTCUSDT 1h | 2020-01 | none |
| BTCUSDT 4h | 2020-01 | none |
| BTCUSDT funding | 2020-01 | none |
| BTCUSDT metrics | 2020-09 | none |

**Candles.**

| timeframe | bars | gaps | zero-trade bars |
|---|---|---|---|
| 15m | 227,808 | none | 15 |
| 1h | 56,952 | none | 1 (2024-10-28 20:00 UTC) |
| 4h | 14,238 | none | none |

- **The 15 zero-trade bars at 15m** fall in five short runs: 2021-03-02 01:15–01:45, 2022-05-01 22:30, 2022-05-28 16:45–17:00, 2023-11-10 15:15–16:15 and 2024-10-28 20:00–20:45. The X6 rule makes the six `btc_` bar columns NaN on those rows.
- **Symbol check:** every step in BTCUSDT's open times equals the timeframe.
- **No coin has a bar without a BTC bar** at 1h or 4h, for all 16 other coins.

**Calibration for the synthetic reference (X5).**

| statistic | 1h | 4h |
|---|---|---|
| correlation of each coin's log return with BTC's: median | 0.673 | 0.677 |
| the same: min and max | 0.546 (DOGE), 0.842 (ETH) | 0.551, 0.838 |
| BTC log quote volume against \|BTC log return\| | 0.408 | 0.389 |
| BTC taker-buy share: mean, sd | 0.498, 0.045 | 0.498, 0.026 |
| BTC taker-buy share: p1 to p99 | 0.389 to 0.607 | 0.434 to 0.563 |

## Decisions that follow

- **`known_gaps` for the reference is empty.** No research month is incomplete and no candle is missing.
- **No recalibration.** The spec's ρ = 0.7 sits inside the 0.5–0.9 band for the median coin (0.673), and BTC volume does rise with |return| (0.41). The synthetic taker share, clip(0.5 + 0.15·tanh(r/σ) + N(0, 0.03)), is wider than the real one, which only makes peeks easier to catch.
- **The spec's data facts are confirmed:**
  - metrics from 2020-09-01;
  - no 1h gap;
  - one zero-trade 1h bar;
  - no coin bar without BTC.
- **Facts the spec left unchecked:**
  - 15m and 4h have no gaps either.
  - 15m has 15 zero-trade bars, which become NaN rows.

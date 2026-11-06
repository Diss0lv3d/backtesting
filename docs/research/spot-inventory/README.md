# Spot real-data inventory (spot-basis spec, X7; plan Task 2)

Run 2026-11-06 by the lead, from the main checkout, with the worktree's code on `PYTHONPATH`. The research span is
2020-01-01 to 2026-07-01 (exclusive). This task comes first: no spot code touches bars until it is committed.

**Commands**
```
PYTHONPATH=<worktree> .venv/bin/lab backfill --datasets spotKlines --tfs 15m,1h,4h
PYTHONPATH=<worktree> .venv/bin/python <worktree>/docs/research/spot-inventory/inventory.py \
    --config <worktree>/config/lab.yaml --out <worktree>/docs/research/spot-inventory
```

**The backfill result.** There were some HTTP 503s, which were retried:

`{"absent": 120, "ok": 4062, "partial": 51, "skip": 0, "missing": 0, "error": 0}`

- The 51 partial months are the open 2026-11 month at 3 timeframes for 17 coins, built from daily files, so the daily URL works.
- The 120 absent months are the months before a late coin's first spot month.

**Files in this folder**

| file | contents |
|---|---|
| `coverage.csv` | one row per (coin, timeframe) |
| `nan_runs.csv` | runs of perp bars with no traded spot bar longer than a day (none) |
| `zero_trade.csv` | spot bars with `trades == 0` |
| `volume_share.csv` | median spot / perp quote-volume share per coin and year, at 1h |
| `btc_monthly_spot.csv` | BTC's monthly spot share and trade count, 2021-07 to 2024-06 |
| `calibration_targets.csv` | the seven pooled 1h statistics that Task 11 calibrates against |

## The spec's facts, checked

| fact (spec, "First build task") | result |
|---|---|
| Each coin's spot pair is the perp's symbol, and it exists | **Confirmed**, for all 17 coins. |
| Spot starts on or before the perp | **Confirmed**, for all 17 coins. |
| No missing monthly file in the middle of the span | **Confirmed.** No coin or timeframe has a manifest problem in the research span. |
| Spot bars sit on the perp's UTC grid | **Confirmed.** Off-grid rows: 0. |
| The symbol check holds (median basis within ±1%) | **Confirmed.** On 2026-06 the medians run from −0.13% (FIL) to +0.05% (BNB). |
| BTC spot volume is inflated in the zero-fee period (about 2022-07 to 2023-03) | **Confirmed**, see below. |

Spot starts on or before the perp for every coin, measured by the first spot day against the first perp day:
- BTC, ETH and BCH start on the same day, 2020-01-01.
- XRP, BNB, DOGE, LINK, ADA, LTC, XLM, ATOM and ETC start on 2020-01-01, days or months before their perps.
- SOL starts 2020-08-11 (perp 2020-09-14) and DOT 2020-08-18 (perp 2020-08-22).
- AVAX starts 2020-09-22 (perp 2020-09-23), and FIL and AAVE 2020-10-15 (perps 2020-10-16).

## Gaps and zero-trade bars

**No long gaps.** No coin has a run of more than a day of perp bars without a traded spot bar. The share of perp bars with no traded spot bar is:

| timeframe | share |
|---|---|
| 1h | 0.04–0.06% per coin |
| 15m | about 0.07% |
| 4h | under 0.01% |

**Zero-trade bars are exchange-wide maintenance windows.** Every one of them falls in a window that hits all 17 coins at once:
- 2020-12-21, about 13:30–14:00 UTC;
- 2021-02-11, about 02:30–03:00 UTC;
- 2023-03-24, 12:00 UTC.

| timeframe | zero-trade bars |
|---|---|
| 1h | 51, i.e. 3 per coin |
| 15m | 343 |
| 4h | none |

The X6 rule makes these rows NaN. They are complete months in the manifests, so they need no `known_gaps` entry.

## Volume share and BTC's zero-fee period

**BTC's spot share of volume** (`btc_monthly_spot.csv`):
- about 0.10–0.18 through 2021-07 to 2022-06;
- then 0.25 (2022-07), rising to 0.45–0.60 (2022-09 to 2023-02) and 0.40 (2023-03);
- back to about 0.10 from 2023-04.

BTC's spot trade count rose 3–5× in the same months. This matches Binance's zero-fee BTC spot promotion, so BTC's spot volume and trade columns are inflated in 2022-07 to 2023-03. The builder must be told.

**All coins' spot share falls over the years.** It was a median of about 0.3–0.7 in 2020 (BNB above 1) and about 0.08–0.24 in 2026 (`volume_share.csv`). A rule should read it as a change against its own recent level, not as a level.

## Basis

The pooled 1h basis (all 17 coins) is negative at the median: the perp's close sits about 3.7 bp under spot's.

| statistic | p1 | p50 | p99 |
|---|---|---|---|
| level | −0.204% | −0.037% | +0.283% |
| 24-bar mean | −0.147% | −0.044% | +0.249% |

The pooled median spot share is 0.213. These seven values are `calibration_targets.csv`.

**Noisy-basis risk** (spec Risks). The 1h median |basis| per coin is 4.6–7.4 bp (BTC 4.6, XLM 7.3, ATOM 7.4). The half-spread wasn't measured here.
- The lab's own slippage estimate (2.1–5.0 bp per coin) is no half-spread proxy. Its floor is 2 bp, so it overstates the half-spread, which the spec puts near 0.02 bp for BTC and ETH and at 0.5–1 bp for thin coins.
- So whether the basis at the median is the two markets' last-trade noise is not established.
- No coin is excluded for this. The context tells the builder.

## The main checkout's plugins

`grep -nE "dropna\(\)|bars\.columns|\.filter\(|spot|basis" strategies/*.py` over the main checkout's 52 plugin files gives no hits. No plugin reads spot columns or works on the whole frame, so the new NaN-bearing columns change no recorded result.

## Decisions

- **`column_groups.spot.exclusions: []`.** Every coin has a same-symbol spot pair that passes the symbol check, with no long holes.
- **`column_groups.spot.known_gaps: []`.** No research month is incomplete.
- **Calibration (Task 11).** The spec's default synthetic basis is wider than these targets: a slow AR sd of 10 bp plus a fast AR plus 20–60 bp jumps, against a real p1/p99 about 17 and 32 bp from the median. Its spot share is also 0.3, against 0.21. Task 11 calibrates to these targets, as its own commit with a `MARKET_VERSION` bump, before the first spot trial.

## Calibration (plan Task 11, 2026-11-07)

**Readiness.** Before calibrating, `column_group_problems` on the real store returned `[]` at 15m, 1h and 4h, with the spot group and its symbol check registered.

**Rule.** The spec's rule was applied once, with S measured on `causality.check_bars()` using the definitions above:
1. `BASIS_C` = the midpoint of T's 24-bar-mean p1 and p99.
2. `BASIS_SLOW_SD` scaled by the ratio of T's and S's 24-bar-mean p99−p1.
3. S recomputed; `BASIS_FAST_SD` and both ends of `BASIS_JUMP` scaled by the same ratio for the level.
4. `SPOT_SHARE` = T's share p50.

Each tail must end within 25% of its distance from T's median (`tests/test_spot_calibration.py`). All four passed after one round, so no ruling was needed.

| statistic | T (real) | S before | S after |
|---|---|---|---|
| level p1 | −0.204% | −0.398% | −0.203% |
| level p50 | −0.037% | −0.061% | +0.010% |
| level p99 | +0.283% | +0.401% | +0.308% |
| 24-bar mean p1 | −0.147% | −0.302% | −0.148% |
| 24-bar mean p50 | −0.044% | −0.060% | +0.011% |
| 24-bar mean p99 | +0.249% | +0.284% | +0.235% |
| spot share p50 | 0.213 | 0.320 | 0.227 |

**Constants**

| constant | before | after |
|---|---|---|
| `BASIS_C` | 0.0 | 0.000512 |
| `BASIS_SLOW_SD` | 0.0010 | 0.000676 |
| `BASIS_FAST_SD` | 0.0005 | 0.000315 |
| `BASIS_JUMP` | (0.0020, 0.0060) | (0.00126, 0.00378) |
| `SPOT_SHARE` | 0.3 | 0.213 |

`MARKET_VERSION` is now 5. No other synthetic value changed.

**Limits**
- **Medians.** The stream is symmetric about `BASIS_C`, so its medians sit near +0.01% while the real ones are near −0.04%. The real basis is right-skewed. The rule and its acceptance test compare tails only.
- **Share.** The share p50 lands at 0.227 rather than 0.213. That comes from the sample path of the φ 0.99 AR(1); the share is not part of the acceptance test.
- **Gated peeks.** The lookahead's gated-peek tests derive their cuts from `check_bars()` and still pass. A peek gated on the ±2·`BASIS_SLOW_SD` band stays closed at every compared cut (ruling S4c), as it was before calibration.

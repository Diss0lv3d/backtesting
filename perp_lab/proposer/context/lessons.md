Human notes from the first six trials: OOS 2022-07 to 2026-07, 8 coins, flat 2 bp slippage. Today's lab: 17 coins from 2020, per-coin slippage.

The numbers come from walk-forward runs (OOS 2022-07 to 2026-07, 8 symbols, 10,000 USDT per
symbol book). Net = gross − fees − funding. Gross already includes slippage, because slippage is
in the fill prices; fees are exchange fees only.

| plugin | tf | trades | gross | fees | funding | net | fees per trade | fees vs gross | OOS (IS) Sharpe | vs random |
|---|---|---|---|---|---|---|---|---|---|---|
| donchian_atr | 4h | 1390 | +13,060 | 3,102 | 1,159 | +8,799 | 2.23 | 24% of gross | 0.45 (1.21) | p48 |
| donchian_atr_wide | 4h | 1160 | +17,449 | 1,814 | 1,314 | +14,321 | 1.56 | 10% of gross | 0.81 (1.16) | p77 |
| donchian_atr | 1h | 4793 | −372 | 15,362 | 1,669 | −17,403 | 3.21 | 41× the gross loss | −0.46 (0.63) | p49 |
| bb_vwap_reversion | 15m | 8435 | −21,466 | 26,366 | −165 | −47,667 | 3.13 | 1.2× the gross loss | −4.30 (−4.02) | — |
| bb_vwap_reversion | 1h | 3759 | −21,548 | 10,584 | −621 | −31,511 | 2.82 | 0.5× the gross loss | −2.18 (−1.83) | p72 |
| funding_fade | 1h | 542 | −621 | 1,293 | −572 | −1,342 | 2.39 | 2.1× the gross loss | −0.30 (1.35) | p68 |

1. **Costs per timeframe.** At 4h, fees were 10–24% of gross, and funding took another 7–9%
   because trend trades are held for days. Costs did not kill the 4h runs. At 1h the same trend
   rule had no gross edge (−372), and 15,362 of fees turned that into −17.4k. At 15m,
   `bb_vwap_reversion` paid 26k of fees on top of a 21k gross loss.
2. **Profit came from the exit, not the entry.** Both 4h Donchian runs made money gross, but they
   sat at p48 and p77 of the random-entry Sharpe distribution, and those random entries used the
   same ATR chandelier exit. Coin-flip entries with that trail earned about as much. In a market
   that trends at the scale of a 3–4 ATR trail on 4h bars, a trailing stop earns whatever the
   entry direction, and the breakout timing added nothing. The lesson for any new candidate:
   - Its exits should earn about zero on random entries: a fixed time exit, a wide stop, no
     trail. Then the only way to beat random p95 is entry timing or direction.
   - Its exits must not depend on the trigger bar. A target "back at the shock bar's open" would
     give random entries different and worse exits, and make the benchmark easy to beat for the
     wrong reason.
   - Neutral exits make the test fair; they don't make a pass likely. The lab has run this exact
     exit template once already: `funding_fade` (1h) fades extreme funding with a stop 3 ATR from
     the close, no target and a time exit. Its OOS Sharpe sat at p68 of the random entries', it
     lost 621 gross before costs, and its IS Sharpe beat its OOS Sharpe by 1.65 (lesson 5).
3. **Fading a stretched price had no gross edge.** `bb_vwap_reversion` lost 21.5k gross at both
   15m and 1h, before costs. At 1h it still beat 72% of random entries, which means its VWAP-target
   exit loses on random entries too. Any new reversion candidate has to say why its trigger is
   more than "price is stretched".
4. **Regime behaviour.** `donchian_atr` 4h lost in `high_vol` (average R −0.135), and
   `donchian_atr_wide` earned in `trending` (average R +0.283). Trend rules pay in trends and
   bleed in high-volatility chop. A candidate that trades in high-volatility moments is a
   different exposure. But a regime filter added now would be fitted to these very numbers, so
   we add none.
5. **In-sample overfitting.** Every IS Sharpe exceeded its OOS Sharpe:
   - by 0.35 for `donchian_atr_wide` (3 combos);
   - by 0.76 for `donchian_atr` 4h (9 combos);
   - by 1.09 for `donchian_atr` 1h;
   - by 1.65 for `funding_fade` (12 combos on a rare trigger, 542 trades, flagged as a narrow
     island).

   The rarer the trigger and the bigger the grid, the more the 6-month training windows picked
   noise. What follows for us: a small grid, plenty of trades per training window even at the
   strictest grid point, and thresholds on a continuous statistic, so that a ±20% move changes
   the trade set gradually.

# New idea cards for the strategy builder (2026-11-01)

The cards alone, as YAML, are in `cards.yaml` next to this file. They follow
`perp_lab/proposer/contract.py::IdeaCard` and were checked against it in code (fields, readable columns, titles new
to the record, grids of at most 16 combos). No card text contains a statistic from our bars, runs or verdicts: its
only numbers are parameters, literature years and the trades-per-year estimate. This note is for the human, so it
does quote our numbers.

How it was made: a read of the research note (sections 1–4, 8–10), the trial-7 and LCE post-mortems, the 12 verdict
files, `lab ideas`, the three builder plugins, `strategy.py`, `indicators.py`, `context.py`, `contract.py`,
`causality.py`, `store.py` and `config/lab.yaml`, plus a literature search. No statistic was computed on real bars
and no backtest was run; trade-count estimates come from design arithmetic, not from looks. `record.db`, the holdout
and `lab exam` were not touched.

## Ranked list

| # | title | tf | why, in one line |
|---|---|---|---|
| 1 | Perp-flow divergence | 4h | When price moves one way over a day while perp takers lean the other way, someone other than the perp crowd is driving the move (spot/ETF demand or a patient limit-order trader), and the perp crowd has to cover. New information: taker flow conditioned on whether price agreed with it. |
| 2 | Signed-jump reversal | 1h | Lottery demand: a week whose variance came mostly from sharp up-moves is followed by lower returns (Bollerslev–Li–Zhao 2020; Lee & Wang 2025 in crypto). The trigger is the shape of the return distribution, not its level. |
| 3 | Absorbed liquidation sweep | 1h | The untested half of the forced-flow story: a one-sided cascade that failed to move the close (long wick) means the forced flow is spent and the passive side showed size. |
| 4 | Serial-dependence switch | 4h | Follow daily moves when the coin's recent variance ratio is above 1, fade them when it is below 1. Its main value is closing the "trend here, fade there" loop the builder is likely to start. |
| 5 | Small-ticket frenzy fade | 4h | Fade a day's move made on a burst of many small trades: retail attention herding (Barber–Huang–Odean–Schwarz 2022). The proxy is weak. |

The idea space we can test is thin. With one coin per plugin, no OI, no basis and no spot flow, these five were the
only ideas found that are new, cost-feasible and not near-copies or sign flips of anything tried. Padding to eight
would have meant parameter variants or calendar ideas that cannot reach the bar. Every one is more likely to be
killed than promoted; they are ranked by what a result would teach as much as by their chance of passing. The
highest-value next step is new data, not trial 13 (see "Needs new data").

## Where the record stands

- **N = 12.** The third builder trial, `px0012_volatility_compression_breakout` at 4h, was killed as no signal:
  gross Sharpe 0.55 against a random p95 of 1.00 (random mean 0.42), top fold share 0.77. All ten closed ideas are
  dead as "no signal", one is excluded, and LCE sits at signal before costs.
- **The Deflated Sharpe bar** (`perp_lab.dsr.sharpe_bar`, 2,191 OOS days): about 1.35 at N = 12, 1.37 at 13,
  1.40 at 15, 1.45 at 20, 1.52 at 30.
- **A lesson the record shows: a stop with no target is a trend-capturing exit, whatever the entry.**
  - Random-entry gross means in the verdicts: Donchian 4h trail +0.95; TSMOM (6-ATR stop, 7–14 day hold) +0.49;
    squeeze (2-ATR stop, 40% stopped) +0.42; flow fade 1h +0.11; funding fade −0.02; weekend (17% stopped) −0.03.
  - A long and a short on the same path with a stop and no target don't cancel: in a trending path one leg loses
    k ATR and the other keeps the whole run. With a **symmetric bracket** (target = stop distance) the short's payoff
    is exactly minus the long's, apart from costs and the stop-first tie rule, so random-side entries earn about zero
    before costs.
  - The random p95 sits about 0.55–0.75 above the random mean in every verdict, so a neutral exit should bring the
    before-costs p95 at 4h down from about 1.0 to about 0.6–0.7.
  - Every card uses a symmetric bracket plus a time exit for this reason. It is also cheaper: target fills pay the
    2 bp maker fee and no slippage.
- **The other lessons, as the cards use them:** entry timing from price patterns (breakouts, squeezes, TSMOM) has
  been no better than random; fading price alone (BB–VWAP, weekend) and fading forced flow that moved price (flow
  shock) lost before costs; funding as a trigger or filter added nothing; 5m and 15m holds can't pay costs. So: use
  non-price information, a coin-specific trigger (independent bets), holds of 6 h to a week, and neutral exits.

## What a plugin can do (checked in code)

- **One coin per plugin.** `prepare(bars)` gets one coin's bars; the engine trades one coin at a time
  (`ctx.symbol` names it); a sealed worker opens no file. Cross-sectional, lead-lag and market-relative ideas are
  impossible.
- **Readable columns:** `open_time`, `close_time`; OHLC, `volume`, `quote_volume`, `trades`; `taker_buy_volume`,
  `taker_buy_quote_volume`; `funding_charge`, `funding_last`, `funding_interval_h` (settled rate only — no
  predicted rate, premium or basis); `regime`, `regime_adx`.
- **Not readable:** `oi`/`oi_value` are NaN on real bars (the loader exists — `store.load_metrics`,
  `align_metrics` — but there is no `data/*/metrics/` on disk); `slippage` exists on real bars but not on the
  synthetic ones, so reading it fails the lookahead check.
- **Timeframes** 15m, 1h, 4h, exactly one per plugin. **Imports:** math, numpy, pandas, `perp_lab.strategy`,
  `perp_lab.indicators`, `perp_lab.engine.backtest`.
- **Entries** are market fills at the next open with a required stop and an optional target (a resting limit:
  maker, no slippage); the stop wins when both are hit in one bar; `manage` can exit or ratchet the stop.
  **Grid:** at most 16 combos.
- **Constraints that shaped the cards:**
  - The defaults must signal on the judge's synthetic bars (3,000 1h bars; taker share uniform 0.3–0.7, `trades`
    uniform 50–5,000, small wicks: high/low noise 0.4% against 1% returns), so warm-ups stay under ~1,600 bars and
    card 3's default wick threshold is loose.
  - Training windows need pooled entries of at least 2.5 × the coins listed (~42 over 17 coins per 6 months), or a
    combo is never picked, so even strict grid corners aim at more than 8 trades per coin per year.
  - The OOS portfolio is the equal-weight mean of daily returns, so days with no trade count as zero: an idea active
    on a fraction f of OOS days has its measured Sharpe cut to about √f of its active Sharpe. That rules out
    BTC/ETH-only ideas that start with the 2024 ETFs.

## What a pass needs (arithmetic, not measurement)

Net annual Sharpe ≈ s × √B, where s is the net mean per trade over its standard deviation and B the number of
effectively independent bets a year across the 17 correlated books; clearing ~1.4 needs s ≈ 1.4/√B. Costs per trade
from the verdicts: ~22 bp at 4h (fees 10, slippage ~6.7, funding ~5 over 2-day holds), ~18 bp at 1h. Typical moves
from section 2 of the research note; the per-trade standard deviation is taken as ~1.25 × the median absolute move
over the hold; B is a guess per card.

| card | hold | median \|move\| over the hold | B guess | gross per trade to pass | as a multiple of cost | gross for "signal" (2× cost) | accuracy that implies |
|---|---|---|---|---|---|---|---|
| 1 divergence | 12 × 4h | ~280 bp | ~200 | ~55 bp | ~2.5× | ~45 bp | ~57% |
| 2 signed jump | 120 × 1h | ~440 bp | ~100 | ~95 bp | ~5× | ~36 bp | ~53% |
| 3 absorbed sweep | 12–24 × 1h | 134–200 bp | ~400 | ~30–36 bp | ~2× | ~36 bp | 57–61% |
| 4 regime switch | 6–12 × 4h | 195–280 bp | ~300 | ~42–50 bp | ~2× | ~45 bp | 57–60% |
| 5 small-ticket | 12 × 4h | ~280 bp | ~150 | ~62 bp | ~3× | ~45 bp | ~57% |

Card 3 has the hardest cost check per trade but the easiest Sharpe arithmetic (coin-specific, many trades). Card 2
has the easiest cost check but needs a large effect to clear the bar; its realistic good outcome is signal before
costs.

---

## Card 1: Perp-flow divergence (4h)

**Rule** (`cards.yaml` #1): when the 24-hour move is at least 1 ATR (4h) and the 24-hour net taker share, z-scored
against the coin's previous 30 days, is on the other side by at least z_thr, trade in the direction of price.
Symmetric 3-ATR bracket, 6/12/18-bar time exit. Grid z_thr [0.5, 1.0] × hold [6, 12, 18] (6 combos).

**Builds on:** price-only timing is random, so add a non-price conditioner; the only flow trial (flow shock) used
flow and price in agreement, so disagreement is untested; coin-specific, 4h holds where costs are cheap.

**Closest ideas:** `flow_shock_fade` (#6) traded against price when flow and price agreed in the extreme — this
card's trigger set is disjoint (flow against price) and it trades with price. Taker-flow persistence (#11,
excluded) follows the 24-hour flow everywhere; this trades against flow, only where price disagreed (#11 was never
judged, so no sign was taken from a result). Donchian and TSMOM also follow moves, but the side-matched random
benchmark with the same exits absorbs plain trend content, so only the divergence condition can produce excess.

**Evidence:** Glassnode, ["Spot Leads, Derivatives Lag"](https://research.glassnode.com/the-week-onchain-week-19-2025/)
(2025) reads spot-led rallies as more durable than perp-led ones; Coinglass's [CVD guide](https://www.coinglass.com/cvd)
cautions a divergence alone can persist. Collin-Dufresne & Fos (2015, *JF*),
["Do prices reveal the presence of informed trading?"](https://ideas.repec.org/a/bla/jfinan/v70y2015i4p1555-1582.html):
informed traders use limit orders when liquidity looks high; Kaniel & Liu (2006, *J. Business*, from memory).
Perp–spot arbitrage: He, Manela, Ross & von Wachter, ["Fundamentals of Perpetual Futures"](https://arxiv.org/html/2212.06888v7);
price discovery led by Binance: [Alexander](https://www.socialscience.international/almost-all-bitcoin-price-transmission-comes-from-binance),
[Pascual et al. 2025](https://arxiv.org/pdf/2506.08718); ETF flow in the 3–4 pm New York window:
[Kaiko](https://research.kaiko.com/insights/btc-etfs-impact-on-spot-market-structure). No peer-reviewed test of
"perp-flow/price divergence → continuation" was found: a mechanism-led hypothesis.

**Kill criterion:** no signal before costs (gross OOS Sharpe ≤ max(0, gross random p95)) → drop kline-level taker
flow as a directional input altogether (shock fade dead, persistence excluded, divergence dead); revisit only with
spot klines or premium-index data. If signal before costs or better: both sides' gross positive
(`diagnostics.sides`), `top_coin_share` and `top_fold_share` each ≤ 0.5 (px0012 had a top fold share of 0.77).

**Cost check:** ~22 bp all-in at 4h against a median absolute 2-day move of ~280 bp; "signal" needs ~45 bp gross
(16% of the typical move, ~57% accuracy), a pass ~55 bp. Feasible if the effect is real.

## Card 2: Signed-jump reversal (1h, ~5-day hold)

**Rule** (`cards.yaml` #2): rsj = (up − down) / (up + down) semivariance over the last 168 hourly returns, ranked
against the previous 60 days; short in the top q, long in the bottom q. Symmetric bracket of ~2 daily ranges, time
exit 72/120/168 bars. Grid q [0.1, 0.2] × hold (6 combos). 1h because 168 hourly returns measure jumps far better
than 42 4h returns, and costs are per trade, not per bar.

**Builds on:** new information (the shape of the distribution, not the level of the move); a one-week hold is
cost-cheap; a named counterparty (lottery buyers).

**Closest ideas:** BB–VWAP reversion and weekend-move reversal (fades). The trigger is the share of variance from
up- against down-moves, so a week can be up with a negative rsj; the mechanism is skewness preference, not
"stretched"; the horizon is a week. Caveat: rsj correlates with the week's return, so part of the trade is a weekly
fade — weekly reversal has never been tested here (the weekend fade was a 2-day move held 24 h).

**Evidence:** Lee & Wang (2025, *JFQA*),
["Variance Decomposition and Cryptocurrency Return Prediction"](https://ideas.repec.org/a/cup/jfinqa/v60y2025i4p1859-1890_9.html)
([working paper](https://www.scheller.gatech.edu/directory/research/finance/lee/pdf/crypto_variance_leewang_5feb2024.pdf)):
high-variance coins earn lower returns in the following weeks, driven by positive-jump and jump-robust variance, not
negative jumps; stronger for small, low-price, retail-heavy coins; tied to lottery preference. In stocks: Bollerslev,
Li & Zhao (2020, *JFQA*), ["Good volatility, bad volatility"](https://ideas.repec.org/a/cup/jfinqa/v55y2020i3p751-781_2.html)
— relative signed jump variation predicts next-week returns negatively (the source of the two-sided design).
Measure: [Patton & Sheppard, realized semivariance](https://public.econ.duke.edu/~ap172/Patton_Sheppard_Realized_Semivariance_7oct11.pdf).
Theory: Barberis & Huang (2008, *AER*, from memory). Against: a
[2024 *Digital Finance* tick study](https://link.springer.com/article/10.1007/s42521-024-00116-1) finds intraday
jumps predict same-day continuation (a different horizon, but the first hours after a jump trend). Gap: all of this
is cross-sectional; no time-series test on large coins was found.

**Kill criterion:** no signal before costs → close lottery/skewness time-series ideas on these large coins (keep
them for a multi-symbol lab, where Lee & Wang's cross-sectional form applies). If signal before costs comes only
from the long side, that is the weaker half of the literature — don't refine toward longs only (it would pick a side
from the result).

**Cost check:** ~18 bp plus funding over 5 days (~3–8 bp a day for longs, received by shorts): call it ~25 bp. The
median absolute 5-day move is ~440 bp, so "signal" needs ~36–50 bp (8–11%, ~53–54% accuracy). Cost is not the
problem; breadth is (~18 trades per coin per year, overlapping, correlated across coins): a pass needs ~95 bp a
trade.

## Card 3: Absorbed liquidation sweep (1h)

**Rule** (`cards.yaml` #3): an hour with net taker selling of at least flow_x of the median bar volume, a lower wick
of at least wick_k × ATR(t−1), and a body of at least −0.25 ATR (the cascade did not move the close) → long;
mirrored for squeezes. Symmetric 3-ATR bracket from the signal close (never the wick low), time exit 12/24 bars.
Grid wick_k [0.5, 1.0] × hold [12, 24] (4 combos). Defaults (flow_x 0.3, wick_k 0.5) are loose because the
synthetic bars' wicks are ~0.4% of price against an ATR of ~1.6%; a plugin whose defaults never signal is killed.

**Builds on:** completes the forced-flow test. Trial 7 showed fading one-sided flow that moved the close lost gross
on both sides; the mechanism's other prediction — absorbed (failed) forced flow reverts — is untested. Uses a
trigger-independent exit (research note §1, lesson 2) and 12–24 h holds (§2).

**Closest idea:** `flow_shock_fade` (#6). Same side relative to flow, so not a sign flip; trigger set disjoint (#6
required a body of at most −1 ATR, this at least −0.25 ATR). Together they answer: does forced flow carry
information when it succeeds, when it fails, or never?

**Evidence:** forced-flow theory in the research note (Grossman & Miller 1988; Brunnermeier & Pedersen 2009; Nagel
2012); Osler (2005, *J. Int. Money & Finance*, from memory): stop-loss clusters produce self-limiting cascades;
[crypto-perp cascade early-warning study (arXiv 2607.27070)](https://arxiv.org/pdf/2607.27070): bar data shows the
consequences of forced liquidation (OI, aggressor flow), not the liquidations themselves — our blind spot too. The
"liquidity sweep" idea is practitioner lore; no peer-reviewed test was found, so it isn't relied on.

**Kill criterion:** no signal before costs → bar-level forced-flow reversal is closed in both halves; stop all
wick/flow/cascade fades until liquidation or OI data exists. Signal before costs but net below random → don't port
it to 4h (it would dilute an hourly mechanism; research note §7, criterion 3).

**Cost check (the weakest per trade):** ~18 bp at 1h against a median absolute move of ~134 bp over 12 h and ~200 bp
over 24 h; "signal" needs ~36 bp gross (27% of the 12-hour move, ~61% accuracy, or 18% of the 24-hour move,
~57.5%). The 24-bar hold is the more plausible grid point (section 9's hurdle for the flow fade was 29–43 bp).

## Card 4: Serial-dependence switch (4h)

**Rule** (`cards.yaml` #4): vr = the 6-bar variance ratio over the previous 540 4h bars (90 days). When the 24h move
is at least 1 ATR: follow it if vr ≥ 1 + band, fade it if vr ≤ 1 − band. Symmetric 3-ATR bracket, time exit 6/12
bars. Grid band [0.1, 0.2] × hold [6, 12] (4 combos).

**Builds on:** trend rules at 4h and fade rules at 1h/24h each came out about random or worse; if serial dependence
changes sign over time, that is exactly what fixed rules would show. This is the single clean test of that
"conditional sign" explanation.

**Closest ideas:** Donchian, TSMOM, squeeze (follow) and BB–VWAP, weekend (fade) — its direction depends on a
measured state, so it is neither family. The `regime` (ADX) label measures trend strength; vr measures serial
dependence directly.

**Evidence:** adaptive-markets tests find time-varying predictability in Bitcoin: Lo (2004);
[Khuntia & Pattanayak 2018, *Economics Letters*](https://ideas.repec.org/a/eee/ecolet/v167y2018icp26-28.html);
["On the Evolution of Cryptocurrency Market Efficiency"](https://arxiv.org/pdf/1904.09403).
[Easley, O'Hara, Yang & Zhang 2024 (SSRN 4814346)](https://stoye.economics.cornell.edu/docs/Easley_ssrn-4814346.pdf):
on Binance, microstructure measures (Roll, VPIN) predict the sign of changes in serial correlation (AUC ~0.55–0.61).
Shen, Urquhart & Wang ([2022, *Financial Review*](https://research.birmingham.ac.uk/en/publications/bitcoin-intraday-time-series-momentum/))
read Bitcoin's intraday momentum as liquidity provision, consistent with the participant mix setting the sign.
Against: a 90-day variance ratio has a sampling standard error of ~0.1, the same size as the band.

**Kill criterion:** no signal before costs → stop all "trend in state X, fade in state Y" refinements of dead ideas
(ADX-, volatility- or funding-regime switches) and tell the builder so in the lessons file. The main value is
stopping that loop before the builder spends four trials on it.

**Cost check:** ~20–22 bp at 4h against a median absolute move of ~195 bp (24 h) or ~280 bp (2 days); "signal" needs
~45 bp (16–23%, ~57–60% accuracy). The most frequent card (~60 trades per coin per year), so B is large and a pass
needs ~42–50 bp.

## Card 5: Small-ticket frenzy fade (4h)

**Rule** (`cards.yaml` #5): over the last 24 h, trades at least n_x × their 30-day median, the average ticket (USDT
per trade) at most 0.85 × its median, and a move of at least 1 ATR → fade the move. Symmetric 3-ATR bracket, time
exit 6/12/18 bars. Grid n_x [1.5, 2.0] × hold (6 combos).

**Builds on:** names a counterparty (retail attention herding), answering research note §1, lesson 3 (a fade must say
why it is more than "price is stretched"); `trades` is the one exchange column no trial has used.

**Closest ideas:** BB–VWAP and weekend reversal fade on price alone; this one fades only on a composition signal.
Card 2 shares the retail-mispricing mechanism through a different proxy.

**Evidence:** Barber, Huang, Odean & Schwarz (2022, *JF*),
["Attention-induced trading and returns: evidence from Robinhood users"](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3715077):
herding episodes are followed by negative returns (gross figures); Barber & Odean (2008, *RFS*, from memory).
Against: Kogan, Makarov, Niessner & Schoar ([2024, *JFE*](https://www.nber.org/papers/w31317)) — crypto retail
traders are momentum traders, so a frenzy can push further first; the weekend fade (a retail-heavy window) lost
before costs; Bianchi, Babiak & Dickerson ([2022, *JBF*](https://ideas.repec.org/a/eee/jbfina/v142y2022ics0378426622001418.html))
— crypto liquidity-provision returns sit in low-cap pairs and interact with volume.

**Kill criterion:** no signal before costs → close participant-composition proxies from klines; revisit with
aggTrades trade-size buckets. Run it after card 2: if card 2 shows no signal before costs, card 5's prior is low
enough to skip it.

**Cost check:** as card 1 — ~22 bp against ~280 bp; "signal" needs ~45 bp, a pass ~62 bp. Fewer trades (~15 per coin
per year) put the strict corner (n_x 2.0) near the training-window floor.

---

## Suggested order

1. Card 1 — highest plausibility, cleanest test; its kill closes a data source.
2. Card 4 — early, to pre-empt regime-filter children of dead ideas.
3. Card 2.
4. Card 3.
5. Card 5, only if card 2 shows signal before costs.

## Considered and rejected

| idea | why not |
|---|---|
| Funding-settlement flow (payers dodge the 00/08/16 UTC snapshot; carry arbitrageurs short before positive settlements and cover after) | The tradable leg is long after a positive-funding settlement, the opposite of `funding_fade`'s short side, which lost 51.5 bp gross a trade — proposing it now picks the sign from that result. The pre-settlement leg has funding fade's sign but can't pay: typical funding (~1 bp per 8 h) gives little reason to dodge, far below 18 bp. |
| ETF fixing-window (3–4 pm New York) pressure and reversal | BTC and ETH only, since 2024: active ~a third of OOS days, so the measured Sharpe is ~√(1/3) of the active one; BRRNY replication is engineered to minimise impact ([CF Benchmarks](https://www.cfbenchmarks.com/blog/optimizing-capital-efficiency-in-replicating-the-brrny-for-the-creation-and-redemption-of-shares-for-us-spot-bitcoin-etfs), [Kaiko](https://research.kaiko.com/insights/btc-etfs-impact-on-spot-market-structure)). |
| Intraday momentum, first half-hour to last ([Shen, Urquhart & Wang 2022](https://research.birmingham.ac.uk/en/publications/bitcoin-intraday-time-series-momentum/)) | The predictable part of a half-hour return is a few bp against an 18 bp round trip. |
| Hour-of-day seasonality ([tea-time paper](https://ideas.repec.org/a/kap/rqfnac/v64y2025i1d10.1007_s11156-024-01304-1.html); [periodicity](https://arxiv.org/pdf/2109.12142)) | One market-wide bet a day, a few bp of excess per window; the [quarter-hour paper](https://arxiv.org/html/2607.09426v1) puts its own effect at ~0.5 bp a trade gross. |
| High-volume return premium (Gervais–Kaniel–Mingelgrin) | [Garfinkel et al. 2025](https://onlinelibrary.wiley.com/doi/10.1111/fima.12491): abnormal volume predicts crypto returns only where shorting is constrained; zero once Binance margin exists. |
| Large-ticket ("informed") move continuation | Following one-sided, high-volume moves on overlapping bars is too close to the sign flip of `flow_shock_fade`. |
| Round-number levels (Osler) | [Urquhart 2017](https://ideas.repec.org/a/eee/ecolet/v159y2017icp145-148.html): Bitcoin prices cluster at round numbers, with no return pattern after them. |
| Downside-semivariance premium (long after crash-like weeks) | Lee & Wang find negative jumps don't predict in crypto; it survives only as card 2's weaker half. |
| Macro-announcement premium (FOMC/CPI) | ~20 correlated events a year can't reach ~1.4; the dates would have to be hard-coded from memory. |

## Directions to stop

1. Trend and breakout entry timing in any form at 1h or 4h (channel breakouts, squeezes, TSMOM, moving-average
   crosses, 52-week/all-time-high proximity, opening-range breakouts). Five trials sat inside random p95 before
   costs. Adding funding, volume, compression or regime filters to a trend entry is the loop to avoid.
2. Fading price on its own (Bollinger, VWAP, RSI, z-scores, weekend gaps), at any horizon: it lost before costs at
   15m, 1h and 24 h.
3. Funding as a directional trigger or filter: extremes, carry, filters, sign changes, settlement-window trades.
4. Fading forced flow that moved price: new thresholds, holds or timeframes for `flow_shock_fade`.
5. Calendar and session seasonality with sub-day holds (hours, sessions, US close, Sunday night, funding hours, turn
   of month, macro days): no breadth, no per-trade size to pay costs.
6. Thin-market (weekend-type) reversal.
7. Anything at 5m or 15m with holds under a few hours: LCE-style intraday level systems.
8. Sign flips of 1–6: weekend continuation, flow continuation or persistence, following funding, breakout fades
   ("turtle soup").
9. Long-only beta timing (volatility-managed long, buy the dip, long when calm): the random benchmark matches the
   side mix, so drift earns nothing against it, and a timed long book on a market with a buy-and-hold Sharpe of 0.72
   can't plausibly reach ~1.4.
10. Volume-attention and round-number ideas on these coins (literature above).
11. Filters read off verdict tables (high_vol only, short side only, LCE branch 6 only, squeeze shorts only): each is
    a choice made after the result.
12. Stop-only or trailing exits on any new idea: they pay random entries for trends and raise the before-costs bar to
    ~1.0–1.3. Use symmetric brackets. Consider adding this to `perp_lab/proposer/context/lessons.md` as a human
    lesson, worded without numbers if it must stay statistic-free.

## Needs new data (or new structure)

| # | data or structure | what it unlocks | notes |
|---|---|---|---|
| 1 | **Open interest and long/short ratios**: Binance `metrics` daily archive (OI, OI value, top-trader and account L/S, taker L/S) | Telling forced from informed flow (price down with OI down = liquidation; with OI up = new shorts) — the ambiguity that sank the flow-shock theory. OI-weighted crowding; top-trader vs retail L/S divergence. | The cheapest unlock: `backfill.datasets` already lists `metrics` and `load_bars` attaches `oi`/`oi_value` with a 5-minute publish lag, so it only needs downloading. Check the archive's start date ([Tardis](https://docs.tardis.dev/historical-data-details/binance-futures) collects OI from 2020-05 and L/S from 2020-12; the vision archive may start later, and an OI plugin dormant in early folds is diluted by the zero-day rule). Fields: [binance-public-data #211](https://github.com/binance/binance-public-data/issues/211). `align_metrics` attaches only `oi`/`oi_value` today; the L/S columns need wiring. |
| 2 | **Premium-index, mark and index klines** (data.binance.vision) | Real-time basis and predicted funding; spot-led vs perp-led measured directly (card 1 done properly); basis mean reversion ([He et al.](https://arxiv.org/html/2212.06888v7)). | New bar columns. |
| 3 | **Binance spot klines** (they carry taker buy volume) | Spot CVD vs perp CVD divergence ([Glassnode](https://research.glassnode.com/the-week-onchain-week-19-2025/)). | A second loader. |
| 4 | **Multi-symbol plugins** (structure) | Cross-sectional momentum, size and volume (Liu, Tsyvinski & Wu 2022, *JF*); cross-sectional lottery/variance ([Lee & Wang 2025](https://ideas.repec.org/a/cup/jfinqa/v60y2025i4p1859-1890_9.html)); BTC lead / alt lag; idiosyncratic-move reversal (Nagel 2012); cross-sectional funding carry (Schmeling, Schrimpf & Todorov, "Crypto carry", BIS). | The biggest unlock, for breadth: nearly all of crypto's documented anomalies are cross-sectional. |
| 5 | **aggTrades** ("not wired" in the config) | Trade-size buckets (card 5 done properly), quarter-hour opening imbalance ([arXiv 2607.09426](https://arxiv.org/html/2607.09426v1)), VPIN, Kyle lambda, Roll ([Easley et al. 2024](https://stoye.economics.cornell.edu/docs/Easley_ssrn-4814346.pdf)). | Huge files; aggregate to bar-level features at backfill time. |
| 6 | **Liquidation prints** (forceOrder; no official archive, so vendors) | Cascade timing and size. | |
| 7 | **Event calendars as columns**: token unlocks, FOMC/CPI dates, Deribit and CME expiries | Unlocks are coin-specific supply shocks with documented pre-unlock weakness ([Keyrock, 16,000 unlocks](https://keyrock.com/from-locked-to-liquidity-what-16000-token-unlocks-teach-us/); secondary write-ups, verify); expiry pinning. | Hard-coding dates in plugin source is fragile. |
| 8 | **ETF flows, plus a span or universe option for BTC/ETH-era ideas** | The fixing window, flow persistence. | Without a span option the zero-day dilution kills them. |
| 9 | **Options data** (Deribit DVOL, OI by strike) | Volatility-risk-premium timing, dealer gamma. | |
| 10 | **Stablecoin mint and burn** | [Tether jumps predict Bitcoin drops](https://osuva.uwasa.fi/handle/11111/2600). | |
| 11 | **ES/SPX** (already being sourced) | The LCE ES leg, TradFi-hours spillovers. | |

Cited from memory and not re-verified: Kaniel & Liu (2006); Barberis & Huang (2008); Barber & Odean (2008); Osler
(2005); Lo (2004); Liu, Tsyvinski & Wu (2022); Schmeling, Schrimpf & Todorov ("Crypto carry"); Nagel (2012).

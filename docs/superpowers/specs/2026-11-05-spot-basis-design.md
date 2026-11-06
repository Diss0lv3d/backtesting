# Spot prices and perp–spot basis

Date: 2026-11-05; revised the same day after review. Status: design, not built. Design 2 of the three that widen what
a plugin can see (1: cross-coin reference bars, `btc_*` columns; 3: the cross-sectional portfolio engine). Build
order: after design 1, before design 3, which consumes these columns. The user skipped the Q&A, so every choice
below is the designer's, marked **Designer's call:** with its reason and its cost if wrong, except the lead's
cross-design rulings X1–X7, marked **Lead's call:**.

Design 1's spec (`2026-11-05-cross-coin-reference-design.md`) *defines* the shared pieces: the column-group registry
`perp_lab/data/columns.py` (X1), the readiness check `column_group_problems` (X3), `plugins.reads_columns` (X4), and the
per-group synthetic streams (X5). This spec *uses* them and adds the `spot` group. Binding wherever this document
doesn't change them: `2026-10-18-wider-data-design.md`, `2026-10-19-proposer-design.md`,
`2026-10-17-plugin-sandbox-design.md`.

## Why

54 trials and no pass. The best results were long continuation after a strong 24h move on unusual volume (#18
large-ticket continuation, #21 news-volume drift) and a weaker cousin on rising open interest: net Sharpe 0.7–1.33,
Deflated Sharpe ≤ 0.89. The builder concluded that single-coin perp bar rules are mined out. Every column a plugin
reads today comes from the perp itself. Nothing says whether a move is carried by unlevered spot buyers or by
leverage, and the only premium signal, funding, is settled every 4–8 hours and is held at its default 0.01% while the
premium stays inside a band (below). After this change a plugin can read, per coin and bar, the coin's Binance spot
price, spot volume, spot trade count and spot taker flow, and the perp's premium over spot.

Hypothesis families this unlocks. A round trip costs about 14 bp (BTC: taker both ways plus 2.1 bp slippage each way)
to 26 bp (thin coins, about 8 bp slippage), more in 2020. Plausibility is the designer's honest prior. Every family
costs trials, and every trial raises the Deflated Sharpe bar for all later ones.

1. **Basis extremes as leverage crowding, faster than funding.** Crowded longs make the perp trade rich to spot.
   Funding reacts only at the next settlement and is pinned at 0.01% inside the clamp band; basis is visible every
   bar, inside the band too. Evidence: Schmeling, Schrimpf and Todorov, "Crypto Carry" (BIS WP 1087, 2023): high carry
   precedes crashes and liquidations, over days to weeks. He, Manela, Ross and von Wachter, "Fundamentals of Perpetual
   Futures" (2022): perp–spot gaps are large and persistent, and move with past returns. **Plausibility at 1h/4h after
   costs: low as a fade, moderate as a filter.** The harness trades no spot leg, so a closing gap pays only through
   the perp's own move. Price-discovery work (Alexander, Choi, Park and Sohn, J. Futures Markets 2020) finds
   derivatives lead spot, so a premium often closes by spot catching up. As a "don't chase into crowding" filter on a
   continuation rule, or a multi-day crowding state at 4h, it is worth a trial.
2. **Spot-led vs perp-led moves.** A move carried by spot buyers is demand; one carried by perp volume alone is
   leverage that must unwind. The continuation behind #18/#21 should then be stronger when spot's share of the move's
   volume is high. Evidence: mostly practitioner lore; academic work is on price-discovery shares, not this
   conditional. **Plausibility: moderate, the best of the four**: it conditions the lab's best near-miss on new
   information. Caveats: a filter that lifts a near-miss over the bar is what deflation discounts, and it still faces
   the island check and the exam. And Binance spot is not all spot: US demand (Coinbase, the ETFs since 2024) is
   invisible here.
3. **Basis convergence around funding settlements.** Payers close or hedge before a large settlement, so the premium
   compresses into it and rebuilds after. Evidence: practitioner observation; the designer knows no peer-reviewed
   result at this granularity. **Plausibility: low**: a few bp around three moments a day; 15m doesn't pay costs.
4. **Spot vs perp taker-flow divergence.** Perp takers buying into spot takers selling is a reversal setup; the
   reverse is an accumulation setup. Evidence: order-flow imbalance predicts returns (Cont, Kukanov and Stoikov, J.
   Financial Econometrics 2014), but over seconds to minutes. The lab's perp-flow fade (`flow_shock_fade`, trial 7)
   was killed. **Plausibility: low to moderate**; the divergence is information the lab has never had.

Cross-sectional carry (ranking coins by basis) is design 3's business; this spec only supplies the column.

## Decisions

| question | decision | why; cost if wrong |
|---|---|---|
| Source | **Designer's call:** Binance spot klines (`data/spot/monthly\|daily/klines/{COIN}USDT/{tf}/`) only. Premium-index, index and mark-price klines are not stored. | The only archive with spot volume and taker flow (hypotheses 2, 4), and it gives a price. The others carry prices only. If the premium index proves the better basis, it is one more dataset and column later; nothing here changes. |
| `basis` | **Designer's call:** `close / spot_close − 1`, a fraction, from the two klines of the same bar. | The gap a Binance trader sees, consistent with `spot_close`. The premium index is measured against a multi-exchange index whose members aren't archived. Cost: last-trade noise, a few bp on thin coins. |
| Columns | **Designer's call:** `spot_close`, `spot_quote_volume`, `spot_taker_buy_quote_volume`, `spot_trades`, `basis`. | No spot open/high/low (spot's return is `spot_close` over its previous value), no base-coin volumes (USDT compares directly with the perp's). Each extra column needs a twin and context text. |
| Funding near-duplicates | **Designer's call:** no premium-index, predicted-funding or averaged-basis column. The context states the clamp exactly. | Each would be funding on a faster clock. An 8h-average-basis idea is a funding idea only outside the clamp band; inside it, funding is pinned and basis is new information. |
| Column group | **Lead's call (X1):** a `spot` group in `perp_lab/data/columns.py`: the five columns, prefix `spot_`, `basis` by name, `rule` 1, its own `synthetic_id`, `exam_required: if_read`, helpers `load_spot`, `attach_spot`. | One registry for every group's alignment, synthetic stream, readiness and exam coverage. |
| Alignment | **Designer's call:** exact join on `open_time`; publish lag 0; no forward fill. | A kline's close is known at the boundary, as the perp's is. A fill would turn a halt into a fake basis. |
| Zero-trade bars | **Lead's call (X6):** the four spot columns are NaN when the spot bar is missing or has `trades == 0`; `basis` is NaN when either the spot or the perp bar has `trades == 0`, or the spot bar is missing. | A zero-trade kline carries a price nobody traded. A mid-series zero-trade perp bar keeps a stale `close` (only the trailing run is dropped, `store._drop_dead_tail`). |
| Symbol | **Designer's call:** the perp's symbol (`BTCUSDT` → spot `BTCUSDT`). A coin with no usable spot pair goes in `spot_exclusions` (X3). | The readiness check's 1% symbol test catches a pair like `1000SHIBUSDT`/`SHIBUSDT` (1000×). |
| Storage | **Designer's call:** `data/{SYM}/spot/{tf}/{YYYY-MM}.parquet` + `_manifest.json`; dataset `spotKlines`; timeframes 15m, 1h, 4h. | Same schema and parser as perp klines. Nothing enumerates `data/{SYM}/`. No 5m: no plugin trades it. |
| Readiness | **Lead's call (X3):** `column_group_problems` checks every research month of every non-excluded coin, with `known_gaps`. It runs before `register_trial`, inside `validate_strategy`, in the portfolio judge, and at `lab propose` start. `load_bars` stays tolerant (NaN plus one warning). | A backfill with holes can't reach a trial, so `data_id` can't silently mix spot data. One bad coin is handled by exclusion, not by halting the lab. |
| `data_id` | **Lead's call (X2):** rule-versioned. `spot` is at `rule` 1, so `data_id` is unchanged today. Any later alignment, NaN or definition change bumps `rule` and moves `data_id`. | Nothing judged reads spot (checked: none of the 53 plugins in `strategies/` names a spot column, 2026-11-05), and X3 makes the data complete before any run reads it. |
| Synthetic | **Lead's call (X5)** for the generator, `default_rng((seed, synthetic_id))`. **Designer's call** for the shape: a slow persistent basis state plus a fast AR(1) plus jumps; spot volume = perp volume × a moving share; two NaN stretches. Calibration rule fixed below. | Persistent states make 24-bar-mean thresholds fire, so the check sees a peek in that branch. Its own stream changes no existing synthetic value. |
| Which plugins read spot | **Lead's call (X4):** `reads_columns(cfg, src, spot)`, static OR dynamic, decisions only. | It decides the exam coverage and the paper refusal, which can't be undone. |
| Exam | **Lead's call (X1, X4):** `exam_required: if_read`: spot must cover the window only for a plugin that reads spot. | A spot gap must not block an unrelated plugin's exam. |
| Paper | **Designer's call:** deferred; `lab paper` refuses a spot reader (X4). Live design below. | Paper already lacks OI, regime and taker columns. Paper needs a passed exam, and none can run before about 2026-12-28. |
| Dashboard | **Designer's call:** no change; the Ideas page's column picker follows `PLUGIN_COLUMNS`. | Nothing to show yet. |

## Goals, non-goals, anti-goals

**Goals.** Five columns on every research bar at 15m, 1h and 4h. Moving synthetic twins, covered by the lookahead
check. Backfill by `lab backfill`, behind the holdout guard. The builder told exactly what they are and how `basis`
relates to funding. A correct exam for plugins that read them.

**Non-goals.** A spot leg or basis-arbitrage book. Premium, index or mark columns. Other venues' spot. A live spot
feed in paper (designed, built later). Any change to gates, costs, benchmarks, folds, the holdout or N.

**Anti-goals: failures even if it works.**
- A spot value on bar t that wasn't known at t's close: a join that picks the next spot bar, a fill from a later bar,
  or **any NaN rule that looks at a later bar**. For example, blanking the bar before a spot halt would announce the
  halt.
- A synthetic twin that is constant, never crosses a real-level threshold, or is NaN-free where real data isn't.
- Any spot row at or after `holdout_start` in a research read.
- A trial, or a walk-forward run, on spot data with holes: it counts in N and can become a reusable parent run.
- A bar that got easier: a separate N for spot trials, or a softer gate for new data.
- A funding idea re-run as a basis idea and counted as new. The context text and the referee are the guard; the
  post-trial correlation check (≥ 0.8 with a closed idea → duplicate) is the backstop.

## Data

### Sources compared (S3 listings, 2026-11-05; unverified at build, see "First build task")

Network access failed partway through this design, and the reviewer's also failed. Every fact below comes from
listings and a partial download made before that, and the first build task re-confirms it.

| coin | spot first month (15m, 1h, 4h, 5m) | perp first month | premium / index / mark first month (1h) | spot vs perp |
|---|---|---|---|---|
| BTC, ETH | 2017-08 | 2020-01 | 2020-01 | before |
| SOL | 2020-08 | 2020-09 | 2020-09 | before |
| BNB | 2017-11 | 2020-02 | 2020-02 | before |
| XRP, XLM | 2018-05 | 2020-01 | 2020-01 | before |
| DOGE | 2019-07 | 2020-07 | 2020-07 | before |
| AVAX | 2020-09 | 2020-09 | 2020-09 | same month |
| LINK | 2019-01 | 2020-01 | 2020-01 | before |
| ADA | 2018-04 | 2020-01 | 2020-01 | before |
| DOT | 2020-08 | 2020-08 | 2020-08 | same month |
| LTC | 2017-12 | 2020-01 | 2020-01 | before |
| BCH | 2019-11 | 2020-01 | 2020-01 | before |
| FIL, AAVE | 2020-10 | 2020-10 | 2020-10 | same month |
| ATOM | 2019-04 | 2020-02 | 2020-02 | before |
| ETC | 2018-06 | 2020-01 | 2020-01 | before |

- Same symbol for all 17: `{COIN}USDT`, quoted in USDT like the perp. No spot market starts in a later month than its
  perp. Day-level order for AVAX, DOT, FIL and AAVE is unknown.
- No missing monthly file in any listed series between its first month and 2026-10.
- Format: the same 12 columns as futures klines, no header row, timestamps in microseconds from 2025 (seen in a
  2026-06 BTC file); `vision._to_ms` handles both.
- Gaps: a BTCUSDT spot 1h sample (60 research months) has 30 missing hours in 11 months, all 2020-02..2021-09
  (maintenance, up to 6 hours), and 2 zero-trade hours. ETH 2020-01..10 shows the same windows. Whether the perp traded
  through them is unknown; either way those bars get NaN spot.
- Rejected: `premiumIndexKlines` (the premium against Binance's multi-exchange index, which funding averages),
  `indexPriceKlines`, `markPriceKlines`. All start with the perp and carry OHLC only, no volume.

### First build task (Lead's call, X7)

Before any other spot task touches bars:
1. `vision.py` learns `spotKlines`.
2. `lab backfill --datasets spotKlines`.
3. A committed inventory, `docs/research/spot-basis-inventory/` (a script plus CSVs; research span only, distributions
   only, no forward returns): per coin and tf, the first spot month and day, manifest sources, missing months, the
   share of perp bars with NaN spot and NaN runs over a day, the symbol-check median, and the calibration statistics
   (below).
4. `spot_exclusions` and the spot `known_gaps` in `lab.yaml` are fixed, then never edited after a spot trial runs.

Facts it must confirm, and what changes if one is false:

| fact | if false |
|---|---|
| a `{COIN}USDT` spot pair exists for all 17 | the coin goes in `spot_exclusions` (all-NaN, disclosed); a symbol map is a later spec |
| spot starts no later than the perp | NaN at the coin's start only; the context wording holds either way |
| no missing spot month 2020-01..2026-06 | each such month goes in `known_gaps` (its bars NaN), or the coin in `spot_exclusions` if the holes are wide |
| the daily URL `data/spot/daily/klines/{SYM}/{tf}/{SYM}-{tf}-{YYYY-MM-DD}.zip` | the current month doesn't build; only spot readers' exam windows end early; fix the URL; research unaffected |
| spot and perp share the UTC grid (4h opens at 00, 04, … UTC) | the exact join attaches nothing (the NaN share shows it); build from a finer timeframe instead |
| maintenance gaps are rare | if a coin's NaN share is large, the context discloses it |
| every symbol-check median is within ±1% | the coin goes in `spot_exclusions` |
| Binance's zero-fee BTC spot period was about 2022-07..2023-03 (from memory) | the README note changes its dates or goes |

### Storage and backfill

- `data/{SYM}/spot/{tf}/{YYYY-MM}.parquet`, schema `vision.CANDLE_SCHEMA`, parsed by `vision.parse_klines`, plus
  `_manifest.json` (the same `Manifest`). `config/lab.yaml`: `backfill.datasets: [klines, fundingRate, metrics,
  spotKlines]`. Months before a coin's first spot file are recorded `absent` (`first_archive_month`).
- Size (listings, prorated from 2020-01; an estimate): zipped 15m ≈ 171 MiB, 1h ≈ 46, 4h ≈ 13, about 230 MiB and
  4,060 monthly files plus checksums; parquet of the same order. Run time roughly 10–30 minutes at 8 workers, not
  measured. Holdout months download too and stay behind the guard.

### Alignment, publish lag, gaps

`store.attach_spot(bars, spot)`, a pure function:
- Each perp bar takes the spot row with the **same `open_time`**. A spot bar and its perp bar then cover the same
  interval and close at the same instant. Spot rows with no perp bar are dropped.
- **Publish lag: none.** A kline's close is the last trade before the boundary. OI's 5-minute lag exists because its
  snapshots come from a separate, delayed endpoint.
- X6 as in Decisions. No forward fill, no interpolation. Each bar's values depend on that bar's two rows alone.
- **NaN:** before a coin's first spot trade; after a spot delisting (flat zero-trade candles); in spot gaps and
  halts; on every bar of an excluded coin; and everywhere when the spot folder is missing. In that last case
  `load_bars` warns once per (coin, tf). That warning is new behaviour: missing metrics are silent today.
- **Spot halted mid-bar while the perp traded:** the bar keeps its last spot trade, and its `basis` may be stale. That
  isn't detectable from the bar itself, and fixing it from the next bar would be lookahead. It is disclosed.
- **Last-trade noise:** negligible for BTC and ETH (half-spread about 0.02 bp); a few bp for thin coins (0.5–1 bp
  half-spreads, per the slippage calibration).

### Holdout

`store.load_spot(cfg, symbol, tf, start=None, end=None, holdout=None)` resolves `end` with `_resolve_end`, as
`load_candles` does: without a token an end past `holdout_start` raises `HoldoutViolation`, and rows are kept in
`[start, end)`. `attach_spot` joins onto perp bars already cut at `end`. These are two guards, each tested alone. The
exam's `load_bars(..., holdout=token)` reads spot through the window.

## Plugin-facing interface

| column | what | unit |
|---|---|---|
| spot_close | the last trade on Binance spot (`{COIN}USDT`) in the bar; NaN where spot didn't trade in the bar | USDT |
| spot_quote_volume | volume traded on spot in the bar; NaN where spot didn't trade | USDT |
| spot_taker_buy_quote_volume | spot volume bought by takers (the rest was sold by takers); NaN where spot didn't trade | USDT |
| spot_trades | number of spot trades in the bar; NaN where spot didn't trade | count |
| basis | `close / spot_close − 1`, the perp's premium over spot at the bar's close; NaN where spot or the perp didn't trade | fraction |

**Basis and funding.**
- For an 8-hour coin, Binance's settled rate is F = P + clamp(0.01% − P, −0.05%, +0.05%), where P is the average
  premium index over the interval.
- Inside the band (P between −0.04% and +0.06%), F is exactly 0.01% whatever P is. There `funding_last` says nothing
  about the premium, and `basis` is the plugin's only view of it.
- Outside the band, F = P − 0.05% (above) or P + 0.05% (below). There, `basis` averaged over the interval tracks
  the next funding rate, and an idea built on that average is a funding idea.
- `funding_last` describes a premium up to 16 hours old on a 1h bar.
- P uses depth-weighted prices against a multi-exchange index, so `basis` ≈ P, not = P.

**How the worker gets them.** They are ordinary columns of the `load_bars` frame. The walk-forward, the random-entry
benchmark (it wraps the plugin's `prepare`), the exam's stream (whole rows) and `client.Batch` send that frame as an
Arrow table (`protocol.table_bytes`). There is no protocol, sandbox or engine change, and the engine reads none of
them. Memory: about 9 MB per coin at 15m, about 150 MB more in a walk-forward pool process (16 coins cached).

**Design 1 (Lead's call, X1).** Design 1 builds `btc_*` from an explicit whitelist over BTC's candles, funding and
metrics, never from BTC's whole `load_bars` frame. So no `btc_spot_*` or `btc_basis` exists. The spot step sits in
`load_bars` after `align_metrics` and before `attach_regime`. Each group has its own synthetic generator (X5), so
build order and draw counts don't couple the two designs.

## The lookahead check and the synthetic bars

The spot group's synthetic function, `columns.spot_synthetic(bars, rng) -> DataFrame`, is called by the registry's fill
in `with_moving_columns` with `rng = default_rng((seed, spot.synthetic_id))` (X5). Like every group, it is filled when
any of its columns is missing, all-NaN or constant, and then all five together. `synthetic_bars` carries the five as
NaN. It reads the filled `close`, `quote_volume`, `trades` and `taker_buy_quote_volume`. Each value at row t uses rows
≤ t:

- `basis = slow + fast`:
  - `slow`: AR(1) with φ 0.995 around a mean `c` (0 by default), stationary sd 10 bp. Its half-life is about 140 bars
    (6 days at 1h), giving multi-day, sign-persistent crowding states.
  - `fast`: AR(1) with φ 0.9, stationary sd 5 bp, plus on 1% of rows a jump of ±20–60 bp that then decays.
  - Constants: `BASIS_SLOW_PHI`, `BASIS_SLOW_SD`, `BASIS_C`, `BASIS_FAST_PHI`, `BASIS_FAST_SD`, `BASIS_JUMP_P`,
    `BASIS_JUMP`.
- `spot_close = close / (1 + basis)`. Then `basis` is recomputed as `close / spot_close − 1`, so the identity is exact.
- `spot_quote_volume = quote_volume × s_t`.
  - log s_t = log `SPOT_SHARE` (0.3) + an AR(1) with φ 0.99 and **stationary** sd 0.3 (innovation sd ≈ 0.042) + iid
    noise with sd 0.3.
  - So it is correlated with perp volume (about 0.8 in logs), but the share moves.
- `spot_taker_buy_quote_volume = spot_quote_volume × clip(0.5 + 0.6·(perp taker share − 0.5) + N(0, 0.05), 0.05,
  0.95)`.
- `spot_trades = round(trades × s'_t)`, where s'_t is a second share built like s_t.
- **NaN stretches:** all five are NaN on rows [0, 288) (spot not listed) and [432, 480) (a halt; moved from
  [1536, 1584) by ruling S5).
  - The halt sits right after the first NaN cut row (399, `causality.NAN_CUTS`), so a rolling window over spot
    clears both by the later cuts and long windows stay visible to the check: up to 620 bars at 1100 and 2020 at
    2500.
  - Both start and end on multiples of 48, so resampled 15m/1h/4h buckets are whole.
  - No `CUTS` value falls inside them.
  - A plugin that crashes on NaN spot fails the check, which is not a trial, rather than crashing on real bars as a
    broken trial that counts in N.

**Calibration rule (Designer's call, fixed now, before seeing data).** Run once after the first build task:
- Targets T: from the inventory, pooled over the non-excluded coins on research-span **1h** bars (the check always
  runs 1h bars, `causality.H`, even for 4h plugins). T is p1 and p99 of (a) the `basis` level and (b) its trailing
  24-bar mean, plus the median of `spot_quote_volume / quote_volume`.
- Synthetic statistics S: the same five, from `with_moving_columns(synthetic_bars(seed=1))`, the check's default bars.
- Steps:
  1. `BASIS_C` = the midpoint of T's 24-bar-mean p1 and p99.
  2. `BASIS_SLOW_SD` ×= (T p99 − T p1) / (S p99 − S p1) of the 24-bar mean.
  3. Recompute S. `BASIS_FAST_SD` and `BASIS_JUMP` ×= the same ratio for the level.
  4. `SPOT_SHARE` = T's median.
- Accept when each of the four tails is within 25% of its target's distance from the median, in both directions. If
  not, repeat steps 2–3 once; if still not, the lead decides before any spot trial.
- **Lead's call (X5):** this lands as its own commit with a `MARKET_VERSION` bump, before the first spot trial.

`perp_lab/validate/synthetic_market.py`:
- `_SUMMED` gains the three spot volume and count columns, summed with `min_count=1` so an all-NaN bucket stays NaN.
- `spot_close` and `basis` take the bucket's last value, so a resampled `basis` equals its `close / spot_close − 1`.
- `MARKET_VERSION` bumps in the group's first commit, and again at calibration.

`causality_failures` and the judge's `dated_lookahead_failures` need no change: both use `with_moving_columns`.

## Judging

- **Unchanged:** gates, thresholds, costs, folds, selection, the island check, minimum trades, the random-entry
  benchmark (it reuses the plugin's `prepare`), and the Deflated Sharpe with one store-wide N. Spot trials count in it.
- **`data_id`:** X2, as in Decisions. A test pins that bumping the spot group's `rule` moves `data_id`.
- **Verdict cache:** unchanged; old verdicts are of plugins that can't read spot.
- **Existing plugins see more columns.** A plugin that works on the whole frame (`bars.dropna()`, a loop over
  `bars.columns`) would change once NaN-bearing columns arrive, which matters for a promoted plugin examined later.
  Checked 2026-11-05: none of the 53 does. A test pins it.
- **Readiness (Lead's call, X3).** The spot group's part of `column_group_problems(cfg, symbols, tf)`. For each coin
  not in `spot_exclusions`, at `tf`:
  - every month from `history_start` to the month before `holdout_start` has a manifest entry with `complete: true`
    and source `absent`, `monthly` or `daily`. `absent` may appear only before the first real month. A month listed in
    the spot `known_gaps` is allowed;
  - the symbol check: the median of `close / spot_close − 1` over bars where both sides traded is within ±1%. It uses
    the last research month with at least 100 such bars. A coin with no such month is a problem; it belongs in
    `spot_exclusions`.
  - It runs before `register_trial` in `judge.judge`, inside `validate_strategy` (`lab walkforward`, refresh and
    parent runs), in design 3's portfolio judge, and at `lab propose` start in `cli.py`. The last place keeps
    `perp_lab/proposer/` from ever reading `data/`.
  - A problem refuses every plugin, because a static scan can't prove a plugin never reads spot. It is a one-time
    gate: exclusions and gaps are fixed in the first build task. (Inside a session a judge refusal resets the infra
    count, `loop._infra(True)`, so only the start check stops a session that would refuse every proposal.)
- **Overlap:** unchanged. On the synthetic market basis and funding are independent, so preflight can't catch a
  basis plugin that is a funding plugin in disguise; the post-trial correlation check can. Accepted.
- **Exam (`exam_required: if_read`).** When `reads_columns(cfg, src, spot)`, the exam also requires spot coverage for the
  exam's non-excluded coins:
  - the window ends at the earliest last spot candle at `tf`;
  - spot months come from `monthly` or `daily` files and are complete;
  - every UTC day holds at least one spot candle **with `trades > 0`**. A day of flat zero-trade candles, a
    delisting, doesn't count.
  - How `window_end` and `window_data_problems` iterate groups is design 1's mechanism. The spot group supplies its
    folder and the `trades > 0` day rule. The commit that changes their signature also updates the stubs in
    `tests/test_judge_exam.py` (`end_after`, `no_gaps`).

## Builder integration

`perp_lab/proposer/context.py`:
- `BAR_COLUMNS` gains the five rows above, after `oi_value`. `PLUGIN_COLUMNS` follows (human, research and
  referee-revised cards are all checked against it in `human.check`; the Ideas page's picker uses it).
- The paragraph under the table gains this text; the bracketed part is built from `spot_exclusions`:

  > `spot_close`, `spot_quote_volume`, `spot_taker_buy_quote_volume` and `spot_trades` come from the same coin's
  > Binance spot market (`BTCUSDT` spot for the `BTCUSDT` perp), from the spot bar with the same open time, and are
  > known at the bar's close like the perp's own columns. They are NaN on a bar in which spot didn't trade (before a
  > coin's spot listing, and in exchange gaps); `basis` is NaN then too, and on a bar in which the perp didn't trade.
  > [Spot columns are NaN on every bar for: none.] A coin's spot columns may start after its first research bar, so a
  > plugin that reads them must handle NaN. A bar in which spot stopped trading early (rare; around exchange
  > maintenance) carries its last spot trade, which may be stale. `basis` is the perp's premium over spot now.
  > `funding_last` is the settled rate; for an 8-hour coin it is F = P + clamp(0.01% − P, −0.05%, +0.05%), P the
  > interval's average premium. F is exactly 0.01% whenever P is between −0.04% and +0.06%, whatever P is, so inside
  > that band funding says nothing about the premium and `basis` is the only view of it. Outside the band F = P − 0.05%
  > (above) or P + 0.05% (below); there, basis averaged over a funding interval tracks the next funding rate, and an
  > idea built on that average is a funding idea. On thin coins `basis` carries a few bp of noise from last trades;
  > smooth it or set thresholds well above that. The harness trades the perp only: a basis that closes pays only
  > through the perp's own move. Spot's share of volume differs by coin and changes over the years; read it as a
  > ratio against its own recent history. A plugin that reads spot columns can't be paper traded yet.

- `_piles`: the synthetic-bars sentence becomes "The synthetic bars' taker, open-interest, volume and spot columns
  are random walks, their basis has slow and fast mean-reverting parts, the spot columns are NaN over two stretches".
- Byte cap: 36,472 bytes today against `STATIC_MAX_BYTES` 100,000; this adds about 2.8 KB.
- Contract and prompt: no change. A dated amendment to the proposer spec, like "Amended: open interest is stored".

## Paper and live path

**Today** paper's bars come from ccxt `fetch_ohlcv` (`paper/runner.py: _warm`, `live.ohlcv_to_candles`) plus funding
seen live. They have no `oi`, `regime` or `slippage`, and NaN taker, trade and quote-volume columns.

**Designer's call: deferred, with a refusal now.** `lab paper` (`cli.py`, beside its existing refusals) exits 2 when
`reads_columns(cfg, src, spot)`: "reads spot columns, which paper does not stream yet". A promoted spot plugin can be
examined but not paper traded until the live path is built, when the first such plugin passes an exam.

**The live path, to build then:**
- `live.fetch_spot_klines(exchange, symbol, tf, since_ms, now_ms)` calls Binance's raw `GET /api/v3/klines` through
  ccxt's implicit `publicGetKlines` on a spot-mode client, `make_exchange(pro=False, market="spot")`. It parses the
  rows with `vision.parse_klines`, so live spot keeps quote volume, trades and taker flow.
- `PaperRunner._warm` attaches spot for the warm-up span with `attach_spot`. `_stream` fetches each closed bar's spot
  kline, retrying up to `paper.spot_wait_s` (designer's call: 10). A miss gets NaN, is logged and is counted in
  `state.json`.
- No `lab tail` for spot: exams require archive sources.

## Changes to existing code and documents

One concern per commit, a test with each. The shared pieces (registry, `column_group_problems`, `reads_columns`,
per-group streams, the exam's group iteration) are design 1's and land first.

1. **First build task (X7):**
   - `perp_lab/data/vision.py`: `SPOT_BASE`; dataset `spotKlines` in `Job`, `first_archive_month`, `dataset_dir`
     (`{symbol}/spot/{tf}`), `_monthly_url`, `_daily_url`, `_PARSERS`, `_SORT_KEY`; per-timeframe jobs in `backfill`.
   - `config/lab.yaml` `backfill.datasets`; the `cli.py` help text.
   - The backfill, then the inventory, then `spot_exclusions` and the spot `known_gaps` in `lab.yaml`.
2. `perp_lab/data/store.py`: `load_spot`, `attach_spot` (X6), the spot step in `load_bars(..., with_spot=True)` with
   its warning. `validate/benchmarks.buy_and_hold` passes `with_spot=False`.
3. `perp_lab/data/columns.py`: the `spot` group (rule 1, `synthetic_id`, `exam_required: if_read`, helpers) and its
   readiness rules: full span, `known_gaps`, exclusions, the symbol check.
4. `perp_lab/validate/causality.py` / `columns.spot_synthetic`: the five NaN columns and the group's stream with the
   default constants. `synthetic_market.py`: `_SUMMED`, `MARKET_VERSION`.
5. `perp_lab/judge/exam.py`: the spot folder and the `trades > 0` day rule through design 1's group iteration;
   `tests/test_judge_exam.py` stubs in the same commit.
6. `perp_lab/cli.py`: the `paper` refusal for spot readers (the `propose` start check is X3's, design 1's).
7. `perp_lab/proposer/context.py`: rows, paragraph (with exclusions), the `_piles` sentence.
8. `tests/conftest.py`: `write_market` writes `spot/{tf}` candles (basis within a few bp) and complete spot manifests.
9. **Calibration commit:** constants from the rule, `MARKET_VERSION` bump. Before the first spot trial.
10. `README.md`: data list, layout, backfill command, paper caveat, the 2020–21 spot maintenance gaps, the zero-fee
    BTC period once confirmed. The proposer spec: the dated amendment.

## Testing strategy

Tests never touch the real `data/`, `runs/`, `proposer/` or `lab.db`; vision tests mock `_get`.

- **Alignment** (`tests/test_spot.py`, new):
  - exact join; an off-grid spot row isn't attached;
  - a missing or zero-trade spot bar → five NaN;
  - a zero-trade perp bar → `basis` NaN and the four spot columns kept;
  - the bar after a gap has its own values;
  - `basis == close / spot_close − 1` exactly.
- **Causality of the join:**
  - `attach_spot(bars[:k], spot)` equals `attach_spot(bars, spot)[:k]`;
  - truncating `spot` after row t changes nothing at or before t;
  - a zero-trade bar at t+1 leaves bar t unchanged.
- **Holdout:**
  - `load_spot` without a token and `end > holdout_start` raises;
  - with spot files running into the holdout, `load_bars` equals the frame with those files deleted, with no value at
    or after `holdout_start`;
  - with a token, the exam's `load_bars` returns holdout spot.
- **Missing spot:** NaN columns and one warning per (coin, tf).
- **Backfill** (`tests/test_backfill_listing.py`): the spot prefix, `dataset_dir`, monthly and daily URLs, jobs per tf,
  `absent` months, a microsecond file parsed to ms.
- **Readiness:**
  - each of these is a problem: a missing mid-span month; a `partial` (incomplete) month; an unmarked month; `absent`
    after the first real month; a 1000× spot price; a coin with no traded month;
  - a `known_gaps` month passes;
  - an excluded coin is skipped and loads all-NaN;
  - `judge` raises `JudgeRefused` with `trial_stats` unchanged;
  - `lab walkforward` is refused with N unchanged;
  - `lab propose` exits 2 before any model call.
- **Reads spot (X4):** static hits on `ctx["basis"]`, `bars.basis` and `bars[["spot_close", "close"]]`; dynamic hit
  on `bars.filter(like="spot_")`; a docstring saying "basis" is no hit; `lab paper` exits 2 for a reader.
- **Lookahead** (`tests/test_causality.py`):
  - `EXCHANGE_COLUMNS` gains the five, so the peeker test covers each;
  - `int(ctx["spot_trades"])` is reported (it crashes on a NaN stretch);
  - a plugin whose default branch thresholds the 24-bar mean of `basis` at ±20 bp and peeks at the next bar is caught;
  - for every plugin in `strategies/`, features and decisions are identical with and without the spot columns.
- **Synthetic:**
  - all five move;
  - the basis identity holds on 5m and resampled 4h bars;
  - lag-24 autocorrelation of `basis` is above 0.3, and its 24-bar mean changes sign at least 3 times in 3,000 bars;
  - corr(log spot, log perp quote volume) is above 0.5;
  - the NaN stretches are where documented and stay whole after resampling;
  - every pre-existing synthetic column is bit-identical (X5);
  - the calibration's four tail statistics are within 25% after the calibration commit.
- **Context** (`tests/test_context.py`):
  - each row;
  - the clamp sentence, exactly as written;
  - "inside that band funding says nothing" and "outside the band … a funding idea";
  - the exclusions line from config;
  - "the perp only" and "can't be paper traded yet";
  - `PLUGIN_COLUMNS` holds the five;
  - the size is under the cap.

## Risks and open questions

- **Stale or noisy basis.** Designer's call: one raw definition. If the inventory shows thin coins' 1h basis
  dominated by noise (p50 |basis| near their half-spread), add the premium index as a sixth column in a follow-up,
  with its own group `rule`, rather than redefining `basis`.
- **Basis as funding in disguise.** Designer's call: don't couple them synthetically. Cost: possibly a trial or two on
  a funding re-run before the record calls it a duplicate.
- **`reads_columns` residual.** A plugin whose decisions on the two synthetic seeds happen not to differ, and that
  names no column literally, is missed (X4). Its exam would ignore spot gaps, and its paper refusal wouldn't fire.
  This is rare, since both tests must miss.
- **Readiness refuses every plugin.** Accepted (lead and reviewer): it is a one-time gate, and exclusions keep one
  bad coin from halting the lab.
- **Calibration targets.** Tails at 1h may make 4h plugins' thresholds fire less on the 1h check bars. The 24-bar mean
  (a day at 1h) is the bridge. If a 4h spot plugin fails "no signals", that is the place to look, and any change is a
  new calibration commit with a `MARKET_VERSION` bump, before that plugin's trial.
- **Survivor bias.** Nothing new for single-coin plugins. Design 3, ranking coins on basis or spot share, inherits the
  universe's bias at full weight.

**Builder questions, answered.**
1. Exclusions and gaps (X3): an excluded coin is skipped, not failed closed. `lab.yaml` is edited by the human or the
   lead, fixed in the first build task, before any spot trial.
2. Design 1's reference step (X1): an explicit whitelist, so no `btc_spot_*` exists.
3. Calibration: the rule above.
4. Dynamic `reads_columns` (X4): decisions only, not feature columns.

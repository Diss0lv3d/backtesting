# Cross-coin reference bars: BTC's bar on every coin's bars

Status: design, 2026-11-05, revised the same day after review and the lead's cross-design rulings. Not built.
Design 1 of 3 from the "widen what strategies can see" brief (1 cross-coin reference, 2 spot and basis, 3
cross-sectional portfolio engine); build order 1, 2, 3. This spec lands first, so it defines the pieces the three
share (X1 to X7 below, each marked "Lead's call"). Design 2 uses them and adds its group; design 3 uses X3 and X4.
The user skipped Q&A, so every other choice is marked "Designer's call", with its reason and its cost if wrong.

Scope: what a single-coin plugin can read (BTC's same-timeframe bar, attached to each coin's bars), the checks that
keep it causal, the exam and paper paths that must carry it, and what the builder is told. Gates, benchmarks,
costs and N don't change.

## Why

After 54 trials the builder concluded that single-coin bar rules are mined out. The best results were long
continuation after a strong 24h move on unusual volume, and a weaker cousin on rising open interest: net Sharpe
0.7 to 1.33, Deflated Sharpe at most 0.89, against a bar of about 1.61 at N = 54. Today a plugin sees one coin. An
alt's hourly move is mostly the market's move (BTC's), so every single-coin signal mixes the coin's own news with
the market's, and no plugin can tell them apart. With BTC's bar on each coin's bars it can.

Cost per trade, from the lab's own model: a round trip pays 10 bp of taker fees plus slippage on both fills, which
the model puts at about 2.5 bp a side for a coin trading $1B a day and about 8 bp for one trading $20M. So about 15 to
25 bp, or 0.08 to 0.13 R with a 2% stop. 15m is cost-killed in this lab; the families below are judged at 1h and 4h.

| family | rule, in short | where it comes from | plausibility after costs |
|---|---|---|---|
| A. BTC leads alts | after a large BTC move on bar t that coin X has not matched, enter X in BTC's direction | lead-lag from large to small assets: Lo and MacKinlay (1990), Hou (2007) in equities; in crypto, documented mainly at minute horizons | Low at 1h, very low at 4h. Most of the catch-up happens within minutes, inside bar t; what is left at bar t+1's open must beat 15 to 25 bp. The next-open fill also flatters an edge that decays in seconds (Risks). |
| B. Residual momentum and residual reversal | residual e = r_X − β·r_BTC, β a rolling estimate. Momentum: trade with a strong 1 to 7 day residual. Reversal: fade a large 1 to 4 bar residual that came without volume | residual momentum: Blitz, Huij and Martens (2011); crypto momentum factor: Liu, Tsyvinski and Wu (2022); short-term reversal as pay for liquidity provision | Momentum at 4h: low to moderate. Reversal at 1h or 4h: low, since its gross profit is mostly the spread a liquidity provider earns and this engine always pays taker. For both: the engine can't hedge BTC, so a position in X carries X's full market beta. The residual is only the signal; the P&L keeps the market noise the signal removed. The hedged version is design 3's. |
| C. BTC regime or trend filter on an alt signal | take an alt signal only while BTC is above its N-bar mean, or ADX-trending, or not crashing | time-series momentum: Moskowitz, Ooi and Pedersen (2012); practitioners' risk-on filters | Moderate as a filter on a signal that already has some edge; low alone. It cuts trades (less evidence per Sharpe point) and adds a parameter. A long-only alt plugin filtered on BTC's uptrend is partly BTC trend-following, and the lab's single-coin trend trials (donchian_atr) died. The market diagnostic below shows how much. |
| D. Relative strength against BTC | trade breakouts of X's price in BTC (X/BTC), with the position in USDT | cross-sectional crypto momentum (Liu, Tsyvinski and Wu 2022); alt/BTC pair trading in practice | Low to moderate at 4h. With β = 1 it is family B. Its plugins will overlap B's in positions and returns, and the duplicate rules will say so. |
| E. Market-wide shock against coin-specific shock, on the volume-continuation effect | the lab's best near-miss, split by whether BTC also moved and traded heavily (market-wide) or not (coin-specific) | high-volume return premium (Gervais, Kaniel and Mingelgrin 2001); coin-specific news diffuses slowly, market moves don't | The most promising, since it aims at the one effect that showed net Sharpe up to 1.33. The honest odds are still low to moderate: the split must lift that to about 1.61 on fewer trades. The record's rules may also block it (Builder integration). |

BTC's funding and open interest also allow a sixth family, market-wide leverage (BTC funding or OI stretched) as a
filter. It is not among the five above; it's there because the weaker near-miss used OI.

## Goals

- Every coin's bars carry BTC's same-timeframe bar in research, in the exam's stream and, after Part B, in paper.
- The lookahead check and preflight cover every new column and BTC's own bars; the builder is told what they are.
- Every rule here (columns, alignment, lags, zero-trade, synthetic data, the paper wait) is fixed before any trial.

## Non-goals

- ETH or a second reference coin; hedged or two-coin positions (design 3); spot and basis (design 2).
- BTC's regime label as a column (a plugin computes any BTC regime from `btc_` OHLC).
- The paper path's existing gaps in a coin's own columns (Risks). Any change to gates, benchmarks, costs or N.

## Anti-goals: failures even if it works

- A `btc_` value on bar t not known at bar t's close (a later or coarser BTC bar, OI without its 5-minute lag, a
  full-sample statistic in the column), or a research read of BTC at or after `holdout_start`.
- A `btc_` column that the synthetic bars lack or hold constant, so a peek at it goes unseen (Ruling R24's case).
- A paper decision on bar t that sees BTC's bar t+1, or an unfinished BTC bar t, or a late BTC bar filled in with
  an earlier one, or a NaN that no log records.
- A bar that got easier: any change to gates, the random benchmark, costs or N for plugins that read `btc_`.
- A BTC-uptrend-filtered alt long read as an edge when it is BTC trend-following in a survivor, bull sample.
- A run on new data mixed with, or reused for, a run on old data without anyone knowing.

## Decisions

| question | decision |
|---|---|
| Reference coins | **BTC only.** Designer's call: BTC is the market factor; ETH's hourly correlation with BTC is high, so `eth_` columns would add little the model can't get from BTC, and a wider column set mostly widens the builder's search, which every trial pays for in N. ETH later is its own column group and spec. Cost if wrong: ETH/BTC ("alt season") signals wait for that spec, or for design 3, which sees ETH anyway. |
| Columns | **Eight:** `btc_open`, `btc_high`, `btc_low`, `btc_close`, `btc_quote_volume`, `btc_taker_buy_quote_volume`, `btc_funding_last`, `btc_oi_value`. Designer's call: OHLC for returns, ranges and indicators (families A to D); quote volume and taker flow for market-wide shocks (E); funding and OI because the weaker near-miss used OI and market leverage is a plausible filter. Left out: base-asset volume and OI (redundant with the USDT ones), trade count, `funding_charge` (an engine charge for the coin's own position), `funding_interval_h`, regime. Bytes are not the constraint: the static context is 36,472 bytes of its 100,000 cap today, and this adds about 3 KB. Cost if wrong: funding and OI add two paper feeds (one of them short, see Paper) and two forking paths. |
| BTC's own bars | **The `btc_` columns equal BTC's own** (`btc_close == close`, and so on), except on BTC's zero-trade bars, where X6 makes them NaN. Designer's call: a trend filter (C) and a shock split (E) make sense on BTC itself; NaN would bench BTC for them too. For A, B and D the residual is exactly 0 on BTC (`rolling_beta` returns exactly 1 there), so such plugins don't signal there, and the builder is told to skip BTC with `ctx.symbol == "BTCUSDT"` where the rule is about other coins. Cost if wrong: a lead-lag plugin that forgets the skip trades BTC's own short-term momentum in its coin pool; a plugin that skips BTC still counts it as a listed coin, diluting its equal-weight portfolio by about 1/17 (the same rule as flat days before OI starts). |
| Alignment | Exact `open_time` match at the plugin's own timeframe, never another timeframe. The six bar columns are NaN on a coin bar whose BTC bar is missing or has zero trades (X6); nothing is filled forward, since a fill would invent a zero BTC return. `btc_funding_last` and `btc_oi_value` are taken as of the coin bar's `close_time` by the coin's own rules (last settlement with `funding_time <= close_time`; last snapshot with stamp + 5 min <= `close_time`), so they don't depend on BTC having a bar. |
| Before listing | A coin has no bars before its listing, so it has no `btc_` values there; a BTC lookback warms up from the coin's first bar. BTC's archive starts 2020-01, as early as any coin. |
| Synthetic data | The check's bars get a synthetic BTC (X5): returns correlated with the coin's (ρ = 0.7), its own trends, volume that rises with its moves, taker flow that leans with them, 8-hourly funding, an OI that is NaN for the first 300 bars. In the preflight market the reference coin is named `BTCUSDT` and the other three move with it; `MARKET_VERSION` goes to 3. |
| Self-reference check | The lookahead check runs "as BTC": symbol `BTCUSDT`, `btc_` columns equal to the coin's own, at the default params and at the frozen exam params when the check is given them. Designer's call: it is the only way a judge without preflight sees a crash or a symbol-gated peek on BTC's own bars, and the exam must not run BTC at params never checked as BTC. Cost: one or two more variants of typically five to nine, so 10 to 25% more check time. |
| How the worker gets them | As columns of the coin's own frame, through `load_bars`, in the Arrow table the sandbox already sends. No protocol change. A worker still holds no series beyond its own frame's rows (see the sandbox spec amendment). |
| Missing BTC data | `load_bars` is tolerant: NaN `btc_` columns and one warning (X3). The readiness check refuses judging before any trial while BTC's research months are incomplete. |
| Judging | Unchanged: gates, thresholds, the random-entry benchmark, sensitivity, the before-costs pass, minimum trades, Deflated Sharpe with the store-wide N, the overlap and correlation rules. New, information only, in Part A: the market diagnostic (BTC beta, correlation, BTC-hedged Sharpe). |
| Data identity | Unchanged today (X2): the reference group is at rule 1. |
| Exam | The window ends where BTC's data does too, and BTC's months and days must come from exchange files, even when BTC is not an exam coin (`exam_required: always`). |
| Paper | Part B: one shared reference feed; coin X waits for BTC's finished bar t until bar t's close + 30 s, then decides with the bar columns NaN on row t and logs it. Until Part B is built, `lab paper` refuses any plugin that reads the group (X4). |
| Builder | Bar table rows and one paragraph in `context.BAR_COLUMNS`; two helpers in `perp_lab/indicators.py`; the preflight sentence on synthetic columns. |

## Shared pieces: X1 to X7 (defined here)

**X1. Column groups.** Lead's call, so designs 1 and 2 add columns the same way and design 3 finds them in one place.
New `perp_lab/data/columns.py` holds `@dataclass(frozen=True) ColumnGroup` and `GROUPS: tuple[ColumnGroup, ...]`:
- `name` (`"reference"`; design 2 adds `"spot"`), `columns` (exact plugin-facing names, in bar-table order),
  `prefix` (`"btc_"`; design 2's `"spot_"`, with `basis` listed by name);
- `rule: int`, starting at 1 (X2); `synthetic_id: int` (X5; the reference is 1, design 2 takes 2);
- `exam_required`: `"always"` or `"if_read"` (the reference is `always`);
- `helpers: tuple[str, ...]`: indicator functions that read the group (`("reference_frame",)`), for X4;
- `paper_ready: bool`: whether `lab paper` can stream the group (the reference is False until Part B). Designer's
  call: the field X4's paper refusal reads, which the lead's list implies.
- `load_source(cfg, tf, start, end, holdout)`: optional; a source shared by every coin, which the walk-forward
  caches and the exam builds once; `attach(bars, symbol, tf, source) -> pd.DataFrame`; `synthetic(bars, seed)`
  (X5); `sources(cfg, symbols, tf)`: the (symbol, dataset) directories X3 checks.

`load_bars` attaches each group in `GROUPS` by its own `attach`. The reference group builds `btc_*` from an explicit
whitelist over BTC's candles, funding and metrics (`perp_lab/data/reference.py`). It never takes BTC's `load_bars`
frame, so no `btc_spot_*` or `btc_basis` can exist.

**X2. Data identity.** Lead's call. `runs.data_identity` gains `{"columns": {group.name: group.rule}}` only for
groups whose `rule` > 1. Today every group is at rule 1, so today's data_id doesn't change and no parent re-runs.
Any later change to a group's alignment, NaN or definition rule must bump its `rule`, which moves data_id. Why:
nothing judged can read the new columns and X3 makes a group's data complete before any run reads it, so no old
run mixes with new data, and a later rule change can't slip through silently.

**X3. Readiness.** Lead's call. `columns.column_group_problems(cfg, symbols, tf) -> list[str]` checks every research
month (`history_start` to the month before `holdout_start`) of every group's source data from the manifests
`lab backfill` writes: a complete entry for each month, `absent` only before the first real month, and the
symbol check (for the reference, one research month of `data/BTCUSDT/<tf>` read for `open_time` only, its steps
equal to the timeframe). It never reads the holdout. It runs before `register_trial` in `judge.judge`, inside
`validate_strategy` (so `lab walkforward`, refreshes and parent runs), in design 3's portfolio judge, and at
`lab propose` start, for each proposer timeframe (in `cli.py`, so `perp_lab/proposer/` still never reads
`data/`). Any problem refuses the run: no trial is counted.
- `load_bars` stays tolerant: a missing source gives NaN columns and one warning per process per (group, tf), so
  fixture data dirs without BTC keep working and `lab backtest` runs with NaN `btc_`.
- `lab.yaml` gains `column_groups:` with per-group lists: `exclusions` (coins with no usable source: all-NaN
  columns, disclosed in the context, skipped by the check; design 2's spot exclusions live at
  `column_groups.spot.exclusions`; the reference has none, since its one source can't be excluded) and
  `known_gaps` (`{symbol, dataset, month, why}` entries the check accepts). `known_gaps` is fixed after the
  real-data step (X7) and before any trial of the group, and never edited after one has run.

**X4. Which plugins read a group.** Lead's call. `plugins.reads_columns(cfg, src, group) -> bool` (`cfg` because the
dynamic half runs the sandbox, imported inside the function: `client` imports `plugins`) is true if either fires:
- Static (`plugins.names_group(src, group) -> list[str]`, AST only, no plugin code run): a string constant or
  f-string part that contains a group column name or the prefix (`"btc_"`); an attribute access named like one
  (`bars.btc_close`); a call to one of `group.helpers` (`reference_frame(bars)`).
- Dynamic: two `client.fresh_run`s at the default params (and the given params, if any) on the check's synthetic
  bars in which only the group's stream differs (`group.synthetic(bars, seed)` with two seeds); any difference
  in entries or exits (decisions, not feature columns) means it reads the group.
- It drives the exam's coverage rule for `if_read` groups (design 2's spot) and `lab paper`'s refusal of any group
  with `paper_ready` False. The reference is `always`, so its exam coverage never depends on the scan.

**X5. Synthetic data.** Lead's call. Each group draws from its own generator, `np.random.default_rng((seed,
synthetic_id))`, so build order and draw counts don't couple groups, and no existing synthetic column changes.
The synthetic reference coin carries the reference coin's real symbol as `ctx.symbol` sees it: `cfg.symbols` is
base + quote, so `"BTCUSDT"` (`reference.SYMBOL`, pinned by a test against `config/lab.yaml`). A plugin's skip-BTC
branch then runs in preflight. Any recalibration of a group's synthetic stream is its own commit with a
`MARKET_VERSION` bump, before that group's first trial.

**X6. Zero-trade bars.** Lead's call. A group's per-bar columns are NaN on a bar whose source bar is missing or
has `trades == 0`; no forward fill anywhere. (Design 2: `basis` is NaN when either the perp or the spot bar has
`trades == 0`.) For the reference, BTC's interior zero-trade candles, which `store._drop_dead_tail` keeps as the
coin's own, give NaN `btc_` bar columns. The as-of two are unaffected.

**X7. Build-time real-data step.** Lead's call. The facts under "Data" were read over the network on 2026-11-05,
before it stopped answering, and are not in the repo. The group's first build task is a committed inventory
(`docs/research/reference-inventory/`): BTCUSDT's first month per dataset and timeframe, its gaps and zero-trade
bars at 15m, 1h and 4h over the research span, the symbol check, and calibration statistics for X5 (real hourly
and 4h return correlations of each coin with BTC, BTC volume against |return|, the spread of BTC's taker share).
Then `known_gaps` is fixed. All of it comes before any trial reads `btc_`. What changes if a fact is false:
- a BTC gap or zero-trade run: listed in `known_gaps`; the NaN rule covers it. BTC metrics later than 2020-09-01
  or a coin older than BTC: the context's fixed dates change; pre-BTC rows are NaN by the alignment rule.
- calibration far from ρ = 0.7 (outside 0.5 to 0.9 for the median coin) or volume not rising with |return|:
  recalibrate the synthetic reference in its own commit, `MARKET_VERSION` bumped, before the first trial.

## Data

**Sources.** No new dataset and no new download: BTC is a universe coin, and `lab backfill` already stores its
klines (15m, 1h, 4h), funding and metrics under `data/BTCUSDT/`. Storage layout and the backfill command are
unchanged. Facts read on 2026-11-05 (S3 listings of data.binance.vision, and the 1h monthly archives of all 17
coins downloaded to a scratch directory, never `data/`), to be confirmed by X7:
- BTCUSDT and ETHUSDT monthly klines exist at 15m, 1h and 4h from 2020-01; funding from 2020-01; metrics from
  2020-09-01 (BTC) and 2021-12-01 (ETH and the other 15).
- First 1h month per coin: BTC, ETH, XRP, LINK, ADA, LTC, BCH, XLM, ETC 2020-01; BNB, ATOM 2020-02; DOGE 2020-07;
  DOT 2020-08; SOL, AVAX 2020-09; FIL, AAVE 2020-10. None precedes BTC.
- BTC's 1h series from 2020-01-01 00:00 to 2026-06-30 23:00 has 56,952 bars, no gap, and one bar with zero
  volume. No coin has a 1h bar where BTC has none. FIL, LTC, SOL, XLM and XRP each have two gaps of their own in
  the monthly files (72 h from 2022-02-26, 48 h from 2022-04-01) while BTC has bars; nothing attaches there.
- BTC at 15m: no gap in the 16 months checked (2024-01 to 2025-04). The rest of 15m and all of 4h are unchecked.
  So at 1h the NaN rule fires in research about once (the zero-volume bar, if it had no trades); it matters live.

**Alignment, lag, holdout.** Both bars cover `[open_time, open_time + tf)` and close at the same instant, so BTC's
bar t is known at the coin's bar t close by the lab's own rule. Funding and OI follow `funding_last` and `oi`
(5-minute lag); a settlement at a boundary (00:00, 08:00, 16:00 UTC) belongs to the bar that opens there. The
reference candles load through `store.load_candles` with the coin's `start`, `end` and token, so `_resolve_end`
guards them; BTC's funding and metrics are cut at the resolved end, so a cached source holds nothing past it.
Row-based lags span gaps: `btc_close.shift(1)` across a coin's own 72-hour gap is a 72-hour BTC move, as today.

## Plugin-facing interface

| column | meaning | unit |
|---|---|---|
| `btc_open`, `btc_high`, `btc_low`, `btc_close` | BTCUSDT perpetual trade prices on its bar with this bar's open time; NaN where that bar is missing or had no trades | USDT |
| `btc_quote_volume` | BTC's volume traded on that bar (NaN as above) | USDT |
| `btc_taker_buy_quote_volume` | BTC's volume bought by takers on that bar (NaN as above) | USDT |
| `btc_funding_last` | BTC's last funding rate settled by this bar's close | fraction of notional per settlement |
| `btc_oi_value` | BTC's open interest at this bar's close (5-minute publish lag, as `oi_value`); NaN before 2020-09-01 | USDT |

`ctx["btc_close"]`, `ctx.hist(...)` and `bars["btc_close"]` work as for any column (`engine.backtest.build_arrays`
takes every bar column); `Strategy`, `Ctx` and the reply contract don't change. Two helpers in
`perp_lab/indicators.py`. Designer's call: the first runs every existing indicator on BTC; the second is the causal
beta, so a plugin doesn't reach for a full-sample `np.polyfit` (a lookahead the check catches, costing a fix-up).
- `reference_frame(bars, prefix="btc_") -> pd.DataFrame`: `open_time`, `close_time` and the six bar columns
  renamed to `open, high, low, close, quote_volume, taker_buy_quote_volume`, same index; e.g.
  `adx(reference_frame(bars))`, `volume_vs_same_hour(reference_frame(bars), 4, col="quote_volume")` (which reads
  `open_time`).
- `rolling_beta(y, x, n) -> pd.Series`: `y.rolling(n).cov(x) / x.rolling(n).cov(x)` (the variance by the same
  cov call, so `rolling_beta(x, x, n)` is exactly 1.0 and a residual against itself exactly 0; `var` differs by
  about 1e-14, and a z-scored residual would then trade noise on BTC); NaN where the variance is 0.

**`perp_lab/data/reference.py`** (the reference group's code; the group itself is registered in `columns.py`):
- `COIN = "BTC"`, `SYMBOL = "BTCUSDT"`, `PREFIX = "btc_"`, `BAR_COLUMNS` (the six), `ASOF_COLUMNS`
  (`funding_last`, `oi_value`).
- `@dataclass(frozen=True) ReferenceSource`: `candles` (open_time, trades and the six bar columns, open_time <
  end), `funding` (funding_time < end), `metrics` (create_time < end); empty frames where the data is missing.
- `load_source(cfg, tf, start=None, end=None, holdout=None) -> ReferenceSource`; `attach(bars, symbol, tf,
  source) -> pd.DataFrame` (exact open_time join with the zero-trade rule; `store.funding_last_at` and
  `store.metrics_at` for the as-of two); `synthetic(bars, seed)` (below).

`store.align_funding` and `align_metrics` move onto pure `funding_last_at(close_times, funding)` and
`metrics_at(close_times, metrics, publish_lag_ms)`, one rule for own and BTC columns, own values unchanged.

**`load_bars`** gains `groups: bool = True` and `sources: dict[str, object] | None = None` (a group's shared source
by group name; None: built here with the same start, end and token). Order: candles, slippage, funding, metrics,
each group in `GROUPS`, regime. `benchmarks.buy_and_hold` passes `groups=False`.

**How the worker gets it.** Each walk-forward pool process caches the shared sources, `walkforward._sources(tf)`
(`lru_cache(maxsize=3)`), passed by `_bars`: one BTC load per process per timeframe, a second or two. Memory: about
15 MB more per coin frame at 15m (4 MB at 1h), up to about 230 MB per pool process with its 16 cached frames. The
exam builds the sources once, with its token. Benchmarks, sensitivity and the before-costs pass use the same bars.

## The lookahead check and the synthetic bars

**`reference.synthetic(bars, seed) -> dict[str, np.ndarray]`**, drawing from `default_rng((seed, 1))` (X5). Each
value at row t uses the coin's rows up to t only:
- BTC log-return r_b[t] = 0.7·r_x[t] + 0.71·σ_t·z[t] + d[t]: r_x the coin's log-return, σ_t its trailing 100-bar
  standard deviation (floored), z standard normal, d a drift held for 300-bar blocks (±0.3·σ_t), so BTC trends
  differ from the coin's and trend filters switch.
- `btc_close` = 20,000·exp(cumsum r_b); `btc_open` the previous close; `btc_high` and `btc_low` the larger and
  smaller of open and close, widened by |N(0, 0.4·σ_t)|.
- `btc_quote_volume` = 1e9 · exp(AR(1) noise) · (1 + 30·|r_b|): heavier on big moves, so shock filters fire.
- `btc_taker_buy_quote_volume` = quote volume × clip(0.5 + 0.15·tanh(r_b/σ_t) + N(0, 0.03), 0.05, 0.95).
- `btc_funding_last`: a new value every 8 hours of bar time, 0.0001 + N(0, 0.0001), one in 15 a ±0.001 spike.
- `btc_oi_value`: a log random walk times `btc_close`, NaN on the first 300 rows, as real BTC OI is NaN before
  2020-09-01, so a plugin that breaks on NaN breaks here. The first cut (400) is past it.
- The six bar columns are NaN on row k−1 for alternate cuts only (`causality.NAN_CUTS`, the first and third), as
  real BTC's are on a zero-trade bar (X6): a fill from later bars (`bfill`, `interpolate`) is caught at those cuts,
  and since a next-bar peek shows only on the cut run's last row, the other two, where BTC's bar is set, still
  catch a peek gated on a `btc_` column.

**`perp_lab/validate/causality.py`.**
- `with_moving_columns` adds each group's columns through `group.synthetic` when any of them is missing, all-NaN
  or constant (as a group, so OHLC stay consistent). Its existing fills stay byte-identical. Both the default
  check and the holdout-dated check (`judge.dated_lookahead_failures`) go through it.
- `as_reference_coin(bars) -> pd.DataFrame`: a copy whose `btc_` columns equal its own (`close`, `quote_volume`,
  `taker_buy_quote_volume`, `funding_last`, `oi_value`, and so on).
- `causality_failures` adds "as BTC" variants: symbol `reference.SYMBOL`, bars `as_reference_coin(...)`, at the
  default params and, when `params` is given (the exam's frozen params, as `dated_lookahead_failures` passes
  them), at those too. Same cuts, same comparison; the no-signals rule doesn't apply (a plugin may rightly skip
  BTC). Failures read `defaults as BTC: ...` or `<params> as BTC: ...`.

What gets caught: `btc_close.shift(-1)` or any negative shift (row k−1 is NaN in the cut run and set in the full
one); a full-sample beta or mean over a `btc_` column; a centred rolling window; a peek through `ctx._a` gated on
`ctx.symbol == "BTCUSDT"`. What isn't new: the check can't see a branch gated on another real symbol's name; that
hole exists today for every column and is bounded by the exam (G3).

**`perp_lab/validate/synthetic_market.py`.** `SYMBOLS = ("BTCUSDT", "SYN2", "SYN3", "SYN4")`: the reference coin
keeps SYN1's path (seed 100), so its prices equal version 2's. SYN2 to SYN4 mix their 5m log-returns with it,
r_i' = ρ_i·r_1 + √(1−ρ_i²)·r_i with ρ = 0.55, 0.7, 0.85 (same variance), before close, open and high/low are
rebuilt (each keeps its distance from the larger or smaller of open and close) and before `with_moving_columns`.
The `btc_` columns `with_moving_columns` adds to each 5m path are dropped; after resampling, each symbol gets
`btc_` columns from BTCUSDT's resampled frame by open time (X6 applied), and BTCUSDT's equal its own.
`MARKET_VERSION = 3`. Designer's call: shifting SYN2 to SYN4 moves the preflight overlap of existing plugins a
little, acceptable for a pre-trial filter that decides no gate; a market whose coins don't move with BTC would make
a correlation-gated plugin hold nothing and its overlap meaningless.

## Judging

Nothing changes in the gates, their thresholds, the random-entry benchmark (it wraps the plugin's own `prepare`,
`bracket` and `manage`, which read the new columns like any other), sensitivity, minimum trades, the before-costs
pass, the Deflated Sharpe gate or N. Trials of plugins that read `btc_` count in the one store-wide N.

- **Market diagnostic (Part A, information only, no gate).** Designer's call, so a BTC-timed alt long is visible
  for what it is: the side-matched random benchmark draws entries uniformly over flat bars and never shares a
  plugin's BTC timing. `benchmarks.btc_exposure(cfg, oos_portfolio, tf, a, b) -> dict` loads BTCUSDT's research
  candles at the run's timeframe and takes UTC-daily close-to-close returns over the OOS span. It returns the OOS
  portfolio's daily beta and correlation to them, and the annualised (√365) Sharpe of the hedged series r_p −
  β·r_BTC, β from the same span. `validate_strategy` records it as `btc_exposure` in the run summary, and
  `judge/diagnostics.py` puts it in the verdict as `diagnostics.market`, so the model reads it with the rest
  (research OOS only; never a holdout number). None for runs made before it.
- **Overlap and correlation** rules are unchanged (preflight uses market version 3). **Data identity:** unchanged
  today (X2). **Readiness:** X3, before any trial.
- **Exam.** `exam.window_end` takes the earliest end over the exam's symbols and `reference.SYMBOL`: candles at the
  plugin's timeframe and funding as today, and for the reference also its last metrics snapshot plus 5 minutes.
  `exam.window_data_problems` checks BTC's months at the plugin's timeframe with `EXCHANGE_SOURCES` (a
  ccxt-stitched month has no quote volume or taker flow), and every UTC day of the window for at least one BTC
  candle and at least one BTC metrics snapshot, with the note " (BTC reference)". That stops a stale
  `btc_oi_value` at the window's tail and over whole missing days. A gap of hours inside a day still carries the
  last snapshot forward, as `align_metrics` does for the coin's own `oi` in research and exams alike. `run_exam`
  builds the sources once with its token. In the stream, coin X's worker at step t holds BTC's rows up to t only,
  the rows BTC's own worker holds at that step, so the lockstep holds.
- **Survivor bias.** The reference (the top coin throughout) isn't biased; residual longs on surviving alts are.
  The side-matched random benchmark absorbs part of that; the Deflated Sharpe gate doesn't. Disclosed as today.

## Builder integration

**`perp_lab/proposer/context.py`.**
- `BAR_COLUMNS` gets the five rows above after `regime_adx` (after design 2's if those land first), and: "The
  `btc_` columns put BTC on every coin's bars: BTC's bar with the same open time at the same timeframe, which
  closes when the coin's does, so a plugin may use them on bar t as it uses the coin's own. On BTC's own bars they
  equal BTC's own columns: a rule about other coins against BTC should not trade BTC (check `ctx.symbol ==
  "BTCUSDT"` in `entry`). The four prices and two volumes are NaN where BTC's bar is missing or had no trades;
  nothing is filled forward. A coin's bars start at its listing, so a long BTC lookback warms up from there. The
  engine still trades one coin at a time: a position in a coin carries that coin's full move with BTC, so a signal
  built on the coin's move net of BTC must still predict the coin's own move to pay. `reference_frame` and
  `rolling_beta` in perp_lab/indicators.py work with these columns. A verdict's `diagnostics.market` gives the OOS
  record's beta and correlation to BTC and its Sharpe with BTC hedged out." Its dates are fixed text (Ruling R23).
- `PLUGIN_COLUMNS` picks up the eight names, so idea cards and the referee's "bar columns a plugin may read"
  accept them.
- `_piles`: "The synthetic bars' taker, open-interest and volume columns are random walks, their `btc_` columns a
  synthetic BTC that moves with the coin, and they have no `slippage` column. The lookahead check also runs each
  plugin as BTCUSDT, its `btc_` columns equal to its own."

The reply contract and prompts need no change. Dashboard: none; the Ideas form's column list is `PLUGIN_COLUMNS`.

**The record may block family E and parts of C.** The research prompt and the referee reject "a filter added to a
closed idea's entry", and if the volume-continuation ideas are exhausted (session notes; the record was not read),
they take no refinement. A human idea card, checked in code and not by the referee, is the route if the user wants
E. Designer's call: this spec seeds no cards.

## Paper and live path

Part A (research, checks, diagnostic, builder) ships first. Part B ships before any plugin that reads `btc_`
starts a paper run. Paper needs a passed exam (`db.approve_paper`), and no exam can run before about 2026-12-28.
Until Part B, `lab paper` refuses (exit 2), before it records the run, any plugin for which
`plugins.reads_columns(cfg, src, group)` is true for a group with `paper_ready` False, naming the group. Part B
sets the reference's `paper_ready` to True.

**Feed.** New `perp_lab/paper/reference.py`, `ReferenceFeed(cfg, exchange, tf, wait_s)`: one per paper run, shared
by every coin. New in `perp_lab/data/live.py`: `fetch_full_klines(exchange, symbol, tf, since_ms, limit)` (every
`CANDLE_SCHEMA` field, quote volume, trades and taker flow included, from Binance's raw klines endpoint through
ccxt's implicit API), `fetch_funding_history(exchange, symbol, since_ms)` (settled rates) and
`fetch_oi_history(exchange, symbol, since_ms)` (5-minute open-interest statistics). Config:
`paper.reference_wait_s: 30`.

**What coin X sees at bar t.** Bar t closes at B = open_time(t) + tf.
1. X's stream gets its own closed candle t at or after B (`live.watch_closed`). Before deciding, X calls
   `feed.bar(t)`. The first coin to ask makes the feed fetch BTC's klines from t by REST; every other coin awaits
   that same fetch. The feed accepts bar t only once the reply also lists bar t+1, as `watch_closed` treats a
   candle as closed, so an unfinished bar t is never used. It retries every 0.5 s and gives up at B +
   `reference_wait_s`.
2. If BTC's bar t came: X's row t carries it (NaN if it had no trades, X6), and X decides.
3. If not: X decides at the deadline with the six bar columns NaN on row t, the research rule for a missing BTC
   bar. Row t is never filled with any other bar. The feed logs `reference_late` (coin, t), and `state.json`
   counts them per coin, so the backtest-against-paper comparison can attribute the divergence.
4. Each step re-attaches the reference to the whole window it sends (paper sends the window every step,
   `worker.step(window=...)`), with the same `reference.attach` research uses. A BTC bar that came late is in the
   history from the next decision on; the decision made on NaN stands.
5. `btc_funding_last` and `btc_oi_value` are set by timestamps, not arrival: the last settlement with
   `funding_time <= close_time(t)`, the last snapshot with stamp + 5 min `<= close_time(t)`, fetched once per bar
   by the feed. If the snapshot the rule names isn't published yet, the latest earlier one is used and the miss
   logged.
6. BTC's own stream uses the same feed, with no special case.

The wait (normally under a second, at most 30 s) delays X's decision and so its fill; the backtest-against-paper
comparison measures that, as it measures the spread. Designer's call: 30 s, because a closed kline is normally on
REST within a second, 30 s covers retries, and past that the feed is impaired and waiting only pushes fills
further from the backtest's. Cost if wrong: too short gives more NaN decisions (counted); too long, later fills.

**Warm-up, and the short OI history.** `feed.warm(since_ms)` fetches BTC's full klines, funding and OI history over
the warm-up span (`paper.warmup_bars: 1000`: about 41 days at 1h, 166 at 4h). Binance's REST serves only about the
last 30 days of 5-minute OI (to confirm when Part B is built), so older warm-up rows have `btc_oi_value` NaN. A
plugin whose OI lookback reaches past 30 days sits flat on that signal until the paper run has been going long
enough, which at 4h is weeks to months, and paper diverges from the backtest meanwhile. Disclosed here and in the
paper README. The fix, if a promoted plugin needs it, is to warm up from data.binance.vision's daily metrics files
over the network (never the research loader); not built now.

## Changes to existing documents and code

Part A, one concern per commit, tests in each:
1. X7's inventory (`docs/research/reference-inventory/`), then `known_gaps`: first, before any trial reads `btc_`.
2. `perp_lab/data/columns.py` (new): `ColumnGroup`, `GROUPS`, `column_group_problems`, `identity_block`.
3. `perp_lab/data/store.py`: `funding_last_at`, `metrics_at` (refactor); `load_bars` gains `groups`, `sources`.
4. `perp_lab/data/reference.py` (new); the reference group registered in `GROUPS`.
5. `perp_lab/runs.py`: `data_identity` merges `columns.identity_block()` (empty today).
6. `perp_lab/plugins.py`: `names_group`, `reads_columns`.
7. `perp_lab/validate/walkforward.py` (`_sources`), `perp_lab/validate/benchmarks.py` (`buy_and_hold` passes
   `groups=False`; `btc_exposure`), `perp_lab/validate/pipeline.py` (X3 check; records `btc_exposure`),
   `perp_lab/judge/diagnostics.py` (`market`).
8. `perp_lab/validate/causality.py`: group fills in `with_moving_columns`, `as_reference_coin`, the "as BTC" variants.
9. `perp_lab/validate/synthetic_market.py`: as above, `MARKET_VERSION = 3`, docstring.
10. `perp_lab/judge/judge.py` (X3 before `register_trial`), `perp_lab/judge/exam.py` (`window_end`,
    `window_data_problems`, `run_exam`), `perp_lab/cli.py` (`lab propose` start runs X3; `lab paper` refusal).
11. `perp_lab/indicators.py`: `reference_frame`, `rolling_beta`. `perp_lab/proposer/context.py`: rows, paragraph,
    `_piles`.
12. `config/lab.yaml`: `column_groups: {reference: {known_gaps: []}}`. `tests/conftest.py`: `write_market` also
    writes funding and metrics files and manifests (BTCUSDT's metrics from its first fixture month), so
    `market_config` judges pass X3; fixtures without BTCUSDT that judge patch `column_group_problems`.
13. Docs: `README.md` (the store line; the exam paragraph: BTC's months and days are checked and the window ends where
    BTC's data does; readiness; `diagnostics.market`); the sandbox spec (amend the "Symbols" decision: a worker is
    sent one traded coin's frame, which may carry other coins' reference columns cut row by row with it and covered
    by the lookahead check, so no worker holds any series past its own frame's rows); the proposer spec ("Amended:
    BTC reference columns", as for OI).

Part B: `perp_lab/data/live.py` (three fetches), `perp_lab/paper/reference.py` (new), `perp_lab/paper/runner.py`
(`_warm` and `_stream` attach through the feed; `state.json` counts), `config/lab.yaml`
(`paper.reference_wait_s: 30`), the reference's `paper_ready = True`, README's paper section.

## Testing

Tests use temporary data dirs, never `data/`, `runs/`, `proposer/` or `lab.db`.

1. **Alignment.** On a hand-built pair (X, BTCUSDT): X's row t carries BTC's bar t exactly; a missing BTC bar and
   a zero-trade BTC bar each give NaN in the six bar columns on that row only, with the as-of two still set;
   BTCUSDT's own bars equal their own columns except on its zero-trade bar; a 1h X never picks up a 4h BTC value.
   OI boundary: a snapshot stamped close_time − 5 min + 1 ms is not on bar t, one stamped 1 ms earlier is. Funding
   boundary: a settlement at a bar's open belongs to that bar.
2. **Holdout.** BTC fixture data runs to 2026-10 with a planted absurd price at `holdout_start` and an absurd funding
   rate after it: a research `load_bars` of ETHUSDT shows neither; `end` past `holdout_start` without a token raises
   `HoldoutViolation`; with a token, BTC's holdout rows appear; a cached source holds nothing at or after its end.
3. **Tolerance and regressions.** With no BTCUSDT data, `load_bars` gives NaN `btc_` columns and one warning.
   `align_funding`, `align_metrics` and `with_moving_columns`' existing fills are byte-identical (pinned).
4. **Helpers.** `rolling_beta(x, x, n)` equals 1.0 exactly wherever defined, and a residual against itself is
   exactly 0; `volume_vs_same_hour(reference_frame(bars), 4, col="quote_volume")` runs and equals
   `volume_vs_same_hour` on a frame built by hand from BTC's columns.
5. **Lookahead.** Fixture plugins: `btc_close.shift(-1)` caught; `btc_oi_value.shift(-1)` caught; a full-sample
   `np.polyfit` beta caught; a peek through `ctx._a` gated on `ctx.symbol == "BTCUSDT"` caught by "as BTC" only;
   a plugin that raises when `close == btc_close` fails "as BTC" only; a peek on `btc_close` past `holdout_start`
   caught by the dated check; a plugin whose peek is gated on a grid value fails "<params> as BTC" when given
   those params; an honest plugin using `rolling_beta` passes; a plugin that never trades BTC passes.
6. **Synthetic data.** BTC returns correlate with the coin's at 0.6 to 0.8; every `btc_` column is non-constant;
   `btc_oi_value` is NaN on exactly the first 300 rows; changing the coin's rows after t leaves every `btc_` value
   up to t unchanged; `reference.SYMBOL` equals `config/lab.yaml`'s universe format for BTC.
7. **Every plugin column moves** (the generic R24 guard, design 2's columns included): each name in
   `context.PLUGIN_COLUMNS` is present and non-constant in `with_moving_columns(synthetic_bars())` and in every
   `market(tf)` frame at 15m, 1h and 4h.
8. **Market.** `MARKET_VERSION == 3`; `SYMBOLS[0] == "BTCUSDT"` with version 2's SYN1 prices; SYN2 to SYN4
   correlate with it in order; every frame's `btc_` columns equal BTCUSDT's resampled ones by open time.
9. **X2.** With every group at rule 1, `data_id` equals today's (pinned value); a group at rule 2 changes it.
10. **X3.** An incomplete BTCUSDT month, a missing funding manifest, or `absent` after the first real month each
    give one problem line; a `known_gaps` entry clears its month; `judge.judge` refuses before `register_trial` (N
    unchanged); `validate_strategy` and `lab propose` refuse too; no holdout month is read.
11. **X4.** Static: `"btc_close"`, `bars.btc_close`, `f"btc_{k}"` and `reference_frame(bars)` each count; a
    comment does not. Dynamic: a plugin that builds the name at run time from unrelated strings is caught; a
    plugin that never reads the group is not.
12. **Walk-forward and exam.** A chunk's worker receives the `btc_` columns; `_sources` is built once per process
    per timeframe; `window_end` stops at BTC's last candle, funding or metrics when BTC ends first and isn't an exam
    coin; `window_data_problems` names a ccxt-stitched BTC month and a day with no BTC metrics; for a causal plugin
    reading `btc_close`, streamed decisions equal batch decisions.
13. **Diagnostic and context.** A synthetic OOS series of 1.5 × BTC's daily return plus noise gives β near 1.5 and
    a hedged Sharpe near the noise's, in the verdict as `diagnostics.market`, read by no gate. The context holds the
    rows and paragraph; `PLUGIN_COLUMNS` is pinned with the eight names; the context stays under its cap.
14. **Paper.** Part A: `lab paper` exits 2 for a plugin calling `reference_frame(bars)`, before any run is
    recorded. Part B, with a fake exchange: X waits for BTC's bar t; a reply that lists bar t but not t+1 is not
    accepted; past the deadline row t is NaN with one `reference_late`; a late bar t is on row t at the next step;
    a funding settlement at B is not on bar t; BTCUSDT itself uses the feed.

## Risks and open questions

- **Next-open fills flatter fast lead-lag.** The engine fills at bar t+1's open; live, the decision waits for BTC
  and the order goes later. An edge that decays in seconds shows in the backtest and not in paper. Accepted: little
  such edge is expected at 1h and 4h, and the paper comparison is where it shows.
- **BTC-filtered longs as market timing.** C-type plugins can pass partly on BTC trend-following. No gate is
  added; `diagnostics.market` shows how much of a record is BTC.
- **Own-column gaps in paper (existing).** Paper today has no quote volume, trade count, taker flow, open interest
  or regime for the traded coin. Part B's full-kline fetch could fill three of them; that is its own issue.
- **Unverified API facts**, confirmed when Part B is built, before its tests: that ccxt's implicit raw-klines call
  returns the quote-volume, trades and taker fields, and that the 5-minute OI history matches the archive's metrics
  and covers about 30 days.
- **Answered review questions.** Missing BTC data in `load_bars`: tolerant, NaN and a warning (X3). Data identity:
  X2. The scanner: `plugins.reads_columns(cfg, src, group)` (X4).
- **Left to the builder (the model):** which families to try and at which timeframe, how to estimate β (window,
  log or simple returns), whether a rule skips BTC.

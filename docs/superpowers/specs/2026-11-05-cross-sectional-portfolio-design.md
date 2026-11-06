# The cross-sectional portfolio engine

Date: 2026-11-05. Status: draft for the lead, revised the same day after review (`review-3-portfolio`) under the lead's
rulings, recorded below as "Lead's call:". Designed now, built after designs 1 (cross-coin reference bars) and 2
(spot and basis). The user skipped the questions, so every other choice is the designer's, marked "Designer's call:"
with the reason and what it costs if wrong.

Scope: a second kind of plugin, the **portfolio plugin**. It sees every coin's bars at once and ranks the coins, and
the harness holds a long/short book of them. This spec covers its API, sandbox path, backtest engine, walk-forward,
benchmarks, gates, holdout exam, paper path, verdicts, store, the builder's view of it, and the dashboard. It adds no
data: it reads the bars `load_bars` builds, including the columns designs 1 and 2 add.

Bound by `2026-10-16-judge-design.md`, `2026-10-17-plugin-sandbox-design.md`, `2026-10-18-wider-data-design.md`,
`2026-10-19-proposer-design.md` and their amendments, except where "Changes to existing documents" says otherwise.

## Why

After 54 trials the builder concluded that single-coin bar rules are mined out (best: long continuation after a
strong 24h move on unusual volume, net Sharpe 0.7–1.33, Deflated Sharpe ≤ 0.89). Its research notes
(`docs/research/2026-11-01-ideas/README.md`, "Multi-symbol plugins") call the cross-section "the biggest unlock":
most of crypto's documented anomalies are relative, and today a plugin can't even name another coin.

What a relative book changes:
- **Breadth.** One ranking decision per rebalance covers 17 coins; a single-coin rule fires rarely per coin.
- **The market mostly drops out.** A dollar-neutral book has no dollar exposure to the 2020–26 drift (buy and hold:
  Sharpe 0.72 over the OOS span). A beta gate checks what is left.
- **The bar is the same.** At N = 54 the annualised OOS Sharpe needed for a Deflated Sharpe of 0.95 over the
  2,191-day OOS span is 1.61 (1.63 at 60, 1.71 at 100; `perp_lab.dsr.sharpe_bar`). A portfolio trial counts in the one
  store-wide N.

**Turnover costs** (`config/lab.yaml`): a taker fill pays 5 bp plus the coin's point-in-time slippage, 2.1–8.3 bp on
2026-06-30, so about 9 bp per unit of notional traded. Replacing the whole book (close 1.0 of gross, open 1.0) costs
about 18 bp of equity: about 65% a year if done daily, about 4% a year weekly with 40% of names replaced.

**Hypothesis families**, with plausibility of passing the bar after costs on 17 large, correlated, survivor-selected
coins. Citations come from the research notes and the designer's memory (see "Facts not verified").

| family | mechanism and evidence | build here | plausibility |
|---|---|---|---|
| Cross-sectional momentum | 1–4 week winners keep outperforming for weeks (Liu, Tsyvinski & Wu 2022, *JF*); strongest in broad universes and small coins. | score = trailing 7–28 day return, skip the last day; weekly or 3d; k = 3–4. ≈ 4%/yr costs. | Low to pass, moderate for signal before costs. Few names; momentum crashes at turns; survivors flatter the long leg. |
| Short-term reversal | 1–3 day losers bounce against winners (liquidity provision). | score = −(1–3 day return); daily or 3d; `buffer`. | Low: a daily book pays ≈ 65%/yr at full turnover, and survivors flatter "buy the losers" most of all. |
| Funding-rate carry | Long the lowest funding, short the highest: the spread plus a crowding premium (Schmeling, Schrimpf & Todorov, "Crypto carry", BIS). | score = −(mean `funding_last` over 3–14 days); 8h or 1d, aligned to the 00/08/16 UTC settlements. | Moderate, the best prior here, but a persistent ranking meets the timing null (gate 2b) head-on; see Risks. |
| Volume or OI growth | Attention: jumps in volume or OI precede brief outperformance, then reversal. | score = 1–7 day log change of `quote_volume` or `oi_value` against its 30-day norm. | Low. Sign unsettled; `oi` starts 2021-12, so the book is flat for the first 17 of 72 OOS months, diluting the Sharpe. |
| Beta / low volatility | Long low beta, short high beta, beta-neutral (betting against beta). | score = −trailing beta or vol; `neutral: beta`. | Low to moderate and suspect: 2022–25 alts bled against BTC, a dominance trend the beta gate doesn't remove. |
| Basis ranking (design 2) | Long the coins whose perp trades lowest against spot, short the highest; known in real time. | score = −`basis` averaged over hours to days. | Moderate to low, and likely a near-copy of carry (correlation ≥ 0.8 files it as a duplicate). |

## Goals

1. A plugin kind that sees all coins up to bar t and returns a ranking; the harness turns it into a long/short book
   from a fixed menu of shapes.
2. A trusted book engine with the lab's costs: per-coin fees and point-in-time slippage on every change of position,
   funding on every leg, isolated margin and liquidation per leg, fixed gross exposure.
3. Judging as strict as for single-coin plugins: the same folds, two nulls that differ from the plugin only in the
   ranking or only in its timing, the island test, minimum activity, the store-wide Deflated Sharpe, and breadth, beta
   and copy gates fixed here.
4. The same sandbox, lookahead, holdout and reproducibility guarantees.
5. The builder can propose portfolio plugins and learn from their verdicts.

## Non-goals

- New data. A point-in-time universe with dead coins is a separate spec, and a prerequisite for portfolio exams.
- Plugin-chosen weights, leverage or stops; long-only books; 15m portfolio plugins.
- Mixing kinds: a plugin is one kind, and so is a family.
- Building the paper path before a portfolio plugin passes an exam (designed here).

## Anti-goals (failure even if it works)

- A score at t that depends on any coin's bar after t, or on data at or after `holdout_start` outside `lab exam`.
- A book whose result is the market, one survivor, or a fixed coin tilt chosen with hindsight, passing as skill.
- A null that differs from the plugin in more than the thing it tests, so that beating it is easy.
- A gate, threshold, cost or benchmark rule changed after any portfolio trial has run.
- A survivor-universe result reported without saying what survivor selection can do to it, or examined on the holdout
  before dead coins are in the universe.
- A portfolio trial that doesn't count in N, or a single-coin run's `data_id` changed by this work.

## Decisions

| question | decision |
|---|---|
| What the plugin returns | Scores: one number per coin per bar; only their order and NaN-ness matter; the harness builds the book. Designer's call: a fixed shape lets the nulls match it exactly and caps exposure in code (ADR 0006). Cost if wrong: conviction sizing is inexpressible. |
| Book shapes | Top k long, bottom k short, k in 1..5; `equal` or `inverse_vol` weights; `dollar` or `beta` neutral; hold `buffer` 0..2; rebalance every 4h, 8h, 1d, 3d or 1w. A literal `book`, not gridded. Designer's call: a small menu covers the families above and stays out of per-fold selection. Cost if wrong: a refinement spent on changing k or cadence. |
| Gross | 1.0 of equity at every rebalance. Designer's call: Sharpe barely depends on gross; a knob adds only liquidation risk. |
| Margin | Isolated per leg; the wallet backs the book (leg margin = its notional ÷ gross, 1× at gross 1.0); liquidation per leg with the coin's bracket. Designer's call: what a book trader would post. Cost if wrong: fewer liquidations than 3× (a 1× short liquidates near +98% within one rebalance, a 3× one near +32%). |
| Engine | New `perp_lab/engine/book.py`, reusing `liq_price`, `EngineParams.from_config`, the bars' `slippage` and `funding_charge`, `daily_returns`, `summarize`. Designer's call: `run_backtest` is one coin driven by signals and stops; bending it would risk the judged single-coin path. |
| Timeframes | 1h and 4h. Designer's call: 15m panels are 4× the memory and no cadence needs them. |
| Minimum coins | A book is held only with ≥ 10 eligible coins; else flat. Designer's call: 11 coins trade from 2020-01/02, so books form from about 2020-03; 10 leaves room for k + buffer = 5 a side. |
| Nulls | Random rankings (turnover-matched, p = 0 included) and the plugin's own scores shifted in time; the plugin must beat the p95 of both. Lead's call (B1); see "The nulls". |
| Survivor bias | Treatment in "Survivor bias"; the point-in-time universe with dead coins and wider-data change 7 are a hard prerequisite for any portfolio exam. Lead's call (S3). |
| Trials | One (plugin version, timeframe) = one trial in the existing `trials` table, registered before plugin code sees real bars. Lead's call: one store-wide N. |
| How it is judged | `lab judge NAME --tf TF`, unchanged CLI and argv allowlist; the judge reads the kind from the source. Designer's call: the proposer's subprocess allowlist (`proposer/proc.py`) stays as it is. |
| Paper | Designed below, built when a portfolio plugin first passes an exam; until then `lab paper` refuses a portfolio run (exit 2). Designer's call: paper needs a passed exam. |

## Data

**No new data and no backfill.** The panel is assembled from `load_bars` per coin, so every guard holds:
`_resolve_end` (no research read at or after `holdout_start` without a token), `_drop_dead_tail`, `align_funding`, OI
with its 5-minute publish lag (`align_metrics`), regime known only after its 4h bar closes, slippage from volume before
the bar's day. Design 1's eight `btc_*` columns (on BTCUSDT's own rows equal to its own columns) and design 2's
`spot_close`, `spot_quote_volume`, `spot_taker_buy_quote_volume`, `spot_trades` and `basis` arrive by name with their
own alignment, lag and zero-trade rules; this design never depends on their internals. Lead's call (X1, X3): the
groups live in the shared registry `perp_lab/data/columns.py`, and `load_bars` stays tolerant (a missing group is NaN
columns and one warning), so a group's completeness is checked before a trial ("Walk-forward", step 0), not at load.

**Per-coin start** (first monthly 1h klines on data.binance.vision): BTC, ETH, XRP, LINK, ADA, LTC, BCH, XLM, ETC
2020-01; BNB, ATOM 2020-02; DOGE 2020-07; DOT 2020-08; SOL, AVAX 2020-09; FIL, AAVE 2020-10 (wider-data spec,
checked 2026-10-18; SOL, AAVE and DOGE re-checked 2026-11-05, the rest timed out). So 11 coins at the first train
window, 12 from 2020-07, 17 from 2020-10.

**The panel** (`perp_lab/data/panel.py`, new): `load_panel(cfg, symbols, tf, start=None, end=None, holdout=None) ->
Panel`, calling `load_bars` per coin with the same `end` and `holdout`, after `research_symbols`.
- Index: the sorted union of every coin's bar open times at `tf` (UTC). Columns: the run's symbols as `cfg.symbols`
  spells them ("BTCUSDT"), in config order, fixed for the run: a coin not yet listed is an all-NaN column.
- One wide frame per bar column (`panel["close"]` is time × symbol). No bar (before listing, a gap, after a dead
  tail) is NaN in every column. No forward fill.
- Carried: every column `load_bars` returns except `client.WITHHELD_COLUMNS` (below). `regime` is a wide object frame.
- The trusted engine takes its own arrays (`open`, `high`, `low`, `close`, `funding_charge`, `slippage`) from the
  parent's copy, never from the worker.

Memory: the 1h research panel is about 57,000 times × 17 coins × ~25 columns, about 200 MB of floats; 4h a quarter.
A sandbox worker has 3,072 MB.

## The plugin API

New module `perp_lab/portfolio.py`, importable by plugins (added to the worker's preload, the import list in
`perp_lab/strategy.py`'s docstring, and `proposer.assemble.ALLOWED_IMPORTS`); it imports only math, numpy and pandas.

`class Panel`, read-only: `panel[col] -> pd.DataFrame` (time × symbol, a fresh copy; NaN where unknown);
`panel.columns`, `panel.symbols` (tuples of str; coin names stay visible to plugins, Lead's call under B2), `panel.index`
(UTC bar open times), `panel.tf`.

`class Portfolio`:
- Literals read by the parent with `ast`, never by running the plugin: `params`, `grid`, `timeframes`, `book`, and
  module-level `PARENT`. No `session`.
- `__init__(self, **kw)` refuses unknown params; `hypothesis()` as `Strategy`.
- `prepare(self, panel: Panel) -> pd.DataFrame`: scores on exactly `panel.index` × `panel.symbols`, float64 (bool and
  int cast). Higher means more long; NaN means "not eligible now"; ±inf is refused.
- **Causality contract:** row t may depend only on panel rows ≤ t, of every coin.

`book` keys: `k` (int), `weight` (`"equal"` | `"inverse_vol"`), `neutral` (`"dollar"` | `"beta"`), `buffer` (int),
`rebalance` (`"4h"` | `"8h"` | `"1d"` | `"3d"` | `"1w"`); defaults equal, dollar, 0; `k` and `rebalance` required.
`check_book(book, tf, cfg) -> list[str]`: 1 ≤ k, k + buffer ≤ ⌊min_coins / 2⌋ = 5 (the sides never overlap),
rebalance a whole multiple of tf and on the config menu, no other keys.

**Metadata** (`perp_lab/plugins.py`): `PluginMeta` gains `kind` (`"strategy"` | `"portfolio"`) and `book` (`{}` for a
strategy). `parse_meta` accepts exactly one subclass of `Strategy` or of `Portfolio`, never both, reads `book` for a
portfolio and refuses it (and `session`) elsewhere. The judge's precheck (and so `lab preflight`) refuses a portfolio
timeframe not in `portfolio.timeframes` and a `PARENT` of the other kind. Designer's call: a family is one mechanism
under one set of gates.

Example for the context: `class XsMom(Portfolio)`, `params = {"lookback_d": 14}`, `grid = {"lookback_d": [7, 14,
28]}`, `timeframes = ["1h"]`, `book = {"k": 3, "rebalance": "1d"}`, `prepare` returning
`panel["close"].pct_change(24 * lookback_d)`.

## The sandbox

A walk-forward job is one (params) on one panel. The worker computes scores and never runs the engine; the trusted
parent runs the book engine on them, so there is no decision replay or fingerprint: the scores are the whole output.

New worker ops (`worker.py`, `session.py`) and parent methods (`client.py`):
- `panel`: header `{"tf", "symbols", "n"}`, one Arrow section (the panel in long form). The worker builds the `Panel`
  in the given symbol order on the union index. `Worker.panel(panel)`; the reply's `n` must match.
- `scores`: header `{"params"}`; reply one `<f8` section of n × m values, row-major, and `{"shape": [n, m]}`.
  `Worker.scores(params) -> np.ndarray`. A wrong shape, another dtype or any ±inf is `PluginError("features")`;
  the reply is within `max_output_mb` (64; 1h scores are about 8 MB).
- `panel_stream_open(params, warmup_n)` and `panel_step(mode, table)`: the exam's and paper's streamed form; a step's
  reply is one `<f8` row of m scores.
- `load`'s reply adds `book` and `kind`; `check_meta` compares them with the source.
- `client.PanelJob`: one worker, sent one panel once, several `scores` calls, retried once on `WorkerLost`;
  `client.fresh_scores(cfg, src, panel, params)`: one call in a new worker sent only that panel.

**Withheld columns** (M5, adopted): one list, `client.WITHHELD_COLUMNS = ("slippage",)`, where bars leave for a
worker. The panel path drops them before sending. The strategy path still sends them (the worker's own engine run
needs `slippage` for the fingerprint the replay checks); there the list is enforced by the check, and a test asserts
that the synthetic bars and synthetic panels lack every listed column, so a plugin reading one fails the lookahead
check. A later design that adds a column plugins must not read adds it to this list.

**Isolation rule.** "A worker is only ever sent one symbol's bars" becomes: a strategy worker is sent one symbol's
bars; a portfolio worker is sent one panel, all coins cut at the same time, never a row later than the one it decides
on. The old rule kept a worker holding BTC's whole series from reading BTC's next bar while deciding ETH; a panel cut
by time has no next bar of any coin, and the panel check cuts all coins at once. Concurrent walk-forward workers hold
the same research panel, so none holds a later bar than another (R6 as today).

**CPU limits** (builder question 1). Lead's call: scale, and never let an exam die after its read.
- A panel `scores` request (and a `panel_stream_open` warm-up) gets `task_cpu_s × n_coins` CPU (900 × 17 = 15,300 s)
  and that × `wall_factor` of wall clock: one panel `prepare` does the work of n coins' prepares.
- A panel worker's lifetime backstop (`--cpu-s`) is 2 × the sum of the request limits planned for it, never less
  than today's 2 × `exam_cpu_hours`.
- A panel stream step gets `stream_step_s × n_coins` (510 s at 17) of CPU and wall clock.
- `exam_cpu_hours` (6) is per panel exam, not per coin: the cost probe's estimate for the whole exam must be within
  it. See "Holdout exam" for the step probe.
- `cpu_limit` stays transient on the first two judge attempts and a kill (counted in N) on the third, as today.

## The panel lookahead check

`perp_lab/validate/causality.py` gains `panel_causality_failures(cfg, src, panel=None, *, cuts=None,
require_book=True, params=None) -> list[str]`, shaped like `causality_failures`: variants are the defaults, each grid
key at its first and last value, and `params` if given; for each cut and the full panel, each variant runs in a fresh
worker sent only `panel[:k]`, in waves of increasing length; each cut's scores must equal the full run's first k rows
(rtol 1e-9, NaN where NaN). `require_book`: with default params at least one decision row must hold a full book,
otherwise `NO_BOOK` (the counterpart of `NO_SIGNALS`).

**The synthetic panel** (`perp_lab/validate/synthetic_panel.py`, new). Lead's call (B2): it looks like the real one
in every way a plugin can name.
- **Symbols:** exactly `cfg.symbols` ("BTCUSDT", …), the same count and config order, as `panel.symbols` has on real
  bars. A plugin that reads `panel["close"]["BTCUSDT"]` runs; a peek keyed on a name or on the count is exercised.
- **Listings:** each coin's first synthetic bar falls on the 15th of its real first month (`LISTING_MONTHS`, a table
  in the module with this spec's months, so the check reads nothing under `data/`); a coin missing from the table
  starts at the span's start. One coin has a 24-bar gap.
- **Columns:** each coin is `synthetic_bars(seed=10 + i, ...)` through `with_moving_columns(seed=200 + i)`, so every
  column that moves in the strategy check moves here. Each column group's columns come from that group's own
  generator (`np.random.default_rng((seed, synthetic_id))`) and helper, as the registry defines them (Lead's call, X5:
  groups don't couple and existing columns don't change). Cross-coin identities of the real panel hold too: each
  `btc_*` column of every coin equals BTCUSDT's own column at the same time, built with the reference group's helper,
  so a plugin can't tell real from synthetic by that identity.
- **Span** (M4 and M6, adopted): one dated panel at the plugin's timeframe from `history_start` (2020-01-01) to the
  dated end the judge uses today (the later of today and `holdout_start`, plus 30 days, floored to 00:00 UTC),
  about 61,000 rows at 1h. Cuts every 182 days from `history_start` + 182 days, then the holdout schedule of
  `dated_lookahead_failures` (from 45 days before `holdout_start`, every 30 days), then the end. So there are cuts
  long after every warm-up (beta books form about 90 days in), and a date-keyed peek anywhere in 2020–2026 meets a
  cut. Independent coins have a beta of about 1 to their equal-weight mean, so beta books form.
- `panel_market(tf)`: a fixed 180-day panel from 2023-01-01 with the same symbols, for preflight's overlap
  (`PANEL_MARKET_VERSION = 1`).

The judge's `lookahead_failures` and `dated_lookahead_failures` dispatch on kind; for a portfolio the single dated
panel above replaces both. The exam repeats the check over its real window before the read, as today. The strategy
check's own gap (default bars dated 2023, dated check over the holdout only, so a peek keyed to 2020–22 escapes) is
inherited and not fixed here; it is flagged for the lead.

## The book engine

`perp_lab/engine/book.py`:
- `BookParams.from_config(cfg, symbols, **over)`: taker, maker, slippage floor, `slippage_scale`, per-coin brackets
  (via `EngineParams.from_config`), gross, min_coins, `min_listed_days` (30), `vol_days` (30), `beta_days` (90),
  `beta_floor` (0.1).
- `schedule(index, tf, rebalance, phase_h=0) -> np.ndarray[int]`: decision rows, at the close of the bar whose next
  open is a multiple of the period from 1970-01-05 00:00 UTC (a Monday), plus `phase_h`. Daily books fill at 00:00
  UTC; 8h ones at 00, 08 and 16 UTC, the funding settlements.
- `target_weights(scores_row, eligible, prev_long, prev_short, book, vol, beta, gross, tie_seed) -> np.ndarray`.
- `run_book(arrays, scores, book, bp, *, start_i=0, end_i=None, decisions=None, held=None, floor_beta=False) ->
  BookResult`; `held` forces the flat set (the nulls), `floor_beta` clips leg betas at the floor (the nulls).
- `BookResult`: `equity`, `time` (per bar close), `held` (per decision row), `legs`, `rebalances` (eligible count,
  entries, one-way turnover, costs), `book_rows` (weights at each bar open, for fold switches and overlap), and
  `contrib`: per UTC day, per coin and side, its net P&L (price, funding, fees, slippage) as a fraction of the book's
  equity at the day's start. Summed over coins and sides it is the day's return.

**Eligibility at decision row t:** a bar at t; a finite score; at least `min_listed_days` of bars (M6: not for the
slippage cap, which `trailing_adv` lifts after a coin's first day, but because a coin's first month is thin and its
trailing windows short-sampled); for `inverse_vol` a trailing vol (daily log-return std over the 30 complete UTC days
before t's day, ≥ 20 valid); for `beta` a trailing beta (slope on the equal-weight mean of the listed coins' daily
price returns, 90 complete days, ≥ 60 valid). Fewer than `min_coins` eligible: flat.

**Selection.** Eligible coins by score, descending. Ties: a permutation drawn per decision row from a seed built from
the plugin's hash and the row's open time, never by name. Lead's call (B1): constant or tied scores give a random
order that changes every rebalance, not an alphabetical tilt, and the same bytes give the same book in the
walk-forward, the exam and paper. With `buffer` b, a coin long at the previous rebalance stays long while eligible
and ranked within the top k + b; the remaining long slots fill from the top. Shorts mirror it from the bottom.
Designer's call: the buffer is the standard turnover cut, in the engine, so the nulls get it too.

**Weights** (Σ|w| = gross): `equal` splits each leg's gross equally among its k coins; `inverse_vol` ∝ 1/vol within a
leg. `dollar`: each leg gross / 2. `beta`: leg grosses a + b = gross with a·β_long = b·β_short (weighted mean leg
betas); a beta book is therefore net long or short in dollars whenever a ≠ b. If a leg's beta is under `beta_floor`
the plugin's book is flat at that row (the nulls clip it instead, `floor_beta`).

**Bar sequence**, for each held leg at each bar t where its coin has a bar:
1. Funding settled in t is charged at t's open to the leg held into it (side × qty × open × `funding_charge`), from
   the wallet and the leg's margin, its liquidation price recomputed: `run_backtest` step 1.
2. Gap: an open beyond the liquidation price liquidates the leg there.
3. Rebalance fill, on the bar after a decision row: every leg is marked to t's open (P&L to the wallet, entry := open);
   equity E = wallet; each coin trades from its notional to w_i × E. Fees taker × |Δnotional|; slippage the bar's
   `slippage` × `slippage_scale` × |Δnotional|. Only the change trades. Each leg's margin is reset to |notional| ÷
   gross and its liquidation price recomputed with `liq_price` and the coin's bracket.
4. Intrabar: low ≤ liquidation price (long) or high ≥ it (short) liquidates the leg there; it loses its margin, pays
   no fee, and stays flat until the next rebalance.
5. Close: mark to market; a coin with no bar is marked at its last close.

Edge cases: a held coin with no bar at a decision row is closed then at its last close, with taker and slippage
(`no_bar`; M3: causal, and it is how a dead coin leaves the book in the point-in-time follow-up). A coin with a bar at
the decision row but none at the fill trades at its next bar's open. The window's last bar closes every leg at its
close with taker and slippage (`eow`). No stops or targets. Designer's call: per-leg stops would make the shape depend
on paths the nulls don't share.

**Legs**, the trade log, with the trade-log columns where the meaning matches so `overlap.positions` and the
diagnostics read them: `symbol`, `tf`, `side`, `signal_time`, `entry_time`, `entry_price`, `exit_time`, `exit_price`,
`exit_reason` (`rebalance`, `liq`, `eow`, `no_bar`), `notional`, `traded_notional`, `weight`, `fees`, `slippage`,
`funding`, `pnl_gross`, `pnl`, `equity_before`, `pnl_pct`, `bars_held`, `regime`, `r_multiple` (NaN), `fold`,
`combo`. Builder question 4, Designer's call: `notional` is the leg's mean absolute notional over its bar closes, so
`per_trade_bp` is P&L per unit of capital held; `traded_notional` (entry, resizes and exit) carries the cost view.

Performance target: one book run over the 1h research panel in ≤ 2 s (positions are constant between fills).

## Walk-forward and selection

`perp_lab/validate/portfolio.py`, `walkforward_portfolio(cfg, src, tf, symbols=None, workers=None, stderr_dir=None)
-> PortfolioWF` (as `WFResult`, plus scores per combo, legs, rebalances, contributions, `market`):
0. **Readiness.** Lead's call (X3): before `register_trial`, `validate_portfolio` calls
   `column_group_problems(cfg, symbols, tf)` with every panel coin, and refuses on any problem: nothing recorded, no
   trial. A panel reads every coin, so every coin's group data must be complete before plugin code sees real bars.
   The judge, `lab walkforward`, verdict refreshes and parent runs all pass through it.
1. The research panel at `tf`, cached once per pool process.
2. One `scores` call per grid combo over the whole research span, combos in parallel up to `client.max_workers`; then
   one continuous engine run per combo.
3. Folds: `make_folds(history_start, holdout_start, 6, 2)`, the same 36.
4. Selection per fold: the best **Sharpe of the book's daily returns** over the train window, not the mean per-coin
   Sharpe. A combo scores −inf unless in the train window the book was held on ≥ 50% of days (`min_held_share`)
   and had ≥ ⌈2.5 × coins listed⌉ leg entries (`walkforward.min_train_trades_per_coin`). Designer's call: the entry
   floor rejects a book that never changes, which has no ranking to select.
5. OOS: the selected combo's daily returns on each test window, stitched; legs with `signal_time` in the window; and
   (S1, adopted) its per-coin, per-side `contrib` on the window, stitched the same way. Gate 6 and the survivor
   diagnostic read the stitched contributions, so numerator and denominator are the same OOS record.
6. **Fold switch** (builder question 3, Designer's call): at the first bar of a test window whose selected combo
   differs from the previous window's, whether or not that bar is a decision row, the book moves from the outgoing
   combo's holdings to the incoming one's, both as drifted weights at that bar's open in their continuous runs:
   Σ|w_in − w_out| × E × (taker + the coin's slippage) is charged to that day's return, in a `switch` contribution
   column (no coin). The strategy walk-forward charges nothing for this; a book switch is a large trade.
7. The "position carried into a test window" caveat of today's walk-forward applies unchanged.

`MARKET`: the equal-weight 1× long portfolio with funding paid (`benchmarks.buy_and_hold`'s daily series).

## The nulls

Both run in the trusted parent on the engine alone (no worker), each seed a continuous run over the OOS span from a
flat book, with the plugin's book literal, gross, schedule, costs, funding and margin. Both copy the plugin's flat set
exactly, whatever caused it (S5, adopted): `held` is the plugin's held/flat flag at each OOS decision row, and a beta
book clips leg betas at the floor rather than going flat on its own. Seeds come from the run id (`gates.seed_from`), so
a judgement is reproducible. Seeds: `validation.random_seeds` (200); percentile `random_percentile` (95).

**2a. Random rankings** (`random_ranking`). At each OOS decision row the eligible set is exactly the plugin's (its
selected combo's finite scores there, and the engine's rules). Each eligible coin keeps its previous random score
with probability 1 − p and redraws U(0,1) with probability p (a newly eligible coin draws). p ∈ {0, 0.02, …, 1.00}
(Lead's call: p = 0, a random static tilt, is on the grid), the one whose random books' mean leg-replacement rate
(entries per rebalance ÷ 2k) is nearest the plugin's OOS rate, estimated on selections alone (no prices) with 20 seeds
per p; ties to the lower p.

**2b. Timing null** (`random_shift`). Lead's call (B1): a book tilted toward coins that did well over the span, chosen
with hindsight, sits in the top tail of random tilts; shifting the plugin's own scores in time keeps each coin's
occupancy, the turnover and the shape, and breaks only the alignment with returns.
- S is the stitched OOS score panel at the D OOS decision rows (each fold's selected combo on its test window).
- W is the largest number of decision rows in any of the 36 test windows.
- Offsets: 200 distinct integers drawn uniformly without replacement from {W, W + 1, …, D − W}; if that set has fewer
  than 200, all of them, and the count is reported (at 1w, D ≈ 313 and W = 9: 296 exist).
- Copy j: S_j[t] = S[(t − o_j) mod D] for every decision row t, all coins shifted together.
- After the shift, a coin is eligible at t only if S_j[t] is finite for it and it is eligible at t under the engine's
  rules; the copy is flat where the plugin is flat; where the plugin holds but fewer than `min_coins` are eligible
  the copy is flat too, and the share of such rows is reported.

**Before costs**: both nulls and the plugin's selected combos re-run with taker, maker and slippage at zero, funding
kept: the verdict's `before_costs`, information only.

What the nulls don't test: when the book is held (both copy the flat set). The Deflated Sharpe judges that.

## Gates

Fixed now, before any portfolio trial. New thresholds are designer's calls argued below except where marked as the
lead's. Run status (`runs.verdict_portfolio`, `noise` | `candidate`, as `runs.verdict`) holds gates 1–4; the judge
holds 5–9, so their thresholds stay out of `data_id` as `judge.dsr_min` does.

| # | gate | threshold |
|---|---|---|
| 1 | OOS net Sharpe | > 0 |
| 2a | beats random rankings | net Sharpe > p95 of 200 turnover-matched random books |
| 2b | beats its own shifted scores | net Sharpe > p95 of 200 time-shifted copies |
| 3 | not a narrow island | each numeric param of the modal combo at ±20%, k ± 1 (within 1..5 − buffer), and the rebalance phase shifted by half a period: none may cut the OOS Sharpe by > 50% (`island_drop`) or flip its sign, when the base is > 0. ±10% reported. |
| 4 | minimum activity | leg entries ≥ ⌈3.75 × coins in the OOS⌉ (64 at 17), and the book held on ≥ 50% of OOS days |
| 5 | Deflated Sharpe | ≥ 0.95, N store-wide (`db.trial_stats`), spread 1/(T−1), on the book's OOS daily returns |
| 6 | breadth | from the stitched contributions: the best coin's net contribution ≤ 50% of the book's (book total ≤ 0 fails); ≥ 50% of the 36 folds positive; the OOS Sharpe with the best fold's days removed > 0 |
| 7 | not a copy of a promotion | |corr| < 0.8 (`proposer.corr_max`, ≥ 30 shared days) with the walk-forward run of every earlier judgement with decision `promote`, except withdrawn ones, outside its family |
| 8 | market beta | β̂ (OLS of OOS daily book returns on `MARKET`): |β̂| ≤ 0.3, and the Deflated Sharpe of r − β̂·m (same N) ≥ 0.95 |
| 9 | parent | a child of a passing parent beats it in ≥ 95% of paired block-bootstrap resamples (unchanged) |

A portfolio parent "passed" when its run is a candidate and gates 5, 6 and 8 pass under today's thresholds.

Why:
- **2b.** Lead's call (B1); a near-static tilt fails it by design (Risks).
- **3.** k and the phase reuse the base scores (engine time only). The phase variant catches a book that works at one
  hour of the day; for 4h books on 4h bars it is n/a.
- **4.** The per-coin count a strategy needs, counted as entries.
- **6.** Lead's call (S2): the earlier top-fold share cap was calibrated on i.i.d. normals and fails a real Sharpe-1.6
  edge 22% of the time when fold volatility varies as crypto's does; dropping the best fold and keeping a positive
  Sharpe still fails a book carried by one crash or one alt season. The reviewer's simulation found the other two
  rarely bind at the DSR bar (folds positive < 50% in < 0.3% of real edges, top coin > 50% in < 1% for a broad edge),
  so they don't misfire, and they still fail one-coin or one-regime books; kept.
- **7.** Lead's call (M1): any promotion except a withdrawn one, so blocked promotions (budget spent, family
  examined, superseded) count. Overlap otherwise stays a diagnostic and a record rule, as for strategies. Narrow on
  purpose: a neutral book beating a killed directional plugin may be the improvement.
- **8.** Dollar-neutral is not market-neutral (long alts against short BTC has positive beta), and a beta book is net
  long or short in dollars. Random books have zero expected beta. A book's Sharpe from the market alone is
  β × (market vol ÷ book vol) × 0.72: at β = 0.2 and a market three times as volatile as the book, about 0.4; hence the
  hedged series must clear the bar too. The in-sample β̂ flatters the hedged series slightly.

## Survivor bias

**The threat.** The 17 coins were chosen on 2026-10-18 as those trading in every sampled month 2021-01 to 2026-06
and top 50 by volume in at least 5 of 7 samples. Every coin in the research span is known to survive and stay large
until mid-2026. Coins that died (LUNA, FTT, SRM, WAVES) or whose contracts ended (EOSUSDT, MATICUSDT) are absent; a
coin that rose from volume rank 25 to 3 (SOL) is in. A cross-sectional book chooses among coins, so selection on their
future is selection on what it ranks.

**Direction, per family** (not one sign): buying losers (reversal, low funding) is flattered most, since every loser
here recovered enough to stay large. Momentum: the long leg is flattered (crashed winners absent, risers present), the
short leg hurt (dead losers would have paid); net likely flattering. Low beta: the short leg misses alts that died;
biased against it. Attention and carry: crowded longs flattered (faded hype coins absent). The market level: the
survivors' average return beats the investable universe's.

**What results here can show:** whether a ranking orders these 17 coins' future relative returns better than random
orderings and better than its own scores out of time, after costs, 2020-07 to 2026-06. **What they cannot show:**
that it would have worked on the universe a trader could have chosen then; how much comes from absent coins; the size
of any edge. A pass on this universe is a screen, not a finding.

**Treatment:**
1. **Neutral books.** Dollar books have no net dollar exposure. Beta books do (a ≠ b): long low-beta, short high-beta
   is net long dollars and harvests some of the survivors' drift; gate 8's hedged Deflated Sharpe is what removes
   that market exposure. Designer's call; corrected under the lead's S3 ruling.
2. **Gates 2b, 6 and 8.** A hindsight tilt, one survivor's run or the market can't carry a pass.
3. **Survivor diagnostic**, every portfolio verdict and report (M2, adopted, rule fixed): net contribution, from the
   stitched contributions, of the coins whose first research bar is on or after the OOS start (2020-07-01; today
   DOGEUSDT, DOTUSDT, SOLUSDT, AVAXUSDT, FILUSDT, AAVEUSDT), and of the long and short legs separately. Information,
   not a gate.
4. **Disclosure.** The universe rule plus "Portfolio results on a survivor universe: a screen, not a finding" in every
   portfolio report, verdict (`survivor_note`), the dashboard and the builder's context.
5. **No haircut.** Without dead-coin data no size is defensible; a guessed one would be a tuned rule.
6. **The exam is not a survivor test.** With a walk-forward record at Sharpe 1.7 and a 180-day exam,
   `gates.holdout_consistent` passes a book whose true holdout Sharpe is 0 about 50% of the time, and one at −0.5
   about 36%: its p5 floor sits near −0.5 standard deviations of a 180-day return, so Sharpe > 0 is the only real test.
   A survivor artefact would pass half the time and spend one of 10 reads.
7. **Hard prerequisite.** Lead's call (S3): no portfolio read is minted until (a) a point-in-time universe with dead
   coins and (b) wider-data change 7 (the exam with coins that stop trading) both exist. `attempt_exam` stops before
   the window step and records `pending` / `universe_blocked`: no read, no attempt. Lead's ruling says "blocked"; it is
   recorded as a pending code because a `blocked` status is final in today's code and this one must lift. The gate is
   `exam.PORTFOLIO_EXAM_READY = False`, pinned by a test and set true only by the PR that lands both. Judging
   continues. No portfolio exam could run before about 2026-12-28 anyway. The follow-up spec decides whether a
   promotion is re-measured on the new universe before its read (a re-run of judged bytes is no new trial).
   Its starting point: each month, the top N USDT perpetuals by trailing 30-day quote volume among all listed then;
   a coin that stops trading leaves through `no_bar`; a migrated contract (MATIC to POL) is one coin. The archive
   keeps them, checked 2026-11-05: monthly 1h klines for LUNAUSDT 2021-01 to 2022-05, SRMUSDT 2020-09 to 2024-05,
   EOSUSDT 2020-01 to 2025-05, MATICUSDT 2020-10 to 2024-09, FTTUSDT 2022-04 and WAVESUSDT 2020-08 to 2026-10 (flat
   zero-trade candles after delisting), and LUNAUSDT funding from 2021-01.

## Holdout exam

`perp_lab/judge/exam.py` gains `run_portfolio_exam(cfg, src, tf, symbols, params, token, end, stderr_dir=None) ->
ExamResult` and `portfolio_cost_hours(...)`; `attempt_exam` dispatches on kind, first applies treatment item 7, then
keeps every step and lock of today's (plugin unchanged, window end, `window_data_problems` per coin, dated lookahead
over the window, cost probe, sandbox check, mint, exam). Lead's call (X4): the window's data check covers, for every
panel coin, each column group with `exam_required: always` and each `if_read` group for which
`reads_columns(cfg, src, group)` is true. For a portfolio, `reads_columns`'s dynamic test runs `fresh_scores` on the
synthetic panel with only that group's stream drawn from two seeds, and compares the books (the long and short sets
at each decision row, from the engine's selection), not the scores; its static test reads the source as for a
strategy.
- The panel is loaded with the token (`load_panel(..., end=end, holdout=token)`) in the judge's process.
- **Streamed, one worker:** sent the research rows and run in batch (`panel_stream_open`: warm-up scores, so the book
  holds at `holdout_start` what it would have); then one `panel_step` per decision row (append every row since the
  last step, `prepare` on all held, return row t's scores). The worker never holds a row after the one it decides on.
  For a causal plugin this equals the batch scores (a test pins it).
- One worker holds every coin, so no lockstep; exams stay one at a time. A lost worker restarts once and must repeat
  every score row already returned (`mismatch` otherwise). After it closes its mapped files are evicted (R7).
- Frozen params: the walk-forward's modal combo; the book literal is the source's. Kept: daily returns and legs with
  `signal_time` in [holdout_start, end).
- **Gate:** `gates.holdout_consistent`, trades = leg entries: ≥ max(10, ⌈1.25 × coins⌉) (22 at 17), Sharpe > 0, return
  ≥ the walk-forward's p5 for that length.
- **Cost probe**, all before the mint (no read on a refusal): the median of 3 `prepare` timings on the research panel;
  `too_slow` if it × the window's decision count exceeds `exam_cpu_hours` (per panel), or if it exceeds a quarter of
  the panel step limit (127 s at 17 coins). Designer's call: 4× headroom on each step, and the lifetime backstop at
  twice the whole budget, so an honest plugin can't die of a limit after its read.

## Paper and live

Built when the first portfolio plugin passes an exam; until then `lab paper` refuses a portfolio run (exit 2,
"portfolio paper is not built yet"). The design (`perp_lab/paper/portfolio.py`, `PortfolioPaperRunner`):
- **Groups without a live feed:** Lead's call (X4): a plugin for which `reads_columns(cfg, src, group)` is true for a group
  whose live feed isn't built is refused, before the live-shape check.
- **One worker** holds the live panel, so the per-coin worker cap `lab paper` enforces today doesn't bind.
  `lab paper --symbols` is refused for a portfolio run (S4): it would trade another cross-section.
- **Warm-up:** `portfolio.paper_warmup_days` (120) of closed candles per coin from REST (`fetch_recent`), never from
  the research loader.
- **Bars:** `watch_closed` per coin; a panel row is complete when every coin's bar t has closed, or 60 s after t's
  close. Live columns: OHLCV from ccxt, funding from observed settlements (`_with_funding`); `quote_volume`, `trades`,
  taker flow and `oi` NaN; design 1 and 2 columns follow those designs' paper rules.
- **Live-shape check** at `lab paper` start (S4, adopted): on the synthetic panel, at 20 decision rows spread over its
  last year, the plugin's scores on the full panel must equal (rtol 1e-9, NaN where NaN) its scores on a live-shaped
  panel: only the 120 days ending at that row, live-missing columns NaN. Any difference refuses paper, naming the
  rows: paper would run a different ranking from the one judged.
- **Fills:** at each decision row, one `panel_step`, targets from `target_weights`; each coin's change fills on the
  first quote after the decision (ask to buy, bid to sell; the spread replaces slippage), taker fee; one
  `watch_bids_asks` subscription for all coins. Funding per leg from the predicted rate before each settlement, as
  `PaperBook.on_funding`; liquidation per leg on live mid.
- **Log:** legs plus `source="paper"`; `paper.min_paper_days` and the regime-change rule unchanged.

## Verdicts, runs, store and reproducibility

- **Runs.** A portfolio walk-forward writes `summary.json`, `oos_daily.parquet` (`PORTFOLIO`; `MARKET`),
  `oos_trades.parquet` (legs), `oos_contrib.parquet` (stitched per coin and side, plus `switch`), `grid`,
  `sensitivity`, `random_sharpes` (both nulls, with a `null` column) and `rebalances.parquet`. No Monte Carlo
  trade-order shuffle (legs overlap; reported n/a).
- **summary.json keys** (builder question 2, Designer's call: existing readers keep their keys):
  - `kind: "portfolio"`, `book`; `metrics` with `trades` = leg entries; `min_oos_trades` = the entry minimum.
  - `random_entry`, for `diagnostics._gates`, piles and the dashboard: `seeds`, `percentile`,
    `random_sharpe_mean` (the rankings'), `random_sharpe_p95` = the higher of the two nulls' p95s,
    `strategy_pctile_vs_random` = the lower of the two percentiles, `beats_random` = both beaten, `long_share` null.
  - `before_costs`: the same shape, gross, again the higher p95.
  - `random_ranking` and `random_shift`: each null in full (p or offsets, mean, p95, percentile, flat-row share).
  - `judge._todays_rules` dispatches on kind: a portfolio run qualifies when `random_ranking`, `random_shift` and
    `before_costs` are present (not `long_share`), so `refresh_verdict` doesn't re-run every portfolio.
- **Store.** `runs.kind` (String(16), NULL = strategy) through `_ADDED_COLUMNS`; `record_run` writes it. `trials`,
  `trial_stats`, `latest_wf_run` and judgements unchanged: a plugin's hash fixes its kind.
- **data_id** (builder question 5). Lead's call: every `portfolio:` key that can change a stored number, none that
  can't. `data_identity(cfg, symbols, kind="strategy")` adds, for portfolio runs only, `"portfolio": {min_coins,
  gross, min_listed_days, vol_days, beta_days, beta_floor, min_held_share}` and `"book_engine": BOOK_ENGINE_VERSION`
  (the engine's constants, such as the 20 and 60 valid-day minimums and the tie rule, are covered by the version).
  Left out: `timeframes` and `rebalance` (menus: they refuse plugins, never change a number), `paper_warmup_days`,
  `beta_max` and `breadth` (judge-time thresholds, like `judge.dsr_min`). Every strategy run's `data_id` is unchanged
  (a test pins an existing identity's hash), so no parent re-runs because of this work. As for strategies today,
  `walkforward` and `validation` are not in the identity; that gap is inherited. Lead's call (X2): the shared
  column-group rule applies to portfolio runs as to strategies, `{"columns": {group: rule}}` only for groups whose
  `rule` > 1 (none today), so a group's later rule change moves every portfolio `data_id` too.
- **Verdict schema 3** (`judge.VERDICT_SCHEMA`): a top-level `kind` on every verdict. Portfolio `diagnostics` keep
  their keys, computed from legs and contributions (`gates` from the keys above; `sides`, `exits`, `regimes`,
  `breadth`, `per_trade_bp`, `selection`, `overlap`), plus `portfolio`: the book literal; mean one-way turnover per
  rebalance; cost drag (bp a year); held share; entries; both nulls' summaries; `beta` {β̂, hedged Sharpe, hedged
  DSR, ok}; `breadth` {top coin share, folds positive, Sharpe without the best fold, ok}; long- and short-leg net and
  gross (bp a year); the survivor diagnostic; `survivor_note`. Aggregates only, never series.
- **Report.** `render_portfolio_report`: verdict, OOS table, nulls, before costs, market, beta, breadth, survivor
  block, per-coin contributions, per-fold selection.

## The builder

The proposer may propose either kind in one session under the same caps (`trials_per_day`, `trials_total`,
`proposals_per_idea`); each portfolio trial raises N like any other.
- **Context** (`context.py`): a section "Portfolio plugins": the source of `perp_lab/portfolio.py`; the panel (the bar
  table applies, wide frames, NaN rules, symbol names); the book menu and engine (schedule, fills, costs per unit of
  turnover, funding, 1× legs); the gates and both nulls, saying plainly that a ranking that barely changes for months
  fails the timing null; the survivor note; the example, labelled as an API example. About 11 KB; today's static
  context is 36.5 KB of the 100 KB cap. `tests/test_context.py` pins it.
- **Contract** (`contract.py`): `Proposal.plugin_kind: Literal["strategy", "portfolio"]`, required (`kind` already
  means new/refinement/port); `ResearchIdea` too. For a portfolio idea, `trades_per_coin_per_year` means leg entries
  per coin per year and `columns` the panel columns it reads.
- **Idea rules** (`assemble.py`): an idea has one plugin kind, as one timeframe. A refinement keeps both; its
  `PARENT` is the champion, of the same kind. A port keeps the kind and moves to that kind's other timeframe (1h ↔
  4h). A single-coin idea made cross-sectional is a **new idea** (a new family), never a port: a different bet under
  different gates. AST checks: one subclass of the declared kind's base; a literal `book` passing `check_book`;
  `perp_lab.portfolio` in `ALLOWED_IMPORTS`.
- **Preflight** (`preflight.py`): for a portfolio, the precheck, the dated panel lookahead, and leg overlap on
  `panel_market(tf)` against every portfolio plugin in `strategies/` (cache keyed by hash, tf, `PANEL_MARKET_VERSION`);
  a mirror (negated scores swap the legs) is refused. Strategies and portfolios are compared only after the trial, on
  real OOS positions and daily returns; the record's duplicate rules apply across kinds.
- **Piles** (`piles.py`): unchanged, except a portfolio verdict is a close call only if its beta gate passed
  (`diagnostics.portfolio.beta.ok`). Designer's call: a book failing beta is a market bet, not a near-miss. Both nulls
  reach the piles through `random_p95`.
- **Record** (`record.py`): `ideas.plugin_kind` and `proposals.plugin_kind`, String(16), server default `"strategy"`,
  added by `ALTER TABLE` on open.
- **Prompt** (`prompt.py`): each idea shows its kind; the round's instruction gains one paragraph on the two kinds and
  how a portfolio is judged. The research round and referee get one line each; `lab idea add` and the card form gain
  `plugin_kind` (default strategy).
- **No seeded portfolio ideas.** Designer's call: the research round should find them; seeding would steer.

## Dashboard

- Strategies page: a Kind column.
- Detail page for a portfolio run: OOS equity against `MARKET`; long- and short-leg cumulative net; per-coin net
  contributions; turnover and cost drag; both nulls; beta and hedged Sharpe; breadth; folds; the survivor banner. The
  trade chart shows legs without stop or target lines.
- Ideas page: a kind badge; a portfolio promotion's review packet adds beta, breadth, the nulls and the survivor
  diagnostic, and says its exam is blocked until treatment item 7 is met.

## Config

`config/lab.yaml`, fixed before any portfolio trial:

```yaml
portfolio:
  timeframes: [1h, 4h]
  rebalance: [4h, 8h, 1d, 3d, 1w]
  min_coins: 10
  gross: 1.0
  min_listed_days: 30
  vol_days: 30
  beta_days: 90
  beta_floor: 0.1
  min_held_share: 0.5
  beta_max: 0.3
  breadth: {top_coin_share_max: 0.5, folds_positive_min: 0.5}
  paper_warmup_days: 120
```

Reused unchanged: `costs`, `margin_brackets`, `walkforward`, `validation`, `judge`, `sandbox` (scaled as in "CPU
limits"), `proposer.corr_max`.

## Changes to existing code (for the planner)

New:
- `perp_lab/portfolio.py`: `Panel`, `Portfolio`, `check_book`, `BOOK_KEYS`.
- `perp_lab/data/panel.py`: `load_panel`, `panel_arrays`, `panel_long`.
- `perp_lab/engine/book.py`: `BookParams`, `schedule`, `target_weights`, `run_book`, `BookResult`,
  `BOOK_ENGINE_VERSION`.
- `perp_lab/validate/portfolio.py`: `walkforward_portfolio`, `random_ranking`, `random_shift`, `book_sensitivity`,
  `book_before_costs`, `market_beta`, `breadth`, `validate_portfolio` (`column_group_problems` for every panel coin,
  then registers the trial, then writes the run).
- `perp_lab/validate/synthetic_panel.py`: `synthetic_panel`, `panel_market`, `LISTING_MONTHS`,
  `PANEL_MARKET_VERSION`.
- `perp_lab/paper/portfolio.py` (built later).

Changed:
- `perp_lab/plugins.py` (`kind`, `book`, `parse_meta`; `reads_columns`'s portfolio branch); `perp_lab/strategy.py`
  (docstring's import list).
- `perp_lab/sandbox/`: `worker.py` (preload, ops), `session.py` (`Portfolio` subclasses, panel ops), `client.py`
  (`Worker.panel`, `scores`, `panel_stream_open`, `panel_step`, `PanelJob`, `fresh_scores`, `_META` + `book`, `kind`,
  `WITHHELD_COLUMNS`, scaled limits), `protocol.py` (score shapes).
- `perp_lab/validate/causality.py`: `panel_causality_failures`, `NO_BOOK`; a test that no withheld column is in any
  synthetic frame.
- `perp_lab/runs.py`: `verdict_portfolio`, `data_identity(..., kind)`. `perp_lab/store/db.py`: `runs.kind`.
- `perp_lab/judge/judge.py`: kind dispatch (`precheck`, both lookahead functions, `judge` with gates 6–8,
  `refresh_verdict`, `_todays_rules`); `VERDICT_SCHEMA = 3`; `kind` in `VERDICT_KEYS`.
- `perp_lab/judge/exam.py`: `run_portfolio_exam`, `portfolio_cost_hours`, `PORTFOLIO_EXAM_READY`, the
  `universe_blocked` code; `attempt_exam` dispatch.
- `perp_lab/judge/diagnostics.py`: the `portfolio` block. `perp_lab/preflight.py`: the portfolio branch.
- `perp_lab/cli.py`: `walkforward`, `backtest`, `paper` dispatch on kind; `paper --symbols` refused for portfolios.
- `perp_lab/proposer/`: `context.py`, `contract.py`, `assemble.py`, `piles.py`, `prompt.py`, `record.py`,
  `research.py`, `human.py`. `proc.py` unchanged.
- `dashboard/app.py`, `dashboard/ideas.py`; `config/lab.yaml`.

## Changes to existing documents

- `2026-10-17-plugin-sandbox-design.md`, Decisions "Symbols" and "What crosses the boundary": the panel rule and
  scores, pointing here.
- `2026-10-16-judge-design.md`: portfolio plugins are judged with this spec's gates.
- `2026-10-19-proposer-design.md`: an amendment, "Portfolio plugins", pointing here.
- `README.md`: the plugin kinds, the gates, the survivor note and the exam prerequisite.

## Testing

Every test builds its own panel from synthetic bars in a temp dir; none touches `data/`, `runs/`, `proposer/` or
`lab.db`.
- **API and metadata:** kind detection; both bases refused; `book` checks (unknown key, k + buffer > 5, a cadence not
  a multiple of tf, 15m); `session` refused on a portfolio; cross-kind `PARENT` refused; `check_meta` catches a
  runtime `book`.
- **Engine** (hand-computed, 3–17 coins): costs on the change only; funding sign per side; fill at the next open;
  mark-to-open; a 1× short liquidated near +98% intrabar and on a gap; flat below `min_coins`; seeded ties (constant
  scores give a book that changes across rows and is the same for the same hash); buffer; dollar legs sum to 0; beta
  legs cancel and go flat under the floor (and clip with `floor_beta`); inverse-vol; `min_listed_days`; a missing bar
  at a fill; `no_bar` at a decision; `eow`; `contrib` sums to each day's return.
- **Lookahead:** peeks that fail: the next row of another coin; a full-sample z-score; the panel's length; the last
  row of a late lister; keyed on a name (`"BTCUSDT" in panel.symbols`); keyed on the count
  (`len(panel.symbols) == 17`); keyed on a date in 2021. An honest plugin naming `panel["close"]["BTCUSDT"]` passes.
  The synthetic panel's symbols equal `cfg.symbols`; a late lister is all-NaN before its listing in every cut;
  `btc_*` identities hold when present; `NO_BOOK`.
- **Sandbox:** panel in, scores out; wrong shape, ±inf and oversize refused; a sealed worker imports
  `perp_lab.portfolio`; no withheld column reaches a panel worker; scaled limits applied.
- **Walk-forward:** a fixture where mean per-coin Sharpe and book Sharpe pick different combos (the book's wins); the
  activity floors; fold-switch cost charged once per change, on drifted weights, off a decision row; stitched
  contributions match stitched returns.
- **Nulls:** identical flat set (including a plugin flat for its beta floor); p = 0 chosen for a static plugin;
  offsets distinct, within [W, D − W], reproducible per run id; a planted static hindsight tilt beats 2a and fails
  2b; a planted timing-aligned ranking edge beats both; pure noise beats neither in most seeds.
- **Gates:** each at its boundary; beta catches long-alts/short-BTC in a rising synthetic market; breadth catches a
  one-coin book and a one-fold book; gate 7 against a blocked promotion and not a withdrawn one.
- **Exam:** `universe_blocked` with no read while `PORTFOLIO_EXAM_READY` is false (and the flag pinned false);
  streamed equals batch for a causal plugin; no row sent after its decision time; holdout rows only with a token;
  eviction; `too_slow` from either probe rule; a retry that changes a row is `mismatch`.
- **Paper:** the live-shape check refuses a plugin with a 200-day feature and one that ranks on `quote_volume`;
  `--symbols` refused; a plugin reading a group without a live feed refused.
- **Column groups:** `validate_portfolio` refuses before `register_trial` when one panel coin's group data is
  incomplete; `reads_columns` finds a portfolio reading `spot_*` statically and one reading it through a computed name
  dynamically (by its books); the exam's coverage includes an `if_read` group exactly when it is read.
- **Reproducibility:** a strategy run's `data_id` unchanged; a portfolio `data_id` moves with each included key and
  not with an excluded one.
- **Builder:** context pinned and under the cap; contract, idea rules (kind kept; a kind change is a new idea), piles
  (beta), record migration; preflight mirror refusal. **Dashboard:** a portfolio run fixture renders.

## Build order

One concern per PR, tests in each, after designs 1 and 2 land:
1. `perp_lab/portfolio.py`, metadata, preload.
2. The book engine.
3. The panel, sandbox ops and limits, `WITHHELD_COLUMNS`, the synthetic panel, the panel lookahead.
4. Walk-forward (with readiness), both nulls, sensitivity, before costs, gates 1–4, report, `runs.kind`, `data_id`.
5. Judge dispatch, gates 5–8, verdict schema 3, diagnostics.
6. The exam, blocked by `PORTFOLIO_EXAM_READY`, with group coverage.
7. Preflight and the builder.
8. Dashboard.
9. Paper, when a portfolio plugin first passes an exam.

The `portfolio:` config and this spec's thresholds merge with PR 4 and never change after the first portfolio trial.

## Risks and open questions

- **The timing null fails near-static books by design.** Lead's call (B1). A book whose ranks barely change for months,
  including a very persistent carry ranking, has shifted copies that hold nearly the same coins, so it can't beat
  their p95 however much it earned. On a survivor universe, with a builder whose training covers the whole span, a
  static tilt is indistinguishable from hindsight, so this is accepted: such books are out of reach of this lab until
  a point-in-time universe and fresh data exist. Carry ideas must earn from changes in the ranking.
- **Scores, not weights** (Designer's call): the nulls are exact, but conviction sizing is impossible.
- **The beta cap at 0.3 plus the hedged DSR** (Designer's call) may fail real momentum edges, which load on beta
  after bull runs; they should use `neutral: beta`.
- **1× isolated legs** (Designer's call): fewer liquidations than 3×; Sharpe barely moves, drawdowns do.
- **Gate 7 only against promotions** (Designer's call, set narrower by the lead's M1): wider would kill improvements.
- **The phase-shift island variant** (Designer's call) is stricter than the strategy test; at 8h cadence the shift is
  4h and each hold still spans one settlement, so a real carry book should survive it.
- **Fold-switch costs** (Designer's call) make the two kinds' Sharpes not quite comparable.
- **The step probe at a quarter of the limit** (Designer's call) may block a heavy honest plugin; it can be sped up
  before its read.
- **The full-span dated panel** makes the check about 60k rows long: roughly a minute per variant set in parallel.
- **The exam prerequisite** (Lead's call) means no portfolio promotion is examined until the follow-up spec is built.

Left to the implementer: the panel's Arrow layout and caching; the engine's vectorisation; the p-grid estimation
within the rule; report and dashboard layout; how `validate_portfolio` shares code with `validate_strategy`.

## Facts not verified

- The citations in "Why" (Liu, Tsyvinski & Wu 2022; Schmeling, Schrimpf & Todorov) come from the research notes and
  memory; not re-read.
- Coin start months other than SOL, AAVE and DOGE are from the wider-data spec (2026-10-18); the 2026-11-05 re-check
  timed out.
- Why the EOSUSDT and MATICUSDT archives end (MATIC's migration to POL is recalled); the archive dates were checked.
- Book volatility, the market-to-book volatility ratio, momentum-crash dates and the 2022–25 alt underperformance are
  recollections; measuring them would read real bars outside a trial. The survivor-exam and breadth probabilities are
  the reviewer's simulations, not re-run here.
- The panel's memory and the 2 s engine and one-minute check figures are estimates from row counts.

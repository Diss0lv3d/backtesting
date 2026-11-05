# Cross-coin Reference Bars (Part A) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every coin's research bars carry BTC's same-timeframe bar as eight `btc_*` columns. The lookahead check, readiness check, exam and builder context cover them, behind the shared column-group machinery (X1–X7) that design 2 will reuse.

**Architecture:**
- A column-group registry (`perp_lab/data/columns.py`) declares each group. The reference group's code lives in `perp_lab/data/reference.py`.
- `store.load_bars` attaches each registered group after funding and metrics. A shared per-timeframe source is cached per walk-forward process.
- Readiness (`column_group_problems`) refuses runs before any trial is counted.
- `plugins.reads_columns` (static AST check OR a dynamic two-seed check) decides paper refusal.
- The lookahead check gains synthetic `btc_` columns and "as BTC" variants.

**Tech Stack:** Python 3.12, pandas, numpy, pytest; the existing sandbox (`perp_lab/sandbox/`).

**Spec:** `docs/superpowers/specs/2026-11-05-cross-coin-reference-design.md` is the binding authority, especially X1–X7, "Plugin-facing interface", "The lookahead check and the synthetic bars", "Judging", "Builder integration" and "Testing". This plan builds **Part A only**. Part B (the paper feed) ships later, before any plugin that reads `btc_` starts a paper run.

## Global Constraints

- Run tests in the worktree with `PYTHONPATH=$PWD /home/burhan/backtesting/.venv/bin/pytest -q -p no:cacheprovider <paths>`. Three tests fail only in worktrees, and only those three are expected:
  - `tests/test_confine.py::test_numpy_pandas_pyarrow_import_after_confinement`
  - `tests/test_confine.py::test_the_seal_reaches_every_thread`
  - `tests/test_worker.py::test_check_raises_when_confinement_fails`
- Tests never read or write the real `data/`, `runs/`, `proposer/` or `lab.db`; use `tmp_path` and `tests/conftest.py` helpers. No subagent reads `data/`.
- Every research read goes through `perp_lab/data/store.py` with `_resolve_end`, and BTC's sources are cut at the resolved end. Nothing reads at or after `holdout_start` without a holdout token.
- No gate, threshold, benchmark rule, cost or N changes. Trials that read `btc_` count in the one store-wide N.
- Exact values (from the spec):
  - `reference.COIN = "BTC"`, `reference.SYMBOL = "BTCUSDT"`, `reference.PREFIX = "btc_"`.
  - Bar columns: `btc_open`, `btc_high`, `btc_low`, `btc_close`, `btc_quote_volume`, `btc_taker_buy_quote_volume`.
  - As-of columns: `btc_funding_last`, `btc_oi_value`.
  - Reference group: `rule = 1`, `synthetic_id = 1`, `exam_required = "always"`, `paper_ready = False`.
  - OI publish lag 300,000 ms. `MARKET_VERSION = 3`. Synthetic ρ to BTC = 0.7. Market ρ = 0.55, 0.7, 0.85 for SYN2–SYN4.
- `load_bars` stays tolerant: a missing group source gives NaN columns and one warning per process per (group, tf).
- Every `btc_*` column is float64, and attaching never changes the coin frame's row count or index.
- One concern per commit. Every commit message ends with these two lines:
  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01WZgKtXA9TNg2AeUCiUM4ii
  ```
- Never `git stash` (bare); never edit `strategies/` plugins that have been judged.
- Task 1 (the X7 inventory) is DONE: commit e19dfe0 and `docs/research/reference-inventory/`. The reference `known_gaps` is `[]`, and no recalibration is needed.

## Review Focus

1. **BTC source rows with a duplicated `open_time`** (overlap between the monthly and daily archives): the attach must not duplicate or drop coin rows. Pinned in Task 4.
2. **BTC data that ends before, or starts after, the coin's frame:** NaN `btc_` rows there, with no exception. Pinned in Task 4.
3. **dtype:** every `btc_*` column is float64, including on an all-NaN attach, so the Arrow send to the sandbox never sees an object or int column. Pinned in Task 4.
4. **Source-cache keys:** a 4h walk-forward job never receives a 1h source, and the exam never reuses the research cache (which is cut at `holdout_start`). Pinned in Tasks 9 and 10.
5. **A plugin that passes every bar column through as features** (`return bars.copy()`) without trading on `btc_`: `reads_columns` dynamic must be False, since it compares decisions only. Pinned in Task 8.

---

### Task 2: Pure as-of helpers in store

**Files:**
- Modify: `perp_lab/data/store.py` (`align_funding`, `align_metrics`)
- Test: `tests/test_store_asof.py` (new)

**Interfaces:**
- Produces:
  - `store.funding_last_at(close_times: np.ndarray, funding: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]`: the last settled rate and its interval hours, as of each close time (settlement `funding_time <= close_time`).
  - `store.metrics_at(close_times: np.ndarray, metrics: pd.DataFrame | None, publish_lag_ms: int = 300_000) -> tuple[np.ndarray, np.ndarray]`: `oi` and `oi_value` as of each close time (stamp + lag `<=` close_time).
- `align_funding` and `align_metrics` call these. Their output must be byte-identical to today's.

- [ ] **Step 1: Write the failing tests.**
  - `test_align_funding_unchanged`: build candles plus funding by hand, and compare against a copy of the pre-refactor values. Capture them by running today's `align_funding` in the test module before the refactor, then pin them as literals.
  - `test_align_metrics_unchanged`: the same for `align_metrics`.
  - `test_funding_last_at_boundary`: a settlement at exactly a bar's close time is on that bar, and one 1 ms later is not.
  - `test_metrics_at_lag_boundary`: a snapshot stamped `close_time − 300_000` is on the bar, and one stamped 1 ms later is not.
- [ ] **Step 2: Run them.** The two helper tests fail (`AttributeError`); the two pins pass against the current code.
- [ ] **Step 3: Implement the helpers** and route `align_funding` and `align_metrics` through them. Keep `funding_charge` as it is.
- [ ] **Step 4: Run the four tests plus `tests/test_data.py tests/test_late_listing.py`.** All pass.
- [ ] **Step 5: Commit** `refactor(store): pure as-of helpers for funding and metrics`.

### Task 3: Column-group registry, readiness and data identity (X1–X3)

**Files:**
- Create: `perp_lab/data/columns.py`
- Modify: `perp_lab/runs.py` (`data_identity`), `config/lab.yaml` (add `column_groups: {reference: {known_gaps: []}}` with a comment pointing at the spec)
- Test: `tests/test_columns.py` (new)

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) ColumnGroup` with fields:
    - `name: str`, `columns: tuple[str, ...]`, `prefix: str`, `rule: int`, `synthetic_id: int`;
    - `exam_required: str` (`"always"` or `"if_read"`), `helpers: tuple[str, ...]`, `paper_ready: bool`;
    - `load_source: Callable | None`, `attach: Callable`, `synthetic: Callable`;
    - `sources: Callable[[cfg, list[str], str], list[tuple[str, str]]]`, giving the (symbol, dataset) directories the readiness check reads.
  - `GROUPS: tuple[ColumnGroup, ...]`. It is EMPTY in this task; Task 4 registers the reference.
  - `register(group) -> None`, for tests only. Production code registers by literal in `columns.py`.
  - `column_group_problems(cfg, symbols: list[str], tf: str) -> list[str]`:
    - checks every research month from `history_start` to the month before `holdout_start`, for each of `group.sources(cfg, symbols, tf)`, against `data/<symbol>/<dataset>/_manifest.json`;
    - each month must have an entry with `complete: true`;
    - `absent` is allowed only before the first real month;
    - entries in `cfg["column_groups"][group.name]["known_gaps"]` (`{symbol, dataset, month, why}`) are accepted;
    - symbols in that group's `exclusions` are skipped.
    - Each problem is one string naming the group, symbol, dataset and month. Months at or after `holdout_start` are never read.
  - `identity_block() -> dict`: `{"columns": {g.name: g.rule}}` for groups with `rule > 1`, else `{}`.
- `runs.data_identity(cfg, symbols)` merges `columns.identity_block()`.

- [ ] **Step 1: Write the failing tests** in `tests/test_columns.py`, using a test-registered fake group over a `tmp_path` data dir with hand-written manifests:
  - `test_complete_months_give_no_problem`;
  - `test_incomplete_month_is_one_problem`;
  - `test_missing_manifest_is_a_problem`;
  - `test_absent_after_first_real_month_is_a_problem`;
  - `test_known_gap_clears_its_month`;
  - `test_excluded_symbol_is_skipped`;
  - `test_holdout_months_are_never_read`: a manifest entry for the `holdout_start` month that is incomplete gives no problem;
  - `test_identity_block_empty_at_rule_1` and `test_data_id_unchanged_today`: pin `runs.data_id(runs.data_identity(cfg, symbols))` for `tests/conftest.py`'s `market_config` to the value computed BEFORE this change, captured in the test as a literal;
  - `test_rule_2_changes_data_id`: register a fake group at rule 2.
- [ ] **Step 2: Run them; they fail** with `ModuleNotFoundError: perp_lab.data.columns`.
- [ ] **Step 3: Implement** `columns.py` and the `runs.data_identity` merge, and add `column_groups` to `config/lab.yaml`. The registry must restore cleanly between tests: provide a fixture or context manager for `register`.
- [ ] **Step 4: Run `tests/test_columns.py tests/test_runs*.py tests/test_config*.py`.** All pass.
- [ ] **Step 5: Commit** `feat(data): column-group registry, readiness check and versioned data identity`.

### Task 4: The reference group and `load_bars`

**Files:**
- Create: `perp_lab/data/reference.py`
- Modify: `perp_lab/data/columns.py` (register the reference group in `GROUPS`), `perp_lab/data/store.py` (`load_bars` gains `groups: bool = True`, `sources: dict[str, object] | None = None`), `perp_lab/validate/benchmarks.py` (`buy_and_hold` passes `groups=False`)
- Test: `tests/test_reference.py` (new)

**Interfaces:**
- Consumes: Task 2's helpers and Task 3's `ColumnGroup`.
- Produces (spec "Plugin-facing interface"):
  - constants `COIN`, `SYMBOL`, `PREFIX`, `BAR_COLUMNS` (the six), `ASOF_COLUMNS = ("funding_last", "oi_value")`, `COLUMNS` (the eight `btc_` names);
  - `@dataclass(frozen=True) ReferenceSource(candles, funding, metrics)`;
  - `load_source(cfg, tf, start=None, end=None, holdout=None) -> ReferenceSource`: BTC candles through `store.load_candles` (so `_resolve_end` guards them), funding and metrics cut at the resolved end, and empty frames when the data is missing;
  - `attach(bars, symbol, tf, source) -> pd.DataFrame`: an exact `open_time` join for the six bar columns, NaN where BTC's bar is missing or has `trades == 0`, and the two as-of columns from Task 2's helpers at the coin's `close_time`;
  - `sources(cfg, symbols, tf) -> [(SYMBOL, tf), (SYMBOL, "funding"), (SYMBOL, "metrics")]`.
- `load_bars` order: candles, slippage, funding, metrics, then each group in `GROUPS` (using `sources[g.name]` if given, else `g.load_source(cfg, tf, start, end, holdout)`), then regime. A missing source logs one warning per (group, tf) per process.

- [ ] **Step 1: Write the failing tests** (spec Testing 1–3, plus Review Focus 1–3), on a `tmp_path` market built with `tests/conftest.py` `write_market` for ETHUSDT and BTCUSDT:
  - `test_row_t_carries_btc_bar_t`;
  - `test_missing_btc_bar_gives_nan_on_that_row_only`;
  - `test_zero_trade_btc_bar_gives_nan_bar_columns_asof_still_set`;
  - `test_btc_own_bars_equal_own_columns`;
  - `test_1h_never_takes_4h_value`;
  - `test_oi_boundary` (stamp `close_time − 300_000` is on the bar, 1 ms later is not);
  - `test_funding_settlement_at_bar_open_belongs_to_that_bar`;
  - `test_holdout_rows_absent_without_token`: BTC data runs past `holdout_start` with a planted absurd price and funding rate there; research `load_bars("ETHUSDT")` shows neither;
  - `test_end_past_holdout_without_token_raises`;
  - `test_with_token_holdout_rows_appear`;
  - `test_no_btc_data_gives_nan_and_one_warning`;
  - `test_duplicate_btc_open_time_keeps_row_count`;
  - `test_btc_shorter_than_coin_gives_nan_tail`;
  - `test_btc_columns_are_float64_even_all_nan`;
  - `test_buy_and_hold_unchanged` (`groups=False`).
- [ ] **Step 2: Run them; they fail** (no module).
- [ ] **Step 3: Implement** `reference.py`, the registration, the `load_bars` changes and `buy_and_hold`. `reference.synthetic` is a stub that raises `NotImplementedError` until Task 6; register it anyway.
- [ ] **Step 4: Run** `tests/test_reference.py`, `tests/test_data.py`, `tests/test_late_listing.py` and `tests/test_benchmarks*.py`, then the full suite (some judge/walk-forward tests may now warn about missing BTC; they must still pass, since nothing refuses yet). All pass except the 3 known worktree failures.
- [ ] **Step 5: Commit** `feat(data): BTC reference columns on every coin's bars`.

### Task 5: Indicator helpers

**Files:**
- Modify: `perp_lab/indicators.py`
- Test: `tests/test_indicators_reference.py` (new)

**Interfaces:**
- Produces:
  - `reference_frame(bars, prefix="btc_") -> pd.DataFrame`: `open_time`, `close_time` and the six bar columns renamed to `open, high, low, close, quote_volume, taker_buy_quote_volume`, on the same index.
  - `rolling_beta(y: pd.Series, x: pd.Series, n: int) -> pd.Series`, computed as `y.rolling(n).cov(x) / x.rolling(n).cov(x)`, and NaN where the denominator is 0.

- [ ] **Step 1: Write the failing tests.**
  - `test_rolling_beta_of_x_on_itself_is_exactly_one`: `== 1.0` wherever defined, not approximately.
  - `test_residual_against_itself_is_exactly_zero`.
  - `test_rolling_beta_nan_on_zero_variance`.
  - `test_volume_vs_same_hour_on_reference_frame` equals the same function on a hand-built frame of BTC's columns.
  - `test_reference_frame_keeps_index_and_times`.
- [ ] **Step 2: Run them; they fail.**
- [ ] **Step 3: Implement both helpers.**
- [ ] **Step 4: Run them; they pass.**
- [ ] **Step 5: Commit** `feat(indicators): reference_frame and an exact rolling_beta`.

### Task 6: Synthetic reference and the lookahead check

**Files:**
- Modify: `perp_lab/data/reference.py` (`synthetic(bars, seed) -> dict[str, np.ndarray]`, per the spec "The lookahead check and the synthetic bars", drawing from `np.random.default_rng((seed, 1))`), `perp_lab/validate/causality.py` (`with_moving_columns` group fills, `as_reference_coin(bars)`, the "as BTC" variants in `causality_failures`)
- Test: `tests/test_causality_reference.py` (new), with fixture plugins under `tests/fixtures/` or written to `tmp_path` the way existing causality tests do

**Interfaces:**
- Consumes: Task 4's `COLUMNS` and the group registry; Task 5's `rolling_beta` (used by the honest fixture plugin).
- Produces:
  - `with_moving_columns(bars, seed)` adds each group's columns through `group.synthetic` when any is missing, all-NaN or constant, as a group. Existing fills stay byte-identical.
  - `causality.as_reference_coin(bars) -> pd.DataFrame`.
  - `causality_failures` runs the "as BTC" variants at the default params, and at `params` too when given. Failures read `defaults as BTC: ...` or `<params> as BTC: ...`. The no-signals rule is not applied to the "as BTC" variants.

- [ ] **Step 1: Write the failing tests** (spec Testing 5–6):
  - caught: `btc_close.shift(-1)`; `btc_oi_value.shift(-1)`; a full-sample `np.polyfit` beta;
  - a peek through `ctx._a` gated on `ctx.symbol == "BTCUSDT"` is caught by "as BTC" only;
  - a plugin that raises when `close == btc_close` fails "as BTC" only;
  - a peek gated on a grid value fails "<params> as BTC" when given those params;
  - an honest `rolling_beta` plugin passes;
  - a plugin that never trades BTC passes;
  - `test_existing_fills_byte_identical`: pin today's `with_moving_columns(synthetic_bars())` non-`btc_` columns;
  - synthetic properties: correlation with the coin is 0.6–0.8; every `btc_` column is non-constant; `btc_oi_value` is NaN on exactly the first 300 rows; changing rows after t leaves every `btc_` value up to t unchanged.
- [ ] **Step 2: Run them; they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the new tests, `tests/test_causality.py`, `tests/test_lookahead.py` and `tests/test_preflight.py`. All pass.
- [ ] **Step 5: Commit** `feat(validate): synthetic BTC reference and the "as BTC" lookahead variants`.

### Task 7: Synthetic market version 3

**Files:**
- Modify: `perp_lab/validate/synthetic_market.py`
- Test: `tests/test_synthetic_market.py` (extend)

**Interfaces:**
- Produces:
  - `SYMBOLS = ("BTCUSDT", "SYN2", "SYN3", "SYN4")` and `MARKET_VERSION = 3`.
  - SYN2–SYN4's 5m log-returns are mixed with the reference at ρ = 0.55, 0.7, 0.85 (same variance), before close, open, high and low are rebuilt.
  - After resampling, each symbol's `btc_` columns come from BTCUSDT's resampled frame by open time (X6 applied); BTCUSDT's equal its own.

- [ ] **Step 1: Write the failing tests** (spec Testing 8):
  - `MARKET_VERSION == 3`;
  - `SYMBOLS[0] == "BTCUSDT"`, with version 2's SYN1 prices: pin a few closes before the change;
  - SYN2–SYN4's return correlation with BTCUSDT is increasing, in order;
  - every frame's `btc_` columns equal BTCUSDT's resampled ones by open time;
  - `reference.SYMBOL` matches `config/lab.yaml`'s universe plus quote for BTC.
- [ ] **Step 2: Run them; they fail.**
- [ ] **Step 3: Implement.** Update the module docstring.
- [ ] **Step 4: Run** `tests/test_synthetic_market.py`, `tests/test_preflight.py` and `tests/test_overlap*.py`. All pass; overlap fixtures may need new expected values if they pin market numbers. If so, change only those values and say so in the commit.
- [ ] **Step 5: Commit** `feat(validate): synthetic market v3 with BTCUSDT as the reference coin`.

### Task 8: Which plugins read a group (X4)

**Files:**
- Modify: `perp_lab/plugins.py`
- Test: `tests/test_reads_columns.py` (new)

**Interfaces:**
- Consumes: the `GROUPS` registry; `group.synthetic`; `perp_lab.sandbox.client.fresh_run` (import it inside the function, because `client` imports `plugins`); the check's synthetic bars (`causality.synthetic_bars` plus `with_moving_columns`).
- Produces:
  - `names_group(src, group) -> list[str]`: AST only. It reports a string constant or f-string part containing a group column or `group.prefix`, an attribute named like one, or a call to a name in `group.helpers`. It returns the offending names or snippets, and an empty list when nothing matches.
  - `reads_columns(cfg, src, group, params: dict | None = None) -> bool`: `bool(names_group(...))`, or else a dynamic check. Two `fresh_run`s at the default params (and at `params` if given), on the same synthetic bars with only `group.synthetic(bars, seed)` changed (seeds 1 and 2). Any difference in entries or exits counts.

- [ ] **Step 1: Write the failing tests** (spec Testing 11, plus Review Focus 5):
  - static: `"btc_close"`, `bars.btc_close`, `f"btc_{k}"` and `reference_frame(bars)` each count; a comment mentioning `btc_close` does not;
  - dynamic: a plugin that builds `"bt" + "c_close"` at run time and trades on it → True;
  - a plugin that never reads `btc_` → False;
  - `test_feature_passthrough_is_not_reading`: `prepare` returns `bars.copy()` and decisions use only `close` → False.
- [ ] **Step 2: Run them; they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them; they pass.** Dynamic tests may need the sandbox. Follow how existing tests call `fresh_run`, and skip under the same conditions they skip.
- [ ] **Step 5: Commit** `feat(plugins): reads_columns, a static OR dynamic test for a column group`.

### Task 9: Walk-forward sources, readiness in `validate_strategy`, and the market diagnostic

**Files:**
- Modify:
  - `perp_lab/validate/walkforward.py`: `_sources(tf)`, `lru_cache(maxsize=3)`, used by `_bars`;
  - `perp_lab/validate/pipeline.py`: in `validate_strategy`, `column_group_problems(cfg, symbols, tf)` runs BEFORE `db.register_trial` and raises the module's existing refusal error, or `ValueError` with the problems joined. It also records `btc_exposure` in the run summary;
  - `perp_lab/validate/benchmarks.py`: `btc_exposure(cfg, oos_portfolio, tf, a, b) -> dict` with keys `beta`, `corr` and `hedged_sharpe`, or None when BTC is unavailable;
  - `perp_lab/judge/diagnostics.py`: `diagnostics.market`, None for older runs;
  - `tests/conftest.py`: `write_market` also writes funding and metrics files and manifests for every symbol (BTCUSDT's metrics from its first fixture month), so `market_config` runs pass X3. Fixtures without BTCUSDT that validate or judge patch `columns.column_group_problems` to return `[]`.
- Test: `tests/test_reference_pipeline.py` (new)

**Interfaces:**
- Consumes: Tasks 3 and 4.
- Produces: `walkforward._sources(tf) -> dict[str, object]`; `benchmarks.btc_exposure` (above); `diagnostics["market"]`.

- [ ] **Step 1: Write the failing tests:**
  - `test_validate_refuses_before_register_trial_on_problem`: N is unchanged;
  - `test_chunk_worker_receives_btc_columns`;
  - `test_sources_built_once_per_process_per_tf`;
  - `test_sources_keyed_by_tf` (Review Focus 4);
  - `test_btc_exposure_recovers_beta`: OOS = 1.5 × BTC daily return + noise → β ≈ 1.5 (±0.1), hedged Sharpe ≈ the noise's;
  - `test_market_diagnostic_in_verdict_and_read_by_no_gate`.
- [ ] **Step 2: Run them; they fail.**
- [ ] **Step 3: Implement**, including the conftest change.
- [ ] **Step 4: Run the full suite.** All pass except the 3 known worktree failures. Every fixture that broke from X3 is fixed by the conftest change or by an explicit patch in that test, never by weakening the check.
- [ ] **Step 5: Commit** `feat(validate): BTC sources per worker, readiness before any trial, market diagnostic`.

### Task 10: Judge, exam and CLI

**Files:**
- Modify:
  - `perp_lab/judge/judge.py`: `column_group_problems` runs before `db.register_trial` (line ~278) and raises `JudgeRefused` with the problems;
  - `perp_lab/judge/exam.py`:
    - `window_end` also takes the earliest end over `reference.SYMBOL`'s candles at tf, its funding, and its last metrics snapshot + 5 min;
    - `window_data_problems` checks BTC's months at tf with `EXCHANGE_SOURCES`, and every UTC day for at least one BTC candle and one BTC metrics snapshot, with the note `" (BTC reference)"`;
    - `run_exam` builds the sources once with its token and passes them to each `load_bars(..., sources=...)`;
  - `perp_lab/cli.py`:
    - `lab propose` runs `column_group_problems` for each `proposer.timeframes` entry before `loop.run_session` and exits 2 on any problem;
    - `lab paper` exits 2, before recording the run, for a plugin where `reads_columns(cfg, src, g)` is true for a group with `paper_ready` False, naming the group.
- Test: `tests/test_reference_judge.py` (new); existing exam test stubs are updated in the same commit if their signatures change.

**Interfaces:**
- Consumes: Tasks 3, 4 and 8.

- [ ] **Step 1: Write the failing tests:**
  - `test_judge_refuses_before_register_trial` (N unchanged);
  - `test_propose_refuses_on_problem` (exit 2, `perp_lab/proposer/` never reads `data/`);
  - `test_window_end_stops_at_btc_end_when_btc_not_an_exam_coin`;
  - `test_window_data_problems_names_ccxt_btc_month`;
  - `test_window_data_problems_names_day_without_btc_metrics`;
  - `test_exam_builds_sources_with_token_not_research_cache` (Review Focus 4);
  - `test_streamed_equals_batch_for_btc_reader`;
  - `test_paper_refuses_reference_reader_before_recording`.
- [ ] **Step 2: Run them; they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the new tests and `tests/test_judge*.py tests/test_exam*.py tests/test_cli*.py tests/test_paper*.py tests/test_proposer_cli.py`. All pass.
- [ ] **Step 5: Commit** `feat(judge): BTC reference in readiness, exam window and paper refusal`.

### Task 11: Builder context

**Files:**
- Modify: `perp_lab/proposer/context.py`
- Test: `tests/test_context.py` (extend); `tests/test_plugin_columns_move.py` (new)

**Interfaces:**
- Produces: `BAR_COLUMNS` gains the five rows (spec "Plugin-facing interface") after `regime_adx`, plus the paragraph quoted in spec "Builder integration". `PLUGIN_COLUMNS` then includes the eight names. `_piles` gets the synthetic-columns sentence quoted in the spec.

- [ ] **Step 1: Write the failing tests:**
  - the context holds each new row and the paragraph's key phrases: `"BTCUSDT"`, `"nothing is filled forward"`, `"diagnostics.market"`, `"reference_frame"`, `"rolling_beta"`;
  - `PLUGIN_COLUMNS` contains the eight names;
  - `_piles` holds `"as BTCUSDT"`;
  - the static context stays under `STATIC_MAX_BYTES`;
  - `test_every_plugin_column_moves` (spec Testing 7): each `PLUGIN_COLUMNS` name is present and non-constant in `with_moving_columns(synthetic_bars())` and in every `synthetic_market.market(tf)` frame at 15m, 1h and 4h.
- [ ] **Step 2: Run them; they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `tests/test_context.py tests/test_plugin_columns_move.py tests/test_prompt.py tests/test_human_ideas.py`. All pass.
- [ ] **Step 5: Commit** `feat(proposer): tell the builder about the btc_ columns`.

### Task 12: Documents

**Files:**
- Modify:
  - `README.md`: the store line; the exam paragraph (BTC's months and days are checked, and the window ends where BTC's data does); readiness; `diagnostics.market`;
  - `docs/superpowers/specs/2026-10-17-plugin-sandbox-design.md`: amend the "Symbols" decision as spec item 13 says;
  - `docs/superpowers/specs/2026-10-19-proposer-design.md`: add a section "## Amended: BTC reference columns (2026-11-05)", like the OI amendment.

- [ ] **Step 1: Edit the three documents.** Every claim must be true of the code as built in Tasks 2–11.
- [ ] **Step 2: Run** `tests/test_readme*.py`, if any exist, and the full suite once. Green except the 3 known.
- [ ] **Step 3: Commit** `docs: BTC reference columns in the README and the amended specs`.

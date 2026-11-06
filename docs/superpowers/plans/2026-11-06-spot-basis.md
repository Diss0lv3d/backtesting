# Spot Prices and Perp–Spot Basis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every coin's research bars carry five spot columns (`spot_close`, `spot_quote_volume`, `spot_taker_buy_quote_volume`, `spot_trades`, `basis`) from Binance spot klines. The lookahead check, the readiness check, the exam (for plugins that read them) and the builder context all cover them.

**Architecture:**
- A `spot` column group, registered in `perp_lab/data/columns.py`. Its code lives in `perp_lab/data/spot.py`, modelled on `reference.py`.
- `lab backfill` learns the `spotKlines` dataset. The lead runs it and commits a real-data inventory before any spot code touches bars (X7).
- `load_bars` attaches spot per coin by an exact `open_time` join (X6). Readiness, the lookahead check, `reads_columns` and paper's refusal pick the group up through design 1's registry.
- The exam's group coverage moves into the registry. Each group declares its exam end and its exam data problems. The exam covers `always` groups, plus the `if_read` groups the plugin reads.

**Tech Stack:** Python 3.12, pandas, numpy, pyarrow, pytest; the existing sandbox (`perp_lab/sandbox/`).

**Spec:** `docs/superpowers/specs/2026-11-05-spot-basis-design.md` is binding. Design 1's X1–X7 are in `docs/superpowers/specs/2026-11-05-cross-coin-reference-design.md`, under "Shared pieces". Where the spec and design 1's built code (head 24f0d77) disagree, the built code wins; see Reconciliations.

## Global Constraints

- Run tests in the worktree with `PYTHONPATH=$PWD /home/burhan/backtesting/.venv/bin/pytest -q -p no:cacheprovider <paths>`. Three tests fail only in worktrees, and only those three are expected:
  - `tests/test_confine.py::test_numpy_pandas_pyarrow_import_after_confinement`
  - `tests/test_confine.py::test_the_seal_reaches_every_thread`
  - `tests/test_worker.py::test_check_raises_when_confinement_fails`
- Tests never read or write the real `data/`, `runs/`, `proposer/` or `lab.db`; use `tmp_path` and the `tests/conftest.py` helpers. Vision tests mock `_get` (or `_fetch_zip_csv`). **No subagent reads `data/`.** Tasks 2 and 11 are LEAD ONLY: the lead does them itself.
- Every research read goes through `store._resolve_end`. Nothing reads at or after `holdout_start` without a holdout token.
- No gate, threshold, cost, benchmark, fold, holdout or N changes. Spot trials count in the one store-wide N.
- Exact values (from the spec):
  - `spot.PREFIX = "spot_"`. `spot.COLUMNS = ("spot_close", "spot_quote_volume", "spot_taker_buy_quote_volume", "spot_trades", "basis")`, in that order.
  - Spot group: `name="spot"`, `rule=1`, `synthetic_id=2`, `exam_required="if_read"`, `helpers=()`, `paper_ready=False`.
  - Definition: `basis = close / spot_close − 1`, from the two bars with the same `open_time`.
  - X6: the four `spot_*` columns are NaN where the spot bar is missing or has `trades == 0`. `basis` is NaN then too, and also where the perp bar has `trades == 0`. A NaN perp `trades` (ccxt-stitched) is not zero. No forward fill anywhere.
  - Spot dataset `spotKlines` covers timeframes `15m`, `1h` and `4h`, stored at `data/<SYMBOL>/spot/<tf>/`. Readiness and `known_gaps` name that dataset `spot/<tf>`.
  - Symbol check: the median basis must be within ±1%, on the last research month with ≥ 100 bars traded on both sides.
  - Synthetic defaults:
    - `BASIS_SLOW_PHI = 0.995`, `BASIS_SLOW_SD = 0.0010`, `BASIS_C = 0.0`;
    - `BASIS_FAST_PHI = 0.9`, `BASIS_FAST_SD = 0.0005`, `BASIS_JUMP_P = 0.01`, `BASIS_JUMP = (0.0020, 0.0060)`;
    - `SPOT_SHARE = 0.3`, `SHARE_PHI = 0.99`, `SHARE_SD = 0.3` (stationary), `SHARE_NOISE_SD = 0.3`, `TAKER_SLOPE = 0.6`, `TAKER_NOISE_SD = 0.05`;
    - `NAN_STRETCHES = ((0, 288), (432, 480))` (moved by ruling S5).
  - `MARKET_VERSION`: 4 in Task 5, 5 in Task 11.
- `load_bars` stays tolerant. A missing spot folder gives NaN columns and one warning per process per (symbol, tf). All five columns are float64. Attaching never changes the frame's row count or index.
- One concern per commit, each with its test. Every commit message ends with:
  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01BeYA1hxQAxAefGrp4EF6kp
  ```
- Never bare `git stash`. Never edit judged plugins in `strategies/`.

## Review Focus

1. **Overlapping monthly and daily spot files** (the same `open_time` twice). Each perp bar still gets one row and the frame keeps its length. Pinned in Task 3.
2. **An exclusion written as a coin (`FIL`) or as a symbol (`FILUSDT`).** Both give all-NaN columns without reading the folder, are skipped by readiness, the symbol check and the exam, and are listed as `FILUSDT` in the context. Pinned in Tasks 3, 6, 9 and 10.
3. **A plugin that reads no spot, with a spot gap or an early spot end in its exam window.** Its exam neither waits nor ends early. Pinned in Task 9.
4. **A plugin that fills spot NaN from later bars** (`bfill`, `interpolate`). The lookahead check reports it. Pinned in Task 4.
5. **dtypes.** All five columns are float64 even when all-NaN (a missing folder, an excluded coin), so the Arrow send never sees an object column. Pinned in Task 3.

## Reconciliations

Built code wins over the spec on these names and signatures:

1. The spec's `store.load_spot`, `store.attach_spot` and `columns.spot_synthetic` live in `perp_lab/data/spot.py`, as the reference's code lives in `reference.py`. The `ColumnGroup` literal goes in `columns.GROUPS`.
2. The spec's `spot_synthetic(bars, rng) -> DataFrame` becomes `spot.synthetic(bars, seed) -> dict[str, np.ndarray]`, the type `ColumnGroup.synthetic` has. It makes `np.random.default_rng((seed, SYNTHETIC_ID))` itself.
3. The spec's `load_bars(..., with_spot=True)` becomes the built `groups: bool = True`. `buy_and_hold` already passes `groups=False`, so there is no new flag.
4. The built `load_source(cfg, tf, start, end, holdout)` is one source shared by every coin, and `attach(bars, symbol, tf, source)` gets no `cfg`. So spot's `load_source` returns a lazy handle, `SpotSource(cfg, tf, end, holdout)`, with no rows in it. Its end is resolved at build, so the holdout guard fires there. `attach` then reads that coin's spot through the handle. Existing tests that count `load_source` calls count the reference's only.
5. The spec's `spot_exclusions` becomes `column_groups.spot.exclusions`. Its `known_gaps` entries use full symbols and `dataset: spot/<tf>`.
6. `helpers`: the spec's `load_spot, attach_spot` becomes `()`. The built field lists plugin-callable indicator functions, which X4's static scan treats as reads. A plugin can't import `perp_lab.data`.
7. The built `symbol_check(cfg, tf)` takes no symbols. Spot's checks every coin in `cfg.symbols` that isn't excluded.
8. Readiness's "source `absent`, `monthly` or `daily`": the built generic check accepts any complete, non-absent source. Only `lab backfill` writes spot manifests (spot has no `lab tail`), so this needs no change.
9. "`synthetic_bars` carries the five as NaN": design 1 adds no group columns to `synthetic_bars`. `with_moving_columns` fills a missing group, so it stays unchanged.
10. Synthetic NaN: beyond the two stretches, the five are also NaN on row k−1 for each `causality.NAN_CUTS` value k (the first cut only, row 399), as `reference.synthetic`'s bar columns are since b113971, so that a `bfill` is caught (Review Focus 4) while a next-bar peek gated on a spot column still shows at the later cuts.
11. Static scan: the built `names_group` counts a group column found in a docstring, but the spec says a docstring saying "basis" is no hit. Rule from here on: in a docstring only the prefixed columns and the prefix count. An unprefixed column (`basis`) counts outside docstrings only. The reference's columns are all prefixed, so its behaviour is unchanged.
12. The spec's dynamic test, `bars.filter(like="spot_")`, is a static hit under the built scan, since `"spot_"` holds the prefix. The dynamic test builds the name at run time instead: `"sp" + "ot_close"`.
13. Exam iteration: the spec defers to "design 1's mechanism", but the built `window_end` and `window_data_problems` hardcode the reference. Task 8 adds it: optional `ColumnGroup.exam_end` and `exam_problems`, the manifest and timestamp helpers moved to `perp_lab/data/archive.py`, a `groups` argument on both exam functions, `exam_groups`, and an `attempt_exam(..., groups_fn=exam_groups)` stand-in.
14. Paper: the built refusal is generic, and its wording wins over the spec's: "reads, or can't be shown not to read, the spot column group (spot_ columns), which paper trading can't feed yet". The `lab propose` start check, `validate_strategy` and `judge` readiness are generic too, so they need no code.
15. Context: `BAR_COLUMNS` is a static constant with no `cfg`, so the spot paragraph, which names the exclusions, is rendered by `_api(cfg)`. The built `_piles` sentence already has a `btc_` clause; Task 10 gives the merged sentence.

Plan decisions where the spec is silent or would break after calibration:

16. The inventory goes in `docs/research/spot-inventory/`, as the lead instructed, not the spec's `spot-basis-inventory/`.
17. Calibration S comes from `causality.check_bars()`. T also needs the p50 of the level and of the 24-bar mean, because the acceptance rule measures each tail's distance from the median.
18. The resampled market `basis` is recomputed from the resampled `close` and `spot_close`, as a real coarser kline's would be.
19. The spec's tests at "±20 bp" and "changes sign" are written as `BASIS_C ± 2·BASIS_SLOW_SD` and "crosses `BASIS_C`". At the default constants these are the spec's values exactly, and they still hold after calibration.

---

### Task 1: The `spotKlines` backfill dataset

**Files:**
- Modify: `perp_lab/data/vision.py`, `config/lab.yaml` (`backfill.datasets: [klines, fundingRate, metrics, spotKlines]`), `perp_lab/cli.py` (the `backfill --datasets` help lists `spotKlines`)
- Test: `tests/test_backfill_listing.py` (extend)

**Interfaces:**
- Produces:
  - `vision.SPOT_BASE = "https://data.binance.vision/data/spot"`, `vision.SPOT_TIMEFRAMES = ("15m", "1h", "4h")`.
  - The dataset `"spotKlines"` wherever a dataset is named: `first_archive_month` lists prefix `data/spot/monthly/klines/{symbol}/{tf}/`; `dataset_dir` gives `data_dir/{symbol}/spot/{tf}`; `_PARSERS` maps it to `parse_klines`, `_SORT_KEY` to `open_time`.
  - `_monthly_url`: `{SPOT_BASE}/monthly/klines/{s}/{tf}/{s}-{tf}-{YYYY-MM}.zip`. `_daily_url`: `{SPOT_BASE}/daily/klines/{s}/{tf}/{s}-{tf}-{YYYY-MM-DD}.zip`.
  - `backfill` makes one job per (symbol, tf, month) for each tf in `tfs` that is also in `SPOT_TIMEFRAMES`. The module docstring's layout gains the spot folder.

- [ ] **Step 1: Write the failing tests.**
  - `test_spot_dataset_dir`: `dataset_dir(d, "ETHUSDT", "spotKlines", "1h") == d / "ETHUSDT" / "spot" / "1h"`.
  - `test_spot_monthly_and_daily_urls`: exact strings, using the month `2025-03` and the day `2025-03-05`.
  - `test_spot_listing_prefix`: the `_get` URL holds `prefix=data/spot/monthly/klines/ETHUSDT/1h/`.
  - `test_spot_jobs_per_timeframe_skip_5m`: `tfs=["5m", "1h", "4h"]` makes jobs at 1h and 4h only.
  - `test_spot_months_before_the_first_file_are_absent`: the same assertions as the klines test, under `ETHUSDT/spot/1h`.
  - `test_spot_month_written_with_the_candle_schema`: with the monthly fetch stubbed, the file `spot/1h/2025-03.parquet` has `CANDLE_SCHEMA` columns, and the manifest says `{"complete": true, "source": "monthly"}`.
  - `test_spot_microsecond_file_parses_to_ms`: `_get` returns a zip of a µs CSV plus a matching `.CHECKSUM`. The parquet then has `open_time` in ms, and `close_time == open_time + 3_599_999`.
  - `test_config_backfills_spot`: `"spotKlines"` is in `config/lab.yaml`'s `backfill.datasets`.
- [ ] **Step 2: Run them; they fail** (`KeyError: 'spotKlines'`, or the dataset is skipped as unsupported).
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `tests/test_backfill_listing.py tests/test_data.py`. All pass.
- [ ] **Step 5: Commit** `feat(backfill): Binance spot klines as the spotKlines dataset`.

### Task 2: Real spot data and its inventory — LEAD ONLY (not a subagent)

The lead does this task itself, in the main checkout, where `data/` lives. No later task touches spot bars until it is committed (X7).

**Files:**
- Create: `docs/research/spot-inventory/inventory.py`, `coverage.csv`, `nan_runs.csv`, `zero_trade.csv`, `volume_share.csv`, `calibration_targets.csv`, `README.md`
- Modify: `config/lab.yaml` (`column_groups.spot`)

**Interfaces:**
- Produces:
  - `config/lab.yaml` gains `column_groups.spot: {exclusions: [...], known_gaps: [...]}`. Exclusions are full symbols (`FILUSDT`). Each gap is `{symbol: ETHUSDT, dataset: spot/1h, month: 2020-03, why: "..."}`: one per research (symbol, tf, month) that the readiness rules would otherwise name.
  - `calibration_targets.csv` has columns `statistic,value` and exactly these rows, as fractions: `level_p1, level_p50, level_p99, mean24_p1, mean24_p50, mean24_p99, share_p50`. Task 11 reads it.

- [ ] **Step 1: Check which code runs.** `cd /home/burhan/backtesting && PYTHONPATH=/home/burhan/backtesting/.worktrees/spot-basis .venv/bin/python -c "import perp_lab.data.vision as v; print(v.__file__, v.SPOT_BASE)"` prints the worktree's path.
- [ ] **Step 2: Backfill.** From `/home/burhan/backtesting`, with the same `PYTHONPATH`: `.venv/bin/lab backfill --datasets spotKlines --tfs 15m,1h,4h`. Expected: a JSON stats line with `"error": 0` and `"partial" >= 1` (the open month was built from daily files, so the daily URL works). On errors, re-run once (it resumes). Keep both JSON lines.
- [ ] **Step 3: Write `inventory.py`.** It runs from the main checkout root with `--config /home/burhan/backtesting/.worktrees/spot-basis/config/lab.yaml`.
  - **Reads** the research span only: perp candles through `store.load_candles` with its default end, spot parquet with `open_time < holdout_start`, manifest months before `holdout_start`. It writes only into its own folder and computes no forward returns.
  - **Definitions** (Task 11 relies on them): `basis` = perp close / spot close − 1 on bars with the same open time where both have `trades > 0`; `mean24` = per coin, `basis.rolling(24, min_periods=24).mean()` over the perp's 1h bars; `share` = spot `quote_volume` / perp `quote_volume` where both traded; "pooled" = every non-excluded coin's 1h values, concatenated; percentiles by `np.nanpercentile` (linear).
  - **Outputs:**
    - `coverage.csv`, one row per (symbol, tf), 17 coins × 3 timeframes: `symbol, tf, first_spot_month, first_spot_day, first_perp_day, monthly_months, daily_months, absent_months, manifest_problems, off_grid_rows, perp_bars, spot_nan_share, nan_runs_over_1d, zero_trade_spot_bars, symbol_check_month, symbol_check_median, abs_basis_p50` (the last at 1h only).
    - `nan_runs.csv` (`symbol, tf, first_open, last_open, bars`: each run of perp bars with NaN spot longer than a day); `zero_trade.csv` (`symbol, tf, open_time`); `volume_share.csv` (`symbol, year, share_p50`, at 1h); `calibration_targets.csv`, as above.
- [ ] **Step 4: Run it, and check each row of the spec's fact table** ("First build task") with its "if false" column: a missing pair, a failed symbol check or wide holes put the coin in exclusions; an isolated missing month goes in `known_gaps`; a later spot start or a large NaN share is noted for the README. If the UTC grid fact is false (`off_grid_rows > 0`), stop and amend this plan (build from a finer timeframe) before Task 3.
- [ ] **Step 5: Write `column_groups.spot`** into `config/lab.yaml`. Re-run the script, so the pooled targets leave out the excluded coins.
- [ ] **Step 6: Check the main checkout's plugins.** Run `grep -nE "dropna\(\)|bars\.columns|\.filter\(|spot|basis" /home/burhan/backtesting/strategies/*.py`. For each hit, record whether NaN-bearing extra columns would change it. The spec says none of the 53 does.
- [ ] **Step 7: Write `README.md`**, like `docs/research/reference-inventory/README.md`. It holds:
  - the run date, the commands and both backfill JSON lines;
  - the fact table, each fact marked confirmed or false, with what changed;
  - each coin's start against its perp, the gaps (the 2020–21 maintenance windows included), the zero-trade bars and the symbol-check medians;
  - BTC's monthly spot share and spot trade count for 2022-01..2023-12, which confirm or drop the zero-fee period;
  - every coin whose 1h `abs_basis_p50` is within 2× its half-spread (the "noisy basis" risk);
  - the Step 6 result, and the exclusions and `known_gaps` with why.
- [ ] **Step 8: Commit twice:** `docs(research): spot real-data inventory (spot-basis X7)` (the folder), then `config: spot exclusions and known gaps from the inventory` (`config/lab.yaml`).

### Task 3: Spot loading and the exact join

**Files:**
- Create: `perp_lab/data/spot.py`
- Modify: `tests/conftest.py` (add `write_spot`)
- Test: `tests/test_spot.py` (new)

**Interfaces:**
- Produces, in `perp_lab/data/spot.py`:
  - `PREFIX`, `COLUMNS`, `SYNTHETIC_ID = 2`, and `dataset(tf) -> str` (returns `f"spot/{tf}"`).
  - `excluded(cfg, symbol) -> bool`: true when `symbol`, or `symbol` minus `cfg["quote"]`, is in `cfg["column_groups"]["spot"]["exclusions"]`. Missing keys mean nothing is excluded.
  - `load_spot(cfg, symbol, tf, start=None, end=None, holdout=None) -> pd.DataFrame`: the rows of `data/<symbol>/spot/<tf>/*.parquet` with `open_time` in `[start or history_start, end)`, `end` resolved by `store._resolve_end`; duplicate `open_time`s dropped, sorted, `CANDLE_SCHEMA` columns on a RangeIndex. Raises `FileNotFoundError` when the folder holds no parquet.
  - `attach_spot(bars, spot: pd.DataFrame | None) -> pd.DataFrame`, pure: `bars` plus the five columns as float64 on the same rows and index, by an exact `open_time` join (spot rows deduplicated first), X6 as in the Global Constraints; `None` gives all-NaN.
  - `@dataclass(frozen=True) SpotSource` with fields `cfg`, `tf: str`, `end: pd.Timestamp`, `holdout`.
  - `load_source(cfg, tf, start=None, end=None, holdout=None) -> SpotSource`: reads no rows. `end` is resolved by `_resolve_end`, so a request past the holdout without a token raises here.
  - `attach(bars, symbol, tf, source: SpotSource) -> pd.DataFrame`:
    - raises `ValueError` for a source at another tf, or one whose `end <=` the bars' last close;
    - an excluded coin gets `attach_spot(bars, None)` without any read;
    - otherwise `attach_spot(bars, load_spot(source.cfg, symbol, tf, <bars' first open>, source.end, source.holdout))`; on `FileNotFoundError`, NaN columns and one `log.warning` per process per (symbol, tf).
  - `sources(cfg, symbols, tf) -> list[tuple[str, str]]`: `[(s, dataset(tf)) for s in symbols]`.
- Produces, in `tests/conftest.py`: `write_spot(data_dir, symbol, tf, candles, manifest, basis=0.0003)`. It writes `data/<symbol>/spot/<tf>/all.parquet` from the perp `candles` (prices ÷ (1 + basis); volumes × 0.3, taker buys half of that; `trades = round(0.5 × trades)`) and `manifest` as the folder's `_manifest.json`.

- [ ] **Step 1: Write the failing tests**, on hand-built bars and a `tmp_path` market:
  - `test_exact_join_on_open_time`;
  - `test_off_grid_spot_row_is_not_attached` (its open is +1 minute);
  - `test_spot_rows_without_a_perp_bar_are_dropped`;
  - `test_missing_spot_bar_gives_five_nan`;
  - `test_zero_trade_spot_bar_gives_five_nan`;
  - `test_zero_trade_perp_bar_gives_nan_basis_keeps_spot`;
  - `test_nan_perp_trades_still_get_basis`;
  - `test_bar_after_a_gap_has_its_own_values`;
  - `test_basis_identity_is_exact`: `basis == close / spot_close - 1`, exactly, wherever it is not NaN;
  - `test_attach_on_a_prefix_equals_the_prefix` (`attach_spot(bars[:k], s)` equals `attach_spot(bars, s)[:k]`), `test_truncating_spot_after_t_changes_nothing_up_to_t`, and `test_zero_trade_bar_at_t_plus_1_leaves_t_unchanged`;
  - `test_duplicate_spot_open_time_keeps_row_count` (Review Focus 1);
  - `test_spot_columns_float64_even_all_nan`, which also checks the row count and index (Review Focus 5);
  - `test_spot_shorter_than_perp_gives_nan_tail`;
  - `test_load_spot_past_holdout_without_token_raises`, `test_load_spot_keeps_start_to_end`, and `test_load_source_resolves_end_and_guards`;
  - `test_missing_spot_folder_gives_nan_and_one_warning_per_coin_tf` (use `caplog`; a second call adds no warning);
  - `test_excluded_coin_is_all_nan_and_reads_nothing`: for both `ETH` and `ETHUSDT`, a planted unreadable file is never opened (Review Focus 2);
  - `test_attach_refuses_other_tf_or_short_source`;
  - `test_sources_name_spot_tf_dirs`.
- [ ] **Step 2: Run them; they fail** (no module).
- [ ] **Step 3: Implement.** The module is not registered in `GROUPS` yet.
- [ ] **Step 4: Run** `tests/test_spot.py`. All pass.
- [ ] **Step 5: Commit** `feat(data): spot loading and the exact spot join (X6)`.

### Task 4: The synthetic spot stream and its lookahead coverage

**Files:**
- Modify: `perp_lab/data/spot.py`
- Test: `tests/test_spot_synthetic.py` (new)

**Interfaces:**
- Consumes: Task 3's `COLUMNS` and `SYNTHETIC_ID`; `causality.CUTS`, `check_bars`, `synthetic_bars`, `with_moving_columns` and `causality_failures`; `columns.registered`; `client.fresh_run`.
- Produces:
  - the constants in the Global Constraints;
  - `synthetic(bars, seed) -> dict[str, np.ndarray]`, step for step as the spec's "The lookahead check and the synthetic bars". It reads the filled `close`, `quote_volume`, `trades` and `taker_buy_quote_volume`. All draws come up front, in a fixed order, from `default_rng((seed, SYNTHETIC_ID))`, so they depend only on `len(bars)`. The slow and fast AR(1)s start from their stationary distributions; a jump enters the fast state and decays with it; `basis` is recomputed from `spot_close`, so the identity is exact. Last of all, the five are set NaN on `NAN_STRETCHES` and on row k−1 for each k in `causality.NAN_CUTS` with k < n.
- Test fixture `with_spot`: `columns.registered(SPOT)`, where `SPOT` is a `ColumnGroup` built in the test module from Task 3 and this task, with the values in the Global Constraints. Task 5 turns it into a no-op.

- [ ] **Step 1: Write the failing tests.**
  - `test_draws_from_seed_and_synthetic_id`: a spy on `spot.np.random.default_rng` sees `((7, 2),)`.
  - `test_all_five_move`.
  - `test_basis_identity_exact_on_5m_bars`.
  - `test_spot_taker_share_within_bounds`: spot taker / spot quote volume lies in [0.05, 0.95].
  - `test_spot_trades_are_whole`.
  - `test_basis_is_persistent`, on `check_bars()` with the stream assigned at `EXTRAS_SEED`:
    - the lag-24 autocorrelation of `basis` is > 0.3;
    - `basis.rolling(24).mean()` crosses `BASIS_C` at least 3 times.
  - `test_spot_volume_follows_perp_volume`: corr(log `spot_quote_volume`, log `quote_volume`) > 0.5.
  - `test_nan_rows_are_the_stretches_and_the_row_before_the_first_cut`: at n = 3000, the NaN rows are exactly [0,288) ∪ {399} ∪ [432,480), for all five (`NAN_CUTS`, ruling S5).
  - `test_values_up_to_t_ignore_later_rows`: the same n, with rows after t changed.
  - Under `with_spot`:
    - `test_peek_at_each_spot_column_is_reported` (parametrized over the five; `fails[0].startswith("defaults: AssertionError")`);
    - `test_int_spot_trades_is_reported` (a line holds `ValueError`);
    - `test_basis_mean_threshold_peek_is_caught`: the plugin enters only when the 24-bar mean of `basis` lies beyond `BASIS_C ± 2·BASIS_SLOW_SD`, and in that branch only reads the series' last close (a peek gated on the ±20 bp band can't be seen at seed 7, ruling S4c);
    - `test_bfill_of_spot_is_caught` (Review Focus 4);
    - `test_honest_basis_plugin_passes` (`[]`).
  - `test_real_plugins_unchanged_by_spot_columns`, parametrized over `REAL_PLUGINS`:
    - `with_ = check_bars()` with `synthetic(…, EXTRAS_SEED)` assigned, and `without = with_.drop(columns=COLUMNS)`;
    - `fresh_run(..., features=True)` gives identical features and the same decisions on both.
- [ ] **Step 2: Run them; they fail** (`AttributeError: synthetic`).
- [ ] **Step 3: Implement.** If seed 7's stream never takes the threshold peek branch, report DONE_WITH_CONCERNS. Don't change the seed.
- [ ] **Step 4: Run** `tests/test_spot_synthetic.py tests/test_causality.py`. All pass.
- [ ] **Step 5: Commit** `feat(spot): synthetic spot stream, covered by the lookahead check`.

### Task 5: Register the spot group

**Files:**
- Modify:
  - `perp_lab/data/columns.py`: register spot after the reference, as `ColumnGroup(name="spot", columns=spot.COLUMNS, prefix=spot.PREFIX, rule=1, synthetic_id=spot.SYNTHETIC_ID, exam_required="if_read", helpers=(), paper_ready=False, load_source=spot.load_source, attach=spot.attach, synthetic=spot.synthetic, sources=spot.sources)`.
  - `perp_lab/validate/synthetic_market.py`, in `_resample`:
    - the three spot volumes and counts are summed with `min_count=1`;
    - `spot_close` takes the bucket's last value;
    - `basis` is recomputed as `close / spot_close − 1`;
    - `MARKET_VERSION = 4`, and the docstring says so.
  - `tests/conftest.py`:
    - `write_market` calls `write_spot` for each symbol and tf, with the klines' manifest;
    - new `write_ready_spot(cfg, tfs)`: a complete `spot/<tf>` manifest for every research month of every `cfg.symbols` coin, plus perp candles (kept if present) and spot candles in the last research month, so Task 6's symbol check passes;
    - `tests/test_store.py` and `tests/test_proposer_cli.py` call it next to `write_ready_reference`.
  - Existing tests updated to the new column set:
    - `tests/test_causality.py`: `EXCHANGE_COLUMNS` gains the five;
    - `tests/test_causality_reference.py::test_existing_fills_byte_identical`: excludes the spot columns, and its column list ends with `reference.COLUMNS + spot.COLUMNS`;
    - `tests/test_synthetic_market.py::test_the_market_is_fixed` (version 4; it drops the spot columns where it drops the reference's);
    - `tests/test_synthetic_market.py::test_every_timeframe_has_every_column_with_no_gaps`: the spot columns sit just before the `btc_` ones (those are joined after resampling, so they stay last), and are left out of `notna`;
    - `tests/test_reference_pipeline.py::counting_groups` and `tests/test_reference_judge.py::test_exam_builds_sources_with_token_not_research_cache`: count the reference group only;
    - warning-counting tests filter by logger;
    - the `with_spot` fixture in `tests/test_spot_synthetic.py` becomes a no-op that asserts spot is in `GROUPS`.
- Test: `tests/test_spot.py`, `tests/test_synthetic_market.py` (extend)

**Interfaces:**
- Consumes: Tasks 3 and 4.
- Produces: `columns.GROUPS == (reference, spot)`, and `load_bars` frames end with `reference.COLUMNS`, then `spot.COLUMNS`, before `regime`. `write_ready_spot` as above.

- [ ] **Step 1: Write the failing tests.**
  - `test_load_bars_carries_spot_after_btc_columns`: the five are float64, and no `btc_spot_*` or `btc_basis` column exists.
  - `test_holdout_spot_files_change_nothing`: absurd spot prices are planted in a separate file at and after `holdout_start`. Research `load_bars` equals the frame with that file deleted, and holds no spot value at or after `holdout_start`.
  - `test_exam_token_reads_holdout_spot`: `load_bars(..., end=END, holdout=token)` carries holdout spot.
  - `test_walkforward_bars_carry_spot` (`wfm._bars`).
  - `test_data_id_unchanged_with_spot_registered`: equal to the value with `GROUPS` patched down to the reference alone.
  - `test_spot_rule_2_moves_data_id`.
  - `test_check_bars_carry_the_spot_stream`: equal to `spot.synthetic` on the filled bars at `EXTRAS_SEED`.
  - `test_market_version_is_4`.
  - `test_market_spot_resampling`: the sums use `min_count=1`, `spot_close` is the last value, and `basis == close / spot_close − 1` exactly at 15m, 1h and 4h.
  - `test_market_spot_nan_stretches_whole`: at 15m, 1h and 4h, the spot NaN bars are exactly those inside the 5m rows [0,288) and [432,480) (ruling S5).
  - `test_market_pre_existing_columns_unchanged`: `pd.util.hash_pandas_object` sums of each frame without the spot columns, captured before the change and pinned.
- [ ] **Step 2: Run them; they fail.**
- [ ] **Step 3: Implement**, including the fixture and existing-test updates listed above. Weaken no check.
- [ ] **Step 4: Run the full suite.** All pass except the 3 known worktree failures.
- [ ] **Step 5: Commit** `feat(data): register the spot column group (market v4)`.

### Task 6: Spot symbol check and readiness refusals

**Files:**
- Modify: `perp_lab/data/spot.py`, `perp_lab/data/columns.py` (`symbol_check=spot.symbol_check`)
- Test: `tests/test_spot_readiness.py` (new)

**Interfaces:**
- Produces: `spot.symbol_check(cfg, tf) -> list[str]`.
  - It runs for each coin in `cfg.symbols` that isn't `excluded`.
  - It reads `open_time`, `close` and `trades` of the perp's `<tf>` candles and the spot's, in the research span only. It joins them on `open_time` and keeps the rows where both have `trades > 0`.
  - It takes the last research month with ≥ 100 such rows. When the median of `close / spot_close − 1` there is outside ±1%, the problem reads `f"column group spot: {sym}/spot/{tf} {month}: median basis {m:+.2%}, outside ±1%; another pair? exclude the coin in column_groups.spot.exclusions"`.
  - When no month qualifies, the problem reads `f"column group spot: {sym}/spot/{tf} has no research month with 100 bars traded on both perp and spot; exclude the coin in column_groups.spot.exclusions"`.

- [ ] **Step 1: Write the tests.** The six manifest tests pass already, because Task 5 put spot's `sources` in readiness.
  - Manifest cases, each giving the generic line for `ETHUSDT/spot/4h`:
    - `test_mid_span_month_without_entry`;
    - `test_incomplete_month`;
    - `test_no_spot_manifest`;
    - `test_absent_after_first_real_month`;
    - `test_known_gap_month_passes`;
    - `test_excluded_coin_skipped`, for both `ETH` and `ETHUSDT`.
  - Symbol check:
    - `test_1000x_spot_price_is_a_problem`;
    - `test_no_month_with_100_traded_bars_is_a_problem`;
    - `test_last_month_with_100_bars_is_used` (the last month has 50 traded bars and a bad price; the month before is fine → `[]`);
    - `test_holdout_spot_never_read` (a 1000× price planted in the holdout → `[]`).
  - Refusals, on a spot problem:
    - `test_judge_refuses_on_spot_problem`: `JudgeRefused`, with `db.trial_stats(cfg)[0] == 0`, and the trials and judgements tables empty;
    - `test_walkforward_refused_on_spot_problem` (exit 2, N unchanged);
    - `test_propose_exits_2_before_any_model_call` (`loop.run_session` patched to fail if called).
- [ ] **Step 2: Run them.** The symbol-check tests fail; the rest pass.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `tests/test_spot_readiness.py tests/test_columns.py tests/test_reference_judge.py tests/test_proposer_cli.py`. All pass.
- [ ] **Step 5: Commit** `feat(spot): readiness symbol check (±1% median basis)`.

### Task 7: Which plugins read spot

**Files:**
- Modify: `perp_lab/plugins.py` (`names_group`, `_named_in`)
- Test: `tests/test_spot_reads.py` (new)

**Interfaces:**
- Produces: in a docstring, `names_group` counts only the columns that start with `group.prefix`, and the prefix itself. Outside docstrings it is unchanged. `reads_columns` is unchanged.

- [ ] **Step 1: Write the failing tests.**
  - Static: `ctx["basis"]` → `["basis"]`; `bars.basis` → `["basis"]`; `bars[["spot_close", "close"]]` → `["spot_close"]`; `bars.filter(like="spot_")` → `["spot_"]`.
  - `test_docstring_saying_basis_is_no_hit`: a module docstring and a function docstring that say "basis" → `[]`. This one fails today.
  - `test_docstring_naming_spot_close_still_counts`.
  - Dynamic: `test_runtime_spot_name_is_read`: a plugin trades on `bars["sp" + "ot_close"]`, so `names_group` gives `[]` and `reads_columns(cfg, src, spot)` is True.
  - `test_plugin_reading_no_spot_is_not_a_reader`.
  - `test_paper_refuses_spot_reader_before_recording`: exit 2, the output holds `"spot column group"`, and no run row is written.
- [ ] **Step 2: Run them.** The docstring test fails.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `tests/test_spot_reads.py tests/test_reads_columns.py tests/test_reference_judge.py`. All pass.
- [ ] **Step 5: Commit** `fix(plugins): an unprefixed group column in a docstring is not a read`.

### Task 8: The exam's group coverage through the registry

**Files:**
- Create: `perp_lab/data/archive.py`
- Modify: `perp_lab/judge/exam.py`, `perp_lab/data/reference.py`, `perp_lab/data/columns.py`, `perp_lab/judge/judge.py`
- Test: `tests/test_exam_groups.py` (new); in the same commit, `tests/test_judge_exam.py`'s stubs: `end_after(days)` returns `lambda cfg, symbols, tf, groups=(): ...`; `no_gaps(cfg, symbols, tf, end, groups=())` and `ccxt_month` likewise; `judged` and `exams` pass `groups_fn=lambda *a: ()`.

**Interfaces:**
- Produces:
  - `archive.py`, moved from `exam.py` with the bodies unchanged: `files(d)`; `stamps(cfg, symbol, dataset) -> np.ndarray`; `data_end(cfg, symbol, tfs) -> int`; `window_months(start, end) -> pd.PeriodIndex`; `month_problems(cfg, symbol, dataset, months, end, sources, note) -> list[str]`; `missing_days(cfg, symbol, dataset, start, end, what="candles", note="") -> list[str]`; and `EXCHANGE_SOURCES`, `REGIME_SOURCES`, `CCXT`, `DAY_MS`, `DEFAULT_FUNDING_H`, which stay importable from `exam`.
  - `ColumnGroup` gains two optional fields, defaulting to None, after `symbol_check`:
    - `exam_end: (cfg, symbols, tf) -> int | None`, the ms where the group's data for those exam coins stops;
    - `exam_problems: (cfg, symbols, tf, end) -> list[str]`.
  - `reference.exam_end` and `reference.exam_problems` hold today's BTC logic and produce the same lines. The reference's entry sets them.
  - `exam.window_end(cfg, symbols, tf, groups=None)` and `exam.window_data_problems(cfg, symbols, tf, end, groups=None)`:
    - `None` means the groups with `exam_required == "always"`;
    - a group's callable that is None, or returns None, contributes nothing.
  - `exam.exam_groups(cfg, src, params) -> tuple[ColumnGroup, ...]`: the `always` groups, plus each `if_read` group where `reads_columns(cfg, src, g, params)`, in `GROUPS` order.
  - `judge.attempt_exam(..., groups_fn=exam_groups)`, which `exam_pending` passes through. `_attempt` calls `groups_fn(cfg, src, params)` inside the sandbox `try`, before step 2, then `end_fn(cfg, syms, tf, groups)` and `data_fn(cfg, syms, tf, end, groups)`.

- [ ] **Step 1: Write the failing tests:**
  - `test_window_end_default_covers_always_groups`;
  - `test_no_groups_means_no_btc_lines` (a day with no BTC metrics is named by default, but not with `groups=()`);
  - `test_exam_groups_reference_always_spot_if_read` (a spot reader → `(reference, spot)`; a non-reader → `(reference,)`);
  - `test_attempt_passes_its_groups_to_end_and_data` (a spy);
  - `test_groups_fn_sandbox_down_is_pending_exam_error` (re-raised, nothing minted).
- [ ] **Step 2: Run them; they fail.**
- [ ] **Step 3: Implement.** Every existing line in `tests/test_exam.py` and `tests/test_reference_judge.py` stays byte-identical.
- [ ] **Step 4: Run** `tests/test_exam_groups.py tests/test_exam.py tests/test_judge_exam.py tests/test_reference_judge.py tests/test_columns.py`. All pass.
- [ ] **Step 5: Commit** `refactor(exam): column-group coverage through the registry (exam_required, reads_columns)`.

### Task 9: Spot coverage in the exam

**Files:**
- Modify: `perp_lab/data/spot.py`, `perp_lab/data/archive.py`, `perp_lab/data/columns.py` (the spot entry sets `exam_end`/`exam_problems`)
- Test: `tests/test_spot_exam.py` (new)

**Interfaces:**
- Consumes: Task 8.
- Produces:
  - `archive.stamps(..., traded=False)`: when `traded`, it reads `open_time` and `trades` and keeps the rows with `trades > 0`. `archive.missing_days(..., traded=False)` passes the flag on.
  - `spot.exam_end(cfg, symbols, tf) -> int | None`: the minimum, over the non-excluded exam coins, of (last spot `open_time` + tf). A coin with no spot files gives `holdout_start` in ms. If every coin is excluded, it returns None.
  - `spot.exam_problems(cfg, symbols, tf, end) -> list[str]`, for each non-excluded exam coin:
    - `month_problems(cfg, s, "spot/<tf>", months, end, EXCHANGE_SOURCES, "")`;
    - plus `missing_days(cfg, s, "spot/<tf>", holdout_start, end, what="spot candles with trades", traded=True)`.

- [ ] **Step 1: Write the failing tests:**
  - `test_every_group_declares_exam_coverage`;
  - `test_spot_reader_window_ends_at_earliest_spot_end`: ETHUSDT spot/4h is cut after 2026-09-20 16:00, so `groups=columns.GROUPS` gives 2026-09-20, while the default gives `END`;
  - `test_spot_month_without_entry_named` (`"ETHUSDT spot/4h 2026-08: no manifest entry"`);
  - `test_zero_trade_spot_day_named` (`"ETHUSDT spot/4h 2026-08-20: no spot candles with trades"`). The same line comes when the candles are dropped instead;
  - `test_excluded_coin_skipped_in_exam` (`ETH` and `ETHUSDT`);
  - `test_non_reader_exam_ignores_spot_gaps` (Review Focus 3): with a cut-short spot and a zero-trade day, `exam_groups` for a non-reader gives `END` and no spot line, while a reader gets both;
  - `test_missing_spot_files_end_the_window_at_holdout_start`.
- [ ] **Step 2: Run them; they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `tests/test_spot_exam.py tests/test_exam.py tests/test_exam_groups.py`. All pass.
- [ ] **Step 5: Commit** `feat(exam): spot coverage for plugins that read spot (trades > 0 days)`.

### Task 10: Builder context

**Files:**
- Modify: `perp_lab/proposer/context.py`
- Test: `tests/test_context.py` (extend); `tests/test_plugin_columns_move.py` runs unchanged

**Interfaces:**
- Produces:
  - `BAR_COLUMNS` gains the five rows of the spec's "Plugin-facing interface" table, verbatim, after `oi_value`. `PLUGIN_COLUMNS` follows from them.
  - `_spot_text(cfg) -> str`: the spec's "Builder integration" paragraph, verbatim. The bracketed sentence is rendered as `Spot columns are NaN on every bar for: none.`, or as the exclusions written as `{COIN}{quote}`, comma-separated, in config order.
  - `_api(cfg)` puts that paragraph after `BAR_COLUMNS`, and `static_context` passes `cfg`.
  - `_piles` sentence, exactly: "The synthetic bars' taker, open-interest, volume and spot columns are random walks, their `basis` has slow and fast mean-reverting parts, their spot columns are NaN over two stretches, their `btc_` columns are a synthetic BTC that moves with the coin, and they have no `slippage` column."

- [ ] **Step 1: Write the failing tests:**
  - `test_spot_rows_in_bar_table`;
  - `test_clamp_sentence_exact` (`"F = P + clamp(0.01% − P, −0.05%, +0.05%)"`);
  - `test_band_phrases`: `"inside that band funding says nothing about the premium"`, `"Outside the band F = P − 0.05% (above) or P + 0.05% (below)"` and `"an idea built on that average is a funding idea"`;
  - `test_spot_exclusions_line_from_config`: `[]` gives `"for: none."`; `["FIL", "AAVEUSDT"]` gives `"for: FILUSDT, AAVEUSDT."`;
  - `test_perp_only_and_paper_phrases` (`"The harness trades the perp only"`, `"can't be paper traded yet"`);
  - `test_plugin_columns_hold_spot`;
  - `test_piles_mentions_spot_and_basis`;
  - `test_static_context_under_cap`.
- [ ] **Step 2: Run them; they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `tests/test_context.py tests/test_plugin_columns_move.py tests/test_prompt.py tests/test_human_ideas.py`. All pass.
- [ ] **Step 5: Commit** `feat(proposer): tell the builder about the spot columns and basis`.

### Task 11: Calibrate the synthetic spot stream — LEAD ONLY (not a subagent)

The lead does this task itself. It needs Task 2's targets and real-data readiness, and its outcome may need the lead's ruling. It comes before any spot trial (X5).

**Files:**
- Modify: `perp_lab/data/spot.py` (the constants), `perp_lab/validate/synthetic_market.py` (`MARKET_VERSION = 5`), `docs/research/spot-inventory/README.md` (a "Calibration" section)
- Test: `tests/test_spot_calibration.py` (new), `tests/test_synthetic_market.py` (version 5)

- [ ] **Step 1: Check readiness on the real data.** From the main checkout root, with the worktree's code and config: `columns.column_group_problems(cfg, cfg.symbols, tf)` must be `[]` at 15m, 1h and 4h. If it isn't, fix `known_gaps` or `exclusions` first, in their own commit.
- [ ] **Step 2: Write the failing test** `test_synthetic_tails_within_25pct_of_targets`.
  - T comes from `docs/research/spot-inventory/calibration_targets.csv`.
  - S comes from `causality.check_bars()`, with Task 2's definitions.
  - For each of `level_p1`, `level_p99`, `mean24_p1` and `mean24_p99`, it asserts `abs(S − T) <= 0.25 * abs(T − T_p50)`, where `T_p50` is the same statistic's p50.
  - `test_market_version_is_5`.
- [ ] **Step 3: Apply the spec's rule** with a scratch script in the scratchpad:
  1. `BASIS_C` = the midpoint of T's `mean24_p1` and `mean24_p99`.
  2. `BASIS_SLOW_SD` ×= the ratio of T's and S's mean24 p99−p1.
  3. Recompute S. `BASIS_FAST_SD` and both ends of `BASIS_JUMP` ×= the same ratio for the level.
  4. `SPOT_SHARE` = T's `share_p50`.
- [ ] **Step 4: Run the test.** If it fails, repeat steps 2–3 once. If it still fails, the lead rules and records the ruling in the README; no spot trial runs before then.
- [ ] **Step 5: Run the full suite.** Task 4's property tests must still hold; their thresholds scale with the constants. Record S before and after, and the final constants, in the README.
- [ ] **Step 6: Commit** `feat(spot): calibrate the synthetic spot stream to the inventory (market v5)`.

### Task 12: Documents

**Files:**
- Modify:
  - `README.md`: the data list and layout (`data/<SYMBOL>/spot/<tf>/`); `lab backfill --datasets spotKlines`; readiness and the exam for spot readers; the paper caveat; the 2020–21 spot maintenance gaps; the zero-fee BTC period as Task 2 found it, or no mention;
  - `docs/superpowers/specs/2026-10-19-proposer-design.md`: add `## Amended: spot columns (2026-11-06)`, like the BTC reference amendment.

- [ ] **Step 1: Edit both.** Every claim must be true of the code as built in Tasks 1–11, and of the inventory's numbers.
- [ ] **Step 2: Run the full suite once.** Green except the 3 known failures.
- [ ] **Step 3: Commit** `docs: spot columns in the README and the proposer spec`.

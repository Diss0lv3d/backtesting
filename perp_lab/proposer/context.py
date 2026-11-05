"""The static context: what the model reads first on every call, the same for a whole session.

Built in code from the repo's own files and config (spec "What the model sees", item 1), so the API it shows
is the API the sandbox runs, and every number in it is the one the harness uses: costs and spans from the
config, the Deflated Sharpe bars from `perp_lab.dsr`, the checks' limits from the proposer's settings. Nothing
here reads data/, runs/ or lab.db, so it carries no time of a run, no run id and no statistic of real bars.

It goes on the command line as `--system-prompt`, and Linux refuses a single argument over 128 KiB, hence
the cap.
"""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

import perp_lab
from perp_lab.dsr import sharpe_bar
from perp_lab.proposer.assemble import ALLOWED_IMPORTS, MAX_BYTES
from perp_lab.proposer.contract import describe
from perp_lab.proposer.settings import ProposerConfig

STATIC_MAX_BYTES = 100_000
DSR_TABLE_N = (7, 10, 20, 30, 50, 100, 200, 500, 1000)
ROOT = Path(perp_lab.__file__).parents[1]
LESSONS = "perp_lab/proposer/context/lessons.md"


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def fence(text: str, lang: str = "") -> str:
    """A fenced block longer than any backtick run inside it, so no source can close it early."""
    ticks = "`" * max(3, 1 + max((len(m) for m in re.findall(r"`+", text)), default=0))
    return f"{ticks}{lang}\n{text.rstrip(chr(10))}\n{ticks}"


def _bp(fraction: float) -> str:
    return f"{round(fraction * 1e4, 6):g}"


def _months(n: int) -> str:
    return {1: "one-month", 2: "two-month", 3: "three-month"}.get(n, f"{n}-month")


def oos_span(cfg) -> tuple[pd.Timestamp, int, int]:
    """(OOS start, folds, OOS days), as the walk-forward cuts them (validate.walkforward.make_folds)."""
    wf = cfg["walkforward"]
    start = ts = cfg.history_start + pd.DateOffset(months=wf["train_months"])
    folds = 0
    while ts + pd.DateOffset(months=wf["test_months"]) <= cfg.holdout_start:
        ts += pd.DateOffset(months=wf["test_months"])
        folds += 1
    return start, folds, (ts - start).days


LAB = """\
## The lab

Perp Lab is a backtesting harness for crypto perpetual futures. You propose strategy plugins: one Python
module each, which the harness runs, never you. You have no tools. Each reply is one proposal: a new idea, a
refinement of an open idea, or a port of an idea to another timeframe, with the plugin's source.

The harness checks the proposal in code (the reply contract, the idea rules, the plugin's source), runs a
preflight on synthetic bars (lookahead, and overlap with plugins already judged), and then judges the plugin: a
walk-forward over real research bars, a random-entry benchmark with the plugin's own exits, a parameter
sensitivity check and the Deflated Sharpe gate. You never see the bars, the runs, the trades or the judge's
output. You see the record: each idea's card, each proposal's verdict with its diagnostics (aggregates only),
and the source of each open idea's champion.

The bars after the research span are a holdout that no run you cause reads. A promoted plugin waits for a
human's exam on the holdout, and each family of plugins gets one read. The goal is a mechanism that pays after
costs and survives that exam. A fit to the research span fails the exam and spends the read."""

BAR_COLUMNS = """\
`prepare(bars)` gets one coin's closed bars as a DataFrame indexed by each bar's open time (UTC):

| column | what | unit |
|---|---|---|
| open_time | the bar's open | ms since the Unix epoch, UTC |
| close_time | the bar's last millisecond | ms since the Unix epoch, UTC |
| open, high, low, close | trade prices | USDT |
| volume | volume traded | coins (base asset) |
| quote_volume | volume traded | USDT |
| trades | number of trades | count |
| taker_buy_volume | volume bought by takers (the rest was sold by takers) | coins |
| taker_buy_quote_volume | volume bought by takers | USDT |
| funding_charge | funding rate settled inside this bar, 0 if none | fraction of notional |
| funding_last | the last funding rate settled by the bar's close | fraction of notional per settlement |
| funding_interval_h | hours between settlements for funding_last | hours |
| oi | open interest at the bar's close (below); NaN before 2021-12-01, or 2020-09-01 for BTC | coins |
| oi_value | open interest at the bar's close (below); NaN before 2021-12-01, or 2020-09-01 for BTC | USDT |
| slippage | not available to plugins: real bars carry it, but the checks' synthetic bars don't, so a plugin that reads it fails the lookahead check | none |
| regime | the coin's 4h regime label (below) | text |
| regime_adx | the 4h ADX behind the label | ADX points |

`regime` is trending, ranging, high_vol, transition, or unknown during warm-up, from the coin's 4h bars; a 4h
label is known only once its 4h bar closes. A value is NaN where nothing is known yet (before a coin's first
funding settlement, or during an indicator's warm-up). `oi` and `oi_value` come from Binance's 5-minute
open-interest snapshots: a bar carries the last snapshot published by its close, a snapshot counting as published 5
minutes after its stamp. They start on 2021-12-01 for every coin but BTC, which starts on 2020-09-01, so they are
NaN on every earlier bar. A plugin that needs them trades nothing before then, and those flat days still count in
its OOS record, diluting its Sharpe. A plugin that reads them must handle NaN. Their level differs by coin and
grows over the years, so read them as a change or a ratio rather than a level."""


def _plugin_columns(table: str) -> tuple[str, ...]:
    """The names in the table's first column, but slippage: it is there only to say a plugin can't read it."""
    cells = [ln.split("|")[1] for ln in table.splitlines() if ln.startswith("| ")][1:]     # [0] is the header
    return tuple(n for cell in cells for n in (x.strip() for x in cell.split(",")) if n != "slippage")


# The columns a human's card may list (human.py, the Ideas page's form): read from the table the model reads.
PLUGIN_COLUMNS = _plugin_columns(BAR_COLUMNS)


def _api() -> str:
    return "\n\n".join([
        "## The plugin API",
        "A plugin is a module with a docstring and exactly one `Strategy` subclass. The harness names the module, "
        "writes the docstring's first line from your hypothesis and, for a refinement, sets `PARENT`; your source "
        "must not. `params`, `grid`, `timeframes` and `session` must be literals. The engine trades one coin at a "
        "time, and `ctx.symbol` names it. Two modules define everything a plugin may use:",
        "### perp_lab/strategy.py", fence(_read("perp_lab/strategy.py"), "python"),
        "### perp_lab/indicators.py", fence(_read("perp_lab/indicators.py"), "python"),
        "### Bar columns", BAR_COLUMNS,
        "### Imports",
        f"A plugin may import only these modules: {', '.join(ALLOWED_IMPORTS)}.",
    ])


def _examples() -> str:
    return "\n\n".join([
        "## Two example plugins",
        "Both were written by the human and judged and killed; the record has their verdicts. `donchian_atr` "
        "declares two timeframes; a proposed plugin declares exactly one.",
        "### strategies/flow_shock_fade.py", fence(_read("strategies/flow_shock_fade.py"), "python"),
        "### strategies/donchian_atr.py", fence(_read("strategies/donchian_atr.py"), "python"),
    ])


def _costs(cfg) -> str:
    c, m, a = cfg["costs"], cfg["costs"]["slippage_model"], cfg["account"]
    cap = a["leverage"] * a.get("max_margin_frac", 1.0)
    return "\n\n".join(["## The cost model", "\n".join([
        f"- Fees: taker {_bp(c['taker'])} bp of notional per fill (entries and exits at the open, stops), maker "
        f"{_bp(c['maker'])} bp (targets).",
        f"- Slippage, a cost the engine charges on every taker fill, per coin and bar: min({m['max_bps']:g}, "
        f"{c['slippage_bps']:g} + {m['spread_bps_at_100m']:g} × (ADV / 100M USDT)^−{m['alpha']:g}) bp, where ADV "
        f"is the coin's mean daily USDT volume over the {m['adv_days']} UTC days before the bar's day "
        f"({m['max_bps']:g} bp when it is unknown). A target fill and a liquidation pay none. Only the engine "
        "charges it: a plugin can't read it (see the bar table).",
        "- Funding, from the real settled rates: at each settlement a long pays rate × notional and a short "
        "receives it (a negative rate reverses that). A bar's settlements are charged at its open to the position "
        "held into that open: an exit filled at that open pays, an entry filled there does not.",
        "- Fills: a signal at bar t's close fills at bar t+1's open. A stop is a stop-market order: filled at the "
        "stop, or at the open when a bar gaps through it. A target is a resting limit, filled only when price "
        "trades through it. When one bar's range holds both, the stop wins.",
        f"- Sizing: each coin trades its own {a['initial_equity']:,.0f} USDT book. An entry is sized so that its "
        f"stop loses {a['risk_per_trade']:.0%} of that wallet (1 R), with notional at most {cap:g}× the wallet, on "
        "isolated margin. An entry is skipped when the open gaps through its stop, or when its liquidation price "
        "would come before its stop. The OOS portfolio is the equal-weight mean of the coins' daily returns, and "
        "Sharpe ratios are annualised with √365.",
    ])])


def _universe(cfg, pc: ProposerConfig) -> str:
    start, folds, days = oos_span(cfg)
    wf = cfg["walkforward"]
    coins = cfg["universe"]
    quote = cfg.get("quote", "USDT")
    lines = [
        f"- {len(coins)} coins, each a {quote}-margined perpetual: {', '.join(coins)}.",
        f"- Research bars from {cfg.history_start:%Y-%m-%d} to {cfg.holdout_start:%Y-%m-%d} (exclusive). A coin "
        "listed later starts at its listing.",
        f"- The walk-forward: OOS from {start:%Y-%m} in {folds} {_months(wf['test_months'])} folds, {days:,} days "
        "in all. Each fold "
        f"picks one grid combination for every coin: the one with the best mean per-coin Sharpe over the previous "
        f"{wf['train_months']} months. A combination with fewer than {wf['min_train_trades_per_coin']:g} entries "
        "per coin listed in that window is never picked. The picked combination trades the next "
        f"{wf['test_months']} months, and the folds' OOS months are stitched into the OOS record.",
        f"- Timeframes a plugin may trade: {', '.join(pc.timeframes)}.",
    ]
    if cfg.get("universe_rule"):
        lines.append(f"- {cfg['universe_rule']}")
    return "\n\n".join(["## The universe and spans", "\n".join(lines)])


def _gates(cfg, pc: ProposerConfig) -> str:
    v, j = cfg["validation"], cfg["judge"]
    _, _, days = oos_span(cfg)
    pct, step = v["random_percentile"], max(abs(s) for s in v["sensitivity_steps"])
    bars = [f"{sharpe_bar(n, days, j['dsr_min']):.2f}" for n in DSR_TABLE_N]
    table = "\n".join([
        "| N | " + " | ".join(f"{n:,}" for n in DSR_TABLE_N) + " |",
        "|---|" + "---|" * len(DSR_TABLE_N),
        "| bar | " + " | ".join(bars) + " |",
    ])
    return "\n\n".join([
        "## The gates, and what a trial is",
        "First, a lookahead check runs the plugin on synthetic bars cut at many points. A feature or signal at bar "
        "t that changes when later bars are added kills the plugin for lookahead (pile broken).",
        "Then `lab judge` promotes a plugin only if every check passes on its OOS record:",
        "\n".join([
            "- the OOS Sharpe is above 0;",
            f"- it is above the p{pct} of {v['random_seeds']} random-entry runs: entries at random bars, at the "
            "plugin's own entry rate per coin, with its own stop, target and exit rules, each side drawn with the "
            "plugin's own OOS long share;",
            "- it is not a narrow parameter island: moving each numeric parameter of the walk-forward's most-picked "
            f"combination by ±{step:.0%} must not cut the OOS Sharpe by more than {v['island_drop']:.0%};",
            "- it has at least the minimum number of OOS trades, which scales with the coins (each verdict gives it "
            "as `diagnostics.gates.min_oos_trades`);",
            "- no OOS entry falls outside a session the plugin declares;",
            f"- its Deflated Sharpe is at least {j['dsr_min']:g}, with N the number of trials in the lab so far;",
            "- a refinement whose parent passed beats the parent in at least "
            f"{j['parent_beat_frac']:.0%} of paired block-bootstrap resamples of their OOS daily returns.",
        ]),
        "The judge also re-runs the walk-forward's picked parameters with fees and slippage set to zero, funding "
        "kept, and the random-entry benchmark the same way: the verdict's `diagnostics.before_costs`. That pass is "
        "information only; it decides no gate.",
        "A trial is one (plugin version, timeframe) whose code was about to run on real research bars. It counts "
        "from that moment, whatever happens next (a crash, a timeout, a kill). A lookahead kill and a refused "
        "plugin are not trials, and judging the same bytes at the same timeframe again adds nothing. N counts "
        "every trial in the lab, the human's included, and every trial raises the Deflated Sharpe bar for every "
        f"plugin judged after it. You may spend {pc.trials_total} trials in all.",
        "The bar: the annualised OOS Sharpe at which a track of normal daily returns over the "
        f"{days:,}-day OOS span reaches a Deflated Sharpe of {j['dsr_min']:g}. Skew and fat tails raise it. The "
        "record ends with today's N and bar.",
        table,
    ])


def _piles(cfg, pc: ProposerConfig) -> str:
    pct = cfg["validation"]["random_percentile"]
    om, cm, per = f"{pc.overlap_max:g}", f"{pc.corr_max:g}", pc.proposals_per_idea
    return "\n\n".join([
        "## Piles, ideas and the idea rules",
        "Every judged plugin is filed in a pile, in code, from its verdict:",
        "\n".join([
            "| pile | rule |",
            "|---|---|",
            "| promoted | decision `promote` |",
            f"| close call | killed, but net OOS Sharpe > 0 and above the net random-entry p{pct} (it failed on the "
            "Deflated Sharpe or a narrow island) |",
            "| signal before costs | not the above, and the gross OOS Sharpe is above both 0 and the gross "
            f"random-entry p{pct} |",
            "| no signal | none of the above, with at least the gate's minimum OOS trades |",
            "| broken | killed for lookahead or a plugin error, refused, an infrastructure error, or too few OOS "
            "trades |",
        ]),
        "A promotion the human withdraws is filed as a close call.",
        "After its trial, a judged plugin is checked against its verdict's overlap block. Against a run of a closed "
        f"idea, a position overlap ≥ {om} or an OOS daily-return correlation ≥ {cm} files it as a duplicate of "
        "that idea, and its pile doesn't count toward its own idea's best pile, unless that pile is better than "
        f"the closed idea's best. A mirror of any judged run (mirror overlap ≥ {om}, or correlation ≤ −{cm}) is a "
        "duplicate whatever its pile. A refinement or a port is exempt against its port group: the source idea "
        "(the idea ported, or the refined idea itself if it ports nothing) and every port of that source.",
        "An idea's champion is the plugin a refinement gets as `PARENT`: the idea's most recent promotion still "
        "waiting for its exam, otherwise its best plugin by pile, then by net OOS Sharpe. Never a broken one.",
        "An idea's status:",
        "\n".join([
            "- open: a new idea; one with only broken attempts; or one whose best pile is signal before costs, close "
            f"call or promoted, with judged plugins left ({per} per idea). Only open ideas take refinements, and "
            f"an open idea at a timeframe other than {', '.join(pc.timeframes)} (a human trial) takes ports only.",
            "- dead: its best pile is no signal. One faithful test that showed no signal before costs ends an idea.",
            f"- exhausted: it used its {per} judged plugins. It keeps its best pile and gets no more trials.",
            "- passed or failed: its family's holdout read was spent on an exam with that outcome.",
            "- duplicate: a new idea whose only judged plugins were filed as duplicates.",
            "- excluded: closed by the human before any trial.",
        ]),
        "The idea rules, checked in code:",
        "\n".join([
            "- A new idea needs a complete card and a title no idea in the record has, whatever the case.",
            "- A refinement names an open idea with judged plugins left, and keeps its timeframe. The harness sets "
            "`PARENT` to the idea's champion, so the refinement is judged against it.",
            "- A port names an idea that is open or exhausted, whose best pile is signal before costs or close call, "
            "and a timeframe that idea has no port on yet. A promoted idea gets no port. The port is a new idea at "
            "the new timeframe, with the idea's card and no `PARENT`.",
            f"- A plugin trades exactly one timeframe, one of {', '.join(pc.timeframes)}: its `timeframes` is "
            "exactly that one.",
            f"- The source has no `PARENT`, exactly one `Strategy` subclass, literal `params`, `grid` and "
            f"`timeframes`, only the allowed imports, at most {pc.max_grid_combos} grid combinations, and at most "
            f"{MAX_BYTES // 1024} KB.",
            "- Preflight runs it on synthetic bars only: the lookahead check, then the overlap of positions held "
            "(each plugin at its defaults) with every judged plugin. A near-copy (overlap ≥ "
            f"{om}) of a judged plugin outside the proposal's port group is refused: a closed idea's outright, an "
            "open idea's with the advice to refine or port that idea instead. A mirror (the same overlap with one "
            "side flipped) of any judged plugin is refused. The synthetic bars' taker, open-interest and volume "
            "columns are random walks, and they have no `slippage` column.",
            f"- A refused reply comes back with the errors. A proposal gets {pc.fixups_per_proposal} fix-ups; then it "
            "is dropped, with no trial.",
        ]),
    ])


def static_context(cfg, pc: ProposerConfig) -> str:
    text = "\n\n".join([
        "# Perp Lab",
        LAB,
        _api(),
        _examples(),
        _costs(cfg),
        _universe(cfg, pc),
        _gates(cfg, pc),
        _piles(cfg, pc),
        "## Lessons from the human's first trials\n\n" + _read(LESSONS).rstrip("\n"),
        "## Your reply\n\n" + describe(pc.timeframes),
    ]) + "\n"
    size = len(text.encode("utf-8"))
    if size > STATIC_MAX_BYTES:
        raise ValueError(f"the static context is {size} bytes; the cap is {STATIC_MAX_BYTES}")
    return text

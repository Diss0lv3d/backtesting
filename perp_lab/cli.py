"""lab CLI: backfill | tail | backtest | walkforward | paper | strategies"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import typer

from perp_lab import runlock
from perp_lab.config import load_config

app = typer.Typer(add_completion=False, no_args_is_help=True)
log = logging.getLogger("lab")

CfgOpt = typer.Option(None, "--config", "-c", help="path to lab.yaml")


@contextmanager
def _sandbox_required():
    """Plugin code runs only in the sandbox: where this machine can't provide one, say why and exit
    1, without a traceback."""
    from perp_lab.sandbox.errors import SandboxUnavailable
    try:
        yield
    except SandboxUnavailable as e:
        typer.secho(f"sandbox unavailable: {e}", fg="red")
        raise typer.Exit(1)


def _syms(cfg, symbols: Optional[str]) -> list[str]:
    if not symbols:
        return cfg.symbols
    q = cfg.get("quote", "USDT")
    return [s if s.endswith(q) else f"{s}{q}" for s in symbols.split(",")]


@app.command()
def backfill(symbols: Optional[str] = typer.Option(None, help="comma list, e.g. BTC,ETH"),
             tfs: Optional[str] = typer.Option(None, help="comma list of timeframes"),
             datasets: Optional[str] = typer.Option(None, help="klines,fundingRate,metrics"),
             start: Optional[str] = None, workers: Optional[int] = None, config: Optional[str] = CfgOpt):
    """Backfill from data.binance.vision → parquet (idempotent, resumable)."""
    from perp_lab.data.vision import backfill as bf
    cfg = load_config(config)
    stats = bf(cfg, _syms(cfg, symbols), tfs.split(",") if tfs else cfg["timeframes"],
               datasets.split(",") if datasets else cfg["backfill"]["datasets"],
               start=pd.Timestamp(start, tz="UTC") if start else None, workers=workers)
    typer.echo(json.dumps(stats))
    if stats["error"]:
        raise typer.Exit(1)


@app.command()
def tail(symbols: Optional[str] = None, tfs: Optional[str] = None, config: Optional[str] = CfgOpt):
    """Stitch parquet → now via ccxt fetchOHLCV."""
    from perp_lab.data.live import make_exchange, tail as do_tail
    cfg = load_config(config)
    ex = make_exchange()
    failed = []
    for s in _syms(cfg, symbols):
        for tf in (tfs.split(",") if tfs else cfg["timeframes"]):
            try:
                n = do_tail(cfg, s, tf, ex)
            except Exception as e:
                typer.secho(f"{s} {tf}: failed: {type(e).__name__}: {e}", fg="red")
                failed.append(f"{s} {tf}")
                continue
            typer.echo(f"{s} {tf}: +{n} candles")
    if failed:
        typer.secho("tail failed for: " + ", ".join(failed), fg="red")
        raise typer.Exit(1)


@app.command()
def strategies():
    """List strategy plugins and their hypotheses."""
    from perp_lab.plugins import PluginSource
    from perp_lab.strategy import list_plugins
    for n in list_plugins():
        s = PluginSource.read(n)
        typer.echo(f"{n} [{s.hash[:10]}] tfs={s.meta.timeframes}\n  {s.meta.hypothesis}")


@app.command()
def backtest(strategy: str, tf: str = "1h", symbols: Optional[str] = None,
             params: str = typer.Option("{}", help='JSON params, e.g. \'{"n": 40}\''),
             config: Optional[str] = CfgOpt):
    """Single fixed-param backtest over the research span. This is IN-SAMPLE by definition."""
    from perp_lab.data.store import load_bars, research_symbols
    from perp_lab.engine.backtest import EngineParams
    from perp_lab.engine.metrics import daily_returns, portfolio_returns, summarize
    from perp_lab.plugins import PluginSource
    from perp_lab.runs import new_run_id, repo_commit, write_run
    from perp_lab.sandbox import client

    cfg = load_config(config)
    src = PluginSource.read(strategy)
    p = json.loads(params)
    per, trades = {}, []
    with runlock.plugin_runs(cfg):
        for s in research_symbols(cfg, _syms(cfg, symbols), tf):
            bars = load_bars(cfg, s, tf)
            with _sandbox_required(), client.Batch(cfg, src, s, tf, bars) as batch:
                r = batch.backtest({**src.meta.params, **p}, EngineParams.from_config(cfg, s))
            per[s] = daily_returns(r.equity, r.time)
            trades += r.trades
    tr = pd.DataFrame(trades)
    m = summarize(portfolio_returns(per), tr)
    rid = new_run_id()
    rec = {"run_id": rid, "strategy": strategy, "strategy_hash": src.hash,
           "hypothesis": src.meta.hypothesis, "repo_commit": repo_commit(), "params": {**src.meta.params, **p},
           "universe": list(per), "timeframes": [tf], "validation_mode": "IS",
           "date_start": str(cfg.history_start.date()), "date_end": str(cfg.holdout_start.date()),
           "metrics": m, "parent_run_id": None, "created_at": datetime.now(timezone.utc).isoformat(),
           "notes": "fixed-param full-span backtest: in-sample, not evidence"}
    d = write_run(cfg, rec, {"trades": tr.assign(source="backtest", run_id=rid) if len(tr) else tr,
                             "daily": pd.DataFrame(per)})
    typer.secho("IN-SAMPLE (not evidence of anything):", dim=True)
    typer.secho(json.dumps({k: round(v, 4) if isinstance(v, float) else v for k, v in m.items()}), dim=True)
    typer.echo(f"run: {d}")


@app.command()
def walkforward(strategy: str, tf: str = "1h", symbols: Optional[str] = None,
                seeds: Optional[int] = typer.Option(None, help="random-entry seeds (default: config)"),
                workers: Optional[int] = None, notes: str = "",
                parent_run: Optional[str] = typer.Option(None, help="parent run_id (lineage)"),
                config: Optional[str] = CfgOpt):
    """Walk-forward + sensitivity + random-entry + MC + regime split. OOS headline only."""
    from perp_lab.plugins import UndeclaredTimeframe
    from perp_lab.validate.pipeline import validate_strategy
    cfg = load_config(config)
    try:
        with runlock.plugin_runs(cfg), _sandbox_required():
            rec, report = validate_strategy(cfg, strategy, tf, _syms(cfg, symbols), seeds, workers, notes,
                                            parent_run_id=parent_run)
    except UndeclaredTimeframe as e:            # a trial at a timeframe it doesn't declare: nothing ran
        typer.secho(f"refused: {e}", fg="red")
        raise typer.Exit(2)
    o, i = rec["metrics"], rec["is_metrics"]
    typer.secho(f"{strategy} @ {tf}  →  {rec['status'].upper()}", bold=True,
                fg="red" if rec["status"] == "noise" else "green")
    typer.echo(f"OOS  Sharpe {o['sharpe']:.2f}  MaxDD {o['max_dd']:.1%}  Return {o['total_return']:.1%}  "
               f"PF {o['profit_factor']:.2f}  trades {o['trades']}")
    typer.secho(f"IS   Sharpe {i['sharpe']:.2f}  MaxDD {i['max_dd']:.1%}  (reference only)", dim=True)
    for f in rec["fail_reasons"]:
        typer.echo(f"  ✗ {f}")
    typer.echo(f"run: {rec['run_dir']}/report.md")


@app.command()
def paper(from_run: str = typer.Argument(..., help="walk-forward run_id approved for paper in the dashboard"),
          symbols: Optional[str] = None, max_bars: Optional[int] = None, config: Optional[str] = CfgOpt):
    """Forward-test an APPROVED walk-forward run on live closed candles (modal params)."""
    import sqlalchemy as sa

    from perp_lab.paper.runner import PaperRunner
    from perp_lab.sandbox import client
    from perp_lab.runs import data_id, data_identity, new_run_id, repo_commit
    from perp_lab.store.db import current_status, engine, record_run, runs
    from perp_lab.plugins import PluginSource
    cfg = load_config(config)
    with engine(cfg).connect() as c:
        r = c.execute(sa.select(runs).where(runs.c.run_id == from_run)).mappings().first()
    if r is None:
        raise typer.BadParameter(f"unknown run {from_run}")
    if current_status(cfg, r["strategy"], r["timeframe"], r["verdict"]) != "paper":
        typer.secho(f"{r['strategy']} @ {r['timeframe']} is not approved for paper. "
                    "Approve it in the dashboard (only walk-forward candidates qualify).", fg="red")
        raise typer.Exit(2)
    params = (r["metrics"] or {}).get("modal_params") or json.loads(
        (cfg.runs_dir / from_run / "summary.json").read_text())["sensitivity"]["base_params"]
    src = PluginSource.read(r["strategy"])
    rid = new_run_id()
    out = cfg.runs_dir / rid
    syms = _syms(cfg, symbols) if symbols else r["universe"]
    held = client.max_workers(cfg)
    if len(syms) > held:
        # One paper worker per coin. Which real fix to pick waits until a strategy first passes an exam.
        typer.secho(f"refused: {len(syms)} coins need {len(syms)} paper workers of {cfg['sandbox']['memory_mb']} MB; "
                    f"this machine holds {held}. Pass --symbols with at most {held} coins.", fg="red")
        raise typer.Exit(2)
    started = datetime.now(timezone.utc)
    record_run(cfg, {"run_id": rid, "strategy": r["strategy"], "strategy_hash": src.hash,
                     "hypothesis": src.meta.hypothesis, "params": params, "universe": syms,
                     "data": data_identity(cfg, syms), "data_id": data_id(data_identity(cfg, syms)),
                     "timeframes": [r["timeframe"]], "validation_mode": "paper", "repo_commit": repo_commit(),
                     "parent_run_id": from_run, "lineage_id": r["lineage_id"], "created_at": started.isoformat(),
                     "paper_started_at": started.isoformat(), "metrics": {},
                     "artifacts": {"trades": str(out / "trades.parquet")}}, out)
    typer.echo(f"paper run {rid} → {out}")
    pr = PaperRunner(cfg, src, params, r["timeframe"], syms, out)
    with _sandbox_required():
        asyncio.run(pr.run(max_bars=max_bars))


@app.command("paper-refresh")
def paper_refresh(run_id: str, config: Optional[str] = CfgOpt):
    """Recompute regime changes + paper_eligible for a paper run (fetches live 4h bars)."""
    from perp_lab.paper.tracking import refresh_paper_run
    typer.echo(json.dumps(refresh_paper_run(load_config(config), run_id), default=str))


@app.command()
def ingest(config: Optional[str] = CfgOpt):
    """Record any run directory not yet in the results store."""
    from perp_lab.store.db import ingest_runs
    added = ingest_runs(load_config(config))
    typer.echo(f"ingested {len(added)} run(s): {added}")


@app.command()
def link(parent: str, child: str, decision: str = typer.Option(..., help="promote | kill | manual"),
         reason: str = typer.Option(..., help="why — shown on the lineage edge"),
         config: Optional[str] = CfgOpt):
    """Record a hand-made lineage edge (Phase 3's judge writes these itself)."""
    from perp_lab.store.db import add_edge
    try:
        add_edge(load_config(config), parent, child, decision, reason, decided_by="human")
    except PermissionError as e:
        typer.secho(f"refused: {e}", fg="red")
        raise typer.Exit(1)
    typer.echo(f"{parent} → {child} [{decision}] {reason}")


@app.command("holdout-init")
def holdout_init(config: Optional[str] = CfgOpt):
    """Create the empty holdout audit log (once). Exams refuse to run without it."""
    from perp_lab.judge.budget import init_log
    try:
        p = init_log(load_config(config))
    except FileExistsError as e:
        typer.secho(str(e), fg="red")
        raise typer.Exit(1)
    typer.echo(f"created {p}")


@app.command()
def judge(strategy: str, tf: str = "4h", symbols: Optional[str] = None, workers: Optional[int] = None,
          proposer: bool = typer.Option(False, "--proposer", help="mark the judgement as the proposer's: a bare "
                                        "`lab exam` skips it, only `lab exam --id N` examines it"),
          config: Optional[str] = CfgOpt):
    """Promote or kill a plugin against its PARENT; promoted plugins queue for the holdout exam,
    which only `lab exam` runs."""
    from perp_lab.judge.judge import JudgeRefused, TransientFailure
    from perp_lab.judge.judge import judge as run_judge
    cfg = load_config(config)
    try:
        with runlock.plugin_runs(cfg), _sandbox_required():
            j = run_judge(cfg, strategy, tf, _syms(cfg, symbols) if symbols else None, workers,
                        judged_by="proposer" if proposer else None)
    except JudgeRefused as e:
        typer.secho(f"refused: {e}", fg="red")
        raise typer.Exit(2)
    except TransientFailure as e:
        typer.secho(f"transient sandbox failure ({e.kind}); the trial is registered; "
                    "run lab judge on the same file again", fg="yellow")
        raise typer.Exit(1)
    again = " (already judged)" if j.get("existing") else ""
    typer.secho(f"{j['strategy']} @ {j['timeframe']}  →  {j['decision'].upper()}{again}", bold=True,
                fg="green" if j["decision"] == "promote" else "red")
    for r in j["reasons"]:
        typer.echo(f"  ✗ {r}")
    dsr = "—" if j["dsr"] is None else f"{j['dsr']:.2f}"
    typer.echo(f"Deflated Sharpe {dsr} (N={j['n_trials']})   holdout: {j['holdout_status']} ({j['holdout_reason']})")
    typer.echo(f"verdict: {j['verdict_path']}")


@app.command("preflight")
def preflight_cmd(file: Path = typer.Argument(..., help="the plugin file; its name is the file's stem"),
                  tf: str = typer.Option(..., "--tf", help="the timeframe it would be judged at"),
                  config: Optional[str] = CfgOpt):
    """The judge's checks a plugin can fail before any trial, on synthetic bars only (nothing recorded):
    metadata, lookahead, a mirror of a judged plugin, and position overlap with every other plugin.
    Prints the JSON report; exits 0 (passed), 3 (failed a check) or 1 (no sandbox, or another error)."""
    from perp_lab import preflight as pf
    from perp_lab.sandbox.errors import SandboxUnavailable
    try:
        cfg = load_config(config)
        with runlock.plugin_runs(cfg):   # outermost only: the lock is not re-entrant
            report = pf.preflight(cfg, file, tf)
    except Exception as e:
        if not isinstance(e, SandboxUnavailable):
            log.exception("preflight of %s failed", file)        # the traceback, for humans, on stderr
        report = {**pf.blank_report(), "error": f"{type(e).__name__}: {e}"}
    typer.echo(json.dumps(report))
    raise typer.Exit(0 if report["ok"] else 1 if report["error"] else 3)


@app.command("verdict-refresh")
def verdict_refresh(strategy: str, tf: str = typer.Option(..., "--tf", help="the timeframe it was judged at"),
                    config: Optional[str] = CfgOpt):
    """Recompute a judgement's diagnostics from a walk-forward run on today's data and rules. The decision
    is unchanged and no trial is added."""
    from perp_lab.judge.judge import JudgeRefused, refresh_verdict
    cfg = load_config(config)
    try:
        with runlock.plugin_runs(cfg), _sandbox_required():
            j = refresh_verdict(cfg, strategy, tf)
    except JudgeRefused as e:
        typer.secho(f"refused: {e}", fg="red")
        raise typer.Exit(2)
    typer.echo(f"{j['strategy']} @ {j['timeframe']}: diagnostics from {j['diagnostics']['data']} data "
               f"(run {j['diagnostics_run_id']})")
    typer.echo(f"verdict: {j['verdict_path']}")


@app.command()
def withdraw(strategy: Optional[str] = typer.Argument(None, help="the plugin, by the name it was judged under"),
             tf: Optional[str] = typer.Option(None, "--tf", help="the timeframe it was judged at"),
             reason: str = typer.Option(..., help="why: recorded in withdrawals.jsonl next to the holdout log"),
             judgement: Optional[int] = typer.Option(None, "--id", help="the judgement id: picks it alone (several "
                                                     "versions pending, or a plugin file renamed since)"),
             config: Optional[str] = CfgOpt):
    """Withdraw a pending promotion before its holdout read: it is never examined, and its family
    keeps its one read. A human's decision on the walk-forward record; the proposer never runs it.
    Name it by PLUGIN and --tf, or by --id (a name or --tf given with it must match)."""
    from perp_lab.judge.budget import WithdrawRefused
    from perp_lab.judge.judge import withdraw as run_withdraw
    from perp_lab.store.db import get_judgement_by_id, pending_judgements
    cfg = load_config(config)

    def refuse(msg: str):
        typer.secho(f"refused: {msg}", fg="red")
        raise typer.Exit(2)
    if judgement is not None:
        j = get_judgement_by_id(cfg, judgement)
        if j is None:
            refuse(f"no judgement {judgement}")
        if strategy not in (None, j["strategy"]) or tf not in (None, j["timeframe"]):
            refuse(f"judgement {judgement} is {j['strategy']} @ {j['timeframe']}")
    elif strategy is None or tf is None:
        refuse("name the plugin and --tf, or pass --id")
    else:
        mine = [j for j in pending_judgements(cfg) if j["strategy"] == strategy and j["timeframe"] == tf]
        if not mine:
            refuse(f"no pending promotion of {strategy} @ {tf}")
        if len(mine) > 1:
            ids = ", ".join(f"{j['id']} ({j['strategy_hash'][:10]})" for j in mine)
            refuse(f"several versions of {strategy} @ {tf} are pending: {ids}; pass --id")
        j = mine[0]
    try:
        w = run_withdraw(cfg, j["id"], reason)
    except WithdrawRefused as e:
        refuse(str(e))
    typer.echo(f"{w['strategy']} @ {w['timeframe']}: withdrawn (judgement {w['id']}); family {w['lineage_id']} keeps "
               "its holdout read")
    typer.echo(f"verdict: {w['verdict_path']}")


# What the web-reviewed mark's lookup reads, and nothing else: a record from before the column has no such idea.
WEB_REVIEWED_VERSIONS = ("SELECT p.hash, p.timeframe FROM proposals AS p JOIN ideas AS i ON p.idea_id = i.id "
                         "WHERE i.web_reviewed")


def _web_reviewed(cfg, judgements: list[dict]) -> set[int]:
    """The ids of the judgements whose plugin version is a proposal of a web-reviewed idea in the proposer's record
    (referee-and-human-stops spec, the web_reviewed mark): their exam is weaker evidence. None when the proposer's
    settings don't load, it has no record, the record predates the mark, or it can't be read: `lab exam` isn't the
    proposer's, so it never fails for want of one.

    It only reads. No Record(): opening one creates and migrates tables. The record runs in WAL mode: while a
    session or the Ideas page holds it, its -wal and -shm files exist and a read-only open reads through them;
    without them record.db holds everything, and an immutable open reads it without creating them, which a
    read-only open would (SQLite 3.45, checked 2026-11-02)."""
    import sqlite3
    from contextlib import closing
    from perp_lab.proposer import context, settings
    if not judgements:
        return set()
    try:
        pc = settings.load(cfg)
    except settings.StartRefused:
        return set()
    path = (context.ROOT / pc.state_dir / "record.db").resolve()
    if not path.exists():
        return set()
    held = all(path.with_name(path.name + side).exists() for side in ("-wal", "-shm"))
    try:
        with closing(sqlite3.connect(f"{path.as_uri()}?{'mode=ro' if held else 'immutable=1'}", uri=True)) as c:
            if "web_reviewed" not in {row[1] for row in c.execute("PRAGMA table_info(ideas)")}:
                return set()
            versions = set(c.execute(WEB_REVIEWED_VERSIONS))
    except sqlite3.Error:
        return set()
    return {j["id"] for j in judgements if (j.get("strategy_hash"), j.get("timeframe")) in versions}


@app.command()
def exam(judgement: Optional[int] = typer.Option(None, "--id", help="examine this one judgement, whoever judged it"),
         config: Optional[str] = CfgOpt):
    """Attempt the holdout exam for every pending promotion (budget rules apply), except a proposer's:
    those wait for `lab exam --id N`. The only command that runs exams; the proposer never runs it."""
    from perp_lab.judge.judge import JudgeRefused, exam_pending
    from perp_lab.proposer.research import WEB_REVIEWED_CAVEAT
    cfg = load_config(config)
    try:
        done = exam_pending(cfg, only_id=judgement)
    except JudgeRefused as e:
        typer.secho(f"refused: {e}", fg="red")
        raise typer.Exit(2)
    if not done:
        typer.echo("no pending exams")
    web = _web_reviewed(cfg, [j for j in done if j.get("awaiting_review")])
    for j in done:
        if j.get("awaiting_review"):
            mark = " (web-reviewed)" if j["id"] in web else ""
            typer.echo(f"{j['strategy']} @ {j['timeframe']}: awaiting review{mark}: lab exam --id {j['id']}")
            if mark:
                typer.echo(f"  {WEB_REVIEWED_CAVEAT}")
            continue
        if j.get("in_progress"):
            typer.echo(f"{j['strategy']} @ {j['timeframe']}: in progress elsewhere (another `lab exam` is running)")
            continue
        err = f" ERROR: {j['error']}" if "error" in j else ""
        typer.echo(f"{j['strategy']} @ {j['timeframe']}: {j['holdout_status']} ({j['holdout_reason']}){err}")
        for line in j.get("detail", []):          # e.g. the months of an incomplete_data wait
            typer.echo(f"  {line}")
    if any("error" in j for j in done):
        raise typer.Exit(1)


@app.command()
def propose(days: float = typer.Option(7, help="stop after this many days"),
            trials: Optional[int] = typer.Option(None, help="stop after this many trials (0: the start checks only)"),
            config: Optional[str] = CfgOpt):
    """Run a proposer session (the unattended strategy builder) until D days, N trials or a stop condition."""
    from perp_lab.proposer import context, loop, settings
    from perp_lab.proposer.record import Record
    if Path.cwd().resolve() != Path(context.ROOT).resolve():
        typer.secho(f"refused: run `lab propose` from the repo root ({context.ROOT})", fg="red", err=True)
        raise typer.Exit(2)
    try:
        cfg = load_config(config)
        pc = settings.load(cfg)
        reason = loop.run_session(cfg, pc, days=days, trials=trials, repo=context.ROOT)
    except settings.StartRefused as e:
        typer.secho(f"refused: {e}", fg="red", err=True)
        raise typer.Exit(2)
    typer.echo(f"stopped: {reason}")
    sid = Record(context.ROOT / pc.state_dir).last_session_id()
    if sid is not None:
        typer.echo(f"digest: {context.ROOT / pc.state_dir / 'sessions' / f'{sid}.md'}")


@app.command()
def ideas(config: Optional[str] = CfgOpt):
    """Print the proposer's record by pile: passed, promoted, close call, signal before costs, open, closed."""
    from perp_lab.proposer import context, piles, settings
    from perp_lab.proposer.record import Record
    try:
        pc = settings.load(load_config(config))
    except settings.StartRefused as e:
        typer.secho(f"refused: {e}", fg="red", err=True)
        raise typer.Exit(2)
    state = context.ROOT / pc.state_dir
    if not (state / "record.db").exists():
        typer.echo("no record yet")
        return
    rec = Record(state)
    rows = rec.ideas()
    if not rows:
        typer.echo("no record yet")
        return
    props = rec.proposals()
    by_idea: dict[int, list[dict]] = {}
    for p in props:
        by_idea.setdefault(p["idea_id"], []).append(p)
    groups: dict[str, list[str]] = {g: [] for g in piles.GROUPS}
    for i in rows:
        mine = by_idea.get(i["id"], [])
        judged = [p for p in mine if p.get("judge_exit") is not None]
        best = piles.best_pile([piles.pile(p.get("verdict")) for p in judged if p.get("duplicate_of") is None])
        tag = ((" (research)" if i["origin"] == "research" else "")
               + (" (web-reviewed)" if i.get("web_reviewed") else ""))     # as the Ideas page's tag
        groups[piles.idea_group(i, mine)].append(
            f"#{i['id']} {i['title']}{tag} \u2014 {i['timeframe']}, {i['status']}, {best or 'none'}, "
            f"judged {len(judged)}")
    for g in piles.GROUPS:
        if groups[g]:
            typer.echo(f"{g} ({len(groups[g])})")
            for line in groups[g]:
                typer.echo(f"  {line}")


idea_app = typer.Typer(add_completion=False, no_args_is_help=True,
                       help="Your own changes to the proposer's record: add, exclude or flag an idea.")
app.add_typer(idea_app, name="idea")


def _refuse(msg: str):
    typer.secho(f"refused: {msg}", fg="red", err=True)
    raise typer.Exit(2)


def _proposer_record(config: Optional[str], missing: str):
    """The record and the proposer's settings; a refusal, never a new record, when record.db doesn't exist."""
    from perp_lab.proposer import context, settings
    from perp_lab.proposer.record import Record
    try:
        pc = settings.load(load_config(config))
    except settings.StartRefused as e:
        _refuse(str(e))
    state = context.ROOT / pc.state_dir
    if not (state / "record.db").exists():
        _refuse(missing)
    return Record(state), pc


@idea_app.command("add")
def idea_add(file: Path = typer.Argument(..., help="a YAML file: one card (its fields, title, mechanism, "
                                         "why_it_persists, signal, trades_per_coin_per_year, likely_failure, "
                                         "columns, and timeframe) or a list of them"),
             config: Optional[str] = CfgOpt):
    """Add your own idea cards: one, or a list, all or none. Each is checked as the model's new ideas are, and
    none goes in before the seed."""
    import yaml

    from perp_lab.proposer import human
    typer.secho(human.WARNING, fg="yellow")
    rec, pc = _proposer_record(config, human.NOT_SEEDED)
    try:
        entry = yaml.safe_load(file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
        _refuse(f"can't read {file}: {e}")
    many = isinstance(entry, list)
    try:
        ids = human.add_all(rec, pc, entry) if many else [human.add(rec, pc, entry)]
    except human.Refused as e:
        _refuse("\n  ".join(["no idea was added:" if many else "the idea was not added:", *e.errors]))
    for i in ids:
        typer.echo(f"added idea #{i}")


@idea_app.command("exclude")
def idea_exclude(idea: int = typer.Argument(..., help="the idea's id"),
                 reason: str = typer.Option(..., help="why: the model reads it on the idea's closed line"),
                 config: Optional[str] = CfgOpt):
    """Close an open, exhausted or dead idea for good."""
    from perp_lab.proposer import human
    typer.secho(human.WARNING, fg="yellow")
    rec, _ = _proposer_record(config, "no proposer record yet")
    try:
        rec.exclude_idea(idea, reason)
    except ValueError as e:
        _refuse(str(e))
    typer.echo(f"excluded idea #{idea}")


@idea_app.command("next")
def idea_next(idea: Optional[int] = typer.Argument(None, help="the idea's id"),
              clear: bool = typer.Option(False, "--clear", help="clear the request instead"),
              config: Optional[str] = CfgOpt):
    """Ask that the builder's next proposal work on an open idea that a proposal could work on now (one idea at a
    time), or --clear the request. It is a request: the idea rules don't change. It stands until a proposal on the
    idea gets a verdict or the judge's refusal, or the idea closes."""
    if clear == (idea is not None):
        _refuse("name an idea's id, or pass --clear")
    from perp_lab.proposer import human
    rec, pc = _proposer_record(config, "no proposer record yet")
    if clear:
        rec.clear_all_next()
        typer.echo("next up cleared")
        return
    try:
        human.flag_next(rec, pc, idea)
    except ValueError as e:
        _refuse(str(e))
    typer.echo(f"idea #{idea} is next up")


stop_app = typer.Typer(add_completion=False, no_args_is_help=True,
                       help="Your stop list: directions every proposal and research prompt rules out.")
app.add_typer(stop_app, name="stop")


@stop_app.command("add")
def stop_add(text: str = typer.Argument(..., help="one line, at most 200 characters: a direction to rule out"),
             config: Optional[str] = CfgOpt):
    """Rule a direction out: the builder and the research step read it in every prompt until you remove it, and
    only you change the list."""
    from perp_lab.proposer import human
    typer.secho(human.WARNING, fg="yellow")
    rec, _ = _proposer_record(config, "no proposer record yet")
    try:
        i = rec.add_stop(text)
    except ValueError as e:
        _refuse(str(e))
    typer.echo(f"added stop #{i}")


@stop_app.command("remove")
def stop_remove(stop: int = typer.Argument(..., help="the item's id, as `lab stop list` shows it"),
                config: Optional[str] = CfgOpt):
    """Take a direction off the stop list: the next prompt no longer carries it."""
    rec, _ = _proposer_record(config, "no proposer record yet")
    try:
        rec.remove_stop(stop)
    except ValueError as e:
        _refuse(str(e))
    typer.echo(f"removed stop #{stop}")


@stop_app.command("list")
def stop_list(config: Optional[str] = CfgOpt):
    """Print the stop list in force, by id."""
    rec, _ = _proposer_record(config, "no proposer record yet")
    items = rec.stops()
    if not items:
        typer.echo("no stop items")
    for s in items:
        typer.echo(f"#{s['id']} {s['text']}")


@app.command()
def dashboard(port: int = 8501,
              address: str = typer.Option("localhost", help="bind address; 0.0.0.0 exposes the dashboard "
                                                             "(and its paper-approval button) to the network"),
              config: Optional[str] = CfgOpt):
    """Launch the Streamlit dashboard (localhost only unless --address says otherwise)."""
    import subprocess
    import sys
    args = [sys.executable, "-m", "streamlit", "run", "dashboard/app.py", "--server.port", str(port),
            "--server.address", address,
            "--browser.gatherUsageStats", "false"]
    if config:
        args += ["--", "--config", config]
    raise typer.Exit(subprocess.call(args))


if __name__ == "__main__":
    app()

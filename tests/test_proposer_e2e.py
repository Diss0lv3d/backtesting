"""End to end: one proposer session on a small synthetic market, with sentinels (spec "Testing").

Real `lab preflight` and `lab judge --proposer` subprocesses in the real sandbox, a fake `claude` first on PATH
(tests/fake_claude.py, scripted call by call), a temp config, store and record, and a temp git repo holding the
config, so the harness pin is the real one. The seeded human trials get fixture verdicts with diagnostics,
which is what check_seed asks for; the seed plugins are the repo's real files, so preflight measures overlap
against them on the synthetic market. Three rounds:
1. a plugin with lookahead, refused by preflight, then its fix through a fix-up, judged;
2. a near-copy of donchian_atr at 4h, whose idea is dead, refused on preflight's overlap until it is dropped;
3. a sound plugin, judged.
Between rounds 1 and 2, a research round: the temp config's `research_every` is 1, so one falls due after round
1's trial, and none again, since round 2 spends no trial and round 3's ends the session. Its reply has two idea
cards and a stop list, which every later proposal is sent. A referee reviews the cards in a web call (the config's
default): its stream shows one allowed arXiv fetch, then a report that accepts the first card and rejects the
second, so only the first enters the record, marked web_reviewed. That web call is probed at the start too, after
the plain probe: the same fenced argv and environment, asked to fetch nothing.

Before the session, sentinels go where the model must never look: a run's notes in lab.db, that run's
report.md, the holdout log, a file under data/, and round 1's plugin's own print, so that the research round, its
referee and rounds 2 and 3 are sent after it exists (the sandbox keeps a plugin's output with its run, under runs/;
the judge echoes it to its stdout only when the plugin fails). A distinctive time goes into the seeded verdict
files' modification times, the run id they name and the holdout log. None of them, and none of the real judge
runs' ids or log times, may appear in any argv or stdin the fake CLI recorded, the research and referee calls'
included: they render the record after a real judge. The page the referee fetched carries a sentinel and the
planted time too: it stays in the round's transcript, and no later call is sent it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
import yaml

from perp_lab.config import load_config
from perp_lab.plugins import blob_hash
from perp_lab.proposer import contract, loop, model, piles, prompt, seed, settings
from perp_lab.proposer.record import Record, research
from perp_lab.runs import write_run
from perp_lab.store import db
from tests.conftest import REPO, write_market
from tests.test_seed import FAMILY, TRIALS

UTC = timezone.utc
SENTINELS = ("SENTINEL_LABDB", "SENTINEL_REPORT", "SENTINEL_HOLDOUT", "SENTINEL_DATA", "SENTINEL_STDOUT",
             "SENTINEL_FETCHED")
PLANTED = datetime(2031, 2, 3, 4, 5, 6, tzinfo=UTC)
PLANTED_RUN = f"{PLANTED:%Y%m%d-%H%M%S}-5e171e"
PLANTED_FORMS = ("2031-02-03T04:05:06", "2031-02-03 04:05:06", "2031-02-03", "20310203", "04:05:06",
                 str(int(PLANTED.timestamp())))

# The seeded trials' piles, through their fixture verdicts: the Donchian ideas are dead, so a near-copy of
# donchian_atr is a near-copy of a closed idea.
SEEDED = {("donchian_atr", "4h"): "no signal", ("donchian_atr_wide", "4h"): "no signal",
          ("donchian_atr", "1h"): "no signal", ("bb_vwap_reversion", "15m"): "no signal",
          ("bb_vwap_reversion", "1h"): "no signal", ("funding_fade", "1h"): "signal before costs",
          ("flow_shock_fade", "1h"): "no signal", ("lce_pct", "5m"): "no signal",
          ("lce_atr", "5m"): "signal before costs"}
STATUSES = {"Donchian breakout": "dead", "Donchian breakout, 1h": "dead", "BB–VWAP reversion": "dead",
            "BB–VWAP reversion, 1h": "dead", "Funding fade": "open", "Flow-shock fade": "dead",
            "LCE: EMA cloud at volume-profile levels": "exhausted",       # two judged plugins: the cap, 2
            "Volatility-compression breakout": "open",
            "Weekend-move reversal": "open", "Time-series momentum with a funding filter": "open",
            "Taker-flow persistence": "excluded",
            "Volume-burst continuation": "dead", "Candle-body reversal": "dead",
            "Liquidation-cascade rebound": "open"}

VOLUME_BURST = '''"""Volume burst."""
from perp_lab.indicators import atr
from perp_lab.strategy import EXIT, HOLD, Strategy, enter


class VolumeBurst(Strategy):
    params = {"vol_n": 30, "vol_x": 1.5, "hold_bars": 2, "atr_n": 14, "stop_atr": 2.0}
    grid = {"vol_x": [1.5, 2.0], "hold_bars": [1, 2]}
    timeframes = ["4h"]

    def prepare(self, bars):
        print("SENTINEL" + "_STDOUT", flush=True)    # built at run time: the source itself never holds it
        med = bars["volume"].shift(1).rolling(self.p["vol_n"]).median()
        rng = (bars["high"] - bars["low"]).replace(0.0, float("nan"))
        loc = (bars["close"] - bars["low"]) / rng
        return bars[[]].assign(vr=(bars["volume"] / med).values, loc=loc.values, atr=atr(bars, self.p["atr_n"]))

    def bracket(self, ctx, side):
        return ctx["close"] - side * self.p["stop_atr"] * ctx["atr"], None

    def entry(self, ctx):
        if not ctx["vr"] > self.p["vol_x"]:
            return HOLD
        if ctx["loc"] > 0.8:
            return enter(1, *self.bracket(ctx, 1))
        if ctx["loc"] < 0.2:
            return enter(-1, *self.bracket(ctx, -1))
        return HOLD

    def manage(self, ctx):
        if ctx.position.bars_held >= self.p["hold_bars"]:
            return EXIT
        return HOLD
'''
# The broken version reads the next bar's volume: preflight's lookahead check refuses it.
PEEKING = VOLUME_BURST.replace('bars["volume"].shift(1)', 'bars["volume"].shift(-1)')
# donchian_atr at 4h with n 44 for 40: a parameter change, so a near-copy (overlap about 0.94).
NEAR_COPY = ((REPO / "strategies" / "donchian_atr.py").read_text()
             .replace('"n": 40,', '"n": 44,').replace('timeframes = ["1h", "4h"]', 'timeframes = ["4h"]'))
BODY_REVERSAL = '''"""Body reversal."""
from perp_lab.indicators import atr
from perp_lab.strategy import EXIT, HOLD, Strategy, enter


class BodyReversal(Strategy):
    params = {"atr_n": 14, "k": 0.75, "hold_bars": 2, "stop_atr": 2.0}
    grid = {"k": [0.75, 1.0], "hold_bars": [2, 3]}
    timeframes = ["4h"]

    def prepare(self, bars):
        body = bars["close"] - bars["open"]
        return bars[[]].assign(body=body.values, atr=atr(bars, self.p["atr_n"]))

    def bracket(self, ctx, side):
        return ctx["close"] - side * self.p["stop_atr"] * ctx["atr"], None

    def entry(self, ctx):
        a = ctx["atr"]
        if a != a:
            return HOLD
        if ctx["body"] < -self.p["k"] * a:
            return enter(1, *self.bracket(ctx, 1))
        if ctx["body"] > self.p["k"] * a:
            return enter(-1, *self.bracket(ctx, -1))
        return HOLD

    def manage(self, ctx):
        if ctx.position.bars_held >= self.p["hold_bars"]:
            return EXIT
        return HOLD
'''
assert "SENTINEL_STDOUT" not in VOLUME_BURST


def card(title, columns):
    return {"title": title, "mechanism": f"{title}: who pays", "why_it_persists": "w", "signal": "s",
            "trades_per_coin_per_year": 40, "likely_failure": "f", "columns": columns}


def new(title, source, columns):
    return {"kind": "new", "timeframe": "4h", "hypothesis": f"{title} pays after costs", "source": source,
            "idea": card(title, columns)}


ROUND_1 = new("Volume-burst continuation", PEEKING, ["volume", "high", "low", "close"])
ROUND_1_FIX = new("Volume-burst continuation", VOLUME_BURST, ["volume", "high", "low", "close"])
ROUND_2 = new("Channel breakout", NEAR_COPY, ["high", "low", "close"])
ROUND_3 = new("Candle-body reversal", BODY_REVERSAL, ["open", "high", "low", "close"])
# The research round's note (contract.ResearchNote): two ideas, which no round proposes for, and a stop list. The
# referee accepts the first and rejects the second.
RESEARCH_IDEA = {**card("Liquidation-cascade rebound", ["open", "high", "low", "close"]), "timeframe": "4h",
                 "builds_on": "the closed breakout ideas"}
REJECTED_IDEA = {**card("Round-number stop cascade", ["high", "low", "close"]), "timeframe": "4h",
                 "builds_on": "the closed breakout ideas"}
STOP = ["Channel breakouts at any lookback", "A filter added to a closed idea's entry"]
RESEARCH = {"ideas": [RESEARCH_IDEA, REJECTED_IDEA], "stop": STOP,
            "summary": "Breakouts are done; a forced-flow rebound is not.", "next_up": None}
ARXIV = "https://arxiv.org/abs/1707.06221"
REPORT = {"verdicts": [
    {"title": "Liquidation-cascade rebound", "decision": "accept", "reason": "Forced flow, not the exit, pays.",
     "sources": [], "revised": None},
    {"title": "Round-number stop cascade", "decision": "reject",
     "reason": "No round-number effect survives costs.", "sources": [ARXIV], "revised": None}]}

# --------------------------------------------------------------------------- the fake CLI's stream

STREAM = (REPO / "tests/fixtures/claude_stream_ok.jsonl").read_text().splitlines()   # the real CLI's, 2.1.316


def reply(structured_output: dict) -> dict:
    """One call's scenario: the fixture's ui_invalidate and init, then its result carrying this reply."""
    result = json.loads(STREAM[5])
    assert result["type"] == "result"
    return {"lines": [STREAM[0], STREAM[1], json.dumps({**result, "structured_output": structured_output})],
            "exit": 0}


def _said(content: list[dict]) -> str:
    """An assistant message with these blocks and, as the real CLI sends it, each tool_use's input as the API sent
    it."""
    m = json.loads(STREAM[2])
    return json.dumps({**m, "message": {**m["message"], "content": content},
                       "wire_tool_inputs": {b["id"]: b["input"] for b in content}})


def _told(tid: str, content: str) -> str:
    """The CLI's tool_result for a tool_use, as a user message."""
    m = {k: v for k, v in json.loads(STREAM[3]).items() if k not in ("tool_use_result", "tool_result_meta")}
    block = {"tool_use_id": tid, "type": "tool_result", "content": content}
    return json.dumps({**m, "message": {**m["message"], "content": [block]}})


def web_reply(structured_output: dict, fetch: bool = True) -> dict:
    """A web call's scenario, in the shapes of CLI 2.1.316 (tests/test_model.py): init with WebFetch and the
    default permission mode, one allowed fetch and its page (unless not `fetch`), then the reply through
    StructuredOutput, and its result."""
    init = json.loads(STREAM[1])
    result = json.loads(STREAM[5])
    get = {"type": "tool_use", "id": "toolu_e2e_fetch", "name": "WebFetch",
           "input": {"url": ARXIV, "prompt": "Does a round-number effect survive costs?"}}
    answer = {"type": "tool_use", "id": "toolu_e2e_report", "name": "StructuredOutput", "input": structured_output}
    page = f"SENTINEL_FETCHED. A paper of {PLANTED:%Y-%m-%d}: no round-number effect survives costs."
    return {"lines": [STREAM[0], json.dumps({**init, "tools": ["StructuredOutput", "WebFetch"],
                                             "permissionMode": "default"}),
                      *([_said([get]), _told(get["id"], page)] if fetch else []),
                      _said([answer]), _told(answer["id"], "Structured output provided successfully"),
                      STREAM[4], json.dumps({**result, "structured_output": structured_output,
                                             "permission_denials": []})],
            "exit": 0}


CALLS = [reply({"ok": True}),                          # the probe
         web_reply({"ok": True}, fetch=False),         # the referee's web call's probe
         reply(ROUND_1), reply(ROUND_1_FIX),
         reply(RESEARCH),                              # due after round 1's trial
         web_reply(REPORT),                            # its referee, a web call
         *[reply(ROUND_2)] * 4,                        # the proposal and its 3 fix-ups
         reply(ROUND_3)]

# --------------------------------------------------------------------------- the temp lab


def seeded_verdict(plugin: str, tf: str, h: str, pile: str, report: str) -> dict:
    gross = {"sharpe": 0.6 if pile == "signal before costs" else -0.5, "random_p95": 0.1}
    data = "judged" if tf == "5m" else "refreshed"        # as on the real store: LCE's from its own judgement
    return {"schema": 2, "strategy": plugin, "timeframe": tf, "strategy_hash": h, "decision": "kill",
            "reasons": ["OOS Sharpe -0.46 <= 0", "Deflated Sharpe 0.00 < 0.95 (N=9)"], "dsr": 0.0, "sr0": 1.24,
            "n_trials": 9, "beats_parent_frac": None, "parent_passed": None, "parent_run_id": None,
            "lineage_id": FAMILY[plugin], "holdout_status": "n/a", "holdout_reason": "n/a",
            "walkforward": {"run_id": PLANTED_RUN, "report": report, "metrics": {"sharpe": -0.46, "trades": 500}},
            "diagnostics": {
                "data": data,
                "gates": {"net_sharpe": -1.0, "random_p95": 0.2, "oos_trades": 500, "min_oos_trades": 64},
                "before_costs": {**gross, "pctile": 0.5, "random_mean": 0.0},
                "overlap": {"position": [], "mirror": [], "correlation": []}}}


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=e2e", "-c", "user.email=e2e@example.invalid",
                    "-c", "commit.gpgsign=false", *args], cwd=root, check=True, capture_output=True)


@pytest.fixture
def lab(tmp_path, monkeypatch):
    root = tmp_path / "lab"
    (root / "config").mkdir(parents=True)
    raw = yaml.safe_load((REPO / "config/lab.yaml").read_text())
    raw.update(data_dir=str(root / "data"), runs_dir=str(root / "runs"), store={"url": f"sqlite:///{root}/lab.db"},
               history_start="2025-01-01", universe=["BTC", "ETH"])
    raw["judge"] = {**raw["judge"], "holdout_log": str(root / "runs" / "holdout_log.jsonl"),
                    "verdicts_dir": str(root / "runs" / "verdicts")}
    raw["validation"] = {**raw["validation"], "random_seeds": 5, "mc_iterations": 200}    # a quicker judge
    raw["proposer"] = {**raw["proposer"], "research_every": 1}          # one research round, after round 1
    path = root / "config" / "lab.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True))
    cfg = load_config(path)
    write_market(cfg.data_dir, cfg.symbols)

    sdir = root / "strategies"
    sdir.mkdir()
    for plugin in FAMILY:
        shutil.copy(REPO / "strategies" / f"{plugin}.py", sdir / f"{plugin}.py")

    # A run in lab.db, named by a planted time, with a sentinel in its notes and its report.md.
    report = cfg.runs_dir / PLANTED_RUN / "report.md"
    write_run(cfg, {"run_id": PLANTED_RUN, "strategy": "donchian_atr", "timeframes": ["4h"],
                    "strategy_hash": blob_hash((sdir / "donchian_atr.py").read_bytes()), "hypothesis": "h",
                    "universe": cfg.symbols, "validation_mode": "walkforward", "status": "noise",
                    "metrics": {"sharpe": -0.46, "trades": 500}, "created_at": PLANTED.isoformat(),
                    "notes": f"SENTINEL_LABDB, noted {PLANTED.isoformat()}"},
              {}, report_md=f"# {PLANTED_RUN}\n\nSENTINEL_REPORT, written {PLANTED.isoformat()}\n")
    # The seeded verdicts name that run, and carry the planted time as their modification time.
    for plugin, tf in TRIALS:
        h = blob_hash((sdir / f"{plugin}.py").read_bytes())
        vpath = seed.verdict_path(cfg, plugin, tf, h)
        vpath.parent.mkdir(parents=True, exist_ok=True)
        vpath.write_text(json.dumps(seeded_verdict(plugin, tf, h, SEEDED[(plugin, tf)], str(report))))
        os.utime(vpath, (PLANTED.timestamp(), PLANTED.timestamp()))
    holdout_log = Path(cfg["judge"]["holdout_log"])
    holdout_log.write_text(json.dumps({"ts": PLANTED.isoformat(), "lineage_id": "family:planted",
                                       "reason": "SENTINEL_HOLDOUT", "nonce": "5e171e5e171e5e17",
                                       "judgement_id": None, "window_end": PLANTED.isoformat()}) + "\n")
    (cfg.data_dir / "notes.txt").write_text("SENTINEL_DATA\n")

    # The fake CLI, first on PATH; HOME is a temp one, so git below reads no user config either.
    bindir = root / "bin"
    bindir.mkdir()
    body = (REPO / "tests/fake_claude.py").read_text().split("\n", 1)[1]
    (bindir / "claude").write_text(f"#!{sys.executable}\n{body}")
    (bindir / "claude").chmod(0o755)
    (bindir / "scenario.json").write_text(json.dumps({"version": "2.1.316 (Claude Code)", "calls": CALLS}))
    (root / "home").mkdir()
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("HOME", str(root / "home"))
    monkeypatch.setattr(model, "BACKOFF_S", ())      # a failed call stops the session at once instead of retrying

    # The root is the session's repo: a git checkout whose tracked config/ the pin holds, and its cwd, which
    # holds strategies/ as the repo root does.
    git(root, "init", "-q")
    git(root, "add", "config")
    git(root, "commit", "-q", "-m", "the temp lab's config")
    monkeypatch.chdir(root)
    return cfg, root


def seen_calls(root: Path) -> list[dict]:
    return [json.loads(line) for line in (root / "bin" / "seen.jsonl").read_text().splitlines()]


def arg(call: dict, flag: str) -> str:
    return call["argv"][call["argv"].index(flag) + 1]


def research_rows(rec: Record) -> list[dict]:
    with rec.engine.connect() as c:
        return [dict(r) for r in c.execute(sa.select(research).order_by(research.c.id)).mappings()]


@pytest.mark.sandbox
def test_a_session_files_three_rounds_and_nothing_forbidden_reaches_the_model(lab):
    cfg, root = lab
    pc = settings.load(cfg)
    seed_bytes = {p.name: p.read_bytes() for p in (root / "strategies").glob("*.py")}

    assert pc.research_referee and pc.research_referee_web             # the config's defaults: a web referee
    reason = loop.run_session(cfg, pc, days=1, trials=2, repo=root)
    assert reason == "trials"

    rec = Record(root / pc.state_dir)
    props = {p["seq"]: p for p in rec.proposals()}
    ideas = {i["id"]: i for i in rec.ideas()}
    title = {i: ideas[i]["title"] for i in ideas}

    # The seed: nine human trials, piled from their fixture verdicts.
    assert [(props[s]["plugin"], props[s]["timeframe"], props[s]["pile"]) for s in range(1, 10)] == \
        [(p, tf, SEEDED[(p, tf)]) for p, tf in TRIALS]
    assert {props[s]["origin"] for s in range(1, 10)} == {"seed"}

    # Round 1: refused by preflight's lookahead check, fixed in a fix-up, judged.
    r1 = props[10]
    assert (r1["origin"], r1["kind"], r1["plugin"], r1["timeframe"], r1["state"], r1["judge_exit"]) == \
        ("proposer", "new", "px0010_volume_burst_continuation", "4h", "judged", 0)
    assert title[r1["idea_id"]] == "Volume-burst continuation"
    # Round 2: a near-copy of a closed idea's plugin, refused on every try, then dropped; it never got an idea.
    r2 = props[11]
    assert (r2["state"], r2["broken_reason"], r2["idea_id"], r2["judge_exit"], r2["plugin"]) == \
        ("dropped", "fixups", None, None, "px0011_channel_breakout")
    assert r2["preflight"]["ok"] is True and any(
        (e["strategy"], e["timeframe"]) == ("donchian_atr", "4h") and e["overlap"] >= pc.overlap_max
        for e in r2["preflight"]["overlap"])
    # Round 3: judged.
    r3 = props[12]
    assert (r3["kind"], r3["plugin"], r3["state"], r3["judge_exit"]) == \
        ("new", "px0012_candle_body_reversal", "judged", 0)
    assert title[r3["idea_id"]] == "Candle-body reversal"
    assert sorted(props) == list(range(1, 13))

    # The two judged plugins: verdicts from the real `lab judge --proposer`, piled in code.
    for p in (r1, r3):
        v = seed.read_verdict(seed.verdict_path(cfg, p["plugin"], "4h", p["hash"]))
        assert v is not None and v["schema"] == 2 and v["diagnostics"]["data"] == "judged"
        assert p["verdict"] == v and p["pile"] == piles.pile(v) == "no signal" and p["duplicate_of"] is None
        j = db.get_judgement(cfg, p["hash"], "4h")
        assert j["judged_by"] == "proposer" and j["strategy"] == p["plugin"]
        assert (root / "strategies" / f"{p['plugin']}.py").read_bytes().decode() == p["source"]
    assert not (root / "strategies" / "px0011_channel_breakout.py").exists()
    assert {n: (root / "strategies" / n).read_bytes() for n in seed_bytes} == seed_bytes   # nothing overwritten
    assert {title[i]: ideas[i]["status"] for i in ideas} == STATUSES
    assert rec.trials_used() == 2

    # The digest.
    sid = rec.last_session_id()
    s = rec.session(sid)
    assert (s["stop_reason"], s["trials_used"]) == ("trials", 2)
    digest = (root / pc.state_dir / "sessions" / f"{sid}.md").read_text()
    assert "- Stop reason: trials" in digest and "Proposals by pile: no signal 2, dropped 1" in digest
    for line in ("0010 · px0010_volume_burst_continuation@4h · new · Volume-burst continuation · no signal",
                 "0011 · px0011_channel_breakout@4h · new · Channel breakout · dropped after 3 fix-ups: "
                 "near-copy of closed idea #1 (Donchian breakout)",
                 "0012 · px0012_candle_body_reversal@4h · new · Candle-body reversal · no signal"):
        assert line in digest

    # The research round, after round 1's trial: of its two cards only the one the referee accepted is in the
    # record, with origin research and marked web_reviewed; its row ok, with the referee's verdicts and its fetch;
    # its digest line (the verdicts, the rejection's reason and the summary under it) between round 1's line and
    # round 2's.
    [rr] = research_rows(rec)
    [rid] = [i for i in ideas if title[i] == "Liquidation-cascade rebound"]
    assert "Round-number stop cascade" not in title.values()
    assert (ideas[rid]["origin"], ideas[rid]["timeframe"], ideas[rid]["card"], ideas[rid]["web_reviewed"]) == \
        ("research", "4h", {k: v for k, v in RESEARCH_IDEA.items() if k not in ("timeframe", "builds_on")}, True)
    assert [i for i in ideas if ideas[i]["web_reviewed"]] == [rid]
    assert (rr["session_id"], rr["trials_used"], rr["outcome"], rr["added"], rr["reply"]) == \
        (sid, 1, "ok", [rid], RESEARCH)
    assert rr["referee"] == {"web": True, "fetches": [ARXIV], "denied": [], "verdicts": [
        {k: v for k, v in verdict.items() if k != "revised"} for verdict in REPORT["verdicts"]]}
    transcript = (root / pc.state_dir / "transcripts" / f"research-{rr['id']}.jsonl").read_text()
    research_line = (f" · research: +1 idea (#{rid}); stop list 2 items; 2 cards: 1 accepted, 0 revised, "
                     "1 rejected\n  rejected: Round-number stop cascade: No round-number effect survives costs.\n"
                     f"  {RESEARCH['summary']}\n")
    assert digest.index("0010 · ") < digest.index(research_line) < digest.index("0011 · ")

    # What the model was sent: the version check, the probe and the web probe, 2 proposal calls, the research call,
    # its referee's, 4 + 1 more. All but the probes have the same system prompt (the static context); the research
    # and referee calls have their own schemas.
    calls = seen_calls(root)
    assert [c["argv"][1:] == ["--version"] for c in calls] == [True] + [False] * 11
    assert (calls[1]["stdin"], calls[2]["stdin"]) == (model.PROBE_USER, model.PROBE_WEB_USER)
    assert [json.loads(arg(c, "--json-schema")) for c in calls[1:3]] == [model.PROBE_SCHEMA] * 2
    proposing = contract.schema(pc.timeframes)
    researching = contract.research_schema(pc.timeframes, pc.research_max_ideas)
    refereeing = contract.referee_schema(pc.timeframes, pc.research_max_ideas)
    assert [json.loads(arg(c, "--json-schema")) for c in calls[3:]] == \
        [proposing] * 2 + [researching, refereeing] + [proposing] * 5
    assert len({arg(c, "--system-prompt") for c in calls[3:]}) == 1
    # The research call: the record and the research instruction, no stop list yet. The referee call: the round's
    # two cards and the referee instruction. Every proposal after them gets the round's stop list, and that the
    # human's request comes before it; none before it.
    assert calls[5]["stdin"].endswith("\n" + prompt.research_instruction(pc) + "\n")
    referee = calls[6]
    assert referee["stdin"].endswith("\n" + prompt.referee_instruction(pc) + "\n")
    assert "### Card 1" in referee["stdin"] and "Round-number stop cascade" in referee["stdin"]
    stop = ("Directions the research step recommends avoiding:\n"
            "- Channel breakouts at any lookback\n- A filter added to a closed idea's entry\n\n"
            "When the human asks for an idea below, that request comes before this list.\n")
    assert [(prompt.STOP_HEADING in c["stdin"], stop in c["stdin"]) for c in calls[3:]] == \
        [(False, False)] * 4 + [(True, True)] * 5
    # The referee alone had the web, and its probe: WebFetch and one allow rule per academic domain, no settings
    # sources, the confined env. Every other call stayed tool-less.
    rules = [f"WebFetch(domain:{d})" for d in pc.referee_domains]
    fenced = ["--tools", "WebFetch", "--allowedTools", *rules, "--setting-sources", ""]
    for c in (calls[2], referee):
        at = c["argv"].index("--tools")
        assert c["argv"][at:at + len(fenced)] == fenced and "WebSearch" not in c["argv"]
        assert c["env"].get("CLAUDE_CODE_EVAL_CONFINED") == "1"
    for c in calls[1:2] + calls[3:6] + calls[7:]:
        assert arg(c, "--tools") == "" and "--allowedTools" not in c["argv"]
        assert "--setting-sources" not in c["argv"] and "CLAUDE_CODE_EVAL_CONFINED" not in c["env"]
    refused = [c["stdin"] for c in calls[3:] if "## Your last reply was refused" in c["stdin"]]
    assert len(refused) == 4
    assert "lookahead: " in refused[0] and "near-copy of closed idea #1 (Donchian breakout)" in refused[1]

    # Every sentinel was planted where it can be found...
    assert "SENTINEL_LABDB" in db.run_row(cfg, PLANTED_RUN)["notes"]
    runs = db.read_table(cfg, db.runs)
    [r1_run] = runs.loc[runs["strategy"] == r1["plugin"], "run_dir"].tolist()
    printed = [f for f in cfg.runs_dir.rglob("*.txt") if "SENTINEL_STDOUT" in f.read_text(errors="replace")]
    assert printed and {f.parent for f in printed} == {Path(r1_run) / "sandbox_stderr"}
    judge_logs = [(root / pc.state_dir / "logs" / f"{p['plugin']}.log").read_text() for p in (r1, r3)]
    run_ids = runs["run_id"].tolist()
    stamps = {m for log in judge_logs for m in re.findall(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", log)}
    assert len(run_ids) >= 3 and stamps
    assert "SENTINEL_FETCHED" in transcript and f"{PLANTED:%Y-%m-%d}" in transcript    # the page the referee read
    # ...and none of them, no planted time, no run id, no time from the judge's output and not the research
    # round's time reached the model, in any call: the research and referee calls' argv and stdin included.
    rr_at = (f"{rr['at']:%Y-%m-%d %H:%M:%S}", f"{rr['at']:%Y-%m-%dT%H:%M:%S}")
    forbidden = [*SENTINELS, *PLANTED_FORMS, *run_ids, *stamps, *(t.replace(" ", "T") for t in stamps), *rr_at]
    for k, c in enumerate(calls):
        sent = "\0".join([*c["argv"], c["stdin"]])
        leaked = [f for f in forbidden if f in sent]
        assert not leaked, f"call {k} carried {leaked}"

"""The propose loop, run with fake Deps and a fake clock: no subprocess, no model and no judge.

The fake model returns scripted replies (or a fresh new idea per call), the fake preflight passes unless told
otherwise, and the fake judge writes a verdict file where the real one would, for the bytes the loop copied.
"""

from __future__ import annotations

import fcntl
import json
import signal
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from perp_lab.plugins import blob_hash, parse_meta
from perp_lab.proposer import loop, proc, settings
from perp_lab.proposer.model import ModelCallError, ModelConfigError, Reply, UsageLimit
from perp_lab.proposer.pin import Pin, PinError
from perp_lab.proposer.record import Record
from perp_lab.proposer.settings import StartRefused
from tests.test_seed import FAMILY, as_today, refreshed
from tests.test_store import cfg  # noqa: F401  (fixture)

UTC = timezone.utc
T0 = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
TO_MIDNIGHT = 14 * 3600.0     # from T0 to the next 00:00 UTC

SRC = '''"""The model's own docstring."""
from perp_lab.strategy import Strategy


class S(Strategy):
    params = {"n": 5}
    grid = {"n": [3, 5]}
    timeframes = ["1h"]
'''
SRC_4H = SRC.replace('["1h"]', '["4h"]')


def card(title):
    return {"title": title, "mechanism": "m", "why_it_persists": "w", "signal": "s",
            "trades_per_coin_per_year": 10, "likely_failure": "f", "columns": ["close"]}


def new(title, tf="1h", source=SRC):
    return {"kind": "new", "timeframe": tf, "hypothesis": f"{title} pays", "source": source, "idea": card(title)}


def port(idea_id, tf="4h", source=SRC_4H):
    return {"kind": "port", "timeframe": tf, "hypothesis": "the port pays", "source": source,
            "ports": {"idea_id": idea_id, "why": "w"}}


BAD = {"kind": "new", "timeframe": "1h"}          # no hypothesis, source or idea


def verdict(pile="no signal", *, overlap=None, **f):
    gates = {"net_sharpe": -1.0, "random_p95": 0.2, "oos_trades": 500, "min_oos_trades": 64}
    before = {"sharpe": -0.5, "random_p95": 0.1}
    if pile == "signal before costs":
        before = {"sharpe": 0.6, "random_p95": 0.1}
    elif pile == "close call":
        gates = {**gates, "net_sharpe": 0.5}
    v = {"schema": 2, "decision": "kill", "reasons": ["Deflated Sharpe 0.10 < 0.95 (N=10)"], "n_trials": 10,
         "holdout_status": "n/a", "holdout_reason": "n/a", "walkforward": {"run_id": "r", "metrics": {}},
         "diagnostics": {"data": "judged", "gates": gates, "before_costs": before,
                         "overlap": {"position": [], "mirror": [], "correlation": [], **(overlap or {})}}}
    return {**v, **f}


def ok_report():
    return {"ok": True, "meta": [], "lookahead": [], "overlap": [], "mirror_of": [], "skipped": {}, "error": None}


USAGE = {"input_tokens": 100, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 1000, "output_tokens": 10}


class Fake:
    """Deps that record what the loop asked of them."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.clock = T0
        self.replies = None            # None: a fresh new idea per call; a list: popped, then ModelCallError
        self.n_new = 0
        self.calls = []
        self.preflights = []
        self.preflight_results = []    # popped; then a pass
        self.judges = []
        self.judge_exits = []          # popped; then 0
        self.verdict = verdict()       # or a callable(name) -> verdict
        self.sleeps = []
        self.pin_checks = 0
        self.pin_error = None          # raised by pin_check, or a callable(n) -> error or None
        self.on_call = self.on_preflight = self.on_judge = self.on_sleep = None
        self.killed = False
        self.start_error = {}          # "pin_take" | "cli_version" | "probe" -> exception
        self.branch = "main"

    def deps(self):
        return loop.Deps(call_model=self.call_model, preflight=self.preflight, judge=self.judge,
                         now=lambda: self.clock, sleep=self.sleep, pin_check=self.pin_check,
                         pin_take=self.pin_take, cli_version=self.cli_version, probe=self.probe,
                         kill_judge=self.kill_judge)

    def call_model(self, system, user, schema, transcript):
        self.calls.append(SimpleNamespace(system=system, user=user, schema=schema, transcript=transcript))
        if self.on_call:
            self.on_call(self)
        if self.replies is None:
            self.n_new += 1
            r = new(f"Idea {self.n_new}")
        elif self.replies:
            r = self.replies.pop(0)
        else:
            raise ModelCallError("the fake has no more replies")
        if isinstance(r, BaseException):
            raise r
        return Reply(proposal=r, usage=dict(USAGE), cost_usd=0.01)

    def preflight(self, path, tf):
        self.preflights.append((Path(path), tf, Path(path).read_bytes()))
        if self.on_preflight:
            self.on_preflight(self)
        return self.preflight_results.pop(0) if self.preflight_results else (0, ok_report())

    def judge(self, name, tf, log):
        self.judges.append((name, tf, Path(log)))
        if self.on_judge:
            self.on_judge(self)
        if self.killed:
            return -9
        code = self.judge_exits.pop(0) if self.judge_exits else 0
        if code == 0:
            data = (Path("strategies") / f"{name}.py").read_bytes()
            v = self.verdict(name) if callable(self.verdict) else self.verdict
            v = {"strategy": name, "timeframe": tf, "lineage_id": f"family:{name}", **v}
            path = Path(self.cfg["judge"]["verdicts_dir"]) / f"{name}@{tf}@{blob_hash(data)[:10]}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(v))
        return code

    def kill_judge(self):
        self.killed = True

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        if self.on_sleep:
            self.on_sleep(self)
        self.clock += timedelta(seconds=seconds)

    def pin_check(self):
        self.pin_checks += 1
        err = self.pin_error(self.pin_checks) if callable(self.pin_error) else self.pin_error
        if err is not None:
            raise err

    def pin_take(self):
        if "pin_take" in self.start_error:
            raise self.start_error["pin_take"]
        return Pin(commit="c0ffee", branch=self.branch)

    def cli_version(self):
        if "cli_version" in self.start_error:
            raise self.start_error["cli_version"]
        return "2.1.316 (Claude Code)"

    def probe(self, transcript):
        if "probe" in self.start_error:
            raise self.start_error["probe"]


@pytest.fixture
def lab(tmp_path, cfg, monkeypatch):  # noqa: F811
    monkeypatch.chdir(tmp_path)                    # strategies/ is the judge's, relative to the working directory
    (tmp_path / "strategies").mkdir()
    pc = settings.load(cfg)
    state = tmp_path / pc.state_dir                # "proposer", resolved against the repo: here tmp_path
    rec = Record(state)
    # An idea, so the seed (Task 25) leaves the record alone: these tests are about the loop.
    rec.add_idea("Placeholder", card("Placeholder"), "seed", "4h")
    return SimpleNamespace(cfg=cfg, pc=pc, rec=rec, root=tmp_path, state=state, fake=Fake(cfg))


def run(lab, pc=None, **kw):
    return loop.run_session(lab.cfg, pc or lab.pc, deps=lab.fake.deps(), repo=lab.root, **kw)


def props(lab):
    return [p for p in lab.rec.proposals() if p["origin"] == "proposer"]


def idea(lab, title):
    return next(i for i in lab.rec.ideas() if i["title"] == title)


def digest(lab):
    (path,) = (lab.state / "sessions").glob("*.md")
    return path.read_text()


def closed_idea(lab, title="Old fade", plugin="old_plugin", tf="1h", pile="no signal"):
    i = lab.rec.add_idea(title, card(title), "proposer", tf)
    lab.rec.add_proposal(idea_id=i, plugin=plugin, timeframe=tf, kind="new", state="judged", judge_exit=0,
                         verdict={**verdict(pile), "lineage_id": f"family:{plugin}"})
    return i


# -- one round
def test_a_round_copies_judges_and_files_the_verdict(lab):
    assert run(lab, trials=2) == "trials"
    p1, p2 = props(lab)
    assert (p1["seq"], p1["plugin"], p1["timeframe"], p1["kind"]) == (1, "px0001_idea_1", "1h", "new")
    assert (p1["state"], p1["judge_exit"], p1["pile"], p1["judged_at"]) == ("judged", 0, "no signal", T0)
    text = (lab.root / "strategies" / "px0001_idea_1.py").read_bytes()
    assert p1["hash"] == blob_hash(text) and p1["source"] == text.decode()
    assert parse_meta(text, "px0001_idea_1").hypothesis == "Idea 1 pays"       # the docstring is the hypothesis
    assert (lab.state / "staging" / "px0001_idea_1.py").read_bytes() == text
    assert p1["verdict"]["strategy"] == "px0001_idea_1" and p1["verdict"]["n_trials"] == 10
    i1 = idea(lab, "Idea 1")
    assert (i1["origin"], i1["timeframe"], i1["status"], i1["family"], i1["root_plugin"]) == \
        ("proposer", "1h", "dead", "family:px0001_idea_1", "px0001_idea_1")
    assert i1["card"] == card("Idea 1") and p1["idea_id"] == i1["id"]
    name, tf, log = lab.fake.judges[0]
    assert (name, tf, log) == ("px0001_idea_1", "1h", lab.state / "logs" / "px0001_idea_1.log")
    assert lab.fake.preflights[0][:2] == (lab.state / "staging" / "px0001_idea_1.py", "1h")
    # the second round's prompt shows the first verdict's N (R32), the first had none to show
    assert "N is now 0;" in lab.fake.calls[0].user and "N is now 10;" in lab.fake.calls[1].user
    assert f"#{i1['id']} Idea 1: dead, no signal" in lab.fake.calls[1].user and p2["plugin"] == "px0002_idea_2"
    # tokens: everything the model read in, and its output (R31)
    assert (p1["tokens_in"], p1["tokens_out"]) == (1100, 10)
    assert lab.rec.usage_on(T0.date()) == (2200, 20)


def test_the_session_is_recorded_with_its_transcripts_and_digest(lab):
    assert run(lab, trials=1) == "trials"
    (s,) = [lab.rec.session(1)]
    assert (s["stop_reason"], s["model"], s["commit"], s["branch"], s["cli_version"], s["trials_used"]) == \
        ("trials", "claude-opus-5-5", "c0ffee", "main", "2.1.316 (Claude Code)", 1)
    assert (s["tokens_in"], s["tokens_out"], s["started_at"]) == (1100, 10, T0)
    lines = [json.loads(ln) for ln in lab.rec.transcript_path(1).read_text().splitlines()]
    assert [ln["user"] for ln in lines] == [c.user for c in lab.fake.calls]
    assert (lab.state / "sessions" / "1.system.txt").read_text() == lab.fake.calls[0].system
    text = digest(lab)
    assert "px0001_idea_1@1h" in text and "Idea 1" in text and "Stop reason: trials" in text
    assert "no signal 1" in text and "Warning" not in text


def test_the_digest_lists_the_ideas_that_rose_into_a_progress_pile(lab):
    i = lab.rec.add_idea("Fade", card("Fade"), "proposer", "1h")
    lab.rec.add_proposal(idea_id=i, plugin="fade_v1", timeframe="1h", kind="new", state="judged", judge_exit=0,
                         verdict={**verdict("signal before costs"), "lineage_id": "family:fade_v1"})
    lab.fake.replies = [{"kind": "refinement", "timeframe": "1h", "hypothesis": "tighter", "source": SRC,
                         "refines": {"idea_id": i, "change": "c"}}, new("Rising"), new("Flat")]
    lab.fake.verdict = lambda name: verdict("no signal" if name.endswith("flat") else "signal before costs")
    assert run(lab, trials=3) == "trials"
    end = digest(lab).split("## End")[1]
    rising = idea(lab, "Rising")["id"]
    assert f"#{rising} Rising: signal before costs" in end
    assert "Fade" not in end and "Flat" not in end       # Fade was there already; Flat has no signal


def test_a_refinement_names_its_ideas_champion_as_parent(lab):
    i = lab.rec.add_idea("Fade", card("Fade"), "proposer", "1h")
    lab.rec.add_proposal(idea_id=i, plugin="fade_v1", timeframe="1h", kind="new", state="judged", judge_exit=0,
                         verdict={**verdict("signal before costs"), "lineage_id": "family:fade_v1"})
    lab.fake.replies = [{"kind": "refinement", "timeframe": "1h", "hypothesis": "tighter", "source": SRC,
                         "refines": {"idea_id": i, "change": "c"}}]
    assert run(lab, trials=2) == "model_call"            # the second round finds no reply
    p = props(lab)[1]
    assert p["idea_id"] == i and p["kind"] == "refinement" and p["plugin"] == "px0002_fade"
    assert parse_meta(p["source"].encode(), "px0002_fade").parent == "fade_v1"


# -- quotas
def test_the_daily_quota_sleeps_until_the_next_midnight_utc(lab):
    pc = replace(lab.pc, trials_per_day=2)
    assert run(lab, pc, trials=3) == "trials"
    assert lab.fake.sleeps == [TO_MIDNIGHT]
    assert [p["judged_at"] for p in props(lab)] == [T0, T0, T0 + timedelta(seconds=TO_MIDNIGHT)]
    assert lab.rec.judge_calls_on(T0.date()) == 2
    assert "paused: the day's 2 judge calls are used; until 2026-11-03 00:00 UTC" in digest(lab)


def test_judge_exits_0_1_and_2_each_count_toward_the_days_quota(lab):
    pc = replace(lab.pc, trials_per_day=3)
    lab.fake.judge_exits = [0, 1, 2, 0]
    assert run(lab, pc, trials=3) == "trials"
    assert lab.rec.judge_calls_on(T0.date()) == 3 and lab.fake.sleeps == [TO_MIDNIGHT]
    assert [(p["state"], p["judge_exit"], p["broken_reason"]) for p in props(lab)] == [
        ("judged", 0, None), ("broken", 1, "judge_error"), ("broken", 2, "refused"), ("judged", 0, None)]
    assert lab.rec.trials_used() == 3                    # a refusal is no trial


@pytest.mark.parametrize("cap", [{"max_input_tokens_per_day": 2000}, {"max_output_tokens_per_day": 15}])
def test_a_daily_token_cap_sleeps_until_the_next_midnight_utc(lab, cap):
    assert run(lab, replace(lab.pc, **cap), trials=3) == "trials"
    assert lab.fake.sleeps == [TO_MIDNIGHT] and len(lab.fake.calls) == 3
    assert "token cap" in digest(lab)


def test_the_total_cap_refuses_the_start(lab):
    for n in (1, 2):
        lab.rec.add_proposal(plugin=f"done{n}", timeframe="1h", state="judged", judge_exit=0, verdict=verdict())
    with pytest.raises(StartRefused, match="trials_total"):
        run(lab, replace(lab.pc, trials_total=2))
    assert lab.fake.calls == [] and lab.fake.judges == []
    assert not (lab.state / "sessions").exists()


def test_the_total_cap_stops_a_session(lab):
    assert run(lab, replace(lab.pc, trials_total=2)) == "trials_total"
    assert len(lab.fake.judges) == 2


def test_days_elapsed_stop_the_session(lab):
    def slow(fake):
        fake.clock += timedelta(hours=10)
    lab.fake.on_judge = slow
    assert run(lab, days=1) == "days"
    assert len(lab.fake.judges) == 3                     # rounds start at 0 h, 10 h and 20 h


def test_the_quota_day_is_the_utc_day_whatever_the_clocks_zone(lab):
    lab.fake.clock = datetime(2026, 11, 3, 1, 0, tzinfo=timezone(timedelta(hours=2)))   # 23:00 UTC on the 2nd
    assert run(lab, replace(lab.pc, trials_per_day=1), trials=2) == "trials"
    assert lab.fake.sleeps == [3600.0]


def test_a_quota_sleep_ends_at_the_session_deadline(lab):
    assert run(lab, replace(lab.pc, trials_per_day=1), days=0.25) == "days"
    assert lab.fake.sleeps == [6 * 3600.0] and len(lab.fake.judges) == 1


# -- stops
def test_three_judge_infrastructure_errors_in_a_row_stop_the_session(lab):
    lab.fake.judge_exits = [1, 1, 0, 1, 1, 1]            # the exit 0 resets the count
    assert run(lab) == "infra_errors"
    assert len(lab.fake.judges) == 6


def test_preflight_infrastructure_errors_count_with_the_judges(lab):
    err = (1, {**ok_report(), "ok": False, "error": "SandboxUnavailable: no bwrap"})
    lab.fake.preflight_results = [err, (0, ok_report()), err]
    lab.fake.judge_exits = [1]
    assert run(lab) == "infra_errors"
    assert [(p["state"], p["broken_reason"]) for p in props(lab)] == [
        ("dropped", "preflight_error"), ("broken", "judge_error"), ("dropped", "preflight_error")]
    assert len(lab.fake.calls) == 3                      # an infrastructure error is not sent back to the model


def test_five_dropped_proposals_in_a_row_stop_the_session(lab):
    lab.fake.replies = [BAD] * 20
    assert run(lab) == "stuck"
    assert len(lab.fake.calls) == 20                     # each proposal: one call and 3 fix-ups
    assert [(p["state"], p["broken_reason"]) for p in props(lab)] == [("dropped", "fixups")] * 5
    assert lab.fake.preflights == [] and lab.fake.judges == []
    assert "dropped 5" in digest(lab)


def test_a_judged_proposal_resets_the_dropped_count(lab):
    lab.fake.replies = [BAD] * 16 + [new("Fine")] + [BAD] * 20
    assert run(lab) == "stuck"
    assert [p["state"] for p in props(lab)] == ["dropped"] * 4 + ["judged"] + ["dropped"] * 5


@pytest.mark.parametrize("error, reason", [
    (ModelCallError("claude exited 1; stderr: CLI-STDERR-TAIL"), "model_call"),
    (ModelConfigError("the CLI's init failed the check: tools ['Bash']"), "model_config"),
])
def test_a_model_error_stops_the_session_and_its_text_reaches_only_the_digest(lab, error, reason):
    lab.fake.replies = [error]
    assert run(lab) == reason
    assert str(error) in digest(lab) and lab.rec.session(1)["stop_reason"] == reason
    (p,) = props(lab)
    assert (p["state"], p["broken_reason"]) == ("dropped", reason)


def test_a_pin_change_stops_with_harness_changed_before_preflight(lab):
    lab.fake.pin_error = PinError("harness_changed")
    assert run(lab) == "harness_changed"
    assert lab.fake.preflights == [] and lab.fake.judges == []
    assert [(p["state"], p["broken_reason"]) for p in props(lab)] == [("dropped", "harness_changed")]


def test_a_pin_change_before_the_judge_leaves_the_copy_for_the_next_start(lab):
    lab.fake.pin_error = lambda n: PinError("harness_changed") if n == 2 else None   # 1: preflight, 2: judge
    lab.fake.replies = [new("A")]
    assert run(lab) == "harness_changed"
    assert lab.fake.judges == [] and [p["state"] for p in props(lab)] == ["copied"]
    lab.fake.pin_error = None
    assert run(lab, trials=0) == "trials"                # recovery judges it first
    assert [j[0] for j in lab.fake.judges] == ["px0001_a"] and props(lab)[0]["state"] == "judged"


def test_a_budget_spent_verdict_stops_the_session_and_refuses_the_next_start(lab):
    lab.fake.verdict = verdict(decision="promote", holdout_status="blocked", holdout_reason="budget_spent")
    assert run(lab) == "budget_spent"
    assert len(lab.fake.judges) == 1 and props(lab)[0]["state"] == "judged"
    with pytest.raises(StartRefused, match="budget"):
        run(lab)
    assert len(lab.fake.calls) == 1


# -- fix-ups
def test_a_fixup_holds_the_rounds_prompt_the_rejected_proposal_and_the_errors(lab):
    lookahead = (3, {**ok_report(), "ok": False, "lookahead": ["peeks at close[t+1] at 2026-07-03"]})
    lab.fake.replies = [BAD, port(999), new("A"), new("A")]
    lab.fake.preflight_results = [lookahead]
    assert run(lab, trials=1) == "trials"
    c = lab.fake.calls
    assert len(c) == 4 and [p["state"] for p in props(lab)] == ["judged"]
    base = c[0].user
    for k, (rejected, error) in enumerate([(BAD, "hypothesis: required"), (port(999), "there is no idea #999"),
                                           (new("A"), "peeks at close[t+1] at 2026-07-03")], start=1):
        assert c[k].user.startswith(base.rstrip("\n"))   # the round's prompt, resent whole
        assert json.dumps(rejected, indent=1) in c[k].user and error in c[k].user
    # each fix-up carries only the last refusal, never one nested inside another
    assert "hypothesis: required" not in c[2].user and "there is no idea #999" not in c[3].user
    assert {x.system for x in c} == {c[0].system}


def test_a_near_copy_found_by_preflight_goes_back_to_the_model(lab):
    i = closed_idea(lab)
    near = {**ok_report(), "overlap": [{"strategy": "old_plugin", "timeframe": "1h", "overlap": 0.7, "mirror": 0.0,
                                        "judged": True}]}
    lab.fake.preflight_results = [(0, near)]
    lab.fake.replies = [new("A"), new("B")]
    assert run(lab, trials=1) == "trials"
    assert f"near-copy of closed idea #{i}" in lab.fake.calls[1].user
    assert [p["plugin"] for p in props(lab)[1:]] == ["px0002_b"]


# -- signals
def test_one_sigint_lets_the_judge_finish_then_stops(lab):
    lab.fake.on_judge = lambda f: signal.raise_signal(signal.SIGINT)
    before = signal.getsignal(signal.SIGINT)
    assert run(lab) == "signal"
    (p,) = props(lab)
    assert (p["state"], p["judge_exit"], p["pile"]) == ("judged", 0, "no signal")
    assert not lab.fake.killed and len(lab.fake.calls) == 1
    assert signal.getsignal(signal.SIGINT) is before


def test_two_signals_kill_the_judge_and_record_interrupted(lab):
    def twice(fake):
        signal.raise_signal(signal.SIGINT)
        signal.raise_signal(signal.SIGTERM)
    lab.fake.on_judge = twice
    assert run(lab) == "signal"
    assert lab.fake.killed
    (p,) = props(lab)
    assert (p["state"], p["broken_reason"], p["judge_exit"], p["judged_at"]) == ("broken", "interrupted", -9, T0)
    assert lab.rec.judge_calls_on(T0.date()) == 1 and lab.rec.trials_used() == 1


def test_a_signal_just_before_the_judge_starts_no_judge(lab):
    # 1: the pin check before preflight, 2: the one before the judge, after the copy
    lab.fake.pin_error = lambda n: signal.raise_signal(signal.SIGINT) if n == 2 else None
    assert run(lab) == "signal"
    assert lab.fake.judges == [] and [p["state"] for p in props(lab)] == ["copied"]   # for the next start
    assert lab.rec.judge_calls_on(T0.date()) == 0


def test_a_signal_just_before_a_recovery_judge_starts_no_judge(lab):
    _copied(lab)
    lab.fake.pin_error = lambda n: signal.raise_signal(signal.SIGTERM)
    assert run(lab) == "signal"
    assert lab.fake.judges == [] and [p["state"] for p in props(lab)] == ["copied"]


def test_a_signal_outside_a_judge_ends_the_wait_at_once(lab):
    lab.fake.on_call = lambda f: signal.raise_signal(signal.SIGTERM)
    assert run(lab) == "signal"
    assert lab.fake.preflights == [] and lab.fake.judges == []
    assert [(p["state"], p["broken_reason"]) for p in props(lab)] == [("dropped", "signal")]


def test_a_signal_during_a_quota_sleep_stops(lab):
    lab.fake.on_sleep = lambda f: signal.raise_signal(signal.SIGINT)
    assert run(lab, replace(lab.pc, trials_per_day=1)) == "signal"
    assert len(lab.fake.judges) == 1 and len(lab.fake.sleeps) == 1


# -- usage limits
@pytest.mark.parametrize("resets, slept", [(T0 + timedelta(hours=2), 7200.0), (None, 3600.0)])
def test_a_usage_limit_sleeps_until_its_reset_then_repeats_the_same_call(lab, resets, slept):
    lab.fake.replies = [UsageLimit(resets), new("A")]
    assert run(lab, trials=1) == "trials"
    assert lab.fake.sleeps == [slept]
    c = lab.fake.calls
    assert len(c) == 2 and c[0].user == c[1].user
    assert [p["state"] for p in props(lab)] == ["judged"]
    assert "paused: the Claude Code usage limit" in digest(lab)


# -- the day changes
def test_a_utc_day_change_between_preflight_and_judge_reruns_preflight(lab):
    lab.fake.clock = datetime(2026, 11, 2, 23, 59, 30, tzinfo=UTC)

    def past_midnight(fake):
        if len(fake.preflights) == 1:
            fake.clock += timedelta(minutes=1)
    lab.fake.on_preflight = past_midnight
    assert run(lab, trials=1) == "trials"
    assert len(lab.fake.preflights) == 2 and len(lab.fake.judges) == 1
    assert lab.fake.pin_checks == 3                      # before each preflight and the judge


def test_without_a_day_change_preflight_runs_once(lab):
    assert run(lab, trials=1) == "trials"
    assert len(lab.fake.preflights) == 1 and lab.fake.pin_checks == 2


# -- the copy and recovery
def test_name_taken_leaves_the_existing_file_unchanged(lab):
    taken = lab.root / "strategies" / "px0001_idea_1.py"
    taken.write_bytes(b'"""left by a crashed session"""\n')
    lab.fake.replies = [new("Idea 1")]
    assert run(lab) == "model_call"
    assert taken.read_bytes() == b'"""left by a crashed session"""\n'
    assert lab.fake.judges == []
    p = props(lab)[0]
    assert (p["state"], p["broken_reason"], p["judge_exit"]) == ("broken", "name_taken", None)
    assert "name_taken" in digest(lab)


def _copied(lab, seq=5, name="px0005_recovered", state="copied", text=None):
    i = lab.rec.add_idea("Recovered", card("Recovered"), "proposer", "1h")
    text = SRC.encode() if text is None else text
    (lab.root / "strategies" / f"{name}.py").write_bytes(text)
    lab.rec.add_proposal(seq=seq, idea_id=i, kind="new", plugin=name, timeframe="1h", hash=blob_hash(SRC.encode()),
                         source=SRC, state=state)
    return i


def test_recovery_rejudges_a_copied_proposal(lab):
    i = _copied(lab)
    assert run(lab, trials=0) == "trials"
    assert lab.fake.calls == [] and [j[0] for j in lab.fake.judges] == ["px0005_recovered"]
    (p,) = props(lab)
    assert (p["state"], p["judge_exit"], p["pile"], p["judged_at"]) == ("judged", 0, "no signal", T0)
    assert lab.rec.idea(i)["family"] == "family:px0005_recovered"
    assert "recovered" in digest(lab)


def test_a_signal_during_recovery_stops_before_the_next_judge(lab):
    _copied(lab)
    i = lab.rec.add_idea("Recovered too", card("Recovered too"), "proposer", "1h")
    (lab.root / "strategies" / "px0006_recovered_too.py").write_bytes(SRC.encode())
    lab.rec.add_proposal(seq=6, idea_id=i, kind="new", plugin="px0006_recovered_too", timeframe="1h",
                         hash=blob_hash(SRC.encode()), source=SRC, state="copied")
    lab.fake.on_judge = lambda f: signal.raise_signal(signal.SIGINT)
    assert run(lab) == "signal"
    assert [j[0] for j in lab.fake.judges] == ["px0005_recovered"]
    assert [p["state"] for p in props(lab)] == ["judged", "copied"]


def test_recovery_judges_a_staged_proposal_whose_copy_was_made(lab):
    _copied(lab, state="staged")
    assert run(lab, trials=0) == "trials"
    assert [j[0] for j in lab.fake.judges] == ["px0005_recovered"] and props(lab)[0]["state"] == "judged"


def test_recovery_never_judges_other_bytes_under_the_name(lab):
    _copied(lab, text=b'"""edited by hand"""\n')
    assert run(lab, trials=0) == "trials"
    assert lab.fake.judges == []
    assert [(p["state"], p["broken_reason"]) for p in props(lab)] == [("broken", "plugin_changed")]


# -- after-trial overlap
def test_a_correlation_of_085_with_a_closed_ideas_run_files_a_duplicate(lab):
    old = closed_idea(lab)
    assert idea(lab, "Old fade")["status"] == "open"     # the session's snapshot settles it as dead
    lab.fake.verdict = verdict("no signal", overlap={
        "correlation": [{"strategy": "old_plugin", "timeframe": "1h", "value": 0.85}]})
    assert run(lab, trials=1) == "trials"
    p = props(lab)[-1]
    assert (p["duplicate_of"], p["pile"]) == (old, "no signal")
    assert idea(lab, "Idea 1")["status"] == "duplicate" and idea(lab, "Old fade")["status"] == "dead"
    assert f"duplicate of idea #{old}" in digest(lab)


def test_a_better_pile_than_the_closed_ideas_best_keeps_its_pile_and_notes_the_overlap(lab):
    old = closed_idea(lab)
    lab.fake.verdict = verdict("signal before costs", overlap={
        "correlation": [{"strategy": "old_plugin", "timeframe": "1h", "value": 0.85}]})
    assert run(lab, trials=1) == "trials"
    p = props(lab)[-1]
    assert (p["duplicate_of"], p["pile"]) == (None, "signal before costs")
    mine = idea(lab, "Idea 1")
    assert mine["status"] == "open"
    (note,) = mine["card"]["notes"]
    assert f"#{old}" in note and "0.85" in note


def test_a_mirror_overlap_of_06_is_a_duplicate_whatever_its_pile(lab):
    old = closed_idea(lab)
    lab.fake.verdict = verdict("close call", overlap={
        "mirror": [{"strategy": "old_plugin", "timeframe": "1h", "value": 0.6}]})
    assert run(lab, trials=1) == "trials"
    p = props(lab)[-1]
    assert (p["duplicate_of"], p["pile"]) == (old, "close call")
    assert idea(lab, "Idea 1")["status"] == "duplicate"


def test_a_correlation_of_minus_085_is_a_mirror_of_an_open_idea_too(lab):
    i = lab.rec.add_idea("Open one", card("Open one"), "proposer", "1h")
    lab.rec.add_proposal(idea_id=i, plugin="open_plugin", timeframe="1h", kind="new", state="judged", judge_exit=0,
                         verdict=verdict("signal before costs"))
    lab.fake.verdict = verdict("close call", overlap={
        "correlation": [{"strategy": "open_plugin", "timeframe": "1h", "value": -0.85}]})
    assert run(lab, trials=1) == "trials"
    assert props(lab)[-1]["duplicate_of"] == i


def test_a_near_copy_of_an_open_idea_after_the_trial_is_no_duplicate(lab):
    i = lab.rec.add_idea("Open one", card("Open one"), "proposer", "1h")
    lab.rec.add_proposal(idea_id=i, plugin="open_plugin", timeframe="1h", kind="new", state="judged", judge_exit=0,
                         verdict=verdict("signal before costs"))
    lab.fake.verdict = verdict("no signal", overlap={
        "position": [{"strategy": "open_plugin", "timeframe": "1h", "value": 0.9}]})
    assert run(lab, trials=1) == "trials"
    assert props(lab)[-1]["duplicate_of"] is None


def test_a_port_is_exempt_from_the_closed_idea_it_ports(lab):
    pc = replace(lab.pc, proposals_per_idea=1)           # one judged plugin exhausts an idea
    x = closed_idea(lab, "Breakout", "brk", pile="signal before costs")
    lab.rec.set_idea(x, card={**card("Breakout"), "notes": ["proposal 1: overlaps something of its own"]})
    lab.fake.replies = [port(x)]
    lab.fake.verdict = verdict("no signal", overlap={
        "correlation": [{"strategy": "brk", "timeframe": "1h", "value": 0.9}],
        "position": [{"strategy": "brk", "timeframe": "1h", "value": 0.8}]})
    assert run(lab, pc, trials=1) == "trials"
    assert idea(lab, "Breakout")["status"] == "exhausted"
    p = props(lab)[-1]
    port_idea = idea(lab, "Breakout, 4h")
    assert (p["kind"], p["idea_id"], p["duplicate_of"]) == ("port", port_idea["id"], None)
    assert (port_idea["port_of"], port_idea["timeframe"], port_idea["card"]) == (x, "4h", card("Breakout"))


# -- the start
def test_the_start_takes_a_lock_a_second_session_is_refused(lab):
    lab.state.mkdir(parents=True, exist_ok=True)
    with open(lab.state / "session.lock", "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(StartRefused, match="another"):
            run(lab)
    assert lab.fake.calls == []
    assert run(lab, trials=0) == "trials"                # released: the next start goes ahead


@pytest.mark.parametrize("step, error", [
    ("pin_take", PinError("dirty")),
    ("pin_take", RuntimeError("git rev-parse HEAD failed (128): not a git repository")),
    ("cli_version", FileNotFoundError(2, "No such file or directory", "claude")),
    ("probe", ModelCallError("claude exited 1; stderr: please run /login")),
    ("probe", ModelConfigError("the CLI's init failed the check: model 'claude-x'")),
])
def test_a_failed_start_check_is_a_start_refusal(lab, step, error):
    lab.fake.start_error[step] = error
    with pytest.raises(StartRefused):
        run(lab)
    assert lab.fake.calls == [] and not (lab.state / "sessions").exists()


def test_a_bad_config_is_refused_at_the_start(lab):
    lab.cfg.raw["proposer"] = {**lab.cfg.raw["proposer"], "model": "opus"}
    with pytest.raises(StartRefused, match="pinned"):
        run(lab)


def test_the_header_warns_off_main(lab):
    lab.fake.branch = "feat/proposer"
    run(lab, trials=0)
    assert "**Warning:** the harness is on branch feat/proposer" in digest(lab)


def test_the_snapshot_rereads_every_verdict_and_settles_the_ideas(lab):
    i = lab.rec.add_idea("Prior", card("Prior"), "proposer", "1h")
    data = SRC.encode()
    pending = {**verdict(decision="promote", holdout_status="pending", holdout_reason="awaiting_exam"),
               "lineage_id": "family:prior"}
    lab.rec.add_proposal(idea_id=i, plugin="prior", timeframe="1h", kind="new", hash=blob_hash(data), source=SRC,
                         state="judged", judge_exit=0, verdict=pending, pile="promoted")
    vdir = Path(lab.cfg["judge"]["verdicts_dir"])
    vdir.mkdir(parents=True)
    (vdir / f"prior@1h@{blob_hash(data)[:10]}.json").write_text(json.dumps({**pending, "holdout_status": "passed"}))
    assert run(lab, trials=0) == "trials"
    assert props(lab)[0]["verdict"]["holdout_status"] == "passed"
    assert (lab.rec.idea(i)["status"], lab.rec.idea(i)["family"]) == ("passed", "family:prior")


def test_an_empty_record_is_seeded_and_refused_until_the_seed_is_refreshed(tmp_path, cfg, monkeypatch):  # noqa: F811
    monkeypatch.chdir(tmp_path)
    sdir = tmp_path / "strategies"
    sdir.mkdir()
    for plugin in FAMILY:
        (sdir / f"{plugin}.py").write_text(f'"""{plugin}, a stand-in."""\n')
    vdir = Path(cfg["judge"]["verdicts_dir"])
    vdir.mkdir(parents=True)
    seedlab = SimpleNamespace(strategies=sdir, verdicts=vdir)
    fake = Fake(cfg)
    pc = settings.load(cfg)
    as_today(seedlab)
    with pytest.raises(StartRefused, match="lab verdict-refresh donchian_atr --tf 4h"):
        loop.run_session(cfg, pc, deps=fake.deps(), repo=tmp_path, trials=0)
    refreshed(seedlab)
    assert loop.run_session(cfg, pc, deps=fake.deps(), repo=tmp_path, trials=0) == "trials"
    rec = Record(tmp_path / pc.state_dir)
    assert len(rec.ideas()) == 11 and rec.trials_used() == 0
    assert {p["pile"] for p in rec.proposals()} >= {"no signal"}           # the snapshot read the refreshed files


# -- the real deps' wiring (no process is started: proc.run is replaced)
class _Popen:
    def __init__(self, argv, out=b"", code=0):
        self.argv, self.out, self.returncode, self.pid = argv, out, None, 4242
        self._code = code

    def communicate(self):
        self.returncode = self._code
        return self.out, b""

    def wait(self):
        self.returncode = self._code
        return self._code


@pytest.mark.parametrize("default", [True, False])
def test_default_deps_run_the_allowed_lab_commands(lab, monkeypatch, default):
    started = []

    def fake_run(argv, *, claude_bin, **kw):
        proc.allowed(argv, claude_bin)                   # the shapes proc.py lets through (R14)
        p = _Popen(argv, out=b"log line\n" + json.dumps(ok_report()).encode() + b"\n")
        started.append((argv, kw))
        return p
    monkeypatch.setattr(loop.proc, "run", fake_run)
    if default:
        monkeypatch.setattr(lab.cfg, "path", loop.DEFAULT_CONFIG)
    deps = loop.default_deps(lab.cfg, lab.pc, lab.root)
    extra = [] if default else ["--config", str(Path(lab.cfg.path).resolve())]
    staged = lab.state / "staging" / "px0001_a.py"
    assert deps.preflight(staged, "1h") == (0, ok_report())
    log = lab.state / "logs" / "px0001_a.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    assert deps.judge("px0001_a", "1h", log) == 0
    (pa, pkw), (ja, jkw) = started
    assert pa == proc.lab_argv("preflight", str(staged), "--tf", "1h", *extra)
    assert ja == proc.lab_argv("judge", "px0001_a", "--tf", "1h", "--proposer", *extra)
    assert jkw["stderr"] is proc.STDOUT and jkw["start_new_session"] and jkw["stdout"].name == str(log)
    assert pkw["stdout"] is proc.PIPE and pkw["start_new_session"]

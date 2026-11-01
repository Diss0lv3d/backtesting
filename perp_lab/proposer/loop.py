"""The propose loop: an unattended session that asks the model for one plugin per round and files the verdict.

Trusted harness code around an untrusted model (ADR 0006; spec "Shape", "One round", "Pace and budget"). Each
round: the prompt (the static context, the record, the round's instruction); a fresh model call; the checks in
code (the contract, the idea rules, the source); `lab preflight` on the staged file; the copy to strategies/;
`lab judge --proposer`; the verdict file read and classified into the record; a digest line. A check that fails
goes back to the model as a fix-up, at most `fixups_per_proposal` per proposal, and then the proposal is dropped.
The model sees only its own refused proposal and the check errors: never the judge's output, never the CLI's
error text (R35), never a time.

Everything that touches the world is a `Deps` callable, so the loop runs in tests with fakes and a fake clock.

`run_session` returns why the session stopped: trials_total, days or trials (the caps); infra_errors (3 judge or
preflight exits of 1 in a row, a judge counting once after its retries); stuck (5 proposals in a row dropped
after their fix-ups); model_call or model_config (the model call); harness_changed (the pin); signal (SIGINT,
SIGTERM or SIGHUP); budget_spent (a verdict shows the global holdout budget spent). A start a guard refuses raises
StartRefused instead (R34).

Paths: the state dir resolves against the repo (the checkout the editable install points at), never the working
directory. strategies/ and the verdicts dir are the judge's own, relative to the working directory, which in a
real session is the repo root (`lab propose` runs from there).
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import signal
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Callable

from perp_lab.config import DEFAULT_CONFIG
from perp_lab.plugins import blob_hash
from perp_lab.proposer import assemble, context, contract, model, piles, pin, proc, prompt, seed, settings
from perp_lab.proposer.digest import Digest
from perp_lab.proposer.record import Record
from perp_lab.proposer.settings import StartRefused

UTC = timezone.utc
INFRA_MAX = 3              # judge or preflight exits of 1 in a row
JUDGE_TRIES = 3            # a judge exit 1 is tried again twice on the same bytes (R45)
STUCK_MAX = 5              # proposals in a row dropped after their fix-ups
USAGE_BACKOFF_S = 3600     # a usage limit that gives no reset time
USAGE_MIN_S = 60           # a reset time already past must not turn the retry into a busy loop
SLEEP_CHUNK_S = 300        # a wait re-reads the wall clock this often: a suspend doesn't advance time.sleep (R44)
PROGRESS = ("promoted", "close call", "signal before costs")
STOPS = ("trials_total", "days", "trials", "infra_errors", "stuck", "model_call", "model_config",
         "harness_changed", "signal", "budget_spent")


@dataclass(frozen=True)
class Deps:
    """What the loop does to the world. `default_deps` wires the real ones."""

    call_model: Callable   # (system, user, schema, transcript) -> model.Reply, after retries; or a model error
    preflight: Callable   # (path, tf) -> (exit code, report): 0 passed, 3 failed a check, 1 infrastructure error
    judge: Callable        # (name, tf, log) -> the judge's exit code, once it has exited
    now: Callable          # () -> an aware datetime
    sleep: Callable        # (seconds) -> None
    pin_check: Callable    # () -> None, or pin.PinError: the harness changed since pin_take
    pin_take: Callable     # () -> pin.Pin, or pin.PinError("dirty")
    cli_version: Callable  # () -> str
    probe: Callable        # (transcript) -> None, or a model error: the CLI is missing, logged out or misconfigured
    kill_judge: Callable   # () -> None: kill the running judge's process group (the second signal)


class _Stop(Exception):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(reason)
        self.reason, self.detail = reason, detail


class _Abort(BaseException):
    """A signal during a wait the loop may cut short. BaseException, so no `except Exception` on the way up eats
    it: a model call's own cleanup still runs (its `finally` kills the CLI)."""


class _RoundOver(Exception):
    """The proposal has its final record and the round ends, with no stop."""


class _Signals:
    """SIGINT, SIGTERM and SIGHUP for the session (spec "Pace and budget", stop conditions). SIGHUP, from a closed
    terminal, is handled as SIGTERM is: unhandled, it would end the session with no record of its end and leave
    the judge orphaned (M-4).

    A judge is never cut short by the first signal: it finishes, its verdict is filed, and the session stops. The
    second kills it. Outside a judge, a signal that comes during a wait (a model call, a preflight, a sleep) ends
    the wait at once; anywhere else, the session stops at its next check. Nothing else is interrupted midway.
    """

    def __init__(self, kill_judge: Callable[[], None]):
        self.count = 0
        self.killed = False
        self._judging = self._waiting = False
        self._kill = kill_judge

    def __call__(self, signum, frame):
        self.count += 1
        if self._judging:
            if self.count >= 2 and not self.killed:
                self.killed = True
                self._kill()
        elif self._waiting:
            self._waiting = False
            raise _Abort

    @contextlib.contextmanager
    def waiting(self):
        if self.count:
            raise _Abort
        self._waiting = True
        try:
            yield
        finally:
            self._waiting = False

    @contextlib.contextmanager
    def judging(self):
        self.killed, self._judging = False, True
        try:
            yield
        finally:
            self._judging = False

    @contextlib.contextmanager
    def installed(self):
        old = {s: signal.signal(s, self) for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        try:
            yield
        finally:
            for s, h in old.items():
                signal.signal(s, h)


@dataclass
class _Prop:
    """What the loop knows of one proposal so far."""

    seq: int
    kind: str | None = None
    tf: str | None = None
    name: str | None = None
    title: str | None = None
    text: bytes | None = None
    hash: str | None = None
    idea_id: int | None = None
    exempt: set = field(default_factory=set)   # ideas its after-trial overlap is not checked against (R46)
    proposal: contract.Proposal | None = None
    target: dict | None = None
    copied: bool = False
    closed: bool = False
    recovered: bool = False


# -- the record, settled from the verdicts
def current_n(record: Record) -> int:
    """N for the prompt: the largest n_trials among the record's verdicts (R32). N never falls, and the loop may
    not read lab.db."""
    ns = [v.get("n_trials") for p in record.proposals() if isinstance(v := p.get("verdict"), dict)]
    ns = [n for n in ns if isinstance(n, (int, float)) and not isinstance(n, bool)]
    return int(max(ns)) if ns else 0


def _walked(verdict) -> bool:
    return isinstance(verdict, dict) and verdict.get("walkforward") is not None


def settle(record: Record, pc) -> None:
    """Every idea's family, root plugin and status, from its proposals' verdicts, in code. The first plugin with a
    walk-forward run sets an unset family (spec, "Assemble and check"); a spent holdout read closes every idea with
    a judged plugin in that family (R43); an excluded idea stays excluded."""
    props = record.proposals()
    outcomes = piles.family_outcomes([p["verdict"] for p in props if isinstance(p.get("verdict"), dict)])
    for idea in record.ideas():
        mine = [p for p in props if p["idea_id"] == idea["id"]]
        f = {}
        if idea.get("family") is None:
            root = next((p for p in mine if p.get("judge_exit") is not None and _walked(p.get("verdict"))), None)
            if root is not None and root["verdict"].get("lineage_id"):
                f["family"] = root["verdict"]["lineage_id"]
                if idea.get("root_plugin") is None:
                    f["root_plugin"] = root["plugin"]
        status = piles.idea_status(idea, mine, piles.idea_outcome(mine, outcomes), pc.proposals_per_idea)
        if status != idea["status"]:
            f["status"] = status
        if f:
            record.set_idea(idea["id"], **f)


def snapshot(record: Record, cfg, pc) -> None:
    """At the session's start only: re-read the verdict file of every proposal with a plugin version, which picks up
    exam outcomes and refreshed diagnostics, then settle the ideas. A missing file keeps what the record has."""
    for p in record.proposals():
        if not (p.get("plugin") and p.get("timeframe") and p.get("hash")):
            continue
        v = seed.read_verdict(seed.verdict_path(cfg, p["plugin"], p["timeframe"], p["hash"]))
        if v is not None and v != p.get("verdict"):
            record.update_proposal(p["seq"], verdict=v, pile=piles.pile(v))
    settle(record, pc)


def _exempt(record: Record, idea_id: int) -> set[int]:
    """The ideas a proposal's after-trial overlap is not checked against: its idea's port group (R46)."""
    ideas = {i["id"]: i for i in record.ideas()}
    return assemble.port_group(ideas, idea_id, ideas[idea_id].get("port_of"))


# What a verdict file says when the judge kills on its last transient attempt: PluginError.public_reason() for the
# sandbox's TRANSIENT_KINDS (died, timeout, cpu_limit), which the proposer may not import; tests/test_judge.py pins
# these against the judge's own verdict file.
TRANSIENT_REASONS = ("plugin_error: died", "plugin_error: timeout", "plugin_error: cpu_limit")


def transient_kill(verdict) -> bool:
    """A kill for a failure the machine can cause, not only the plugin: an infrastructure error, not a result
    (R48)."""
    return (isinstance(verdict, dict) and verdict.get("decision") == "kill"
            and any(str(r) in TRANSIENT_REASONS for r in verdict.get("reasons") or []))


def _budget_spent(verdict) -> bool:
    return isinstance(verdict, dict) and verdict.get("holdout_reason") == "budget_spent"


def _best(record: Record, idea_id: int) -> str | None:
    return piles.best_pile([piles.pile(p.get("verdict")) for p in record.proposals(idea_id)
                            if p.get("judge_exit") is not None and p.get("duplicate_of") is None])


def _value(e: dict) -> float | None:
    v = e.get("value")
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def after_trial(record: Record, pc, seq: int, exempt: set, pile: str, verdict: dict) -> tuple[int | None, list[str]]:
    """(the idea it duplicates, or None; notes for its card), from the verdict's overlap block (spec, "After-trial
    overlap"). Runs map to ideas through the record; a run the record doesn't hold is no idea's.

    A mirror of any judged run (mirror overlap >= overlap_max, or correlation <= -corr_max) is a duplicate whatever
    its pile. A near-copy (overlap >= overlap_max, or correlation >= corr_max) of a closed idea, other than the
    exempt ones, is a duplicate unless its pile beats that idea's best: then it keeps its pile and the overlap goes
    on its card. An idea with no best pile known can't be beaten."""
    ov = ((verdict.get("diagnostics") or {}).get("overlap")) or {}
    owner = {(x["plugin"], x["timeframe"]): x["idea_id"] for x in record.proposals()
             if x.get("plugin") and x["seq"] != seq}
    ideas = {i["id"]: i for i in record.ideas()}

    def hits(key, test):
        for e in ov.get(key) or []:
            v, idea = _value(e), ideas.get(owner.get((e.get("strategy"), e.get("timeframe"))))
            if idea is not None and v is not None and test(v):
                yield e, v, idea

    for key, test in (("mirror", lambda v: v >= pc.overlap_max), ("correlation", lambda v: v <= -pc.corr_max)):
        for _, _, idea in hits(key, test):
            return idea["id"], []
    notes, seen = [], set()
    for key, limit, what in (("position", pc.overlap_max, "OOS position overlap"),
                             ("correlation", pc.corr_max, "OOS daily-return correlation")):
        for e, v, idea in hits(key, lambda v, limit=limit: v >= limit):
            if idea["id"] in exempt or idea["status"] == "open" or idea["id"] in seen:
                continue
            best = _best(record, idea["id"])
            if best is None or pile not in piles.RANK or piles.RANK[pile] <= piles.RANK[best]:
                return idea["id"], []
            seen.add(idea["id"])
            notes.append(f"proposal {seq}: {what} {v:.2f} with {e['strategy']}@{e['timeframe']} of closed idea "
                         f"#{idea['id']} ({idea['title']}); it keeps its pile, {pile}, above that idea's best, {best}")
    return None, notes


def _tokens(usage: dict) -> tuple[int, int]:
    """Everything the model read, cached or not, and what it wrote (R31). A missing key counts 0."""
    def n(k):
        v = usage.get(k)
        return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else 0
    return n("input_tokens") + n("cache_creation_input_tokens") + n("cache_read_input_tokens"), n("output_tokens")


def _preflight_errors(report: dict) -> list[str]:
    errs = [f"metadata: {m}" for m in report.get("meta") or []]
    errs += [f"lookahead: {m}" for m in report.get("lookahead") or []]
    errs += [f"a mirror of the judged plugin {m} (the same positions, opposite side) is refused"
             for m in report.get("mirror_of") or []]
    errs += [f"{k} could not be checked: {why}" for k, why in (report.get("skipped") or {}).items()]
    return errs + ["the preflight report: " + json.dumps(report, sort_keys=True, ensure_ascii=False)]


def _card(c: contract.IdeaCard) -> dict:
    return {**asdict(c), "title": c.title.strip(), "trades_per_coin_per_year": float(c.trades_per_coin_per_year),
            "columns": list(c.columns)}


def _short(s: str, n: int = 160) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


# -- the session
class _Session:
    def __init__(self, cfg, pc, deps: Deps, record: Record, state: Path, harness: pin.Pin, cli: str):
        self.cfg, self.pc, self.deps, self.record, self.state = cfg, pc, deps, record, state
        self.harness, self.cli = harness, cli
        self.system = context.static_context(cfg, pc)
        self.system_sha = hashlib.sha256(self.system.encode("utf-8")).hexdigest()
        self.schema = contract.schema(pc.timeframes)
        self.sig = _Signals(deps.kill_judge)
        self.infra = self.stuck = 0
        self.tokens = [0, 0]
        self.handled: list[int] = []

    # -- start and end
    def run(self, days: float | None, trials: int | None) -> str:
        start = self.deps.now()
        self.deadline = start + timedelta(days=days) if days is not None else None
        self.trials, self.base = trials, self.record.trials_used()
        self.best0 = {i["id"]: _best(self.record, i["id"]) for i in self.record.ideas()}
        caps = {k: getattr(self.pc, k) for k in ("trials_per_day", "trials_total", "proposals_per_idea",
                                                 "fixups_per_proposal", "max_input_tokens_per_day",
                                                 "max_output_tokens_per_day")}
        # The handlers stay installed until the session's record is ended: a second Ctrl-C can't cut that short.
        with self.sig.installed():
            self.sid = self.record.start_session(
                started_at=start, model=self.pc.model, cutoff=str(self.pc.model_training_cutoff),
                cli_version=self.cli, commit=self.harness.commit, branch=self.harness.branch,
                caps={**caps, "days": days, "trials": trials})
            self.digest, self.ended = None, False
            try:
                try:
                    sessions = self.state / "sessions"
                    self.digest = Digest(sessions / f"{self.sid}.md", now=self.deps.now)
                    self.digest.header(self.pc.model, self.pc.model_training_cutoff, self.cli, self.harness.commit,
                                       self.harness.branch)
                    (sessions / f"{self.sid}.system.txt").write_text(self.system, encoding="utf-8")   # every call's
                    if self.trials == 0:            # a dry run: a recovery judge can be a trial (M-6)
                        raise _Stop("trials")
                    self._recover()
                    while True:
                        self._gate()
                        self._round()
                except _Stop as e:
                    reason, detail = e.reason, e.detail
                except _Abort:
                    reason, detail = "signal", ""
                self._end(reason, detail)
            except BaseException as e:   # fail closed (spec, Failures): the session stops, and says so where it can
                self._end("error", f"{type(e).__name__}: {e}", quiet=True)
                raise
        return reason

    def _end(self, reason: str, detail: str, quiet: bool = False) -> None:
        """The session's record first, then the digest's end. `quiet` (on an error) keeps going past a step that
        fails, so a broken digest can't leave the record's session open, and never overwrites an ended record."""
        keep = contextlib.suppress(Exception) if quiet else contextlib.nullcontext()
        if not self.ended:
            with keep:
                self.record.end_session(self.sid, reason, ended_at=self.deps.now(),
                                        trials_used=self.record.trials_used() - self.base,
                                        tokens_in=self.tokens[0], tokens_out=self.tokens[1])
                self.ended = True
        with keep:
            self._finish_digest(reason, detail)

    def _finish_digest(self, reason: str, detail: str) -> None:
        rows = {p["seq"]: p for p in self.record.proposals()}
        counts: dict[str, int] = {}
        for seq in self.handled:
            p = rows[seq]
            key = ("dropped" if p["state"] in ("dropped", "staged") else "copied" if p["state"] == "copied"
                   else "duplicate" if p["duplicate_of"] is not None else p["pile"] or "broken")
            counts[key] = counts.get(key, 0) + 1
        # New in the progress piles: an idea whose best pile is a progress pile above where it stood at the start.
        progress = []
        for i in self.record.ideas():
            best = _best(self.record, i["id"])
            if best in PROGRESS and piles.RANK[best] > piles.RANK.get(self.best0.get(i["id"]), -1):
                progress.append(f"#{i['id']} {i['title']}: {best}")
        self.digest.finish(counts, progress, f"{reason}: {detail}" if detail else reason, *self.tokens)

    # -- before each round: stops, then the day's quotas
    def _gate(self) -> None:
        while True:
            if self.sig.count:
                raise _Stop("signal")
            used = self.record.trials_used()
            if used >= self.pc.trials_total:
                raise _Stop("trials_total")
            if self.trials is not None and used - self.base >= self.trials:
                raise _Stop("trials")
            now = self.deps.now()
            if self.deadline is not None and now >= self.deadline:
                raise _Stop("days")
            day = now.astimezone(UTC).date()
            why = self._quota(day)
            if why is None:
                return
            until = self._next_day(day)
            self.digest.pause(why, until)
            self._sleep_until(until)

    def _next_day(self, day) -> datetime:
        """When a quota wait ends: the next 00:00 UTC, or the session's deadline if that comes first."""
        until = datetime.combine(day + timedelta(days=1), dtime(0), tzinfo=UTC)
        return min(until, self.deadline) if self.deadline is not None else until

    def _judge_quota(self) -> None:
        """Before a judge's retry: a call past the day's quota waits for the next UTC day, as any judge does."""
        while True:
            now = self.deps.now()
            if self.deadline is not None and now >= self.deadline:
                raise _Stop("days")
            day = now.astimezone(UTC).date()
            if self.record.judge_calls_on(day) < self.pc.trials_per_day:
                return
            until = self._next_day(day)
            self.digest.pause(f"the day's {self.pc.trials_per_day} judge calls are used", until)
            self._sleep_until(until)

    def _quota(self, day) -> str | None:
        if self.record.judge_calls_on(day) >= self.pc.trials_per_day:
            return f"the day's {self.pc.trials_per_day} judge calls are used"
        tin, tout = self.record.usage_on(day)
        if tin >= self.pc.max_input_tokens_per_day:
            return f"the day's input token cap is reached ({tin:,} of {self.pc.max_input_tokens_per_day:,})"
        if tout >= self.pc.max_output_tokens_per_day:
            return f"the day's output token cap is reached ({tout:,} of {self.pc.max_output_tokens_per_day:,})"
        return None

    def _sleep_until(self, until: datetime) -> None:
        """Sleep until a wall-clock time, in chunks: time.sleep's clock stops while the machine is suspended, so
        one long sleep would overshoot by the time spent suspended (R44). A signal ends the wait at once."""
        while (seconds := (until - self.deps.now()).total_seconds()) > 0:
            with self.sig.waiting():
                self.deps.sleep(min(seconds, SLEEP_CHUNK_S))

    # -- one round
    def _round(self) -> None:
        # The row first, before anything is named from its seq (the transcript, the staged file). Until the
        # round says otherwise the proposal is dropped: a session that dies mid-round leaves no trial behind.
        t = _Prop(seq=self.record.next_seq())
        self.record.add_proposal(seq=t.seq, session_id=self.sid, state="dropped", created_at=self.deps.now())
        self.handled.append(t.seq)
        try:
            if self._propose(t):
                self._trial(t)
        except _RoundOver:
            pass
        except _Stop as e:
            self._abandon(t, e.reason)
            raise
        except _Abort:
            self._abandon(t, "signal")
            raise

    def _abandon(self, t: _Prop, reason: str) -> None:
        """A stop mid-round: a proposal not yet copied is dropped; a copy is left for the next start's recovery."""
        if not (t.closed or t.copied):
            self.record.update_proposal(t.seq, state="dropped", broken_reason=reason)
            self.digest.line(t.seq, t.name, t.tf, t.kind, t.title, f"dropped: the session stopped ({reason})")

    def _propose(self, t: _Prop) -> bool:
        """Calls and checks until the proposal passes preflight (True), or it is dropped (False)."""
        base = (prompt.render_record(self.record, current_n(self.record), self.pc.timeframes) + "\n"
                + prompt.instruction() + "\n")
        user = base
        for attempt in range(self.pc.fixups_per_proposal + 1):
            raw = self._call(t.seq, user).proposal
            errors = self._check(t, raw)
            if errors is None:
                return True
            # Every fix-up is a fresh call: the round's prompt, the refused reply and only its errors.
            user = prompt.fixup(base, raw, errors)
        self._close(t, "dropped", f"dropped after {self.pc.fixups_per_proposal} fix-ups: {_short(errors[0])}",
                    broken_reason="fixups")
        self.stuck += 1
        if self.stuck >= STUCK_MAX:
            raise _Stop("stuck")
        return False

    def _call(self, seq: int, user: str) -> model.Reply:
        transcript = self.record.transcript_path(seq)
        while True:
            if self.sig.count:
                raise _Stop("signal")
            with open(transcript, "a", encoding="utf-8") as f:       # the prompt beside the stream it got
                f.write(json.dumps({"type": "proposer_prompt", "system_sha256": self.system_sha, "user": user},
                                   ensure_ascii=False) + "\n")
            try:
                with self.sig.waiting():
                    reply = self.deps.call_model(self.system, user, self.schema, transcript)
            except model.UsageLimit as e:
                now = self.deps.now()
                until = e.resets_at or now + timedelta(seconds=USAGE_BACKOFF_S)
                until = max(until, now + timedelta(seconds=USAGE_MIN_S))
                if self.deadline is not None:                         # --days ends the wait, as it ends a quota's
                    until = min(until, self.deadline)
                self._log_call(seq, 0, 0, None, "usage_limit")
                self.digest.pause("the Claude Code usage limit is reached", until)
                self._sleep_until(until)
                if self.deadline is not None and self.deps.now() >= self.deadline:
                    raise _Stop("days")
                continue                                              # the same call again
            except (model.ModelCallError, OSError) as e:              # OSError: the CLI gone mid-session
                self._log_call(seq, 0, 0, None, "model_call")
                raise _Stop("model_call", str(e)) from None
            except model.ModelConfigError as e:
                self._log_call(seq, 0, 0, None, "model_config")
                raise _Stop("model_config", str(e)) from None
            tin, tout = _tokens(reply.usage)
            self._log_call(seq, tin, tout, reply.cost_usd, "reply")
            return reply

    def _log_call(self, seq, tin, tout, cost, outcome) -> None:
        self.record.add_call(session_id=self.sid, seq=seq, at=self.deps.now(), tokens_in=tin, tokens_out=tout,
                             cost_usd=cost, outcome=outcome)
        if tin or tout:
            self.tokens[0] += tin
            self.tokens[1] += tout
            p = next(x for x in self.record.proposals() if x["seq"] == seq)
            self.record.update_proposal(seq, tokens_in=(p["tokens_in"] or 0) + tin,
                                        tokens_out=(p["tokens_out"] or 0) + tout)

    def _check(self, t: _Prop, raw: dict) -> list[str] | None:
        """None once the proposal passed every check and preflight; else the errors for its fix-up."""
        try:
            p = contract.parse(raw, self.pc.timeframes)
            target = assemble.check_idea_rules(p, self.record, self.pc)
            assemble.check_model_source(p.source)               # the raw source, before assembly
            title = p.idea.title.strip() if p.kind == "new" else target["title"]
            name = assemble.plugin_name(t.seq, title)
            parent = None
            if p.kind == "refinement":
                champ = piles.champion(self.record.proposals(target["id"]))
                parent = champ["plugin"] if champ else None      # no champion yet: the plugin is a root
            text = assemble.assemble(p, name, parent)
            assemble.check_source(text, name, p.timeframe, self.pc)
        except contract.ContractError as e:
            return list(e.errors)
        except assemble.Rejected as e:
            return [str(e)]
        t.proposal, t.target, t.kind, t.tf, t.name, t.title, t.text = p, target, p.kind, p.timeframe, name, title, text
        t.hash = blob_hash(text)
        staged = self.state / "staging" / f"{name}.py"
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(text)
        self.record.update_proposal(t.seq, state="staged", kind=p.kind, plugin=name, timeframe=p.timeframe,
                                    hash=t.hash, source=text.decode("utf-8"),
                                    idea_id=target["id"] if p.kind == "refinement" else None)
        code, report = self._preflight(t, staged)
        if code == 1:
            self._close(t, "dropped", "dropped: preflight infrastructure error", broken_reason="preflight_error")
            self._infra(False)
            raise _RoundOver
        if code == 3:
            return _preflight_errors(report)
        try:
            assemble.check_overlap_rules(report, p, target, self.record, self.pc)   # only on a passing report
        except assemble.Rejected as e:
            return [str(e)]
        return None

    def _preflight(self, t: _Prop, staged: Path) -> tuple[int, dict]:
        """Preflight after a pin check; again while it passed but the UTC day changed since it started, so its
        lookahead span is the judge's (spec, One round, step 8)."""
        while True:
            self._pin_check()
            day = self.deps.now().astimezone(UTC).date()
            with self.sig.waiting():
                code, report = self.deps.preflight(staged, t.tf)
            if not isinstance(report, dict):
                report = {"ok": False, "error": "preflight gave no report"}
            if code not in (0, 3) or (code == 0 and report.get("ok") is not True):
                code = 1
            self.record.update_proposal(t.seq, preflight=report)
            if code != 0 or self.deps.now().astimezone(UTC).date() == day:
                return code, report

    def _infra(self, ok: bool) -> None:
        """Count infrastructure errors in a row: judge exits of 1 and preflight exits of 1. Only a judge that
        reached a result (a verdict, or a refusal) resets the count: a passing preflight comes before every judge,
        so it must not, or judge exits of 1 could never be in a row (spec, Residuals: the exit-code channel)."""
        self.infra = 0 if ok else self.infra + 1
        if self.infra >= INFRA_MAX:
            raise _Stop("infra_errors")

    def _pin_check(self) -> None:
        try:
            self.deps.pin_check()
        except (pin.PinError, RuntimeError, OSError) as e:    # a pin that can't be checked is not held
            raise _Stop("harness_changed", str(e)) from None

    def _trial(self, t: _Prop) -> None:
        if self.sig.count:
            raise _Stop("signal")
        self._idea(t)
        self.stuck = 0
        if not self._copy(t):
            self._close(t, "broken", "broken: name_taken (strategies/ holds other bytes under its name)",
                        broken_reason="name_taken", pile="broken")
            return
        t.copied = True
        self.record.update_proposal(t.seq, state="copied")
        self._judge(t)

    def _idea(self, t: _Prop) -> None:
        """The proposal's idea: the refined one, or a new idea (a port is a new idea and a new family)."""
        p, target = t.proposal, t.target
        if p.kind == "refinement":
            t.idea_id = target["id"]
        elif p.kind == "new":
            t.idea_id = self.record.add_idea(t.title, _card(p.idea), "proposer", p.timeframe)
        else:
            taken = {i["title"].casefold() for i in self.record.ideas()}
            title = f"{target['title']}, {p.timeframe}"
            if title.casefold() in taken:
                title = f"{title} (#{t.seq})"
            card = {k: v for k, v in (target.get("card") or {}).items() if k != "notes"}   # notes on its proposals
            t.idea_id = self.record.add_idea(title, card, "proposer", p.timeframe, port_of=target["id"])
            t.title = title
        t.exempt = _exempt(self.record, t.idea_id)
        self.record.update_proposal(t.seq, idea_id=t.idea_id)

    def _copy(self, t: _Prop) -> bool:
        """Copy to strategies/, never over a file: other bytes under the name are refused (name_taken)."""
        dest = seed.STRATEGIES / f"{t.name}.py"
        try:
            with open(dest, "xb") as f:
                f.write(t.text)
        except FileExistsError:
            return dest.read_bytes() == t.text
        return True

    def _judge(self, t: _Prop) -> None:
        """`lab judge` on the copied bytes. An exit of 1 (an infrastructure error) is tried again, up to
        JUDGE_TRIES calls in all, so a transient failure doesn't file an honest plugin as broken and cost its idea
        a slot; the same bytes add no trial (R45). Each call is made after its own pin and signal checks, and
        within the day's quota."""
        logs = self.state / "logs"
        for attempt in range(JUDGE_TRIES):
            if attempt:
                self._judge_quota()
            self._pin_check()
            logs.mkdir(parents=True, exist_ok=True)
            # Only a judge already running may finish after a signal: none starts after one. The copy stays
            # `copied` for the next start's recovery (this covers recovery's own judges too).
            if self.sig.count:
                raise _Stop("signal")
            with self.sig.judging():
                code = self.deps.judge(t.name, t.tf, logs / f"{t.name}.log")
            # Every judge call counts toward the day's quota, whatever its exit code.
            at = self.deps.now()
            self.record.add_judge_call(seq=t.seq, at=at, exit=code)
            if code != 1 or self.sig.killed:
                break
        done = {"judge_exit": code, "judged_at": at}
        if self.sig.killed and code != 0:
            self._close(t, "broken", "broken: interrupted (a second signal killed the judge)",
                        broken_reason="interrupted", pile="broken", **done)
            raise _Stop("signal")
        if code == 0:
            v = seed.read_verdict(seed.verdict_path(self.cfg, t.name, t.tf, t.hash))
            if v is not None:
                self._filed(t, v, done)
                return
            self._close(t, "broken", "broken: the judge exited 0 without a verdict file", broken_reason="no_verdict",
                        pile="broken", **done)
            self._infra(False)
        elif code == 2:
            self._close(t, "broken", "broken: refused by the judge (exit 2)", broken_reason="refused", pile="broken",
                        **done)
            self._infra(True)
        else:
            self._close(t, "broken", f"broken: judge exit {code}", broken_reason="judge_error", pile="broken", **done)
            self._infra(False)

    def _filed(self, t: _Prop, verdict: dict, done: dict) -> None:
        """File a judge's verdict, then count it. A kill on the judge's last transient attempt is an infrastructure
        error, whatever the retries before it: the judge returns it instead of a 3rd exit 1, so a systemic sandbox
        failure (memory pressure, a loaded host) would otherwise reset the count and never stop the session, each
        proposal adding a trial (R48). Any other verdict is a result and resets it."""
        self._file(t, verdict, done)
        if _budget_spent(verdict):
            raise _Stop("budget_spent")
        self._infra(not transient_kill(verdict))

    def _file(self, t: _Prop, verdict: dict, done: dict) -> None:
        pile = piles.pile(verdict)
        dup, notes = after_trial(self.record, self.pc, t.seq, t.exempt, pile, verdict)
        outcome = pile if dup is None else f"duplicate of idea #{dup} ({pile})"
        if notes and t.idea_id is not None:
            idea = self.record.idea(t.idea_id)
            card = dict(idea.get("card") or {})
            card["notes"] = [*(card.get("notes") or []), *notes]
            self.record.set_idea(t.idea_id, card=card)
            outcome += "; overlaps a closed idea but keeps its pile (noted on the card)"
        if _budget_spent(verdict):
            outcome += "; the global holdout budget is spent"
        self._close(t, "judged", outcome, verdict=verdict, pile=pile, duplicate_of=dup, **done)

    def _close(self, t: _Prop, state: str, outcome: str, **f) -> None:
        """The proposal's final record, its idea settled, and its digest line."""
        self.record.update_proposal(t.seq, state=state, **f)
        t.closed = True
        settle(self.record, self.pc)
        self.digest.line(t.seq, t.name, t.tf, t.kind, t.title, ("recovered: " if t.recovered else "") + outcome)

    # -- crash recovery
    def _recover(self) -> None:
        """A proposal whose plugin is in strategies/ but whose verdict was never read is judged again (spec,
        Failures). Its bytes must be the ones recorded; anything else under its name is never judged as it. If its
        verdict file is already there, a judge finished after all (the orphan of a hard kill may finish after the
        restart), so it is filed from the file and not judged again (M-5)."""
        for p in self.record.proposals():
            if p["origin"] != "proposer" or p["state"] not in ("staged", "copied"):
                continue
            if self.sig.count:
                raise _Stop("signal")
            idea = self.record.idea(p["idea_id"]) if p["idea_id"] is not None else None
            t = _Prop(seq=p["seq"], kind=p["kind"], tf=p["timeframe"], name=p["plugin"], hash=p["hash"],
                      idea_id=p["idea_id"], title=idea["title"] if idea else None, recovered=True)
            if idea is not None:
                t.exempt = _exempt(self.record, idea["id"])
            dest = seed.STRATEGIES / f"{p['plugin']}.py" if p["plugin"] else None
            ours = bool(dest is not None and p["hash"] and dest.is_file() and blob_hash(dest.read_bytes()) == p["hash"])
            self.handled.append(p["seq"])
            if ours:
                t.copied = True
                self.record.update_proposal(p["seq"], state="copied")
                path = seed.verdict_path(self.cfg, p["plugin"], p["timeframe"], p["hash"])
                v = seed.read_verdict(path)
                if v is None:
                    self._judge(t)
                else:
                    self._file_found(t, v, path)
            elif p["state"] == "staged":                 # the session stopped before its copy
                self._close(t, "dropped", "dropped: the session stopped before its copy", broken_reason="interrupted")
            else:
                self._close(t, "broken", "broken: plugin_changed (strategies/ no longer holds its bytes)",
                            broken_reason="plugin_changed", pile="broken")

    def _file_found(self, t: _Prop, verdict: dict, path: Path) -> None:
        """File a verdict a judge wrote before this session started, as of when it was written."""
        try:
            at = datetime.fromtimestamp(path.stat().st_mtime, UTC)
        except OSError:
            at = self.deps.now()
        self._filed(t, verdict, {"judge_exit": 0, "judged_at": at})


# -- the start
@contextlib.contextmanager
def _session_lock(state: Path):
    """One session at a time: a non-blocking flock, released when the file is closed (also by a crash)."""
    state.mkdir(parents=True, exist_ok=True)
    path = state / "session.lock"
    with open(path, "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise StartRefused(f"another proposer session holds {path}") from None
        yield


def _start_checks(deps: Deps, pc, state: Path) -> tuple[pin.Pin, str]:
    """The pin, the CLI's version and the probe call: each failure is a start refusal, never a traceback (R34)."""
    try:
        harness = deps.pin_take()
    except pin.PinError as e:
        raise StartRefused("perp_lab/ or config/ has uncommitted changes; commit or stash them first: a session "
                           f"runs on one harness version ({e.reason})") from None
    except (RuntimeError, OSError) as e:
        raise StartRefused(f"can't pin the harness: {e}") from None
    try:
        cli = deps.cli_version()
        deps.probe(state / "logs" / "probe.jsonl")
    except FileNotFoundError:
        raise StartRefused(f"the Claude Code CLI ({pc.claude_bin}) is not installed or not on PATH") from None
    except model.UsageLimit as e:                       # not a logged-out CLI: say when to start again (M-7)
        when = (f"it resets at {e.resets_at.astimezone(UTC):%Y-%m-%d %H:%M} UTC" if e.resets_at is not None
                else "no reset time given")
        raise StartRefused(f"the Claude Code usage limit is reached, so the model probe was refused ({when}); "
                           "start again once it resets") from None
    except (model.ModelCallError, model.ModelConfigError, OSError) as e:
        raise StartRefused(f"the model probe failed (is the CLI logged in?): {e}") from None
    return harness, cli


def _refuse(record: Record, pc) -> None:
    used = record.trials_used()
    if used >= pc.trials_total:
        raise StartRefused(f"the proposer has used {used} of its {pc.trials_total} trials (proposer.trials_total); "
                           "raising the cap is the human's decision")
    if any(_budget_spent(p.get("verdict")) for p in record.proposals()):
        raise StartRefused("a verdict shows the global holdout budget spent (blocked / budget_spent): no promotion "
                           "can reach an exam any more, so the proposer does not start")


def run_session(cfg, pc, *, days: float | None = None, trials: int | None = None, deps: Deps | None = None,
                repo: Path | None = None) -> str:
    """Run one session until a stop condition; return the stop reason (one of STOPS). StartRefused if a start guard
    fails: the lock, the config, the pin, the CLI, the seed, the trials cap or a spent holdout budget."""
    repo = Path(repo) if repo is not None else context.ROOT
    state = repo / pc.state_dir
    deps = deps if deps is not None else default_deps(cfg, pc, repo)
    with _session_lock(state):
        settings.load(cfg)
        record = Record(state)
        harness, cli = _start_checks(deps, pc, state)
        seed.seed(record, cfg)
        seed.check_seed(record, cfg)
        snapshot(record, cfg, pc)
        _refuse(record, pc)
        return _Session(cfg, pc, deps, record, state, harness, cli).run(days, trials)


# -- the real deps
def _reap(p) -> None:
    """Unless it was reaped, kill the process group it leads (it has its own session); then reap it."""
    if p.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(p.pid, signal.SIGKILL)
        p.wait()


def _report(out: bytes) -> dict:
    """`lab preflight` prints its JSON report as the last line of stdout."""
    lines = [ln for ln in out.decode("utf-8", "replace").splitlines() if ln.strip()]
    try:
        r = json.loads(lines[-1]) if lines else None
    except ValueError:
        r = None
    return r if isinstance(r, dict) else {"ok": False, "error": "lab preflight printed no JSON report"}


def default_deps(cfg, pc, repo: Path) -> Deps:
    repo = Path(repo)
    state = repo / pc.state_dir
    # A config other than the default one goes along, so its judge writes to its own store (R33).
    extra = [] if Path(cfg.path) == DEFAULT_CONFIG else ["--config", str(Path(cfg.path).resolve())]
    running: dict = {}
    pinned: dict = {}

    def preflight(path: Path, tf: str) -> tuple[int, dict]:
        logs = state / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        with open(logs / f"{Path(path).stem}.preflight.log", "ab") as err:
            p = proc.run(proc.lab_argv("preflight", str(path), "--tf", tf, *extra), claude_bin=pc.claude_bin,
                         stdout=proc.PIPE, stderr=err, start_new_session=True)
            try:
                out, _ = p.communicate()
            finally:
                _reap(p)
        return p.returncode, _report(out)

    def judge(name: str, tf: str, log: Path) -> int:
        # Its own session: a Ctrl-C at the terminal reaches the loop, which decides (the first lets it finish).
        with open(log, "ab") as out:
            p = proc.run(proc.lab_argv("judge", name, "--tf", tf, "--proposer", *extra), claude_bin=pc.claude_bin,
                         stdout=out, stderr=proc.STDOUT, start_new_session=True)
            running["judge"] = p
            try:
                return p.wait()
            finally:
                running.pop("judge", None)
                _reap(p)

    def kill_judge() -> None:
        p = running.get("judge")
        if p is not None and p.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(p.pid, signal.SIGKILL)

    def pin_take() -> pin.Pin:
        pinned["pin"] = pin.take(repo, pc.claude_bin)
        return pinned["pin"]

    def pin_check() -> None:
        pin.check(repo, pinned["pin"], pc.claude_bin)

    return Deps(
        call_model=lambda system, user, schema, transcript: model.call_with_retries(pc, system, user, schema,
                                                                                    transcript),
        preflight=preflight, judge=judge, now=lambda: datetime.now(UTC), sleep=time.sleep, pin_check=pin_check,
        pin_take=pin_take, cli_version=lambda: model.cli_version(pc), probe=lambda t: model.probe(pc, t),
        kill_judge=kill_judge)

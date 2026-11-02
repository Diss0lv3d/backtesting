"""The session digest: one markdown file per session, `<state_dir>/sessions/<id>.md`, for the human.

It can be read without the dashboard (spec "Where the user reads the record"). Every write is appended and
flushed at once, so a session that crashes or is killed still leaves a line for each proposal it made. The
digest is never sent to the model, so it carries times, and the CLI's error text when a model call stops the
session (ruling R35).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

# Progress first, then the rest; anything else after these, by name.
ORDER = ("passed", "promoted", "close call", "signal before costs", "no signal", "broken", "unknown", "duplicate",
         "dropped")
# A failed referee's kinds, as its error starts (loop._referee): these first, then any other (a stop's), by name.
REFEREE_FAILURES = ("model_call", "fence", "invalid")


def _utc(t: datetime) -> datetime:
    return t.astimezone(timezone.utc)


def _when(t: datetime) -> str:
    return _utc(t).strftime("%Y-%m-%d %H:%M UTC")


def _one_line(x) -> str:
    """The model writes idea titles: a newline in one must not start a markdown line of its own."""
    return " ".join(str(x).split()) if x is not None else "—"


def _plural(n: int, what: str) -> str:
    return f"{n} {what}" + ("" if n == 1 else "s")


def _referees(reviews: list) -> str:
    """The session's referee reviews (each a research row's referee JSON) in one line: the rounds it reviewed, their
    cards by verdict, and the reviews that failed by kind (the error's code, before any detail)."""
    reviews = [r for r in reviews if isinstance(r, dict)]
    if not reviews:
        return "- Referee: none"
    n = dict.fromkeys(("accept", "revise", "reject"), 0)
    failed: dict[str, int] = {}
    for r in reviews:
        if r.get("error") is not None:
            kind = _one_line(str(r["error"]).split(":", 1)[0]) or "?"
            failed[kind] = failed.get(kind, 0) + 1
        for v in r.get("verdicts") if isinstance(r.get("verdicts"), list) else ():
            if isinstance(v, dict) and v.get("decision") in n:
                n[v["decision"]] += 1
    rank = {k: i for i, k in enumerate(REFEREE_FAILURES)}
    kinds = sorted(failed.items(), key=lambda kv: (rank.get(kv[0], len(rank)), kv[0]))
    return (f"- Referee: {_plural(len(reviews), 'round')}, {_plural(sum(n.values()), 'card')}: {n['accept']} "
            f"accepted, {n['revise']} revised, {n['reject']} rejected; failed: "
            + (", ".join(f"{k} {c}" for k, c in kinds) or "none"))


class Digest:
    def __init__(self, path: Path, now: Callable[[], datetime] | None = None):
        self.path = Path(path)
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _write(self, *lines: str) -> None:
        with open(self.path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
            f.flush()

    def header(self, model: str, cutoff, cli_version: str, commit: str, branch: str) -> None:
        lines = [f"# Proposer session {self.path.stem}", "",
                 f"- Started: {_when(self.now())}",
                 f"- Model: {model}, training cutoff {cutoff}",
                 f"- Claude Code CLI: {_one_line(cli_version)}",
                 f"- Harness: commit {commit} on branch {branch}"]
        if branch != "main":
            lines.append(f"- **Warning:** the harness is on branch {branch}, not main: this session's trials are "
                         "judged by code that is not on main.")
        self._write(*lines, "", "## Proposals", "")

    def line(self, seq: int, plugin: str | None, tf: str | None, kind: str | None, idea_title: str | None,
             outcome: str) -> None:
        run = f"{plugin}@{tf}" if plugin else "no plugin"
        self._write(f"- {_utc(self.now()):%H:%M} · {seq:04d} · {_one_line(run)} · {_one_line(kind)} · "
                    f"{_one_line(idea_title)} · {_one_line(outcome)}")

    def research(self, added_ids: list[int], stop_count: int, outcome: str, summary: str | None,
                 detail: str | None = None, held: int = 0, referee: dict | None = None,
                 next_up: str | None = None) -> None:
        """One line per research round, and the note's summary indented under it (research-round spec). `detail`,
        the CLI's error text for a failed call or a refused note's first error, goes on the line; `held`, the ideas
        a capped round held back. `referee`, when a referee reviewed the round's cards (referee-and-human-stops
        spec): its verdicts, counted on the line with each rejection's reason under it, or its error. `next_up`,
        what came of the note's request for an idea to be tried next (flagged, or why not), ends the line."""
        rejected = []
        if outcome in ("ok", "capped"):
            if outcome == "capped":
                ideas = f"{held} {'idea' if held == 1 else 'ideas'} not added (cap)"
            else:
                n = len(added_ids)
                ideas = f"+{n} {'idea' if n == 1 else 'ideas'}"
                if added_ids:
                    ideas += " (" + ", ".join(f"#{i}" for i in added_ids) + ")"
            what = f"{ideas}; stop list {stop_count} {'item' if stop_count == 1 else 'items'}"
            if referee is not None and "error" in referee:
                what += f"; referee error: {referee['error']}"
            elif referee is not None:
                verdicts = referee.get("verdicts") or []
                n = {d: sum(v.get("decision") == d for v in verdicts) for d in ("accept", "revise", "reject")}
                what += (f"; {len(verdicts)} {'card' if len(verdicts) == 1 else 'cards'}: {n['accept']} accepted, "
                         f"{n['revise']} revised, {n['reject']} rejected")
                rejected = [f"  rejected: {_one_line(v.get('title'))}: {_one_line(v.get('reason'))}"
                            for v in verdicts if v.get("decision") == "reject"]
            if next_up:
                what += f"; {next_up}"
        elif outcome == "invalid":
            what = "invalid after fix-ups"
        else:
            what = outcome.replace(": ", " ", 1)                 # error: model_call -> error model_call
        if detail:
            what += f": {detail}"
        # Indented, the model's lines stay inside the round's list item: none starts a markdown line of its own.
        notes = [f"  {ln.rstrip()}" for ln in (summary or "").splitlines() if ln.strip()]
        self._write(f"- {_utc(self.now()):%H:%M} · research: {_one_line(what)}", *rejected, *notes)

    def pause(self, reason: str, until: datetime) -> None:
        self._write(f"- {_utc(self.now()):%H:%M} · paused: {_one_line(reason)}; until {_when(until)}")

    def finish(self, counts_by_pile: dict[str, int], new_progress_ideas: Iterable[str], stop_reason: str,
               tokens_in: int, tokens_out: int, referees: list | None = None) -> None:
        """The session's end. `referees`, the session's referee reviews in order (each its research row's referee
        JSON), are summed on one line, so a week of rejections or fence breaches shows in one place."""
        rank = {p: k for k, p in enumerate(ORDER)}
        counts = sorted(counts_by_pile.items(), key=lambda kv: (rank.get(kv[0], len(ORDER)), kv[0]))
        ideas = [f"  - {_one_line(i)}" for i in new_progress_ideas]
        self._write("", "## End", "",
                    f"- Ended: {_when(self.now())}",
                    f"- Stop reason: {_one_line(stop_reason)}",
                    "- Proposals by pile: " + (", ".join(f"{p} {n}" for p, n in counts) or "none"),
                    "- New ideas in the progress piles:" + ("" if ideas else " none"), *ideas,
                    *([_referees(referees)] if referees is not None else []),
                    f"- Tokens: {tokens_in:,} in, {tokens_out:,} out")

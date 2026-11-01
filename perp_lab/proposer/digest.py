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


def _utc(t: datetime) -> datetime:
    return t.astimezone(timezone.utc)


def _when(t: datetime) -> str:
    return _utc(t).strftime("%Y-%m-%d %H:%M UTC")


def _one_line(x) -> str:
    """The model writes idea titles: a newline in one must not start a markdown line of its own."""
    return " ".join(str(x).split()) if x is not None else "—"


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
                 detail: str | None = None, held: int = 0) -> None:
        """One line per research round, and the note's summary indented under it (research-round spec). `detail`,
        the CLI's error text for a failed call or a refused note's first error, goes on the line; `held`, the ideas
        a capped round held back."""
        if outcome in ("ok", "capped"):
            if outcome == "capped":
                ideas = f"{held} {'idea' if held == 1 else 'ideas'} not added (cap)"
            else:
                n = len(added_ids)
                ideas = f"+{n} {'idea' if n == 1 else 'ideas'}"
                if added_ids:
                    ideas += " (" + ", ".join(f"#{i}" for i in added_ids) + ")"
            what = f"{ideas}; stop list {stop_count} {'item' if stop_count == 1 else 'items'}"
        elif outcome == "invalid":
            what = "invalid after fix-ups"
        else:
            what = outcome.replace(": ", " ", 1)                 # error: model_call -> error model_call
        if detail:
            what += f": {detail}"
        # Indented, the model's lines stay inside the round's list item: none starts a markdown line of its own.
        notes = [f"  {ln.rstrip()}" for ln in (summary or "").splitlines() if ln.strip()]
        self._write(f"- {_utc(self.now()):%H:%M} · research: {_one_line(what)}", *notes)

    def pause(self, reason: str, until: datetime) -> None:
        self._write(f"- {_utc(self.now()):%H:%M} · paused: {_one_line(reason)}; until {_when(until)}")

    def finish(self, counts_by_pile: dict[str, int], new_progress_ideas: Iterable[str], stop_reason: str,
               tokens_in: int, tokens_out: int) -> None:
        rank = {p: k for k, p in enumerate(ORDER)}
        counts = sorted(counts_by_pile.items(), key=lambda kv: (rank.get(kv[0], len(ORDER)), kv[0]))
        ideas = [f"  - {_one_line(i)}" for i in new_progress_ideas]
        self._write("", "## End", "",
                    f"- Ended: {_when(self.now())}",
                    f"- Stop reason: {_one_line(stop_reason)}",
                    "- Proposals by pile: " + (", ".join(f"{p} {n}" for p, n in counts) or "none"),
                    "- New ideas in the progress piles:" + ("" if ideas else " none"), *ideas,
                    f"- Tokens: {tokens_in:,} in, {tokens_out:,} out")

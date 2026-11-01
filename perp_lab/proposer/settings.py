"""The proposer's settings and the guards that refuse to start on a bad one.

The model ID must be pinned in full (an alias would let the model change under a trial record), and its
training cutoff must precede the holdout start (a model that may have seen the holdout period proves nothing).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

MODEL_ID = re.compile(r"^claude-[a-z]+(-\d+)+$")


class StartRefused(Exception):
    """A start guard failed; the proposer must not run."""


@dataclass(frozen=True)
class ProposerConfig:
    model: str
    model_training_cutoff: date
    claude_bin: str
    effort: str
    state_dir: Path
    timeframes: tuple[str, ...]
    trials_per_day: int
    trials_total: int
    proposals_per_idea: int
    fixups_per_proposal: int
    max_grid_combos: int
    overlap_max: float
    corr_max: float
    max_input_tokens_per_day: int
    max_output_tokens_per_day: int
    call_timeout_s: int
    research_every: int             # proposer trials between research rounds; 0 turns research off
    research_max_ideas: int
    research_open_cap: int


def _cutoff(v) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        try:
            return date.fromisoformat(v)
        except ValueError:
            pass
    raise StartRefused(f"proposer.model_training_cutoff is missing or not a date: {v!r}")


def _count(raw: dict, key: str, least: int) -> int:
    v = raw.get(key)
    if isinstance(v, bool) or not isinstance(v, int) or v < least:
        raise StartRefused(f"proposer.{key} must be an integer of at least {least}, not {v!r}")
    return v


def load(cfg) -> ProposerConfig:
    raw = cfg.get("proposer")
    if not isinstance(raw, dict):
        raise StartRefused("config has no proposer: block")
    model = raw.get("model")
    if not isinstance(model, str) or not MODEL_ID.match(model):
        raise StartRefused(f"proposer.model must be a full pinned ID like claude-opus-5-5, not {model!r}")
    cutoff = _cutoff(raw.get("model_training_cutoff"))
    holdout = cfg.holdout_start.date()
    if cutoff >= holdout:
        raise StartRefused(f"model_training_cutoff {cutoff} is not before holdout_start {holdout}")
    tfs = tuple(raw["timeframes"])
    unknown = [t for t in tfs if t not in cfg["timeframes"]]
    if unknown:
        raise StartRefused(f"proposer.timeframes {unknown} are not in the lab's timeframes")
    return ProposerConfig(
        model=model, model_training_cutoff=cutoff, claude_bin=str(raw["claude_bin"]), effort=str(raw["effort"]),
        state_dir=Path(raw["state_dir"]), timeframes=tfs,
        trials_per_day=int(raw["trials_per_day"]), trials_total=int(raw["trials_total"]),
        proposals_per_idea=int(raw["proposals_per_idea"]), fixups_per_proposal=int(raw["fixups_per_proposal"]),
        max_grid_combos=int(raw["max_grid_combos"]),
        overlap_max=float(raw["overlap_max"]), corr_max=float(raw["corr_max"]),
        max_input_tokens_per_day=int(raw["max_input_tokens_per_day"]),
        max_output_tokens_per_day=int(raw["max_output_tokens_per_day"]),
        call_timeout_s=int(raw["call_timeout_s"]),
        research_every=_count(raw, "research_every", 0), research_max_ideas=_count(raw, "research_max_ideas", 1),
        research_open_cap=_count(raw, "research_open_cap", 1),
    )

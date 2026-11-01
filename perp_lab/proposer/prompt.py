"""The record as the model sees it, the round's instruction and the fix-up prompt.

The record is the model's only memory (spec "What the model sees", items 2 and 3). It is rendered from the
ideas' cards, the proposals' order and whitelisted verdict fields only: never a proposal's times, session,
hash or preflight report, and never a run id or report path, since a run id encodes a time. Proposals are
numbered in order and nothing is dated. A verdict whose diagnostics come from a refresh on today's data keeps
only the old judgement's decision, so the model never sees two different OOS Sharpes for one plugin.
"""

from __future__ import annotations

import copy
import json
import math
import re

from perp_lab.dsr import sharpe_bar
from perp_lab.proposer import piles
from perp_lab.proposer.context import fence
from perp_lab.proposer.record import Record

VERDICT_FIELDS = ("decision", "reasons", "dsr", "n_trials", "sr0", "beats_parent_frac", "holdout_status",
                  "holdout_reason", "diagnostics")
# A refresh leaves the old judgement's own numbers stale, and its reasons quote them ("OOS Sharpe -0.46 <= 0").
STALE_AFTER_REFRESH = ("oos_metrics", "dsr", "sr0", "beats_parent_frac", "reasons")
SHOWN_STATES = ("judged", "broken")      # dropped, staged and copied proposals never reached a judge
DIGITS = 4                               # decimals kept in the rendered verdicts: enough to read, fewer tokens
_CODE = re.compile(r"[a-z][a-z_]*")


def public_verdict(v: dict) -> dict:
    out = {k: copy.deepcopy(v[k]) for k in VERDICT_FIELDS if k in v}
    wf = v.get("walkforward")
    out["oos_metrics"] = copy.deepcopy(wf.get("metrics")) if isinstance(wf, dict) else None
    if (v.get("diagnostics") or {}).get("data") == "refreshed":
        for k in STALE_AFTER_REFRESH:
            out.pop(k, None)
    return out


def _rounded(x):
    if isinstance(x, float) and math.isfinite(x):
        return round(x, DIGITS)
    if isinstance(x, dict):
        return {k: _rounded(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_rounded(v) for v in x]
    return x


def card_text(card) -> str:
    return fence(json.dumps(card, indent=1, ensure_ascii=False), "json")


def _num(x) -> str:
    ok = isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)
    return f"{x:.2f}" if ok else "n/a"


def _judged(props: list[dict]) -> list[dict]:
    return [p for p in props if p.get("judge_exit") is not None]


def _pile(p: dict) -> str:
    return piles.pile(p.get("verdict")) if p.get("judge_exit") is not None else "broken"


def _best(props: list[dict]) -> str | None:
    return piles.best_pile([_pile(p) for p in _judged(props) if p.get("duplicate_of") is None])


def _why(idea: dict, props: list[dict], best: str | None) -> str:
    status, judged = idea["status"], _judged(props)
    if status == "excluded":
        return idea.get("excluded_reason") or "excluded by the human"
    if status in ("passed", "failed"):
        return f"its family's holdout exam {status}"
    if status == "duplicate":
        dups = sorted({p["duplicate_of"] for p in judged if p.get("duplicate_of") is not None})
        return "its judged plugins were near-copies of " + ", ".join(f"#{d}" for d in dups)
    if best == "broken":
        return f"{len(judged)} judged plugins, all broken"
    champ = piles.champion(props)
    if champ is None:
        return "no judged plugin with diagnostics" if judged else "no judged plugin"
    v = champ["verdict"] or {}
    d = v.get("diagnostics") or {}
    g, b = d.get("gates") or {}, d.get("before_costs") or {}
    gross = f"gross Sharpe {_num(b.get('sharpe'))} against the gross random p95 {_num(b.get('random_p95'))}"
    net = f"net Sharpe {_num(g.get('net_sharpe'))} against the net random p95 {_num(g.get('random_p95'))}"
    return {"no signal": gross, "signal before costs": f"{gross}; {net}", "close call": net,
            "promoted": f"promoted, exam {v.get('holdout_status')}"}.get(best, gross)


def _proposal(p: dict) -> list[str]:
    head = f"- seq {p['seq']}, {p.get('kind') or 'unknown kind'}, {p.get('plugin') or 'no plugin'}@{p.get('timeframe')}"
    pile = _pile(p)
    if pile == "broken" and p.get("judge_exit") not in (None, 0):
        pile += f" (judge exit {p['judge_exit']})"
    # broken_reason is the loop's own code word; anything else (free text) could carry a time, so it isn't shown
    if p.get("broken_reason") and _CODE.fullmatch(str(p["broken_reason"])):
        pile += f", {p['broken_reason']}"
    lines = [f"{head}: {pile}"]
    if p.get("duplicate_of") is not None:
        lines.append(f"  filed as a duplicate of idea #{p['duplicate_of']}: its pile does not count toward this "
                     "idea's best pile")
    if p.get("verdict") is not None:
        lines.append("  verdict: " + json.dumps(_rounded(public_verdict(p["verdict"])), sort_keys=True,
                                                ensure_ascii=False))
    return lines


def _open_idea(idea: dict, props: list[dict], timeframes) -> str:
    best, tf = _best(props), idea.get("timeframe")
    # A refinement keeps its idea's timeframe, so an idea at one no plugin may trade (a human trial at 5m) takes
    # ports only, and no refinement gets its champion as PARENT (M-1).
    ports_only = tf not in timeframes
    status = f"status {idea['status']}"
    if ports_only:
        status += f", ports only: its timeframe {tf} is not one a plugin may trade"
    facts = [f"timeframe {tf}", status, f"best pile {best or 'none yet'}", f"judged plugins {len(_judged(props))}"]
    if idea.get("port_of") is not None:
        facts.append(f"port of #{idea['port_of']}")
    out = [f"### #{idea['id']} {idea['title']}", "; ".join(facts), "Card:", card_text(idea.get("card") or {})]
    out.append("Proposals:" if props else "Proposals: none yet")
    for p in props:
        out += _proposal(p)
    champ = piles.champion(props)
    if champ is not None:
        name = f"{champ.get('plugin')}@{champ.get('timeframe')}"
        out.append(f"Its best plugin (a port of this idea is a root, with no PARENT): {name}" if ports_only
                   else f"Champion, the PARENT a refinement of this idea gets: {name}")
        out.append(fence(champ["source"], "python") if champ.get("source") else "(its source is not in the record)")
    return "\n".join(out)


def render_record(record: Record, current_n: int, timeframes) -> str:
    """The record for the prompt; `timeframes` is proposer.timeframes, the ones a plugin may trade."""
    opened, closed = [], []
    for idea in record.ideas():
        props = [p for p in record.proposals(idea["id"]) if p.get("state") in SHOWN_STATES]
        if idea["status"] == "open":
            opened.append(_open_idea(idea, props, tuple(timeframes)))
        else:
            best = _best(props)
            label = best or ("untested" if not _judged(props) else "unknown")
            closed.append(f"#{idea['id']} {idea['title']}: {idea['status']}, {label} ({_why(idea, props, best)})")
    return "\n\n".join([
        "# The record",
        "Ideas by id, proposals by sequence number. A verdict shows the judge's decision, reasons, Deflated Sharpe "
        "(dsr), N (n_trials), sr0, beats-parent fraction, holdout status and code, its diagnostics, and the "
        "walk-forward's OOS metrics (oos_metrics). A verdict whose diagnostics.data is refreshed shows diagnostics "
        "from a re-run on today's data and only the old judgement's decision.",
        "## Open ideas",
        "\n\n".join(opened) or "None.",
        "## Closed ideas",
        "\n".join(closed) or "None.",
        f"N is now {current_n}; the bar is {sharpe_bar(current_n):.2f}",
    ]) + "\n"


INSTRUCTION = """\
## This round

Return one proposal. Aim where nothing in the record rules it out: no near-copy or mirror of a judged plugin, \
and no mechanism a closed idea has already tested. Refinements of ideas in the signal before costs and close \
call piles are welcome: change what their diagnostics point at, and cite them. So is a port of such an idea to a \
timeframe where its diagnostics say the effect should keep and cost less. A new idea needs a mechanism: who \
pays, and why it persists.

Reply with one JSON object, as the reply contract describes, and nothing else."""

NEXT_UP = "The human asks that this round's proposal work on idea #{id}."


def instruction(next_up: dict | None = None) -> str:
    """The round's instruction. The idea the human flagged (record.next_up), while it is open, adds one fixed
    sentence with its id: never the flag's time, and never words of the human's. It asks; the rules don't change."""
    if next_up is not None and next_up.get("status") == "open":
        return INSTRUCTION + "\n\n" + NEXT_UP.format(id=int(next_up["id"]))
    return INSTRUCTION


def fixup(user: str, rejected: dict, errors: list[str]) -> str:
    errs = "\n".join("- " + str(e).replace("\n", "\n  ") for e in errors)
    return "\n\n".join([
        user.rstrip("\n"),
        "## Your last reply was refused",
        "Your reply:",
        fence(json.dumps(rejected, indent=1, ensure_ascii=False), "json"),
        "The errors:",
        errs,
        "Return a corrected proposal: one JSON object, as the reply contract describes.",
    ]) + "\n"

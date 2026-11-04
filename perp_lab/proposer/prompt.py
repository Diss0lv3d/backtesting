"""The record as the model sees it, the round's instruction and the fix-up prompt.

The record is the model's only memory (spec "What the model sees", items 2 and 3). It is rendered from the
ideas' cards, the proposals' order and whitelisted verdict fields only: never a proposal's times, session,
hash or preflight report, and never a run id or report path, since a run id encodes a time. Proposals are
numbered in order and nothing is dated. A verdict whose diagnostics come from a refresh on today's data keeps
only the old judgement's decision, so the model never sees two different OOS Sharpes for one plugin.

A research round (research-round spec) gets the same record with a fixed instruction of its own. Of what it
returns, its stop list reaches later prompts (stop_section) and the cards that enter the record reach them as the
record's ideas, never its time or summary. The human's own stop list (human_stop_section) goes before the research
step's in every proposal and research prompt: the items' text only, never when they were added.

With research_referee, a round's cards first go to a referee call (referee-and-human-stops spec, "The referee"):
the same record and stop sections, every field of the cards as the research step wrote them, builds_on included
(note_cards), and a fixed instruction of its own (referee_instruction), from the config alone. With the web, that
instruction holds the only dates this module puts in a prompt: the lab's holdout start, before which the referee's
sources must be published, and the day before it, where its searches end (R-RF7, M3). Of the round's cards, the
titles of those the referee rejected also reach every later research prompt (rejected_section, R-RF8): the
research step's own words, never the referee's reasons or sources, which are web-informed.
"""

from __future__ import annotations

import copy
import json
import math
import re
from dataclasses import asdict
from datetime import timedelta

from perp_lab.dsr import sharpe_bar
from perp_lab.proposer import contract, piles
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
    # ports only, and no refinement gets its champion as PARENT (M-1). So does an exhausted idea, shown here only
    # when it is asked for next (render_record).
    exhausted = idea["status"] == "exhausted"
    ports_only = exhausted or tf not in timeframes
    status = f"status {idea['status']}"
    if exhausted:
        status += ", ports only: it has used its judged plugins"
    elif ports_only:
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
    """The record for the prompt; `timeframes` is proposer.timeframes, the ones a plugin may trade. An exhausted
    idea asked for next (a port only) is shown as an open one is, in its own section: a port needs its card, plugins
    and best plugin's source, which a closed idea's one line doesn't carry. The flag's time is never shown."""
    opened, asked, closed = [], [], []
    for idea in record.ideas():
        props = [p for p in record.proposals(idea["id"]) if p.get("state") in SHOWN_STATES]
        if idea["status"] == "open":
            opened.append(_open_idea(idea, props, tuple(timeframes)))
        elif idea["status"] == "exhausted" and idea.get("next_up") is not None:
            asked.append(_open_idea(idea, props, tuple(timeframes)))
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
        *(["## Asked for next (exhausted: a port only)", "\n\n".join(asked)] if asked else []),
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

Judge a design by its payoff, not its win rate. What pays is the win rate times the average winner against the \
loss rate times the average loser, after costs: a design that wins a quarter of its trades with winners four times \
its losers (1:4) is past break-even (20%) before costs, and one that wins most trades with small winners and \
large losers can still lose. Where the mechanism says moves run, exits that let winners run (a target several R \
away, a trailing or time exit) against a tight stop are welcome. Two things bind: a tighter stop means a larger \
position for the same 1 R (until the notional cap binds), so fees and slippage take a larger share of each R; and \
the random-entry benchmark trades the plugin's own exits, so the payoff must come with an entry that beats \
random. A new verdict's oos_metrics show win_rate, avg_win_r, avg_loss_r and payoff_ratio (avg_win_r over the \
size of avg_loss_r); an older verdict may have none.

Reply with one JSON object, as the reply contract describes, and nothing else."""

NEXT_UP = "The human asks that this round's proposal work on idea #{id}."
NEXT_UP_RESEARCH = "The research step asks that this round's proposal work on idea #{id}."
NEXT_UP_PORT = "The human asks that this round's proposal port idea #{id} to another timeframe."
NEXT_UP_PORT_RESEARCH = "The research step asks that this round's proposal port idea #{id} to another timeframe."


def instruction(next_up: dict | None = None) -> str:
    """The round's instruction. The flagged idea (record.next_up), while it is open or exhausted, adds one fixed
    sentence with its id, naming who asked (next_by: research, or the human, whose flag may predate the column):
    never the flag's time, and never words of the human's or the research step's. An exhausted idea takes no
    refinement, so its sentence asks for a port. It asks; the rules don't change."""
    status = next_up.get("status") if next_up is not None else None
    if status in ("open", "exhausted"):
        research = next_up.get("next_by") == "research"
        if status == "open":
            text = NEXT_UP_RESEARCH if research else NEXT_UP
        else:
            text = NEXT_UP_PORT_RESEARCH if research else NEXT_UP_PORT
        return INSTRUCTION + "\n\n" + text.format(id=int(next_up["id"]))
    return INSTRUCTION


def fixup(user: str, rejected: dict, errors: list[str], what: str = "proposal") -> str:
    """The round's prompt, the refused reply and its errors; `what` names the reply asked for again."""
    errs = "\n".join("- " + str(e).replace("\n", "\n  ") for e in errors)
    return "\n\n".join([
        user.rstrip("\n"),
        "## Your last reply was refused",
        "Your reply:",
        fence(json.dumps(rejected, indent=1, ensure_ascii=False), "json"),
        "The errors:",
        errs,
        f"Return a corrected {what}: one JSON object, as the reply contract describes.",
    ]) + "\n"


STOP_HEADING = "Directions the research step recommends avoiding:"
# The research step advises; the human decides. A proposal's instruction (below the list) may carry the human's ask.
HUMAN_FIRST = "When the human asks for an idea below, that request comes before this list."


def _items(items: list[str]) -> list[str]:
    # one line each, whatever the row holds: an item must not start a markdown line of its own
    return [f"- {' '.join(str(s).split())}" for s in items]


def stop_section(stop: list[str], *, human_first: bool = False) -> str:
    """The stop list in force (the latest valid research round's), for every later proposal and research round;
    "" when it has none. `human_first`, in a proposal's prompt, adds that the human's request comes first."""
    if not stop:
        return ""
    return "\n".join([STOP_HEADING, *_items(stop), *(["", HUMAN_FIRST] if human_first else [])]) + "\n"


HUMAN_STOP_HEADING = "Directions the human has ruled out:"


def human_stop_section(items: list[str]) -> str:
    """The human's stop list (record.stops: only the items' text), for every proposal and research prompt, before
    the research step's; "" when it has none."""
    if not items:
        return ""
    return "\n".join([HUMAN_STOP_HEADING, *_items(items)]) + "\n"


RESEARCH_INSTRUCTION = """\
## This round: research

This round is a research round, not a proposal: no plugin, and no trial. Read what the record has taught: the \
piles, the diagnostics of every judged proposal, and what closed each idea. Then write new idea cards that build \
on those lessons rather than repeat them. Each card needs a mechanism: who pays, and why it persists. Your cards \
join the record as open ideas that later rounds can propose plugins for.

Redundant, and so not wanted: a near-copy of any judged plugin's mechanism; a sign flip of a closed idea; a \
filter added to a closed idea's entry; an idea whose edge would come from its exit rather than its entry; a \
restatement of an open idea in the record.

A stop with no target earns on trends whatever the entry, so an exit alone can make an entry look like an edge. \
Say in each card how its result stays a measure of its entry: neutral exits, or, where its mechanism says moves \
run, exits that let winners run against a tight stop for a payoff of several R to one. The random-entry \
benchmark trades the same exits, so either way the entry must beat random. Judge a design by its payoff, not its \
win rate.

A card must not cite numbers from the record: mechanism, literature and rule only.

Each stop item names a direction (a mechanism or a family of rules), never an open idea of the record: only the \
human closes an idea. The directions the human has ruled out, when listed above, are the human's: not yours to \
revise, and your stop list must not restate them."""

# Only when a stop list is in force: the prompt shows it (stop_section) between the record and this instruction.
REVISE_STOP = """\
The stop list above is the one in force. Revise it: keep what still holds, drop what the record has since \
answered, and add what it now shows."""

REJECTED_HEADING = "Cards the referee rejected (do not propose them again):"


def rejected_section(titles: list[str]) -> str:
    """The titles of the cards the referee rejected in earlier rounds, for every research prompt after the stop
    sections; "" when there are none."""
    if not titles:
        return ""
    return "\n".join([REJECTED_HEADING, *_items(titles)]) + "\n"


# Only when the prompt lists rejected cards (rejected_section), between the stop sections and this instruction.
REJECTED_NOTE = """\
The cards listed above as rejected by the referee were reviewed and turned down: do not propose them again, under \
their titles or new ones."""

RESEARCH_RETURN = """\
Return at most {max_ideas} idea cards (none, when the record points nowhere new), a stop list of at most {stop} \
one-line directions the builder should stop trying (each later round sees it, in place of the last one), and a \
summary for the human. This round's reply contract is the one below, not the one under "Your reply":"""

# When the record holds research_open_cap untried research ideas: the loop sets aside any card the round returns.
RESEARCH_RETURN_CAPPED = """\
Return no idea cards this round: the record already holds the untried research ideas the cap allows, so a card \
would be set aside. Revise only the stop list (at most {stop} one-line directions the builder should stop trying; \
each later round sees it, in place of the last one) and the summary for the human. This round's reply contract is \
the one below, not the one under "Your reply":"""


def research_instruction(pc, *, revising: bool = False, capped: bool = False, rejected: bool = False) -> str:
    """A research round's instruction: fixed text and the research contract, from the config alone. `revising`,
    when a stop list is in force, asks the round to revise it; `capped`, when untried research ideas are at their
    cap, asks for no cards; `rejected`, when the prompt lists cards the referee rejected, asks for none of them
    again."""
    ret = (RESEARCH_RETURN_CAPPED if capped else RESEARCH_RETURN).format(max_ideas=pc.research_max_ideas,
                                                                          stop=contract.STOP_MAX_ITEMS)
    parts = [RESEARCH_INSTRUCTION, REVISE_STOP if revising else None, REJECTED_NOTE if rejected else None, ret,
             contract.describe_research(pc.timeframes, pc.research_max_ideas)]
    return "\n\n".join(x for x in parts if x)


CARDS_HEADING = "## The research step's cards"
CARDS_INTRO = """\
The idea cards this research round returned, in its order and as it wrote them, each with the lesson in the record \
it says it builds on (builds_on). None is in the record yet."""


def note_cards(note: contract.ResearchNote) -> str:
    """The cards a referee reviews: every field the research step wrote, builds_on included, numbered in the note's
    order. Nothing else of the note: never its stop list or summary."""
    out = [CARDS_HEADING, CARDS_INTRO]
    for k, idea in enumerate(note.ideas, 1):
        out += [f"### Card {k}", card_text({**asdict(idea), "columns": list(idea.columns)})]
    return "\n\n".join(out) + "\n"


REFEREE_INSTRUCTION = """\
## This round: the referee

This round is a review, not a proposal: no plugin, and no trial. You are a skeptical referee of the cards above: \
none joins the record unless it survives your review. For each card decide: accept (it joins as written), revise \
(your corrected card joins instead) or reject (nothing joins).

Check each card against the record. Reject a near-copy of a judged plugin's mechanism, a sign flip of a closed \
idea, a filter added to a closed idea's entry, a restatement of an open idea, an idea in one of the directions the \
human has ruled out (when listed above), and an edge that would come from its exit rather than its entry.

{literature}

Check it against costs: can the move it expects pay the round-trip cost of the cost model at its timeframe and its \
trade count?

Check it against the data: its signal must be computable from the columns it lists, and those must be bar columns \
a plugin may read.

Revise only to correct a claim, sharpen the rule, neutralise an exit its mechanism doesn't support or fix the \
timeframe, keeping the idea: a different idea is a reject. Exits that let winners run for a payoff of several R \
to one are not an exit edge when the mechanism says moves run: the random-entry benchmark trades the same exits. \
A revised card keeps its own title or takes one no idea in the record and no other card in this report uses. A \
card, revised or not, cites no numbers from the record: mechanism, literature and rule only.

Return one verdict per card, in the cards' order, each with its reason. This round's reply contract is the one \
below, not the one under "Your reply":"""

# The referee's web access is fenced in code (model.py): these sentences only tell it what the fence allows, and
# that its review, unlike a proposal, has a tool, where the static context (its system prompt) says none. A paper
# from the holdout period may discuss it, so the referee is asked to leave such papers alone (R-RF7) and to bound its
# searches so that they don't return one (M3: the real referee run read an October 2026 paper's metadata); neither
# can be checked in code, and the web_reviewed mark stays the safeguard. Each search's bound is the day before the
# holdout start, in the form its API takes. arXiv's refuses an open range ([* TO ...]: an error entry, checked
# 2026-11-02), so its range starts before arXiv's first submission (August 1991).
LITERATURE_WEB = """\
Check it against the literature: use the allowed academic sources to find published evidence for or against it, \
and to check that the papers it cites exist and say what it claims. In this review, unlike a proposal, you have one \
tool: WebFetch, limited to the hosts below. You may fetch only from these hosts: {hosts}; any other fetch is \
refused. Never look for prices, charts or market news. Cite only sources published before {before}, and disregard \
any published on or after that date. Bound every search to papers from before that date: Crossref with \
filter=until-pub-date:{last}, Semantic Scholar with publicationDateOrYear=:{last}, arXiv with \
submittedDate:[199101010000 TO {last:%Y%m%d}2359] joined to its search_query by AND. In each verdict's sources, name \
the URLs you read on those hosts that the verdict relies on, and no other."""

LITERATURE_NO_WEB = """\
Check it against the literature you know: published evidence for or against it, and whether the papers it cites \
exist and say what it claims. You have no web access in this review, so every verdict's sources stay empty."""


def referee_instruction(pc, *, web: bool | None = None) -> str:
    """The referee's instruction: fixed text and the referee contract, from the config alone. `web` (default
    pc.research_referee_web): whether the referee may fetch from pc.referee_domains, whose hosts it names, and
    then the only dates in it, the lab's holdout start, before which its sources must be published, and the day
    before it, where its searches end."""
    web = pc.research_referee_web if web is None else web
    literature = (LITERATURE_WEB.format(hosts=", ".join(pc.referee_domains), before=f"{pc.holdout_start:%Y-%m-%d}",
                                        last=pc.holdout_start - timedelta(days=1))
                  if web else LITERATURE_NO_WEB)
    return "\n\n".join([REFEREE_INSTRUCTION.format(literature=literature),
                         contract.describe_referee(pc.timeframes, pc.research_max_ideas)])

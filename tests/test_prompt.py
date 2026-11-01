"""The record as the model sees it: whitelisted verdict fields, no time, no run id."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, replace
from datetime import datetime, timezone

import pandas as pd
import pytest

from perp_lab.dsr import sharpe_bar
from perp_lab.proposer import contract, prompt, settings
from perp_lab.proposer.record import Record, ideas
from tests.test_store import cfg  # noqa: F401  (a temp config and store)

SENTINEL = "SENTINEL_TIME_2026-10-21T03:04:05"
RUN_ID = "20291021-030405-abcdef"
PLANTED = datetime(2029, 3, 4, 5, 6, 7, tzinfo=timezone.utc)
DATE = re.compile(r"20\d\d-\d\d-\d\d")


def diagnostics(*, data="judged", net=-1.26, p95=-0.19, gross=-0.9, gross_p95=0.1):
    return {"data": data,
            "gates": {"net_sharpe": net, "random_p95": p95, "oos_trades": 3385, "min_oos_trades": 64},
            "before_costs": {"sharpe": gross, "random_p95": gross_p95},
            "breadth": {"coins": 17, "coins_net_pos": 5, "folds": 36, "folds_net_pos": 14}}


def verdict(*, data="judged", sharpe=-1.26, **diag):
    return {"schema": 2, "strategy": "p", "timeframe": "1h", "strategy_hash": "abc", "lineage_id": "family:p",
            "parent_run_id": RUN_ID, "decision": "kill", "reasons": [f"OOS Sharpe {sharpe:.2f} <= 0"],
            "dsr": 0.01, "n_trials": 7, "sr0": 0.03, "parent_passed": None, "beats_parent_frac": None,
            "holdout_status": "n/a", "holdout_reason": "n/a",
            "walkforward": {"run_id": RUN_ID, "report": f"runs/{RUN_ID}/report.md", "started_at": SENTINEL,
                            "metrics": {"sharpe": sharpe, "trades": 3385, "days": 2191}},
            "diagnostics": diagnostics(data=data, **diag),
            "created_at": SENTINEL, SENTINEL: SENTINEL}


def test_the_whitelist():
    assert prompt.VERDICT_FIELDS == ("decision", "reasons", "dsr", "n_trials", "sr0", "beats_parent_frac",
                                     "holdout_status", "holdout_reason", "diagnostics")


def test_public_verdict_keeps_the_whitelist_and_drops_everything_else():
    v = verdict()
    out = prompt.public_verdict(v)
    assert set(out) == set(prompt.VERDICT_FIELDS) | {"oos_metrics"}
    assert out["oos_metrics"] == v["walkforward"]["metrics"] and out["decision"] == "kill"
    blob = json.dumps(out)
    for planted in (RUN_ID, "report.md", "created_at", "started_at", SENTINEL, "03:04:05", "lineage", "abc"):
        assert planted not in blob, planted


def test_a_kill_without_a_run_has_no_oos_metrics():
    out = prompt.public_verdict({**verdict(), "walkforward": None, "diagnostics": None})
    assert out["oos_metrics"] is None and out["decision"] == "kill"


def test_public_verdict_does_not_alias_the_record():
    v = verdict()
    prompt.public_verdict(v)["diagnostics"]["gates"]["net_sharpe"] = 99
    assert v["diagnostics"]["gates"]["net_sharpe"] == -1.26


def test_a_refreshed_verdict_shows_the_refreshed_numbers_and_the_old_decision_only():
    v = verdict(data="refreshed", sharpe=-0.46, net=0.31, gross=0.52)
    out = prompt.public_verdict(v)
    assert out["decision"] == "kill" and out["diagnostics"]["data"] == "refreshed"
    assert out["diagnostics"]["gates"]["net_sharpe"] == 0.31
    for gone in ("oos_metrics", "dsr", "sr0", "beats_parent_frac", "reasons"):
        assert gone not in out, gone
    assert "-0.46" not in json.dumps(out)        # the old judgement's OOS Sharpe, in its metrics and reasons


# public_verdict passes `diagnostics` and `walkforward.metrics` through whole, so their keys are pinned here
# from what the judge writes. A key the judge adds later (one that might carry a time) fails this test first and
# forces a review of what reaches the model.
DIAGNOSTICS_BLOCKS = {
    "gates": {"net_sharpe", "random_mean", "random_p95", "pctile", "long_share", "oos_trades", "min_oos_trades"},
    "before_costs": {"sharpe", "random_mean", "random_p95", "pctile"},
    "per_trade_bp": {"gross", "fees", "slippage", "funding", "net"},
    "sides": {"long", "short", "long_share", "long_gross_bp", "long_net_bp", "short_gross_bp", "short_net_bp"},
    "exits": {"share", "mean_bars_held"},
    "breadth": {"coins", "coins_net_pos", "folds", "folds_net_pos", "top_coin_share", "top_fold_share"},
    "is_vs_oos": {"is_sharpe", "oos_sharpe"},
    "overlap": {"position", "mirror", "correlation"},
}
DIAGNOSTICS_KEYS = {"data", "regimes", "selection", *DIAGNOSTICS_BLOCKS}
EXIT_REASONS = {"stop", "target", "signal", "liq", "eow"}          # the engine's close_pos reasons
OOS_METRICS_KEYS = {"sharpe", "sortino", "max_dd", "total_return", "cagr", "days", "exposure", "trades", "win_rate",
                    "profit_factor", "avg_r", "avg_bars_held", "liquidations", "fees", "funding"}


def test_the_blocks_passed_through_whole_have_exactly_the_keys_the_judge_writes(cfg):
    from perp_lab.engine.metrics import summarize
    from perp_lab.judge.diagnostics import diagnostics
    from perp_lab.validate.regime import REGIMES
    from tests.test_diagnostics import DAYS, recorded_run, trades

    earlier = [{"strategy": "s0", "timeframe": "1h", "run_dir": str(recorded_run(cfg, "r0", trades()))}]
    d = diagnostics(cfg, recorded_run(cfg, "r1", trades()), data="judged", earlier=earlier)
    t = trades().assign(r_multiple=0.5)
    m = summarize(pd.Series(0.001, index=DAYS), t, 0.2)      # the walk-forward's OOS metrics (oos_summary)
    out = prompt.public_verdict({"decision": "kill", "diagnostics": d, "walkforward": {"run_id": RUN_ID,
                                                                                      "metrics": m}})
    assert out["diagnostics"] == d and out["oos_metrics"] == m
    assert set(d) == DIAGNOSTICS_KEYS
    for block, keys in DIAGNOSTICS_BLOCKS.items():
        assert set(d[block]) == keys, block
    assert d["regimes"] and set(d["regimes"]) <= set(REGIMES)
    assert all(set(v) == {"trades", "gross_bp"} for v in d["regimes"].values())
    assert d["exits"]["share"] and set(d["exits"]["share"]) <= EXIT_REASONS
    assert d["selection"] and all(isinstance(v, int) for v in d["selection"].values())
    entries = [e for lst in d["overlap"].values() for e in lst]
    assert entries and all(set(e) == {"strategy", "timeframe", "value"} for e in entries)
    assert set(m) == OOS_METRICS_KEYS


TFS = ("15m", "1h", "4h")                 # proposer.timeframes
CARD_A = {"title": "Flow fade", "mechanism": "forced flow reverts", "signal": "fade the hour",
          "note": "from the research note of 2026-10-18"}
CARD_B = {"title": "Donchian", "mechanism": "trends persist"}
SOURCE = '"""Fade forced flow."""\nfrom perp_lab.strategy import Strategy\n\n\nclass P(Strategy):\n    pass\n'


@pytest.fixture
def rec(tmp_path):
    r = Record(tmp_path / "state")
    sid = r.start_session(started_at=PLANTED, model="claude-x-1", cutoff="2026-01-01")
    a = r.add_idea("Flow fade", CARD_A, "seed", "1h")
    b = r.add_idea("Donchian", CARD_B, "seed", "4h")
    c = r.add_idea("Sign flip of A", {"title": "Sign flip of A"}, "seed", "4h")
    r.set_idea(b, status="dead")
    r.set_idea(c, status="excluded", excluded_reason="it is A's sign flip")
    r.set_idea(a, next_up=PLANTED)                       # the human's "try next": a time, never in a prompt
    planted = dict(session_id=sid, created_at=PLANTED, judged_at=PLANTED, hash="2029030405abcdef",
                   preflight={"at": SENTINEL})
    r.add_proposal(seq=1, idea_id=a, kind="new", plugin="flow_a", timeframe="1h", source=SOURCE, state="judged",
                   judge_exit=0, verdict=verdict(net=0.4, p95=0.2), **planted)
    r.add_proposal(seq=2, idea_id=a, kind="refinement", plugin="flow_b", timeframe="1h", source="x = 1\n",
                   state="judged", judge_exit=0, verdict=verdict(), duplicate_of=b, **planted)
    r.add_proposal(seq=3, idea_id=a, kind="refinement", plugin="flow_c", timeframe="1h", source="y = 2\n",
                   state="broken", judge_exit=1, broken_reason="interrupted", **planted)
    r.add_proposal(seq=4, idea_id=a, kind="refinement", state="dropped", **planted)
    r.add_proposal(seq=5, idea_id=b, kind="new", plugin="donchian_atr", timeframe="4h", source="z = 3\n",
                   state="judged", judge_exit=0, verdict=verdict(gross=-0.9, gross_p95=0.1), **planted)
    # A research round: its time, summary and stop list are the loop's to use (the stop list only), never the record's
    r.add_research(session_id=sid, at=PLANTED, trials_used=2, added=[], outcome="ok",
                   reply={"ideas": [], "stop": ["STOP_LINE"], "summary": SENTINEL})
    return r


def test_render_record_carries_no_time_and_no_run_id(rec):
    text = prompt.render_record(rec, 9, TFS)
    for planted in (SENTINEL, RUN_ID, "report.md", "2029", "03:04:05", "05:06:07", "claude-x-1", "abcdef",
                    "STOP_LINE"):
        assert planted not in text, planted
    outside = text
    for card in (CARD_A, CARD_B):
        outside = outside.replace(prompt.card_text(card), "")
    assert prompt.card_text(CARD_A) in text
    assert not DATE.search(outside), DATE.search(outside)


def test_an_open_idea_is_shown_in_full(rec):
    text = prompt.render_record(rec, 9, TFS)
    assert "#1 Flow fade" in text and "close call" in text
    for seq, plugin in ((1, "flow_a"), (2, "flow_b"), (3, "flow_c")):
        assert f"seq {seq}" in text and plugin in text
    assert "seq 4" not in text                           # dropped: never a trial
    assert "duplicate of idea #2" in text and "interrupted" in text
    assert json.dumps(prompt.public_verdict(verdict(net=0.4, p95=0.2))["diagnostics"]["gates"], sort_keys=True) \
        in text
    assert SOURCE in text                                # the champion's source: flow_a, its best plugin


def test_an_open_idea_at_a_timeframe_no_plugin_may_trade_takes_ports_only(rec):
    """M-1: LCE (5m, a human trial) is open, but a refinement keeps its idea's timeframe and 5m is not one of
    proposer.timeframes, so it takes ports only, and no refinement can get its champion as PARENT."""
    lce = rec.add_idea("LCE", {"title": "LCE"}, "seed", "5m")
    rec.add_proposal(seq=6, idea_id=lce, kind="new", plugin="lce_atr", timeframe="5m", source=SOURCE,
                     state="judged", judge_exit=0, verdict=verdict(gross=0.67, gross_p95=0.49))
    text = prompt.render_record(rec, 9, TFS)
    block = text.split(f"### #{lce} LCE\n")[1].split("\n## ")[0]
    assert "status open, ports only: its timeframe 5m is not one a plugin may trade" in block
    assert "Champion" not in block and "refinement" not in block
    assert "lce_atr@5m" in block and SOURCE in block           # its best plugin, for a port to start from
    assert "status open; " in text and "Champion, the PARENT a refinement of this idea gets: flow_a@1h" in text


def test_a_closed_idea_gets_one_line(rec):
    lines = prompt.render_record(rec, 9, TFS).splitlines()
    assert "#2 Donchian: dead, no signal (gross Sharpe -0.90 against the gross random p95 0.10)" in lines
    assert "#3 Sign flip of A: excluded, untested (it is A's sign flip)" in lines
    assert "trends persist" not in "\n".join(lines)      # a closed idea's card is not shown


def test_it_ends_with_n_and_the_bar(rec):
    last = prompt.render_record(rec, 9, TFS).rstrip().splitlines()[-1]
    assert last == f"N is now 9; the bar is {sharpe_bar(9):.2f}"


def test_the_instruction_asks_for_one_proposal():
    text = prompt.instruction()
    assert "one proposal" in text and "signal before costs" in text and "close call" in text


def test_a_flagged_open_idea_adds_exactly_the_fixed_sentence_and_no_time(rec):
    flagged = rec.next_up()
    assert flagged["id"] == 1 and flagged["next_up"] == PLANTED
    text = prompt.instruction(flagged)
    assert text == prompt.INSTRUCTION + "\n\nThe human asks that this round's proposal work on idea #1."
    whole = prompt.render_record(rec, 9, TFS) + text
    for planted in ("2029", "05:06:07"):
        assert planted not in whole, planted


def test_no_flag_and_a_flagged_idea_no_longer_open_add_nothing(rec):
    assert prompt.instruction() == prompt.instruction(None) == prompt.INSTRUCTION
    with rec.engine.begin() as c:            # closed by a write that left its flag (set_idea would clear it)
        c.execute(ideas.update().where(ideas.c.id == 1).values(status="exhausted"))
    flagged = rec.next_up()
    assert flagged["status"] == "exhausted" and prompt.instruction(flagged) == prompt.INSTRUCTION


def test_a_fixup_holds_the_prompt_the_rejected_reply_and_the_errors():
    rejected = {"kind": "new", "timeframe": "2h", "source": "x"}
    text = prompt.fixup("THE ROUND'S PROMPT", rejected, ["timeframe: must be one of ['1h']", "idea: required"])
    assert text.startswith("THE ROUND'S PROMPT")
    at = [text.find(s) for s in ("THE ROUND'S PROMPT", json.dumps(rejected, indent=1),
                                  "timeframe: must be one of ['1h']", "idea: required")]
    assert -1 not in at and at == sorted(at)


STOP_HEADING = "Directions the research step recommends avoiding:"


def test_the_stop_section_is_its_heading_and_one_line_per_direction():
    assert prompt.stop_section([]) == ""
    assert prompt.stop_section(["Sign flips of dead fades", "Exit-only\nchanges "]) == \
        f"{STOP_HEADING}\n- Sign flips of dead fades\n- Exit-only changes\n"


HUMAN_FIRST = "When the human asks for an idea below, that request comes before this list."


def test_in_a_proposals_prompt_the_stop_list_gives_way_to_the_humans_request():
    assert prompt.stop_section([], human_first=True) == ""
    assert prompt.stop_section(["Exit-only\nchanges "], human_first=True) == \
        f"{STOP_HEADING}\n- Exit-only changes\n\n{HUMAN_FIRST}\n"
    assert HUMAN_FIRST not in prompt.stop_section(["Exit-only changes"])      # a research round's: no request


HUMAN_STOP_HEADING = "Directions the human has ruled out:"


def test_the_humans_stop_section_is_its_heading_and_one_line_per_item():
    assert prompt.human_stop_section([]) == ""
    assert prompt.human_stop_section(["Round-number stop cascades", "High-volume\nreturn  premia "]) == \
        f"{HUMAN_STOP_HEADING}\n- Round-number stop cascades\n- High-volume return premia\n"
    assert HUMAN_FIRST not in prompt.human_stop_section(["Round-number stop cascades"])


HUMANS = ("The directions the human has ruled out, when listed above, are the human's: not yours to revise, and "
          "your stop list must not restate them.")


def test_every_research_round_is_told_the_humans_directions_are_not_its_to_revise_or_restate(cfg):
    pc = settings.load(cfg)
    for revising in (False, True):
        for capped in (False, True):
            text = prompt.research_instruction(pc, revising=revising, capped=capped)
            assert HUMANS in " ".join(text.split()), (revising, capped)


def test_a_stop_item_names_a_direction_never_an_open_idea(cfg):
    pc = settings.load(cfg)
    for text in (prompt.research_instruction(pc), prompt.research_instruction(pc, revising=True)):
        assert ("Each stop item names a direction (a mechanism or a family of rules), never an open idea of the "
                "record: only the human closes an idea.") in " ".join(text.split())


def test_the_research_instruction_says_what_the_round_is_for_and_ends_with_its_contract(cfg):
    pc = settings.load(cfg)
    text = prompt.research_instruction(pc)
    described = contract.describe_research(pc.timeframes, pc.research_max_ideas)
    assert text.endswith(described)
    words = " ".join(text[: -len(described)].split())
    for s in ("research round, not a proposal", "the piles, the diagnostics of every judged proposal, and what "
              "closed each idea", "build on", "near-copy of any judged plugin's mechanism", "sign flip of a closed "
              "idea", "filter added to a closed idea's entry", "from its exit rather than its entry",
              "a restatement of an open idea in the record",
              "A stop with no target earns on trends whatever the entry", "exits stay neutral",
              "must not cite numbers from the record", "mechanism, literature and rule only",
              f"at most {pc.research_max_ideas} idea cards", f"at most {contract.STOP_MAX_ITEMS}", "summary",
              "not the one under \"Your reply\""):
        assert s in words, s
    assert not DATE.search(text)


REVISE = "keep what still holds, drop what the record has since answered, and add what it now shows"


def test_a_research_round_with_a_stop_list_in_force_is_asked_to_revise_it(cfg):
    pc = settings.load(cfg)
    first, later = prompt.research_instruction(pc), prompt.research_instruction(pc, revising=True)
    words = " ".join(later.split())
    assert "The stop list above is the one in force" in words and REVISE in words
    assert later.endswith(contract.describe_research(pc.timeframes, pc.research_max_ideas))
    # a first round has no list to revise: its instruction says nothing of one, and is the later one less that
    assert "in force" not in first and REVISE not in " ".join(first.split())
    assert later.replace(prompt.REVISE_STOP + "\n\n", "", 1) == first


def test_a_capped_research_round_is_asked_for_no_cards_only_the_stop_list_and_summary(cfg):
    pc = settings.load(cfg)
    for revising in (False, True):
        text = prompt.research_instruction(pc, revising=revising, capped=True)
        assert text.endswith(contract.describe_research(pc.timeframes, pc.research_max_ideas))
        words = " ".join(text.split())
        for s in ("Return no idea cards this round: the record already holds the untried research ideas the cap "
                  "allows", "Revise only the stop list", "and the summary for the human",
                  "Each stop item names a direction", "not the one under \"Your reply\""):
            assert s in words, s
        assert f"at most {pc.research_max_ideas} idea cards" not in words
        assert ("The stop list above is the one in force" in words) is revising
    assert "Return no idea cards" not in prompt.research_instruction(pc)


def test_the_research_instruction_follows_the_configured_limit(cfg):
    pc = settings.load(cfg)
    text = prompt.research_instruction(replace(pc, research_max_ideas=2))
    assert "at most 2 idea cards" in text and "at most 2;" in text and "at most 3 idea cards" not in text


def test_a_research_fixup_asks_for_a_corrected_note():
    text = prompt.fixup("THE ROUND'S PROMPT", {"stop": 1}, ["stop: must be an array"], what="research note")
    assert text.rstrip().endswith("Return a corrected research note: one JSON object, as the reply contract "
                                  "describes.")
    assert prompt.fixup("P", {}, ["e"]).rstrip().endswith("Return a corrected proposal: one JSON object, as the "
                                                          "reply contract describes.")


# -- the referee (referee-and-human-stops spec, "The referee")
def _note(*titles):
    return contract.parse_research({"ideas": [{"title": t, "mechanism": f"{t}: who pays", "why_it_persists": "w",
                                               "signal": "s", "trades_per_coin_per_year": 12,
                                               "likely_failure": "f", "columns": ["close", "funding_last"],
                                               "timeframe": "1h", "builds_on": f"{t} builds on the fades"}
                                              for t in titles], "stop": ["x"], "summary": "SUMMARY"},
                                   ("1h", "4h"), 3)


def test_the_cards_under_review_are_the_notes_cards_as_written_in_order_with_builds_on():
    n = _note("Basis squeeze", "Carry unwind")
    text = prompt.note_cards(n)
    assert text.startswith(prompt.CARDS_HEADING + "\n\n") and text.endswith("\n")
    for k, idea in enumerate(n.ideas, 1):
        written = {**asdict(idea), "columns": list(idea.columns)}
        assert f"### Card {k}\n\n" + prompt.card_text(written) in text
    assert text.index("### Card 1") < text.index("Basis squeeze") < text.index("### Card 2") < \
        text.index("Carry unwind")
    assert "Basis squeeze builds on the fades" in text and '"builds_on"' in text
    # the cards only: never the round's summary or stop list
    assert "SUMMARY" not in text and not DATE.search(text)


REFEREE_CHECKS = (
    "skeptical referee", "accept", "revise", "reject",
    "a near-copy of a judged plugin's mechanism", "a sign flip of a closed idea",
    "a filter added to a closed idea's entry", "a restatement of an open idea",
    "an edge that would come from its exit rather than its entry", "the directions the human has ruled out",
    "round-trip cost", "bar columns a plugin may read",
    "Revise only to correct a claim, sharpen the rule, neutralise the exit or fix the timeframe",
    "a different idea is a reject", "no numbers from the record", "one verdict per card, in the cards' order",
    "A revised card keeps its own title or takes one no idea in the record and no other card in this report uses.",
    "not the one under \"Your reply\"")


def test_a_web_referee_is_told_its_checks_its_hosts_and_never_to_look_for_market_data(cfg):
    pc = settings.load(cfg)
    assert pc.research_referee_web
    text = prompt.referee_instruction(pc)
    assert text == prompt.referee_instruction(pc, web=True)
    described = contract.describe_referee(pc.timeframes, pc.research_max_ideas)
    assert text.endswith(described)
    words = " ".join(text[: -len(described)].split())
    for s in (*REFEREE_CHECKS, "use the allowed academic sources to find published evidence for or against it",
              "the papers it cites exist and say what it claims",
              "You may fetch only from these hosts: " + ", ".join(pc.referee_domains),
              "Never look for prices, charts or market news",
              "the URLs you read on those hosts that the verdict relies on", SOURCES_BEFORE):
        assert s in words, s
    assert "no web access" not in words
    assert DATE.findall(text) == ["2026-07-01"]           # the lab's holdout start, from the config: no other date


def test_a_referee_without_the_web_is_told_its_sources_stay_empty(cfg):
    pc = replace(settings.load(cfg), research_referee_web=False)
    text = prompt.referee_instruction(pc)
    assert text == prompt.referee_instruction(settings.load(cfg), web=False)
    described = contract.describe_referee(pc.timeframes, pc.research_max_ideas)
    assert text.endswith(described)
    words = " ".join(text[: -len(described)].split())
    for s in (*REFEREE_CHECKS, "You have no web access in this review, so every verdict's sources stay empty"):
        assert s in words, s
    assert "fetch" not in words and not any(d in text for d in pc.referee_domains)
    assert not DATE.search(text)                          # no sources, so no date for them


SOURCES_BEFORE = ("Cite only sources published before 2026-07-01, and disregard any published on or after that "
                  "date.")


def test_the_referees_sources_end_at_the_configured_holdout_start(cfg):
    """R-RF7: a paper from the holdout period may discuss it; the web_reviewed mark stays the safeguard."""
    pc = replace(settings.load(cfg), holdout_start=datetime(2026, 9, 1).date())
    words = " ".join(prompt.referee_instruction(pc).split())
    assert SOURCES_BEFORE.replace("2026-07-01", "2026-09-01") in words and "2026-07-01" not in words


def test_the_referee_instruction_follows_the_configured_hosts_and_limit(cfg):
    pc = replace(settings.load(cfg), referee_domains=("arxiv.org", "www.nber.org"), research_max_ideas=2)
    text = prompt.referee_instruction(pc)
    assert "You may fetch only from these hosts: arxiv.org, www.nber.org;" in " ".join(text.split())
    assert "ssrn" not in text and text.endswith(contract.describe_referee(pc.timeframes, 2))


def test_a_referee_fixup_asks_for_a_corrected_report():
    text = prompt.fixup("THE ROUND'S PROMPT", {"verdicts": 1}, ["verdicts: must be an array"], what="referee report")
    assert text.rstrip().endswith("Return a corrected referee report: one JSON object, as the reply contract "
                                  "describes.")

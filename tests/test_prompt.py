"""The record as the model sees it: whitelisted verdict fields, no time, no run id."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import pytest

from perp_lab.dsr import sharpe_bar
from perp_lab.proposer import prompt
from perp_lab.proposer.record import Record

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
    return r


def test_render_record_carries_no_time_and_no_run_id(rec):
    text = prompt.render_record(rec, 9)
    for planted in (SENTINEL, RUN_ID, "report.md", "2029", "03:04:05", "05:06:07", "claude-x-1", "abcdef"):
        assert planted not in text, planted
    outside = text
    for card in (CARD_A, CARD_B):
        outside = outside.replace(prompt.card_text(card), "")
    assert prompt.card_text(CARD_A) in text
    assert not DATE.search(outside), DATE.search(outside)


def test_an_open_idea_is_shown_in_full(rec):
    text = prompt.render_record(rec, 9)
    assert "#1 Flow fade" in text and "close call" in text
    for seq, plugin in ((1, "flow_a"), (2, "flow_b"), (3, "flow_c")):
        assert f"seq {seq}" in text and plugin in text
    assert "seq 4" not in text                           # dropped: never a trial
    assert "duplicate of idea #2" in text and "interrupted" in text
    assert json.dumps(prompt.public_verdict(verdict(net=0.4, p95=0.2))["diagnostics"]["gates"], sort_keys=True) \
        in text
    assert SOURCE in text                                # the champion's source: flow_a, its best plugin


def test_a_closed_idea_gets_one_line(rec):
    lines = prompt.render_record(rec, 9).splitlines()
    assert "#2 Donchian: dead, no signal (gross Sharpe -0.90 against the gross random p95 0.10)" in lines
    assert "#3 Sign flip of A: excluded, untested (it is A's sign flip)" in lines
    assert "trends persist" not in "\n".join(lines)      # a closed idea's card is not shown


def test_it_ends_with_n_and_the_bar(rec):
    assert prompt.render_record(rec, 9).rstrip().splitlines()[-1] == f"N is now 9; the bar is {sharpe_bar(9):.2f}"


def test_the_instruction_asks_for_one_proposal():
    text = prompt.instruction()
    assert "one proposal" in text and "signal before costs" in text and "close call" in text


def test_a_fixup_holds_the_prompt_the_rejected_reply_and_the_errors():
    rejected = {"kind": "new", "timeframe": "2h", "source": "x"}
    text = prompt.fixup("THE ROUND'S PROMPT", rejected, ["timeframe: must be one of ['1h']", "idea: required"])
    assert text.startswith("THE ROUND'S PROMPT")
    at = [text.find(s) for s in ("THE ROUND'S PROMPT", json.dumps(rejected, indent=1),
                                  "timeframe: must be one of ['1h']", "idea: required")]
    assert -1 not in at and at == sorted(at)

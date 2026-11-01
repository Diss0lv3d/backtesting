"""Piles, idea statuses and the champion: a table of verdict fixtures."""

from __future__ import annotations

import pytest

from perp_lab.proposer.piles import best_pile, champion, family_outcomes, idea_outcome, idea_status, pile


def verdict(*, net=-1.26, p95=-0.19, trades=3385, floor=64, gross=-0.9, gross_p95=0.1, decision="kill",
            reasons=(), holdout="n/a", diagnostics=True, before=True, lineage="L"):
    diag = None
    if diagnostics:
        diag = {"gates": {"net_sharpe": net, "random_p95": p95, "oos_trades": trades, "min_oos_trades": floor},
                "before_costs": {"sharpe": gross, "random_p95": gross_p95} if before else None}
    return {"decision": decision, "reasons": list(reasons), "holdout_status": holdout,
            "lineage_id": lineage, "schema": 2, "diagnostics": diag}


@pytest.mark.parametrize("v, expected", [
    (verdict(), "no signal"),  # trial 7
    (verdict(decision="promote", holdout="withdrawn"), "close call"),
    (verdict(gross=-0.2, gross_p95=-0.3), "no signal"),
    (verdict(diagnostics=False), "unknown"),
    (verdict(reasons=["lookahead: x"]), "broken"),
    (verdict(reasons=["plugin_error: boom"]), "broken"),
    (verdict(trades=10), "broken"),
    (verdict(decision="promote"), "promoted"),
    (None, "broken"),
    (verdict(net=0.5, p95=0.2), "close call"),
    (verdict(net=0.5, p95=None), "no signal"),
    (verdict(gross=0.5, gross_p95=-0.3), "signal before costs"),
    (verdict(gross=0.5, gross_p95=None), "no signal"),
    (verdict(before=False), "no signal"),
    (verdict(floor=None, trades=1), "no signal"),
    (verdict(decision="promote", reasons=["lookahead: x"]), "broken"),
] + [
    (verdict(**{k: bad}), "no signal")
    for bad in (float("inf"), float("-inf"), float("nan"))
    for k in ("net", "p95", "gross", "gross_p95")
] + [
    (verdict(net=0.5, p95=0.2, **{"gross": bad}), "close call") for bad in (float("inf"), float("nan"))
] + [
    (verdict(gross=0.5, gross_p95=-0.3, net=bad), "signal before costs") for bad in (float("inf"), float("nan"))
])
def test_pile(v, expected):
    assert pile(v) == expected


def test_best_pile_ignores_unknown():
    assert best_pile(["unknown", "no signal", "close call"]) == "close call"
    assert best_pile(["unknown"]) is None and best_pile([]) is None


def test_family_outcomes():
    vs = [verdict(holdout="passed", lineage="A"), verdict(holdout="failed", lineage="B"),
          verdict(holdout="pending", lineage="C")]
    assert family_outcomes(vs) == {"A": "passed", "B": "failed"}


def prop(seq, v, *, exit=0, dup=None, idea=1):
    return {"seq": seq, "idea_id": idea, "judge_exit": exit, "verdict": v, "duplicate_of": dup}


IDEA = {"id": 1, "origin": "proposer", "excluded_reason": None}


def test_an_ideas_outcome_comes_from_any_family_its_judged_plugins_belong_to():
    # R43: a broken first plugin set the idea's family; a later root started a second one
    outcomes = {"B": "passed", "C": "failed"}
    a, b, c = verdict(lineage="A"), verdict(lineage="B"), verdict(lineage="C")
    assert idea_outcome([prop(1, a), prop(2, b)], outcomes) == "passed"
    assert idea_outcome([prop(1, a), prop(2, c)], outcomes) == "failed"
    assert idea_outcome([prop(1, c), prop(2, b)], outcomes) == "passed"      # the edge wins a disagreement
    assert idea_outcome([prop(1, a)], outcomes) is None
    assert idea_outcome([prop(1, b, exit=None), prop(2, None)], outcomes) is None   # never judged: no family
    assert idea_outcome([], outcomes) is None


def test_status_table():
    ns, cc = verdict(), verdict(net=0.5, p95=0.2)
    assert idea_status({**IDEA, "excluded_reason": "x"}, [], None, 4) == "excluded"
    assert idea_status({**IDEA, "origin": "seed", "port_of": 3}, [prop(1, ns)], "passed", 4) == "passed"
    assert idea_status(IDEA, [prop(1, ns)], "failed", 4) == "failed"
    assert idea_status(IDEA, [prop(1, ns, dup=2)], None, 4) == "duplicate"
    assert idea_status({**IDEA, "origin": "seed"}, [prop(1, ns, dup=2)], None, 4) == "open"
    assert idea_status(IDEA, [prop(1, ns)], None, 4) == "dead"
    assert idea_status(IDEA, [prop(i, cc) for i in range(4)], None, 4) == "exhausted"
    assert idea_status(IDEA, [prop(1, cc)], None, 4) == "open"
    assert idea_status(IDEA, [], None, 4) == "open"
    assert idea_status(IDEA, [prop(1, None)], None, 4) == "open"
    assert idea_status(IDEA, [prop(1, ns, dup=9), prop(2, cc)], None, 4) == "open"
    assert idea_status(IDEA, [prop(1, ns, exit=None)], None, 4) == "open"


def test_champion():
    pend1 = verdict(decision="promote", holdout="pending")
    pend2 = verdict(decision="promote", holdout="pending", net=0.1)
    assert champion([prop(1, pend1), prop(5, pend2), prop(3, verdict(net=0.9, p95=0.1))])["seq"] == 5
    broken = verdict(reasons=["lookahead: x"])
    assert champion([prop(1, broken), prop(2, verdict(diagnostics=False))]) is None
    assert champion([]) is None
    a, b = verdict(net=0.5, p95=0.2), verdict(net=0.9, p95=0.2)
    assert champion([prop(1, a), prop(2, b), prop(3, broken)])["seq"] == 2
    assert champion([prop(1, verdict(gross=0.5, gross_p95=0), dup=None), prop(2, a)])["seq"] == 2
    assert champion([prop(1, b, dup=9)]) is None


def test_champion_nan_net_sharpe_sorts_last_in_any_order():
    nan = verdict(net=float("nan"), p95=-5.0)
    nan["diagnostics"]["gates"]["net_sharpe"] = float("nan")
    good, other = verdict(gross=0.5, gross_p95=0), verdict(gross=0.6, gross_p95=0)
    good["diagnostics"]["gates"]["net_sharpe"] = -0.5
    other["diagnostics"]["gates"]["net_sharpe"] = -0.2
    nan_p = lambda: prop(1, {**verdict(gross=0.7, gross_p95=0), "diagnostics": nan["diagnostics"] | {
        "before_costs": {"sharpe": 0.7, "random_p95": 0}}})
    ps = [nan_p(), prop(2, good), prop(3, other)]
    for order in (ps, ps[::-1], [ps[1], ps[0], ps[2]]):
        assert champion(order)["seq"] == 3
    assert champion([ps[0], ps[1]])["seq"] == 2
    assert champion([ps[1], ps[0]])["seq"] == 2

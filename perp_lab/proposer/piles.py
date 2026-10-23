"""Piles, idea statuses and the champion, all assigned in code from the verdict (ADR 0006).

Pure functions over the dicts the record returns. The model never sees or sets any of this.
A missing number is never read as 0: a diagnostics block, a gross benchmark or a trade minimum
that is absent makes the test that needs it unable to pass, not pass by default.
"""

from __future__ import annotations

RANK = {"promoted": 4, "close call": 3, "signal before costs": 2, "no signal": 1, "broken": 0}


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def pile(verdict: dict | None) -> str:
    if verdict is None:
        return "broken"
    reasons = verdict.get("reasons") or []
    if any(str(r).startswith(("lookahead:", "plugin_error")) for r in reasons):
        return "broken"
    if verdict.get("holdout_status") == "withdrawn":
        return "close call"
    if verdict.get("decision") == "promote":
        return "promoted"
    diag = verdict.get("diagnostics")
    if not diag:
        return "unknown"
    gates = diag.get("gates") or {}
    trades, floor = gates.get("oos_trades"), gates.get("min_oos_trades")
    if _num(trades) and _num(floor) and trades < floor:
        return "broken"
    net, p95 = gates.get("net_sharpe"), gates.get("random_p95")
    if _num(net) and _num(p95) and net > 0 and net > p95:
        return "close call"
    gross = diag.get("before_costs") or {}
    g, gp95 = gross.get("sharpe"), gross.get("random_p95")
    if _num(g) and _num(gp95) and g > max(0, gp95):
        return "signal before costs"
    return "no signal"


def best_pile(piles: list[str]) -> str | None:
    known = [p for p in piles if p in RANK]
    return max(known, key=RANK.__getitem__) if known else None


def family_outcomes(verdicts: list[dict]) -> dict[str, str]:
    """lineage_id -> "passed" | "failed", for the families whose holdout read was spent."""
    out: dict[str, str] = {}
    for v in verdicts:
        status, family = (v or {}).get("holdout_status"), (v or {}).get("lineage_id")
        if family is not None and status in ("passed", "failed"):
            out[family] = status
    return out


def _judged(proposals: list[dict]) -> list[dict]:
    return [p for p in proposals if p.get("judge_exit") is not None]


def _pile_of(p: dict) -> str:
    return pile(p.get("verdict"))


def idea_status(idea: dict, proposals: list[dict], family_outcome: str | None, per_idea: int) -> str:
    if idea.get("excluded_reason"):
        return "excluded"
    if family_outcome in ("passed", "failed"):
        return family_outcome
    judged = _judged(proposals)
    if idea.get("origin") == "proposer" and judged and all(p.get("duplicate_of") is not None for p in judged):
        return "duplicate"
    best = best_pile([_pile_of(p) for p in judged if p.get("duplicate_of") is None])
    if best == "no signal":
        return "dead"
    if len(judged) >= per_idea:
        return "exhausted"
    return "open"


def _net_sharpe(p: dict) -> float:
    net = (((p.get("verdict") or {}).get("diagnostics") or {}).get("gates") or {}).get("net_sharpe")
    return net if _num(net) else float("-inf")


def champion(proposals: list[dict]) -> dict | None:
    cands = [p for p in _judged(proposals)
             if p.get("duplicate_of") is None and _pile_of(p) not in ("broken", "unknown")]
    if not cands:
        return None
    pending = [p for p in cands
               if _pile_of(p) == "promoted" and (p["verdict"] or {}).get("holdout_status") == "pending"]
    if pending:
        return max(pending, key=lambda p: p["seq"])
    return max(cands, key=lambda p: (RANK[_pile_of(p)], _net_sharpe(p)))

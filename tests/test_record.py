"""The record store: round trips, the trial count, UTC-day splits, duplicate titles."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from perp_lab.proposer.record import Record

UTC = timezone.utc


@pytest.fixture
def rec(tmp_path):
    return Record(tmp_path / "state")


def _prop(rec, plugin, exit_=None, reasons=None, **f):
    seq = rec.next_seq()
    rec.add_proposal(seq=seq, plugin=plugin, state="judged", judge_exit=exit_,
                     verdict=None if reasons is None else {"reasons": reasons}, **f)
    return seq


def test_session_round_trip(rec):
    sid = rec.start_session(model="claude-x-1", cutoff="2026-01-01", caps={"a": 1}, branch="b")
    rec.end_session(sid, "cap", trials_used=3, tokens_in=5, tokens_out=6)
    s = rec.session(sid)
    assert s["caps"] == {"a": 1} and s["stop_reason"] == "cap" and s["trials_used"] == 3
    assert s["started_at"].tzinfo is not None and s["ended_at"] >= s["started_at"]


def test_idea_round_trip_and_set(rec):
    i = rec.add_idea("Funding fade", {"why": "x"}, "seed", "1h")
    j = rec.add_idea("Funding fade 4h", {"why": "x"}, "proposer", "4h", port_of=i)
    assert rec.idea(i)["card"] == {"why": "x"} and rec.idea(i)["status"] == "open"
    assert rec.idea(j)["port_of"] == i
    rec.set_idea(i, status="exhausted", root_plugin="p1")
    assert rec.idea(i)["status"] == "exhausted" and rec.idea(i)["root_plugin"] == "p1"
    assert [x["id"] for x in rec.ideas()] == [i, j]


@pytest.mark.parametrize("dup", ["Funding fade", "funding FADE", "FUNDING FADE"])
def test_duplicate_title_refused_whatever_case(rec, dup):
    rec.add_idea("Funding fade", {}, "seed", "1h")
    with pytest.raises(ValueError):
        rec.add_idea(dup, {}, "seed", "1h")
    assert len(rec.ideas()) == 1


def test_proposal_round_trip_ordered(rec):
    i = rec.add_idea("A", {}, "seed", "1h")
    assert rec.next_seq() == 1
    rec._seq = 0   # give the number back for the fixed expectations below
    s1 = rec.add_proposal(idea_id=i, plugin="p1", state="staged", preflight={"ok": True}, source="x")
    s2 = rec.add_proposal(idea_id=i, plugin="p2", state="staged")
    assert (s1, s2) == (1, 2) and rec.next_seq() == 3
    rec.update_proposal(1, state="judged", judge_exit=0, verdict={"reasons": []}, pile="close call")
    p = rec.proposals(i)
    assert [x["seq"] for x in p] == [1, 2] and p[0]["verdict"] == {"reasons": []} and p[0]["preflight"] == {"ok": True}
    assert rec.proposals(999) == [] and len(rec.proposals()) == 2
    with pytest.raises(Exception):
        rec.add_proposal(plugin="p1", state="staged")


def test_a_run_is_unique_by_plugin_and_timeframe(rec):
    # a seed plugin runs under two ideas at two timeframes: an idea and its port
    a, b = rec.add_idea("Donchian 4h", {}, "seed", "4h"), rec.add_idea("Donchian 1h", {}, "seed", "1h")
    rec.add_proposal(idea_id=a, plugin="donchian_atr", timeframe="4h", state="judged")
    rec.add_proposal(idea_id=b, plugin="donchian_atr", timeframe="1h", state="judged")
    assert {(x["plugin"], x["timeframe"], x["idea_id"]) for x in rec.proposals()} == {
        ("donchian_atr", "4h", a), ("donchian_atr", "1h", b)}
    with pytest.raises(Exception):
        rec.add_proposal(idea_id=b, plugin="donchian_atr", timeframe="4h", state="judged")
    assert len(rec.proposals()) == 2


def test_call_round_trip(rec):
    sid = rec.start_session()
    rec.add_call(session_id=sid, seq=1, tokens_in=1, tokens_out=2, cost_usd=0.5, outcome="ok")
    assert rec.usage_on(datetime.now(UTC).date()) == (1, 2)


def test_trials_used_rules(rec):
    _prop(rec, "a", 0, ["Sharpe low"])
    _prop(rec, "b", 1, ["x"])
    _prop(rec, "c", 2, ["refused"])
    _prop(rec, "d", 0, ["lookahead: peeks"])
    _prop(rec, "e", None)
    assert rec.trials_used() == 2


def test_trials_used_counts_interrupted_judge(rec):
    _prop(rec, "a", -2)
    _prop(rec, "b", -15, ["whatever"])
    assert rec.trials_used() == 2


def test_a_proposal_is_the_proposers_unless_marked_seed(rec):
    a = _prop(rec, "a", None)
    b = _prop(rec, "b", 0, [], origin="seed")
    assert [(p["seq"], p["origin"]) for p in rec.proposals()] == [(a, "proposer"), (b, "seed")]
    with pytest.raises(Exception):
        _prop(rec, "c", 0, [], origin="seeded")
    assert len(rec.proposals()) == 2


def test_a_seeded_trial_counts_in_neither_trials_used_nor_judge_calls_on(rec):
    # trials_total is for the proposer's own trials; the human's were judged on no day of the proposer's
    today = datetime.now(UTC)
    _prop(rec, "human", 0, ["Sharpe low"], origin="seed")
    _prop(rec, "human2", 1, ["x"], origin="seed")
    assert rec.trials_used() == 0 and rec.judge_calls_on(today.date()) == 0
    seq = _prop(rec, "mine", 0, ["Sharpe low"], judged_at=today)
    rec.add_judge_call(seq=seq, at=today, exit=0)
    assert rec.trials_used() == 1 and rec.judge_calls_on(today.date()) == 1


def test_judge_calls_on_counts_every_call_not_every_proposal(rec):
    # R45: a judge exit 1 is retried on the same bytes; the day's quota counts calls (spec, Pace and budget)
    today = datetime.now(UTC)
    seq = _prop(rec, "mine", 0, ["Sharpe low"], judged_at=today)
    for code in (1, 1, 0):
        rec.add_judge_call(seq=seq, at=today, exit=code)
    assert rec.judge_calls_on(today.date()) == 3 and rec.trials_used() == 1


def test_day_splits_at_midnight_utc(rec):
    d = date(2026, 10, 20)
    before = datetime(2026, 10, 19, 23, 59, 59, tzinfo=UTC)
    at = datetime(2026, 10, 20, 0, 0, 0, tzinfo=UTC)
    last = datetime(2026, 10, 20, 23, 59, 59, tzinfo=UTC)
    nxt = datetime(2026, 10, 21, 0, 0, 0, tzinfo=UTC)
    for n, t in enumerate([before, at, last, nxt]):
        rec.add_call(at=t, tokens_in=10, tokens_out=1)
        rec.add_judge_call(seq=_prop(rec, f"p{n}", 1, ["x"], judged_at=t), at=t, exit=1)
    assert rec.usage_on(d) == (20, 2) and rec.judge_calls_on(d) == 2
    # a non-UTC aware time is converted: 01:00+02:00 on the 20th is 23:00 UTC on the 19th
    rec.add_call(at=datetime(2026, 10, 20, 1, tzinfo=timezone(timedelta(hours=2))), tokens_in=7, tokens_out=0)
    assert rec.usage_on(d) == (20, 2) and rec.usage_on(date(2026, 10, 19))[0] == 17
    assert rec.usage_on(date(2030, 1, 1)) == (0, 0)


def test_transcript_path(rec, tmp_path):
    p = rec.transcript_path(7)
    assert p == tmp_path / "state" / "transcripts" / "0007.jsonl" and p.parent.is_dir()


def test_persists_across_instances(tmp_path):
    Record(tmp_path).add_idea("A", {}, "seed", "1h")
    assert len(Record(tmp_path).ideas()) == 1


def test_next_seq_reserves(rec):
    a, b = rec.next_seq(), rec.next_seq()
    assert a != b and b == a + 1


def test_abandoned_seq_is_never_reused(rec, tmp_path):
    seq = rec.next_seq()
    rec.transcript_path(seq).write_text("{}\n")      # written, then the proposal dropped before add_proposal
    assert rec.next_seq() == seq + 1
    fresh = Record(tmp_path / "state")
    assert fresh.next_seq() == seq + 1   # only the transcript-bearing number is pinned across sessions


def test_fresh_record_starts_above_recorded_proposals(tmp_path):
    r = Record(tmp_path)
    r.add_proposal(plugin="p", state="staged")
    assert Record(tmp_path).next_seq() == 2


def test_add_proposal_uses_reserved_seq(rec):
    seq = rec.next_seq()
    rec.next_seq()
    assert rec.add_proposal(seq=seq, plugin="p", state="staged") == seq
    assert [p["seq"] for p in rec.proposals()] == [seq]
    assert rec.next_seq() == seq + 2


def test_add_seed_is_one_transaction_and_only_into_a_record_with_no_ideas(rec):
    idea = {"id": 1, "title": "A", "card": {}, "origin": "seed", "timeframe": "1h", "status": "open"}
    prop = {"seq": 1, "idea_id": 1, "origin": "seed", "plugin": "p", "timeframe": "1h", "state": "judged"}
    with pytest.raises(Exception):
        rec.add_seed([idea], [prop, {**prop, "plugin": "q"}])    # the second row's seq is taken: nothing stays
    assert rec.ideas() == [] and rec.proposals() == []
    assert rec.add_seed([idea], [prop]) is True
    assert [i["title"] for i in rec.ideas()] == ["A"] and rec.next_seq() == 2
    assert rec.add_seed([{**idea, "id": 2, "title": "B"}], []) is False and len(rec.ideas()) == 1

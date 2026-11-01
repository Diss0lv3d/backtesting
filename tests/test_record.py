"""The record store: round trips, the trial count, UTC-day splits, duplicate titles."""

from __future__ import annotations

import sqlite3
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


def test_the_record_is_in_wal_mode_with_a_busy_timeout(rec, tmp_path):
    """M-12: the Ideas page reads record.db while a session writes it; a "database is locked" would end a week-long
    session. WAL lets readers and the writer go on together, and a busy timeout waits out a brief lock."""
    for r in (rec, Record(tmp_path / "state")):          # every connection, the second process's too
        with r.engine.connect() as c:
            assert c.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
            assert c.exec_driver_sql("PRAGMA busy_timeout").scalar() == 30_000


# The ideas table as record.db was first created (seeded 2026-11-01): no next_up column.
OLD_IDEAS = """CREATE TABLE ideas (
    id INTEGER NOT NULL, title VARCHAR(256) NOT NULL, card JSON, origin VARCHAR(16) NOT NULL,
    timeframe VARCHAR(8), status VARCHAR(16) NOT NULL, port_of INTEGER, family VARCHAR(128),
    root_plugin VARCHAR(128), excluded_reason TEXT, created_at DATETIME NOT NULL, PRIMARY KEY (id),
    FOREIGN KEY(port_of) REFERENCES ideas (id))"""


def _idea_columns(state) -> list[str]:
    c = sqlite3.connect(state / "record.db")
    try:
        return [row[1] for row in c.execute("PRAGMA table_info(ideas)")]
    finally:
        c.close()


def test_an_old_record_gets_the_next_up_column_once_and_keeps_its_ideas(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    c = sqlite3.connect(state / "record.db")
    c.execute(OLD_IDEAS)
    c.execute("INSERT INTO ideas (title, origin, status, created_at) VALUES ('Old', 'seed', 'open', "
              "'2026-11-01 00:00:00')")
    c.commit()
    c.close()
    rec = Record(state)
    assert rec.idea(1)["title"] == "Old" and rec.idea(1)["next_up"] is None
    assert _idea_columns(state).count("next_up") == 1
    rec.flag_next(1)
    again = Record(state)                       # the migration is idempotent
    assert _idea_columns(state).count("next_up") == 1 and again.next_up()["id"] == 1


def test_one_idea_is_next_up_at_a_time_and_only_an_open_one(rec):
    a, b = rec.add_idea("A", {}, "seed", "1h"), rec.add_idea("B", {}, "human", "1h")
    dead = rec.add_idea("C", {}, "seed", "1h", status="dead")
    assert rec.next_up() is None
    rec.flag_next(a)
    assert rec.next_up()["id"] == a and rec.next_up()["next_up"].tzinfo is not None
    rec.flag_next(b)                            # setting one clears the others
    assert rec.next_up()["id"] == b and rec.idea(a)["next_up"] is None
    with pytest.raises(ValueError, match="dead"):
        rec.flag_next(dead)
    with pytest.raises(ValueError, match="no idea #99"):
        rec.flag_next(99)
    assert rec.next_up()["id"] == b             # a refused flag leaves the flagged idea as it was
    rec.clear_next(a)                           # a's flag is already gone: b keeps its own
    assert rec.next_up()["id"] == b
    rec.clear_next(b)
    assert rec.next_up() is None
    rec.flag_next(a)
    rec.clear_next()
    assert rec.next_up() is None


def test_the_flag_goes_when_its_idea_leaves_open(rec):
    a = rec.add_idea("A", {}, "seed", "1h")
    rec.flag_next(a)
    rec.set_idea(a, family="family:a")
    assert rec.next_up()["id"] == a
    rec.set_idea(a, status="exhausted")
    assert rec.next_up() is None and rec.idea(a)["next_up"] is None


@pytest.mark.parametrize("status", ["open", "exhausted", "dead"])
def test_an_open_exhausted_or_dead_idea_can_be_excluded_with_a_reason(rec, status):
    i = rec.add_idea("A", {}, "seed", "1h", status=status)
    rec.exclude_idea(i, "  it is B's sign flip  ")
    x = rec.idea(i)
    assert (x["status"], x["excluded_reason"]) == ("excluded", "it is B's sign flip")


@pytest.mark.parametrize("status", ["passed", "failed", "excluded", "duplicate"])
def test_no_other_idea_can_be_excluded(rec, status):
    i = rec.add_idea("A", {}, "seed", "1h", status=status)
    with pytest.raises(ValueError, match=f"idea #{i} is {status}; only an open, exhausted or dead idea"):
        rec.exclude_idea(i, "why")
    assert (rec.idea(i)["status"], rec.idea(i)["excluded_reason"]) == (status, None)


def test_an_exclusion_needs_a_reason_and_an_idea(rec):
    i = rec.add_idea("A", {}, "seed", "1h")
    with pytest.raises(ValueError, match="needs a reason"):
        rec.exclude_idea(i, " \n ")
    with pytest.raises(ValueError, match="there is no idea #9"):
        rec.exclude_idea(9, "why")
    assert rec.idea(i)["status"] == "open"


def test_an_excluded_idea_loses_its_flag_and_stays_excluded_when_settled(rec):
    from types import SimpleNamespace

    from perp_lab.proposer import loop
    i = rec.add_idea("A", {}, "seed", "1h")
    rec.flag_next(i)
    rec.exclude_idea(i, "why")
    assert rec.next_up() is None
    loop.settle(rec, SimpleNamespace(proposals_per_idea=4))
    assert rec.idea(i)["status"] == "excluded"

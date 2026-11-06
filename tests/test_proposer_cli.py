from __future__ import annotations

import pytest
import yaml
from typer.testing import CliRunner

from perp_lab.cli import app
from perp_lab.proposer import context, human, piles
from perp_lab.proposer.record import Record
from perp_lab.config import load_config
from tests.conftest import REPO, write_ready_reference, write_ready_spot

runner = CliRunner()


def _config(tmp_path, **over):
    raw = yaml.safe_load(open(REPO / "config/lab.yaml"))
    raw["data_dir"] = str(tmp_path / "data")
    raw["proposer"] = {**raw["proposer"], "state_dir": str(tmp_path / "proposer"), **over}
    p = tmp_path / "lab.yaml"
    p.write_text(yaml.safe_dump(raw))
    return str(p)


def test_propose_with_an_alias_model_exits_2_with_the_reason(tmp_path, monkeypatch):
    monkeypatch.chdir(context.ROOT)
    r = runner.invoke(app, ["propose", "--config", _config(tmp_path, model="opus")])
    assert r.exit_code == 2
    assert "pinned ID" in r.output


def test_propose_outside_the_repo_root_exits_2(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = runner.invoke(app, ["propose", "--config", _config(tmp_path)])
    assert r.exit_code == 2
    assert "repo root" in r.output


def test_propose_prints_the_stop_reason_and_digest_path(tmp_path, monkeypatch):
    monkeypatch.chdir(context.ROOT)
    cfg = _config(tmp_path)
    write_ready_reference(load_config(cfg), tfs=("15m", "1h", "4h"))     # readiness (X3) at each proposer timeframe
    write_ready_spot(load_config(cfg), tfs=("15m", "1h", "4h"))
    state = tmp_path / "proposer"

    def fake(cfg_, pc, *, days, trials, repo):
        assert (days, trials, repo) == (7, 0, context.ROOT)
        Record(state).start_session(model="m")
        return "trials"
    monkeypatch.setattr("perp_lab.proposer.loop.run_session", fake)
    r = runner.invoke(app, ["propose", "--trials", "0", "--config", cfg])
    assert r.exit_code == 0
    assert "stopped: trials" in r.output
    assert str(state / "sessions" / "1.md") in r.output.replace("\n", "")


def _idea(rec, title, status, tf="1h"):
    i = rec.add_idea(title, {}, "proposer", tf)
    rec.set_idea(i, status=status)
    return i


def _judged(rec, idea, seq, verdict):
    rec.add_proposal(seq=seq, idea_id=idea, plugin=f"p{seq}", timeframe="1h", state="judged", judge_exit=0,
                     verdict=verdict)


PROMOTED = {"decision": "promote"}
CLOSE = {"diagnostics": {"gates": {"oos_trades": 50, "min_oos_trades": 10, "net_sharpe": 1.0,
                                   "random_p95": 0.5}}}
SIGNAL = {"diagnostics": {"gates": {"oos_trades": 50, "min_oos_trades": 10, "net_sharpe": -0.2, "random_p95": 0.5},
                          "before_costs": {"sharpe": 1.0, "random_p95": 0.5}}}


def test_ideas_prints_groups_in_the_spec_order(tmp_path, monkeypatch):
    monkeypatch.chdir(context.ROOT)
    rec = Record(tmp_path / "proposer")
    _idea(rec, "closed one", "dead")
    _idea(rec, "open one", "open")
    close = _idea(rec, "close one", "open")
    prom = _idea(rec, "promo one", "open")
    _idea(rec, "passed one", "passed")
    _judged(rec, close, 1, CLOSE)
    _judged(rec, prom, 2, PROMOTED)
    sig = _idea(rec, "signal one", "open")
    _judged(rec, sig, 3, SIGNAL)
    r = runner.invoke(app, ["ideas", "--config", _config(tmp_path)])
    assert r.exit_code == 0, r.output
    blocks: dict[str, str] = {}
    for ln in r.output.splitlines():
        if not ln.startswith(" "):
            head = ln.split(" (")[0]
            blocks[head] = ""
        else:
            blocks[head] += ln + "\n"
    assert list(blocks) == ["passed", "promoted", "close call", "signal before costs", "open", "closed"]
    for head, title in [("passed", "passed one"), ("promoted", "promo one"), ("close call", "close one"),
                        ("signal before costs", "signal one"), ("open", "open one"), ("closed", "closed one")]:
        assert title in blocks[head] and blocks[head].count("#") == 1, (head, blocks[head])
    assert f"#{prom} promo one \u2014 1h, open, promoted, judged 1" in blocks["promoted"]
    assert f"#{sig} signal one \u2014 1h, open, signal before costs, judged 1" in blocks["signal before costs"]


def test_ideas_marks_research_ideas_after_the_title(tmp_path, monkeypatch):
    monkeypatch.chdir(context.ROOT)
    rec = Record(tmp_path / "proposer")
    rec.add_idea("from research", {}, "research", "4h")
    _idea(rec, "from the model", "open")
    r = runner.invoke(app, ["ideas", "--config", _config(tmp_path)])
    assert r.exit_code == 0, r.output
    assert "#1 from research (research) \u2014 4h, open, none, judged 0" in r.output
    assert "#2 from the model \u2014 1h, open, none, judged 0" in r.output


def test_ideas_marks_web_reviewed_ideas_after_the_title(tmp_path, monkeypatch):
    """M8: an idea a web-informed referee reviewed, or a port of one, says so, as the Ideas page's tag does."""
    monkeypatch.chdir(context.ROOT)
    rec = Record(tmp_path / "proposer")
    a = rec.add_idea("refereed", {}, "research", "4h", web_reviewed=True)
    rec.add_idea("its port", {}, "proposer", "1h", port_of=a, web_reviewed=True)
    rec.add_idea("unreviewed", {}, "research", "4h")
    r = runner.invoke(app, ["ideas", "--config", _config(tmp_path)])
    assert r.exit_code == 0, r.output
    assert "#1 refereed (research) (web-reviewed) \u2014 4h, open, none, judged 0" in r.output
    assert "#2 its port (web-reviewed) \u2014 1h, open, none, judged 0" in r.output
    assert "#3 unreviewed (research) \u2014 4h, open, none, judged 0" in r.output


def test_ideas_on_a_missing_record(tmp_path, monkeypatch):
    monkeypatch.chdir(context.ROOT)
    r = runner.invoke(app, ["ideas", "--config", _config(tmp_path)])
    assert r.exit_code == 0 and "no record yet" in r.output
    assert not (tmp_path / "proposer").exists()


def test_idea_group_rule():
    assert piles.GROUPS == ("passed", "promoted", "close call", "signal before costs", "open", "closed")
    g = piles.idea_group
    assert g({"status": "passed"}, []) == "passed"
    assert g({"status": "failed"}, [{"judge_exit": 0, "verdict": PROMOTED}]) == "closed"
    assert g({"status": "open"}, []) == "open"
    assert g({"status": "exhausted"}, []) == "closed"
    assert g({"status": "exhausted"}, [{"judge_exit": 0, "verdict": CLOSE}]) == "close call"
    assert g({"status": "open"}, [{"judge_exit": 0, "verdict": SIGNAL}]) == "signal before costs"
    assert g({"status": "exhausted"}, [{"judge_exit": 0, "verdict": SIGNAL}]) == "signal before costs"
    dup = {"judge_exit": 0, "verdict": PROMOTED, "duplicate_of": 3}
    assert g({"status": "open"}, [dup]) == "open"


def test_last_session_id(tmp_path):
    rec = Record(tmp_path / "s")
    assert rec.last_session_id() is None
    rec.start_session(model="m")
    assert rec.last_session_id() == 1


# -- lab idea: the human's own changes to the record
ENTRY = {"title": "Basis squeeze", "mechanism": "m", "why_it_persists": "w", "signal": "s",
         "trades_per_coin_per_year": 12, "likely_failure": "f", "columns": ["close"], "timeframe": "1h"}


def _seeded(tmp_path):
    rec = Record(tmp_path / "proposer")
    rec.add_idea("Seeded", {}, "seed", "4h")
    return rec


def _file(tmp_path, text=None, **over):
    p = tmp_path / "idea.yaml"
    p.write_text(text if text is not None else yaml.safe_dump({**ENTRY, **over}))
    return str(p)


def _idea_cmd(tmp_path, *args):
    return runner.invoke(app, ["idea", *args, "--config", _config(tmp_path)])


def test_idea_add_adds_a_human_idea_and_warns(tmp_path):
    rec = _seeded(tmp_path)
    r = _idea_cmd(tmp_path, "add", _file(tmp_path))
    assert r.exit_code == 0, r.output
    (x,) = [i for i in rec.ideas() if i["origin"] == "human"]
    assert (x["title"], x["timeframe"], x["status"]) == ("Basis squeeze", "1h", "open")
    assert f"added idea #{x['id']}" in r.output and human.WARNING in r.output


def test_idea_add_prints_every_error_and_exits_2(tmp_path):
    rec = _seeded(tmp_path)
    r = _idea_cmd(tmp_path, "add", _file(tmp_path, title="seeded", timeframe="5m", mechanism=""))
    assert r.exit_code == 2
    for e in ("mechanism: must be a non-empty string", "title: an idea titled 'seeded' is already in the record",
              "timeframe: must be one of ['15m', '1h', '4h'], got '5m'"):
        assert e in r.output, e
    assert len(rec.ideas()) == 1


@pytest.mark.parametrize("text", ["title: [unclosed", "- a list\n"])
def test_idea_add_refuses_a_file_that_is_no_card(tmp_path, text):
    rec = _seeded(tmp_path)
    r = _idea_cmd(tmp_path, "add", _file(tmp_path, text))
    assert r.exit_code == 2 and "refused" in r.output and len(rec.ideas()) == 1


def test_idea_add_waits_for_the_seed(tmp_path):
    r = _idea_cmd(tmp_path, "add", _file(tmp_path))
    assert r.exit_code == 2 and human.NOT_SEEDED in r.output.replace("\n", " ")
    assert not (tmp_path / "proposer").exists()                       # a missing record is not created
    Record(tmp_path / "proposer").add_idea("Mine", {}, "proposer", "1h")
    r = _idea_cmd(tmp_path, "add", _file(tmp_path))
    assert r.exit_code == 2 and human.NOT_SEEDED in r.output.replace("\n", " ")


def test_idea_exclude_closes_an_idea_with_its_reason(tmp_path):
    rec = _seeded(tmp_path)
    passed = rec.add_idea("Passed", {}, "seed", "4h", status="passed")
    r = _idea_cmd(tmp_path, "exclude", "1", "--reason", "it is B's sign flip")
    assert r.exit_code == 0, r.output
    assert "excluded idea #1" in r.output and human.WARNING in r.output
    assert (rec.idea(1)["status"], rec.idea(1)["excluded_reason"]) == ("excluded", "it is B's sign flip")
    for args in ([str(passed), "--reason", "why"], ["1", "--reason", "again"], ["9", "--reason", "why"],
                 [str(passed), "--reason", " "], [str(passed)]):
        r = _idea_cmd(tmp_path, "exclude", *args)
        assert r.exit_code == 2, (args, r.output)
    assert rec.idea(passed)["status"] == "passed"


def test_idea_next_flags_one_open_idea_and_clear_unflags_it(tmp_path):
    rec = _seeded(tmp_path)
    other = rec.add_idea("Other", {}, "seed", "1h")
    dead = rec.add_idea("Dead", {}, "seed", "1h", status="dead")
    r = _idea_cmd(tmp_path, "next", "1")
    assert r.exit_code == 0 and "idea #1 is next up" in r.output and rec.next_up()["id"] == 1
    assert _idea_cmd(tmp_path, "next", str(other)).exit_code == 0 and rec.next_up()["id"] == other
    r = _idea_cmd(tmp_path, "next", str(dead))
    assert r.exit_code == 2 and "only an open or exhausted idea" in r.output and rec.next_up()["id"] == other
    for args in ([], ["1", "--clear"]):                               # an id or --clear, not both or neither
        assert _idea_cmd(tmp_path, "next", *args).exit_code == 2
    r = _idea_cmd(tmp_path, "next", "--clear")
    assert r.exit_code == 0 and "next up cleared" in r.output and rec.next_up() is None


def test_idea_exclude_and_next_on_a_missing_record_exit_2(tmp_path):
    for args in (["exclude", "1", "--reason", "why"], ["next", "1"], ["next", "--clear"]):
        r = _idea_cmd(tmp_path, *args)
        assert r.exit_code == 2 and "refused: no proposer record yet" in r.output, (args, r.output)
    assert not (tmp_path / "proposer").exists()


def test_idea_next_refuses_an_idea_no_proposal_could_work_on(tmp_path):
    rec = _seeded(tmp_path)
    lce = rec.add_idea("LCE", {}, "seed", "5m")                       # ports only, untested
    r = _idea_cmd(tmp_path, "next", str(lce))
    assert r.exit_code == 2 and f"no proposal can work on idea #{lce} (LCE) now" in r.output.replace("\n", " ")
    assert rec.next_up() is None


def test_idea_add_and_exclude_refuse_a_line_break(tmp_path):
    rec = _seeded(tmp_path)
    r = _idea_cmd(tmp_path, "add", _file(tmp_path, title="Basis\nsqueeze"))
    assert r.exit_code == 2 and "title: must be one line" in r.output and len(rec.ideas()) == 1
    r = _idea_cmd(tmp_path, "exclude", "1", "--reason", "sign flip\nof B")
    assert r.exit_code == 2 and "must be one line" in r.output and rec.idea(1)["status"] == "open"


CARDS = REPO / "docs/research/2026-11-01-ideas/cards.yaml"


def test_idea_add_takes_a_list_of_cards_the_research_notes_five(tmp_path):
    rec = _seeded(tmp_path)
    r = _idea_cmd(tmp_path, "add", str(CARDS))
    assert r.exit_code == 0, r.output
    added = [i for i in rec.ideas() if i["origin"] == "human"]
    assert [i["title"] for i in added] == [c["title"] for c in yaml.safe_load(CARDS.read_text())]
    assert len(added) == 5 and all(f"added idea #{i['id']}" in r.output for i in added)


def test_idea_add_with_a_list_adds_none_when_any_card_fails(tmp_path):
    rec = _seeded(tmp_path)
    cards = [ENTRY, {**ENTRY, "title": "Second", "timeframe": "5m"}]
    r = _idea_cmd(tmp_path, "add", _file(tmp_path, yaml.safe_dump(cards)))
    assert r.exit_code == 2
    assert "card #2 (Second): timeframe: must be one of ['15m', '1h', '4h'], got '5m'" in r.output
    assert "added idea" not in r.output and len(rec.ideas()) == 1


# -- the human's stop list (referee-and-human-stops spec)
def _stop_cmd(tmp_path, *args):
    return runner.invoke(app, ["stop", *args, "--config", _config(tmp_path)])


def test_stop_add_warns_and_adds_an_item_to_an_unseeded_record_too(tmp_path):
    rec = Record(tmp_path / "proposer")                              # a stop item doesn't keep the seed out
    r = _stop_cmd(tmp_path, "add", "Round-number stop cascades")
    assert r.exit_code == 0, r.output
    assert human.WARNING in r.output.replace("\n", " ") and "added stop #1" in r.output
    assert [(s["id"], s["text"]) for s in rec.stops()] == [(1, "Round-number stop cascades")]
    assert rec.ideas() == []


@pytest.mark.parametrize("text, why", [(" ", "needs text"), ("Round-number\nstop cascades", "must be one line"),
                                       ("x" * 201, "at most 200 characters")])
def test_stop_add_refuses_an_empty_multi_line_or_long_item(tmp_path, text, why):
    rec = _seeded(tmp_path)
    r = _stop_cmd(tmp_path, "add", text)
    assert r.exit_code == 2 and "refused:" in r.output and why in r.output, r.output
    assert "added stop" not in r.output and rec.stops() == []


def test_stop_remove_takes_an_item_out_of_force_once(tmp_path):
    rec = _seeded(tmp_path)
    a, b = rec.add_stop("Round-number stop cascades"), rec.add_stop("High-volume return premia")
    r = _stop_cmd(tmp_path, "remove", str(a))
    assert r.exit_code == 0 and f"removed stop #{a}" in r.output and [s["id"] for s in rec.stops()] == [b]
    r = _stop_cmd(tmp_path, "remove", str(a))
    assert r.exit_code == 2 and f"refused: stop #{a} was already removed" in r.output
    r = _stop_cmd(tmp_path, "remove", "9")
    assert r.exit_code == 2 and "refused: there is no stop #9" in r.output and [s["id"] for s in rec.stops()] == [b]


def test_stop_list_prints_the_items_in_force_by_id(tmp_path):
    rec = _seeded(tmp_path)
    r = _stop_cmd(tmp_path, "list")
    assert r.exit_code == 0 and r.output == "no stop items\n"
    a, b = rec.add_stop("Round-number stop cascades"), rec.add_stop("High-volume return premia")
    c = rec.add_stop("Exit-only changes")
    rec.remove_stop(b)
    r = _stop_cmd(tmp_path, "list")
    assert r.exit_code == 0 and r.output == f"#{a} Round-number stop cascades\n#{c} Exit-only changes\n"


def test_stop_commands_on_a_missing_record_exit_2_and_create_nothing(tmp_path):
    for args in (["add", "Round-number stop cascades"], ["remove", "1"], ["list"]):
        r = _stop_cmd(tmp_path, *args)
        assert r.exit_code == 2 and "refused: no proposer record yet" in r.output, (args, r.output)
    assert not (tmp_path / "proposer").exists()

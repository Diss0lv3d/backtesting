from __future__ import annotations

import yaml
from typer.testing import CliRunner

from perp_lab.cli import app
from perp_lab.proposer import context, piles
from perp_lab.proposer.record import Record
from tests.conftest import REPO

runner = CliRunner()


def _config(tmp_path, **over):
    raw = yaml.safe_load(open(REPO / "config/lab.yaml"))
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


def test_ideas_prints_groups_in_the_spec_order(tmp_path, monkeypatch):
    monkeypatch.chdir(context.ROOT)
    rec = Record(tmp_path / "proposer")
    closed = _idea(rec, "closed one", "dead")
    opened = _idea(rec, "open one", "open")
    close = _idea(rec, "close one", "open")
    prom = _idea(rec, "promo one", "open")
    passed = _idea(rec, "passed one", "passed")
    _judged(rec, close, 1, CLOSE)
    _judged(rec, prom, 2, PROMOTED)
    r = runner.invoke(app, ["ideas", "--config", _config(tmp_path)])
    assert r.exit_code == 0, r.output
    heads = [ln.split(" (")[0] for ln in r.output.splitlines() if not ln.startswith(" ")]
    assert heads == ["passed", "promoted", "close call", "open", "closed"]
    assert f"#{prom} promo one — 1h, open, promoted, judged 1" in r.output
    assert opened and closed and passed


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
    dup = {"judge_exit": 0, "verdict": PROMOTED, "duplicate_of": 3}
    assert g({"status": "open"}, [dup]) == "open"


def test_last_session_id(tmp_path):
    rec = Record(tmp_path / "s")
    assert rec.last_session_id() is None
    rec.start_session(model="m")
    assert rec.last_session_id() == 1

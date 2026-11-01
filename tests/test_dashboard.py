"""Every dashboard page renders against a populated store without exceptions."""

import pytest
import streamlit as st
import yaml
from streamlit.testing.v1 import AppTest

from perp_lab.judge import budget
from perp_lab.store import db
from tests.test_store import cfg, fake_run  # noqa: F401


@pytest.fixture
def populated(cfg, monkeypatch):
    st.cache_data.clear()  # the app caches by function, not by store: drop other tests' data
    fake_run(cfg, "p1", strategy="alpha", sharpe=0.4)
    fake_run(cfg, "c1", strategy="alpha_child", sharpe=0.6, parent="p1", verdict="candidate")
    fake_run(cfg, "b1", strategy="beta", sharpe=-0.5)
    db.add_edge(cfg, "p1", "c1", "kill", "fails random-entry")
    # no proposer record by default: the real state dir must never leak into a test
    raw = yaml.safe_load(cfg.path.read_text())
    raw["proposer"]["state_dir"] = str(cfg.path.parent / "no_proposer_state")
    cfg.path.write_text(yaml.safe_dump(raw))
    monkeypatch.setenv("PERP_LAB_CONFIG", str(cfg.path))
    return cfg


@pytest.mark.parametrize("page", ["Strategies", "Strategy detail", "Pairs", "Lineage", "Ideas"])
def test_page_renders(populated, page):
    at = AppTest.from_file("../dashboard/app.py", default_timeout=60)
    at.run()
    at.sidebar.radio(key="page").set_value(page).run()
    assert not at.exception, [e.message for e in at.exception]


def _detail(pick):
    at = AppTest.from_file("../dashboard/app.py", default_timeout=60)
    at.run()
    at.sidebar.radio(key="page").set_value("Strategy detail").run()
    at.selectbox(key="detail_pick").set_value(pick).run()
    return at


def _judge_c1(cfg, status, reason, holdout_run_id=None):
    jid = db.record_judgement(cfg, {
        "strategy": "alpha_child", "timeframe": "4h", "strategy_hash": "abc", "child_run_id": "c1",
        "parent_run_id": "p1", "lineage_id": "p1", "decision": "promote", "reasons": [],
        "dsr": 0.97, "n_trials": 3, "sr0": 0.01, "parent_passed": False, "beats_parent_frac": None,
        "holdout_status": status, "holdout_reason": reason})
    if holdout_run_id:
        db.update_holdout(cfg, jid, status, reason, holdout_run_id)


def test_approval_needs_a_candidate_with_a_passed_exam(populated):
    labels = lambda at: [b.label for b in at.get("form_submit_button")]  # noqa: E731
    assert "Approve for paper trading" not in labels(_detail("alpha @ 4h"))
    at = _detail("alpha_child @ 4h")
    assert "Approve for paper trading" not in labels(at)
    assert any("passed holdout exam" in c.value for c in at.caption)
    _judge_c1(populated, "passed", "passed")
    assert "Approve for paper trading" in labels(_detail("alpha_child @ 4h"))


def test_judgement_panel_shows_holdout_details_to_the_human(populated):
    fake_run(populated, "h1", strategy="alpha_child", mode="holdout", verdict="passed", sharpe=1.1)
    _judge_c1(populated, "passed", "passed", holdout_run_id="h1")
    at = _detail("alpha_child @ 4h")
    assert not at.exception, [e.message for e in at.exception]
    assert {"Decision", "Deflated Sharpe", "Holdout"} <= {m.label for m in at.metric}
    assert any("Holdout 2024-01-01" in c.value for c in at.caption)


def test_judgement_panel_survives_a_missing_holdout_run(populated):
    _judge_c1(populated, "passed", "passed", holdout_run_id="ghost")
    at = _detail("alpha_child @ 4h")
    assert not at.exception, [e.message for e in at.exception]
    assert any("not found" in c.value for c in at.caption)


def test_judgement_panel_survives_a_holdout_run_without_trades(populated):
    fake_run(populated, "h2", strategy="alpha_child", mode="holdout", verdict="failed")
    with db.engine(populated).begin() as c:
        c.execute(db.runs.update().where(db.runs.c.run_id == "h2").values(trades=None))
    _judge_c1(populated, "failed", "failed", holdout_run_id="h2")
    at = _detail("alpha_child @ 4h")
    assert not at.exception, [e.message for e in at.exception]
    assert any("0 trades" in c.value for c in at.caption)


def test_sidebar_shows_the_holdout_budget(populated):
    at = AppTest.from_file("../dashboard/app.py", default_timeout=60)
    at.run()
    assert any("not initialised" in c.value for c in at.sidebar.caption)
    budget.init_log(populated)
    at = AppTest.from_file("../dashboard/app.py", default_timeout=60)
    at.run()
    assert any("holdout reads: 0/10" in c.value for c in at.sidebar.caption)
    assert any("earliest exam date: 2026-12-28" in c.value for c in at.sidebar.caption)


def _launch_args(monkeypatch, *cli_args):
    import subprocess

    from typer.testing import CliRunner

    from perp_lab.cli import app
    seen = []
    monkeypatch.setattr(subprocess, "call", lambda args: seen.append(args) or 0)
    r = CliRunner().invoke(app, ["dashboard", *cli_args])
    assert r.exit_code == 0, r.output
    return seen[0]


def test_dashboard_listens_on_localhost_by_default(monkeypatch):
    args = _launch_args(monkeypatch)
    i = args.index("--server.address")
    assert args[i + 1] == "localhost"


def test_dashboard_turns_streamlit_usage_telemetry_off(monkeypatch):
    for args in (_launch_args(monkeypatch), _launch_args(monkeypatch, "--config", "x.yaml")):
        i = args.index("--browser.gatherUsageStats")
        assert args[i + 1] == "false" and i < (args.index("--") if "--" in args else len(args))


def test_dashboard_address_can_be_opened_up_explicitly(monkeypatch):
    args = _launch_args(monkeypatch, "--address", "0.0.0.0")
    assert args[args.index("--server.address") + 1] == "0.0.0.0"


def test_sidebar_states_the_universe_rule(populated):
    at = AppTest.from_file("../dashboard/app.py", default_timeout=60)
    at.run()
    assert any(populated["universe_rule"] in c.value for c in at.sidebar.caption)


def test_sidebar_without_a_universe_rule_says_it_is_not_stated(populated, tmp_path, monkeypatch):
    raw = yaml.safe_load(open(populated.path))
    del raw["universe_rule"]
    p = tmp_path / "no_rule.yaml"
    p.write_text(yaml.safe_dump(raw))
    monkeypatch.setenv("PERP_LAB_CONFIG", str(p))
    at = AppTest.from_file("../dashboard/app.py", default_timeout=60)
    at.run()
    assert not at.exception, [e.message for e in at.exception]
    assert any(c.value == "Universe rule not set in this config: survivor bias not stated."
               for c in at.sidebar.caption)


# ---------------------------------------------------------------- the Ideas page

def _ideas_at():
    at = AppTest.from_file("../dashboard/app.py", default_timeout=60)
    at.run()
    at.sidebar.radio(key="page").set_value("Ideas").run()
    return at


def test_ideas_page_without_a_record_says_so_and_creates_nothing(populated):
    at = _ideas_at()
    assert not at.exception, [e.message for e in at.exception]
    assert any("No proposer record" in i.value for i in at.info)
    assert not (populated.path.parent / "no_proposer_state").exists()


def _record(cfg):
    from perp_lab.proposer.record import Record
    state = cfg.path.parent / "no_proposer_state"
    r = Record(state)
    diag = {"sides": {"long_share": 0.6, "long_net_bp": 4.0, "short_net_bp": -1.5},
            "breadth": {"top_coin_share": 0.35}}      # no top_fold_share: reads n/a
    kw = dict(origin="proposer", kind="new", judge_exit=0, state="judged")
    ids = {t: r.add_idea(t, {"mechanism": f"card of {t}"}, "proposer", "4h", status=s)
           for t, s in [("closed one", "dead"), ("open one", "open"), ("close one", "open"),
                        ("promoted one", "open"), ("passed one", "passed")]}
    promo = {"decision": "promote", "reasons": [], "holdout_status": "pending", "diagnostics": diag}
    close = {"decision": "kill", "reasons": ["x"], "holdout_status": "n/a",
             "diagnostics": {"gates": {"net_sharpe": 1.0, "random_p95": 0.5, "oos_trades": 90, "min_oos_trades": 30}}}
    r.add_proposal(idea_id=ids["promoted one"], plugin="plug", timeframe="4h", hash="hh", source="SRC_PROMO",
                   verdict=promo, **kw)
    r.add_proposal(idea_id=ids["close one"], plugin="plug2", timeframe="4h", hash="h2", source="x", verdict=close, **kw)
    r.add_proposal(idea_id=ids["open one"], plugin="plug3", timeframe="4h", hash="h3", source="x", origin="proposer",
                   kind="new", state="broken", broken_reason="boom")
    sid = r.start_session(model="m")
    r.end_session(sid, "trials_total")
    r.add_call(tokens_in=1234, tokens_out=56, session_id=sid)
    fake_run(cfg, "w1", strategy="plug", strategy_hash="hh")
    db.record_judgement(cfg, {"strategy": "plug", "timeframe": "4h", "strategy_hash": "hh", "child_run_id": "w1",
                              "lineage_id": "w1", "decision": "promote", "reasons": [], "holdout_status": "pending",
                              "holdout_reason": "pending"})
    return r


def test_ideas_page_header_groups_and_review_packet(populated):
    _record(populated)
    jid = db.get_judgement(populated, "hh", "4h")["id"]
    n, _ = db.trial_stats(populated)
    at = _ideas_at()
    assert not at.exception, [e.message for e in at.exception]
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Proposer trials"] == "2 / 100"
    assert metrics["N store-wide"] == str(n)
    assert metrics["Session"] == "stopped: trials_total"
    assert "1,234" in metrics["Tokens today"]
    heads = [h.value for h in at.subheader]
    assert [h.split(" (")[0] for h in heads] == ["Passed", "Promoted", "Close call", "Other open ideas"]
    assert any(e.label.startswith("Closed (1)") for e in at.expander)
    assert any(e.label.startswith("Broken attempts (1)") for e in at.expander)
    shown = [c.value for c in at.code]
    assert f"lab exam --id {jid}" in shown and "SRC_PROMO" in shown
    text = " ".join(m.value for m in at.markdown)
    assert "top fold share of net: n/a" in text and "long share: 60.0%" in text
    assert "Walk-forward run detail" in [b.label for b in at.button]


def test_ideas_detail_button_opens_the_strategy_detail_page(populated):
    _record(populated)
    at = _ideas_at()
    next(b for b in at.button if b.label == "Walk-forward run detail").click().run()
    assert not at.exception, [e.message for e in at.exception]
    assert at.sidebar.radio(key="page").value == "Strategy detail"
    assert at.selectbox(key="detail_pick").value == "plug @ 4h"


def test_ideas_page_says_so_when_a_promoted_plugin_has_no_judgement(populated):
    r = _record(populated)
    with db.engine(populated).begin() as c:
        c.execute(db.judgements.delete())
    at = _ideas_at()
    assert not at.exception, [e.message for e in at.exception]
    assert any("No judgement found" in w.value for w in at.warning)
    assert not any(c.value.startswith("lab exam") for c in at.code)


def test_session_state_probes_the_lock_and_reads_the_last_pause(tmp_path):
    import fcntl
    from datetime import datetime, timedelta, timezone

    from dashboard import ideas
    assert not ideas.session_running(tmp_path)               # no lock file: stopped, and none is created
    assert not (tmp_path / "session.lock").exists()
    with open(tmp_path / "session.lock", "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert ideas.session_running(tmp_path)
    assert not ideas.session_running(tmp_path)
    now = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
    (tmp_path / "sessions").mkdir()
    pause = lambda t: f"- 11:00 · paused: usage limit; until {t:%Y-%m-%d %H:%M} UTC\n"  # noqa: E731
    (tmp_path / "sessions" / "1.md").write_text("- 10:00 · 0001 · a@4h\n" + pause(now + timedelta(hours=2)))
    assert ideas.sleeping_until(tmp_path, 1, now) == now + timedelta(hours=2)
    (tmp_path / "sessions" / "1.md").write_text(pause(now + timedelta(hours=2)) + "- 13:00 · 0002 · b@4h\n")
    assert ideas.sleeping_until(tmp_path, 1, now) is None    # work resumed after the pause
    (tmp_path / "sessions" / "1.md").write_text(pause(now - timedelta(hours=1)))
    assert ideas.sleeping_until(tmp_path, 1, now) is None


def test_sleeping_until_ignores_a_summary_line_and_an_impossible_date(tmp_path):
    from datetime import datetime, timezone

    from dashboard import ideas
    now = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
    (tmp_path / "sessions").mkdir()
    f = tmp_path / "sessions" / "1.md"
    f.write_text("- 11:00 · research: 1 idea added\n  paused: x; until 2030-01-01 00:00 UTC\n")
    assert ideas.sleeping_until(tmp_path, 1, now) is None    # model-written summary, indented
    f.write_text("- 11:00 · paused: usage limit; until 2026-02-30 10:00 UTC\n")
    assert ideas.sleeping_until(tmp_path, 1, now) is None


# ---------------------------------------------------------------- the human's ideas on the Ideas page

def _seeded(cfg):
    r = _record(cfg)
    r.add_idea("Seeded one", {"mechanism": "card of seeded one"}, "seed", "4h")    # what seeded() looks for
    return r


def _id(r, title):
    return next(i["id"] for i in r.ideas() if i["title"] == title)


def _fill(at, **over):
    fields = {"title": "Basis squeeze", "mechanism": "m", "why_it_persists": "w", "signal": "s",
              "likely_failure": "f", **over}
    at.text_input(key="new_title").input(fields.pop("title"))
    for name, text in fields.items():
        at.text_area(key=f"new_{name}").input(text)


def _submit(at):
    (button,) = [b for b in at.get("form_submit_button") if b.label == "Add idea"]
    return button.click().run()


def test_the_add_form_adds_a_human_idea_to_a_seeded_record(populated):
    from perp_lab.proposer import human
    r = _seeded(populated)
    at = _ideas_at()
    assert not at.exception, [e.message for e in at.exception]
    assert any(e.label == "Add an idea" for e in at.expander)
    assert human.WARNING in [w.value for w in at.warning]
    _fill(at)
    at.number_input(key="new_trades_per_coin_per_year").set_value(12.0)
    at.multiselect(key="new_columns").select("close").select("funding_last")
    at.selectbox(key="new_timeframe").set_value("1h")
    _submit(at)
    assert not at.exception, [e.message for e in at.exception]
    x = r.idea(_id(r, "Basis squeeze"))
    assert (x["origin"], x["status"], x["timeframe"]) == ("human", "open", "1h")
    assert x["card"]["columns"] == ["close", "funding_last"] and x["card"]["trades_per_coin_per_year"] == 12.0
    assert f"Added idea #{x['id']}" in [s.value for s in at.success]
    assert "#### Basis squeeze" in [m.value for m in at.markdown]
    assert at.text_input(key="new_title").value == ""                # a fresh form for the next one


def test_the_add_form_shows_every_error_and_adds_nothing(populated):
    r = _seeded(populated)
    n = len(r.ideas())
    at = _ideas_at()
    _fill(at, title="open one", mechanism="")
    _submit(at)
    errors = [e.value for e in at.error]
    assert "mechanism: must be a non-empty string" in errors
    assert "trades_per_coin_per_year: must be a finite number" in errors          # left empty
    assert "title: an idea titled 'open one' is already in the record" in errors
    assert len(r.ideas()) == n and at.text_input(key="new_title").value == "open one"   # kept, to fix


def test_the_add_form_waits_for_the_seed(populated):
    from perp_lab.proposer import human
    _record(populated)                          # the proposer's ideas only: no seed
    at = _ideas_at()
    assert not at.exception, [e.message for e in at.exception]
    assert human.NOT_SEEDED in [i.value for i in at.info]
    assert not [b for b in at.get("form_submit_button") if b.label == "Add idea"]


def test_without_a_record_the_add_form_says_to_seed_first(populated):
    from perp_lab.proposer import human
    at = _ideas_at()
    assert human.NOT_SEEDED in [i.value for i in at.info]
    assert not (populated.path.parent / "no_proposer_state").exists()


def test_exclude_closes_an_idea_with_the_humans_reason(populated):
    from perp_lab.proposer import human
    r = _seeded(populated)
    i, passed = _id(r, "open one"), _id(r, "passed one")
    at = _ideas_at()
    keys = {c.key for c in at.checkbox}
    assert f"exclude_open_{i}" in keys and f"exclude_open_{_id(r, 'closed one')}" in keys
    assert f"exclude_open_{passed}" not in keys                     # never a passed idea
    at.checkbox(key=f"exclude_open_{i}").check().run()
    assert human.WARNING in [w.value for w in at.warning]
    at.button(key=f"exclude_{i}").click().run()                      # no reason yet
    assert "an exclusion needs a reason" in [e.value for e in at.error] and r.idea(i)["status"] == "open"
    at.text_input(key=f"exclude_reason_{i}").input("its mechanism is the seeded one's")
    at.button(key=f"exclude_{i}").click().run()
    assert not at.exception, [e.message for e in at.exception]
    assert (r.idea(i)["status"], r.idea(i)["excluded_reason"]) == ("excluded", "its mechanism is the seeded one's")
    assert f"Excluded idea #{i}" in [t.value for t in at.toast]


def test_try_next_flags_one_open_idea_and_clear_unflags_it(populated):
    r = _seeded(populated)
    a, b, dead = _id(r, "open one"), _id(r, "close one"), _id(r, "closed one")
    at = _ideas_at()
    assert f"try_next_{dead}" not in {x.key for x in at.button}      # open ideas only
    at.button(key=f"try_next_{a}").click().run()
    assert r.next_up()["id"] == a and f"clear_next_{a}" in {x.key for x in at.button}
    assert any("next up" in m.value for m in at.markdown)
    at.button(key=f"try_next_{b}").click().run()
    assert r.next_up()["id"] == b and f"try_next_{a}" in {x.key for x in at.button}
    at.button(key=f"clear_next_{b}").click().run()
    assert not at.exception, [e.message for e in at.exception]
    assert r.next_up() is None


def test_try_next_is_offered_only_for_an_idea_a_proposal_could_work_on(populated):
    r = _seeded(populated)
    untested = r.add_idea("LCE untested", {}, "seed", "5m")          # ports only, and no pile a port needs
    portable = r.add_idea("LCE signal", {}, "seed", "5m")
    r.add_proposal(idea_id=portable, plugin="lce_atr", timeframe="5m", kind="new", state="judged", judge_exit=0,
                   verdict={"decision": "kill", "reasons": [], "holdout_status": "n/a", "diagnostics": {
                       "gates": {"net_sharpe": -1, "random_p95": 0, "oos_trades": 100, "min_oos_trades": 64},
                       "before_costs": {"sharpe": 0.5, "random_p95": 0.1}}})
    at = _ideas_at()
    assert not at.exception, [e.message for e in at.exception]
    keys = {b.key for b in at.button}
    assert f"try_next_{untested}" not in keys and f"try_next_{portable}" in keys
    assert any(c.value.startswith(f"Try next: no proposal can work on idea #{untested} (LCE untested) now")
               for c in at.caption)


def _research_row(r, ids, **over):
    sid = r.start_session(model="m")
    reply = {"ideas": [], "stop": ["fading wicks on 5m", "plain momentum"], "summary": "Funding ideas look thin."}
    return r.add_research(session_id=sid, trials_used=5, reply=reply, added=ids, tokens_in=10, tokens_out=5,
                          outcome="ok", **over)


def test_ideas_page_shows_research_ideas_tagged_and_the_latest_research_notes(populated):
    r = _record(populated)
    i = r.add_idea("basis squeeze", {"mechanism": "m"}, "research", "4h")
    _research_row(r, [i])
    at = _ideas_at()
    assert not at.exception, [e.message for e in at.exception]
    assert any(c.value == f"idea {i} · research · 4h · open" for c in at.caption)
    (notes,) = [e for e in at.expander if e.label == "Research notes"]
    text = " ".join(m.value for m in notes.markdown)
    assert "ok" in text and "5 trials" in text and "UTC" in text
    assert "Funding ideas look thin." in text
    assert "fading wicks on 5m" in text and "plain momentum" in text
    assert f"#{i} basis squeeze" in text


def test_with_research_off_the_research_notes_say_no_stop_list_is_in_force(populated):
    raw = yaml.safe_load(populated.path.read_text())
    raw["proposer"]["research_every"] = 0
    populated.path.write_text(yaml.safe_dump(raw))
    _research_row(_record(populated), [])
    at = _ideas_at()
    assert not at.exception, [e.message for e in at.exception]
    (notes,) = [e for e in at.expander if e.label == "Research notes"]
    text = " ".join(m.value for m in notes.markdown)
    assert "research is off: no stop list is in force" in text.lower()
    assert "fading wicks on 5m" not in text and "plain momentum" not in text
    assert "Funding ideas look thin." in text                  # the round's summary is still the human's to read


def test_ideas_page_has_no_research_notes_without_a_research_row(populated):
    _record(populated)
    at = _ideas_at()
    assert not at.exception, [e.message for e in at.exception]
    assert not [e for e in at.expander if e.label == "Research notes"]


@pytest.mark.parametrize("reply,added", [({"stop": 5}, []), ({"stop": "abc"}, []), ("x", []), (None, "x"),
                                         ({"stop": ["a"], "summary": 3}, [None, "y", True])])
def test_ideas_page_survives_a_malformed_research_row(populated, reply, added):
    r = _record(populated)
    r.add_research(trials_used=5, reply=reply, added=added, tokens_in=1, tokens_out=1, outcome="invalid")
    r.add_research(trials_used=6, reply=reply, added=added, tokens_in=1, tokens_out=1, outcome="ok")
    at = _ideas_at()
    assert not at.exception, [e.message for e in at.exception]
    assert [e for e in at.expander if e.label == "Research notes"]


def test_research_notes_show_the_stop_list_in_force_not_a_refused_rounds(populated):
    r = _record(populated)
    r.add_research(trials_used=5, reply={"stop": ["in force line"], "summary": "good"}, added=[], tokens_in=1,
                   tokens_out=1, outcome="ok")
    r.add_research(trials_used=10, reply={"stop": ["refused line"], "summary": "bad"}, added=[], tokens_in=1,
                   tokens_out=1, outcome="invalid")
    at = _ideas_at()
    (notes,) = [e for e in at.expander if e.label == "Research notes"]
    text = " ".join(m.value for m in notes.markdown)
    assert "invalid" in text and "in force line" in text and "in force" in text.lower()
    assert "refused line" not in text and "bad" not in text

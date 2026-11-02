"""Every dashboard page renders against a populated store without exceptions."""

import html
import re

import pytest
import streamlit as st
import yaml
from markdown_it import MarkdownIt
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


# AppTest gives an element's markdown source; CommonMark, which Streamlit's markdown builds on, says what it shows.
_MD = MarkdownIt("commonmark")


def _tokens(src: str) -> list:
    return [t for b in _MD.parse(src) for t in [b, *(b.children or [])]]


def _shown(src: str) -> str:
    """The text a markdown source shows: rendered, its tags dropped."""
    return html.unescape(re.sub(r"<[^>]+>", "", _MD.render(src))).strip()


def _hrefs(src: str) -> list[str]:
    return [t.attrGet("href") for t in _tokens(src) if t.type == "link_open"]


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
    assert any(_shown(c.value).startswith(f"Try next: no proposal can work on idea #{untested} (LCE untested) now")
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
    text = " ".join(_shown(m.value) for m in notes.markdown)
    assert "ok" in text and "5 trials" in text and "UTC" in text
    assert "Funding ideas look thin." in text
    assert "fading wicks on 5m" in text and "plain momentum" in text
    assert f"#{i} basis squeeze" in text


def test_research_notes_show_what_each_added_idea_builds_on(populated):
    r = _record(populated)
    i = r.add_idea("Basis squeeze", {"mechanism": "m"}, "research", "4h")
    j = r.add_idea("Carry unwind", {"mechanism": "m"}, "research", "4h")
    reply = {"ideas": [None, {"title": 7, "builds_on": 8}, {"title": " Basis squeeze ", "builds_on": "the dead fades"},
                       {"title": "Carry unwind", "builds_on": ["not", "a line"]}],
             "stop": [], "summary": "s"}
    r.add_research(session_id=r.start_session(model="m"), trials_used=5, reply=reply, added=[i, j], tokens_in=1,
                   tokens_out=1, outcome="ok")
    at = _ideas_at()
    assert not at.exception, [e.message for e in at.exception]
    (notes,) = [e for e in at.expander if e.label == "Research notes"]
    lines = [m.value for m in notes.markdown]
    assert f"- #{i} Basis squeeze · builds on: the dead fades" in lines
    assert f"- #{j} Carry unwind" in lines                    # a builds_on that is no line shows nothing
    assert not [c for c in notes.caption if "unreadable" in c.value]


def _notes_head(at) -> str:
    (notes,) = [e for e in at.expander if e.label == "Research notes"]
    return notes.markdown[0].value


def test_a_round_the_running_session_has_under_way_shows_as_under_way(populated):
    """The loop writes a round's row first, as interrupted, and its outcome at its end: while the session that
    owns it runs, the round is under way (a call, or a usage-limit wait, can take hours)."""
    import fcntl
    r = _record(populated)
    older = r.start_session(model="m")                   # never ended: it died mid-round
    r.add_research(session_id=older, trials_used=4, added=[], outcome="error: interrupted")
    now = r.start_session(model="m")                     # the session running now, with no round yet
    lock = populated.path.parent / "no_proposer_state" / "session.lock"
    with open(lock, "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert _notes_head(_ideas_at()).endswith("· error: interrupted · at 4 trials")   # an earlier session's
        r.add_research(session_id=now, trials_used=5, added=[], outcome="error: interrupted")
        assert _notes_head(_ideas_at()).endswith("· under way · at 5 trials")
    assert _notes_head(_ideas_at()).endswith("· error: interrupted · at 5 trials")      # stopped: cut short


def test_with_research_off_the_research_notes_say_the_research_stop_list_is_not_in_force(populated):
    raw = yaml.safe_load(populated.path.read_text())
    raw["proposer"]["research_every"] = 0
    populated.path.write_text(yaml.safe_dump(raw))
    _research_row(_record(populated), [])
    at = _ideas_at()
    assert not at.exception, [e.message for e in at.exception]
    (notes,) = [e for e in at.expander if e.label == "Research notes"]
    text = " ".join(_shown(m.value) for m in notes.markdown)
    assert "research is off: the research step's stop list is not in force" in text.lower()
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


# ---------------------------------------------------------------- the referee's verdicts

ARXIV = "https://arxiv.org/abs/1707.06221"
ARXIV_2 = "https://export.arxiv.org/abs/2501.01234"
GECKO = "https://api.coingecko.com/api/v3/coins/bitcoin"
GECKO_SHOWN = "https[:]//api.coingecko.com/api/v3/coins/bitcoin"       # off the allowlist: shown, never linked


def _notes(at):
    (notes,) = [e for e in at.expander if e.label == "Research notes"]
    return [m.value for m in notes.markdown], [c.value for c in notes.caption]


def _refereed(r, referee, titles=("Basis squeeze", "Carry unwind", "Round-number stops"), added=None):
    reply = {"ideas": [{"title": t, "builds_on": "b"} for t in titles], "stop": [], "summary": "s"}
    return r.add_research(session_id=r.start_session(model="m"), trials_used=5, reply=reply,
                          added=[] if added is None else added, tokens_in=1, tokens_out=1, outcome="ok",
                          referee=referee)


def test_research_notes_list_each_cards_verdict_with_its_sources_and_the_idea_it_became(populated):
    r = _record(populated)
    i = r.add_idea("Basis squeeze", {"mechanism": "m"}, "research", "4h")
    j = r.add_idea("Carry unwind, entry only", {"mechanism": "m"}, "research", "4h")
    for k in (i, j):
        r.set_idea(k, web_reviewed=True)
    _refereed(r, {"web": True, "fetches": [ARXIV, GECKO, ARXIV_2], "denied": [GECKO], "verdicts": [
        {"title": "Basis squeeze", "decision": "accept", "reason": "Consistent with the basis literature.",
         "sources": [ARXIV]},
        {"title": "Carry unwind", "decision": "revise", "reason": "Its edge came from the exit.", "sources": []},
        {"title": "Round-number stops", "decision": "reject", "reason": "Published evidence finds no effect.",
         "sources": [ARXIV_2]}]}, added=[i, j])
    at = _ideas_at()
    assert not at.exception, [e.message for e in at.exception]
    lines, captions = _notes(at)
    shown = {_shown(x): x for x in lines}
    assert "Referee · 3 cards: 1 accepted, 1 revised, 1 rejected · with academic web access" in shown
    accepted = f"Basis squeeze: accepted as #{i} · Consistent with the basis literature. · sources: {ARXIV}"
    revised = f"Carry unwind: revised into #{j} Carry unwind, entry only · Its edge came from the exit."
    rejected = f"Round-number stops: rejected · Published evidence finds no effect. · sources: {ARXIV_2}"
    assert {accepted, revised, rejected} <= set(shown)
    assert (_hrefs(shown[accepted]), _hrefs(shown[revised]), _hrefs(shown[rejected])) == ([ARXIV], [], [ARXIV_2])
    # the fetches and denials, compact: a denied host is shown as text, never linked
    (fetched,) = [c for c in captions if c.startswith("fetches")]
    assert _shown(fetched) == \
        f"fetches (3): {ARXIV}, {GECKO_SHOWN}, {ARXIV_2} · denied by the CLI (1): {GECKO_SHOWN}"
    assert _hrefs(fetched) == [ARXIV, ARXIV_2]
    assert not any("unreadable" in c for c in captions)
    metas = [c.value for c in at.caption]
    assert f"idea {i} · research · 4h · open · web-reviewed" in metas
    assert f"idea {j} · research · 4h · open · web-reviewed" in metas


def test_research_notes_say_when_the_referee_failed_and_no_card_was_added(populated):
    r = _record(populated)
    _refereed(r, {"web": True, "fetches": [], "denied": [], "error": "fence: off-list fetch: api.coingecko.com"})
    lines, _ = _notes(_ideas_at())
    assert ("Referee failed · with academic web access: fence: off-list fetch: api.coingecko.com. "
            "No card from this round was added.") in [_shown(x) for x in lines]
    assert "**Ideas added**" not in lines


def test_a_tool_less_referee_says_so_and_links_no_source_off_the_allowlist(populated):
    r = _record(populated)
    i = r.add_idea("Basis squeeze", {"mechanism": "m"}, "research", "4h")
    _refereed(r, {"web": False, "fetches": [], "denied": [], "verdicts": [
        {"title": "Basis squeeze", "decision": "accept", "reason": "r", "sources": [GECKO, "javascript:alert(1)"]}]},
        titles=("Basis squeeze",), added=[i])
    at = _ideas_at()
    lines, captions = _notes(at)
    shown = {_shown(x): x for x in lines}
    assert "Referee · 1 card: 1 accepted, 0 revised, 0 rejected · without web access" in shown
    line = shown[f"Basis squeeze: accepted as #{i} · r · sources: {GECKO_SHOWN}, javascript:alert(1)"]
    assert _hrefs(line) == []
    assert not [c for c in captions if c.startswith("fetches")]
    assert f"idea {i} · research · 4h · open" in [c.value for c in at.caption]      # no web, no tag


@pytest.mark.parametrize("referee", [
    "x", 5, [], {}, {"verdicts": "x", "fetches": "abc", "denied": 3},
    {"web": "yes", "verdicts": [None, 7, {"decision": 3}, {"title": ["t"], "decision": "accept", "reason": None,
                                                          "sources": "https://arxiv.org/abs/1"}]},
    {"error": 7, "fetches": [None, 3]}, {"verdicts": [{"title": "a", "decision": "accept"}], "error": None},
])
def test_research_notes_survive_a_malformed_referee_value(populated, referee):
    r = _record(populated)
    i = r.add_idea("Basis squeeze", {"mechanism": "m"}, "research", "4h")
    _refereed(r, referee, added=[i])
    at = _ideas_at()
    assert not at.exception, [e.message for e in at.exception]
    lines, captions = _notes(at)
    assert not any("unreadable" in c for c in captions)            # guarded, not caught
    assert f"- #{i} Basis squeeze · builds on: b" in lines           # the rest of the notes is there


def test_verdicts_that_do_not_pair_with_the_ideas_added_name_no_idea(populated):
    r = _record(populated)
    i = r.add_idea("Basis squeeze", {"mechanism": "m"}, "research", "4h")
    _refereed(r, {"web": True, "fetches": [], "denied": [], "verdicts": [
        {"title": "Basis squeeze", "decision": "accept", "reason": "r", "sources": []},
        {"title": "Carry unwind", "decision": "accept", "reason": "q", "sources": []}]}, added=[i])
    shown = [_shown(x) for x in _notes(_ideas_at())[0]]
    assert "Basis squeeze: accepted · r" in shown and "Carry unwind: accepted · q" in shown


# What a page the referee read could have put in its text, and so in anything a model wrote: an image the viewer's
# browser would fetch, a link, HTML, bare URLs, www. hosts and an email GFM would link (Streamlit's frontend links
# them in the text as shown, after the escapes are read: a real browser showed it), and Streamlit's LaTeX and colour
# directives. As shown: every URL, www. host and email broken, the rest as written.
EVIL = ("![](https://evil.example/x) [a](https://evil.example) <img src=x> https://evil.example www.evil.example "
        "WWW.Evil.example x@evil.example mailto:y@evil.example $x$ :red[r] *b* `c`")
EVIL_SHOWN = ("![](https[:]//evil.example/x) [a](https[:]//evil.example) <img src=x> https[:]//evil.example "
              "www[.]evil.example WWW[.]Evil.example x[@]evil.example mailto:y[@]evil.example $x$ :red[r] *b* `c`")
OFF_LIST = ("https://evil.example/s", "https[:]//evil.example/s")     # a source off the allowlist, and as shown
FETCHED = ("https://evil.example/f", "https[:]//evil.example/f")      # a fetch off it, denied
# GFM's autolink literals: a URL, a www. host, an email address (mdast-util-gfm-autolink-literal's patterns)
_AUTOLINK = re.compile(r"https?://|www(?=\.)|[-.\w+]+@[-\w]+(?:\.[-\w]+)+", re.I)


def _unlinked(src: str) -> str:
    """The text a source shows outside its links and code: what GFM would scan for a link to make."""
    out, depth = [], 0
    for t in _tokens(src):
        depth += {"link_open": 1, "link_close": -1}.get(t.type, 0)
        if depth == 0 and t.type == "text":
            out.append(t.content)
        elif t.type in ("softbreak", "hardbreak"):
            out.append("\n")
    return "".join(out)


def _no_markup(texts: list[str]) -> None:
    """No image, HTML or link off the allowlist anywhere in these sources, and no URL, www. host or email in their
    shown text outside a link, so no autolink can form. The code's own emphasis and code spans may be there, so the
    shown text is what says the model's `*b*` and `c` stayed literal."""
    marks = [t for x in texts for t in _tokens(x)]
    assert not [t for t in marks if t.type in ("image", "html_inline", "html_block")]
    assert {t.attrGet("href") for t in marks if t.type == "link_open"} <= {ARXIV}
    assert not [x for x in texts if "$x$" in x or ":red[" in x]
    assert not [x for x in texts if _AUTOLINK.search(_unlinked(x))]


def test_model_written_text_shows_as_written_and_never_as_markup(populated):
    r = _seeded(populated)
    seed = r.add_idea(f"LCE {EVIL}", {}, "seed", "5m")              # no proposal can work on it: "Try next: ..."
    i = r.add_idea(f"Basis {EVIL}", {"mechanism": EVIL}, "research", "4h")
    r.add_proposal(idea_id=i, plugin="px_evil", timeframe="4h", kind="new", origin="proposer", state="broken",
                   broken_reason=EVIL)
    summary = f"{EVIL}\n# heading\n- item\n    code"
    reply = {"ideas": [{"title": f"Basis {EVIL}", "builds_on": EVIL}], "stop": [EVIL], "summary": summary}
    r.add_research(session_id=r.start_session(model="m"), trials_used=5, reply=reply, added=[i], tokens_in=1,
                   tokens_out=1, outcome="ok", referee={
                       "web": True, "fetches": [ARXIV, FETCHED[0]], "denied": [FETCHED[0]], "verdicts": [
                           {"title": EVIL, "decision": "accept", "reason": EVIL, "sources": [ARXIV, OFF_LIST[0]]}]})
    at = _ideas_at()
    assert not at.exception, [e.message for e in at.exception]
    texts = [e.value for e in [*at.markdown, *at.caption, *at.error, *at.warning]]
    _no_markup(texts)
    shown = [_shown(x) for x in texts]
    E = EVIL_SHOWN
    for line in (f"Basis {E}", f"mechanism: {E}", f"Summary: {E}\n# heading\n- item\ncode", E,
                 f"#{i} Basis {E} · builds on: {E}",
                 f"{E}: accepted as #{i} · {E} · sources: {ARXIV}, {OFF_LIST[1]}",
                 f"fetches (2): {ARXIV}, {FETCHED[1]} · denied by the CLI (1): {FETCHED[1]}"):
        assert line in shown
    assert any(x.startswith(f"Try next: no proposal can work on idea #{seed} (LCE {E}) now") for x in shown)
    assert any(x.endswith(f"px_evil @ 4h · Basis {E} · {E}") for x in shown)        # the broken attempt
    verdict = next(x for x in texts if _shown(x).startswith(f"{E}: accepted"))
    assert _hrefs(verdict) == [ARXIV]                       # the allowlisted source is still a link, the other not


def test_a_referee_error_shows_as_written(populated):
    _refereed(_record(populated), {"web": True, "fetches": [], "denied": [], "error": f"invalid: {EVIL}"})
    lines, _ = _notes(_ideas_at())
    _no_markup(lines)
    assert (f"Referee failed · with academic web access: invalid: {EVIL_SHOWN}. No card from this round was added."
            in [_shown(x) for x in lines])


def test_a_promoted_web_reviewed_ideas_review_packet_carries_the_caveat(populated):
    from dashboard.ideas import WEB_REVIEWED_CAVEAT
    r = _record(populated)
    assert WEB_REVIEWED_CAVEAT not in [w.value for w in _ideas_at().warning]
    promoted = next(i["id"] for i in r.ideas() if i["title"] == "promoted one")
    r.set_idea(promoted, web_reviewed=True)
    at = _ideas_at()
    assert not at.exception, [e.message for e in at.exception]
    assert WEB_REVIEWED_CAVEAT == ("A web-informed referee reviewed this card: academic papers published after the "
                                   "model's training cutoff may discuss the holdout period, so treat its exam as "
                                   "weaker evidence.")
    assert [w.value for w in at.warning].count(WEB_REVIEWED_CAVEAT) == 1    # the packet's, not every card's
    assert f"idea {promoted} · proposer · 4h · open · web-reviewed" in [c.value for c in at.caption]


# ---------------------------------------------------------------- the human's stop list on the Ideas page

def _stop_buttons(at, label):
    return [b for b in at.button if b.label == label]


def test_the_stop_list_adds_an_item_and_shows_it_with_the_warning(populated):
    from perp_lab.proposer import human
    r = _record(populated)                       # unseeded: a stop item adds no idea, so it needs no seed
    at = _ideas_at()
    assert not at.exception, [e.message for e in at.exception]
    assert any(e.label == "Your stop list" for e in at.expander)
    assert human.WARNING in [w.value for w in at.warning]
    at.text_input(key="new_stop").input("  no funding-sign flips  ")
    _stop_buttons(at, "Add")[0].click().run()
    assert not at.exception, [e.message for e in at.exception]
    assert [s["text"] for s in r.stops()] == ["no funding-sign flips"]
    assert "#1 no funding-sign flips" in [_shown(m.value) for m in at.markdown]
    assert "Stop #1 added" in [t.value for t in at.toast]
    assert at.text_input(key="new_stop").value == ""


def test_the_stop_list_refuses_what_the_record_refuses_and_adds_nothing(populated):
    r = _record(populated)
    at = _ideas_at()
    at.text_input(key="new_stop").input("x" * 201)
    _stop_buttons(at, "Add")[0].click().run()
    assert any("at most 200 characters" in c.value for c in at.caption)
    at.text_input(key="new_stop").input("   ")
    _stop_buttons(at, "Add")[0].click().run()
    assert "a stop item needs text" in [c.value for c in at.caption]
    assert r.stops() == [] and not at.exception


def test_the_stop_list_removes_an_item_and_reports_a_stale_one(populated):
    r = _record(populated)
    a, b = r.add_stop("first"), r.add_stop("second")
    at = _ideas_at()
    assert {"#1 first", "#2 second"} <= {m.value for m in at.markdown}
    r.remove_stop(b)                             # removed elsewhere (the CLI) while the page is open
    _stop_buttons(at, "Remove")[1].click().run()
    assert f"stop #{b} was already removed" in [c.value for c in at.caption]
    _stop_buttons(at, "Remove")[0].click().run()
    assert r.stops() == [] and "Stop #1 removed" in [t.value for t in at.toast]
    assert not at.exception


def test_the_humans_stop_text_shows_as_written_and_never_as_markup(populated):
    """M4: the human wrote it, but it is text the code didn't write, maybe pasted from what their own web research
    read: shown as written, and linking nothing, as every such string on the page."""
    r = _record(populated)
    r.add_stop(EVIL)
    at = _ideas_at()
    assert not at.exception, [e.message for e in at.exception]
    texts = [m.value for m in at.markdown if m.value.startswith("#1 ")]
    _no_markup(texts)
    assert [_shown(x) for x in texts] == [f"#1 {EVIL_SHOWN}"]


def test_the_stop_list_is_absent_without_a_record_and_creates_nothing(populated):
    at = _ideas_at()
    assert not any(e.label == "Your stop list" for e in at.expander)
    assert not (populated.path.parent / "no_proposer_state").exists()


def test_a_stop_list_that_cannot_be_read_does_not_take_the_page_down(populated, monkeypatch):
    from perp_lab.proposer.record import Record
    _record(populated)

    def boom(self):
        raise RuntimeError("bad row")
    monkeypatch.setattr(Record, "stops", boom)
    at = _ideas_at()
    assert not at.exception, [e.message for e in at.exception]
    assert "Stop list unreadable" in [c.value for c in at.caption]
    assert "#### open one" in [m.value for m in at.markdown]          # the rest of the page is there


def test_the_stop_list_absence_is_about_the_dir_the_page_reads(populated):
    # positive control: the dir the absence test checks is the one the page reads, since a record made there shows it
    assert not any(e.label == "Your stop list" for e in _ideas_at().expander)
    _record(populated)
    assert any(e.label == "Your stop list" for e in _ideas_at().expander)

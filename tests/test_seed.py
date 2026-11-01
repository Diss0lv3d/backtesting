"""The seed: the human trials and candidates B-E as the record's first ideas, and the start check on their
verdicts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import perp_lab.strategy
from perp_lab.plugins import blob_hash, parse_meta
from perp_lab.proposer import contract, piles, settings
from perp_lab.proposer.assemble import Rejected, check_idea_rules
from perp_lab.proposer.contract import Ports, Proposal, Refines
from perp_lab.proposer.prompt import render_record
from perp_lab.proposer.record import Record
from perp_lab.proposer.seed import STRATEGIES, check_seed, load_seed, seed
from perp_lab.proposer.settings import StartRefused
from tests.conftest import REPO
from tests.test_store import cfg  # noqa: F401  (fixture)

# The seeded trials in seq order, and the idea each one sits under.
TRIALS = [("donchian_atr", "4h"), ("donchian_atr_wide", "4h"), ("donchian_atr", "1h"),
          ("bb_vwap_reversion", "15m"), ("bb_vwap_reversion", "1h"), ("funding_fade", "1h"),
          ("flow_shock_fade", "1h"), ("lce_pct", "5m"), ("lce_atr", "5m")]
JUDGED = ["Donchian breakout", "Donchian breakout, 1h", "BB–VWAP reversion", "BB–VWAP reversion, 1h",
          "Funding fade", "Flow-shock fade", "LCE: EMA cloud at volume-profile levels"]
OPEN = ["Volatility-compression breakout", "Weekend-move reversal", "Time-series momentum with a funding filter"]
E = "Taker-flow persistence"
E_REASON = ("A's sign flip; choosing it after A's result would pick the sign from an OOS outcome (research note "
            "sections 4 and 10)")
UNDER = [JUDGED[0], JUDGED[0], JUDGED[1], JUDGED[2], JUDGED[3], JUDGED[4], JUDGED[5], JUDGED[6], JUDGED[6]]
FAMILY = {"donchian_atr": "family:donchian_atr", "donchian_atr_wide": "family:donchian_atr",
          "bb_vwap_reversion": "family:bb_vwap_reversion", "funding_fade": "family:funding_fade",
          "flow_shock_fade": "family:flow_shock_fade", "lce_pct": "family:lce_pct", "lce_atr": "family:lce_pct"}
SEVEN = ["lab verdict-refresh donchian_atr --tf 4h", "lab verdict-refresh donchian_atr_wide --tf 4h",
         "lab verdict-refresh donchian_atr --tf 1h", "lab verdict-refresh bb_vwap_reversion --tf 15m",
         "lab verdict-refresh bb_vwap_reversion --tf 1h", "lab verdict-refresh funding_fade --tf 1h",
         "lab verdict-refresh flow_shock_fade --tf 1h"]


def old(plugin, tf):
    """Schema 1, as six of the seven human verdicts are today: no diagnostics."""
    return {"strategy": plugin, "timeframe": tf, "decision": "kill", "reasons": ["OOS Sharpe -0.46 <= 0"],
            "lineage_id": FAMILY[plugin], "holdout_status": "n/a", "n_trials": 9}


def new(plugin, tf, gross=-0.5, gross_p95=0.1, holdout="n/a"):
    gates = {"net_sharpe": -1.0, "random_p95": 0.2, "oos_trades": 500, "min_oos_trades": 64}
    return {**old(plugin, tf), "schema": 2, "holdout_status": holdout, "diagnostics": {
        "data": "refreshed", "gates": gates, "before_costs": {"sharpe": gross, "random_p95": gross_p95}}}


LCE = {"lce_pct": dict(gross=-4.43, gross_p95=-5.68), "lce_atr": dict(gross=0.67, gross_p95=0.49)}   # trials 8-9


@pytest.fixture
def lab(tmp_path, cfg):  # noqa: F811
    """Stand-in plugin files (the seed only hashes and keeps them), the temp config's verdicts dir, a record."""
    sdir = tmp_path / "strategies"
    sdir.mkdir()
    for plugin in FAMILY:
        (sdir / f"{plugin}.py").write_text(f'"""{plugin}, a stand-in."""\n')
    vdir = Path(cfg["judge"]["verdicts_dir"])
    vdir.mkdir(parents=True)
    return SimpleNamespace(cfg=cfg, strategies=sdir, verdicts=vdir, rec=Record(tmp_path / "state"))


def write(lab, plugin, tf, verdict):
    h = blob_hash((lab.strategies / f"{plugin}.py").read_bytes())
    path = lab.verdicts / f"{plugin}@{tf}@{h[:10]}.json"
    if verdict is None:
        path.unlink(missing_ok=True)
    else:
        path.write_text(verdict if isinstance(verdict, str) else json.dumps(verdict))


def as_today(lab):
    """The real store before the refreshes: seven schema-1 verdicts, and LCE's two with diagnostics."""
    for p, tf in TRIALS[:7]:
        write(lab, p, tf, old(p, tf))
    for p in LCE:
        write(lab, p, "5m", new(p, "5m", **LCE[p]))


def refreshed(lab, **f):
    for p, tf in TRIALS[:7]:
        write(lab, p, tf, new(p, tf, **f.get(f"{p}@{tf}", {})))


def do_seed(lab):
    seed(lab.rec, lab.cfg, strategies=lab.strategies)
    return {i["title"]: i for i in lab.rec.ideas()}


def test_the_seed_makes_eleven_ideas_seven_judged_b_to_d_open_and_e_excluded(lab):
    as_today(lab)
    ideas = do_seed(lab)
    assert list(ideas) == [*JUDGED, *OPEN, E]
    assert {i["origin"] for i in ideas.values()} == {"seed"}
    assert [t for t, i in ideas.items() if lab.rec.proposals(i["id"])] == JUDGED
    assert [(ideas[t]["status"], ideas[t]["timeframe"], ideas[t]["family"]) for t in OPEN] == \
        [("open", "4h", None)] * 3
    assert ideas[E]["status"] == "excluded" and ideas[E]["excluded_reason"] == E_REASON
    assert not lab.rec.proposals(ideas[E]["id"])
    assert [ideas[t]["timeframe"] for t in JUDGED] == ["4h", "1h", "15m", "1h", "1h", "1h", "5m"]


def test_the_seeded_trials_are_judged_proposals_1_to_9_that_count_toward_no_quota(lab):
    as_today(lab)
    ideas = do_seed(lab)
    title = {i["id"]: t for t, i in ideas.items()}
    props = lab.rec.proposals()
    assert [(p["seq"], p["plugin"], p["timeframe"]) for p in props] == [(n, *t) for n, t in enumerate(TRIALS, 1)]
    assert [title[p["idea_id"]] for p in props] == UNDER
    assert [p["kind"] for p in props] == ["new", "refinement", "port", "new", "port", "new", "new", "new",
                                          "refinement"]
    assert {(p["origin"], p["state"], p["judge_exit"], p["judged_at"]) for p in props} == {
        ("seed", "judged", 0, None)}
    for p in props:
        text = (lab.strategies / f"{p['plugin']}.py").read_bytes()
        assert p["hash"] == blob_hash(text) and p["source"] == text.decode()
    assert lab.rec.trials_used() == 0 and lab.rec.judge_calls_on(datetime.now(timezone.utc).date()) == 0
    assert lab.rec.next_seq() == 10


def test_the_two_ports_carry_port_of(lab):
    as_today(lab)
    ideas = do_seed(lab)
    ports = {t: i["port_of"] for t, i in ideas.items() if i["port_of"] is not None}
    assert ports == {"Donchian breakout, 1h": ideas["Donchian breakout"]["id"],
                     "BB–VWAP reversion, 1h": ideas["BB–VWAP reversion"]["id"]}


def test_families_and_piles_come_from_the_verdicts(lab):
    as_today(lab)
    ideas = do_seed(lab)
    assert ideas["Donchian breakout"]["family"] == ideas["Donchian breakout, 1h"]["family"] == \
        "family:donchian_atr"
    assert ideas["BB–VWAP reversion"]["family"] == ideas["BB–VWAP reversion, 1h"]["family"] == \
        "family:bb_vwap_reversion"
    assert [(ideas[t]["family"], ideas[t]["root_plugin"]) for t in JUDGED[4:]] == [
        ("family:funding_fade", "funding_fade"), ("family:flow_shock_fade", "flow_shock_fade"),
        ("family:lce_pct", "lce_pct")]
    assert [ideas[t]["root_plugin"] for t in JUDGED[:4]] == ["donchian_atr", "donchian_atr", "bb_vwap_reversion",
                                                             "bb_vwap_reversion"]
    props = lab.rec.proposals()
    # schema 1 has no diagnostics: unknown until refreshed; LCE's piles are mechanical (R18)
    assert [p["pile"] for p in props] == ["unknown"] * 7 + ["no signal", "signal before costs"]
    assert props[0]["verdict"] == old("donchian_atr", "4h")
    assert {i["status"] for t, i in ideas.items() if t in JUDGED} == {"open"}


def test_statuses_follow_the_verdicts_and_a_spent_read_closes_the_seeded_port(lab):
    refreshed(lab, **{"donchian_atr@4h": {"holdout": "passed"}})
    for p in LCE:
        write(lab, p, "5m", new(p, "5m", **LCE[p]))
    ideas = do_seed(lab)
    assert [ideas[t]["status"] for t in JUDGED] == ["passed", "passed", "dead", "dead", "dead", "dead", "open"]
    assert {p["pile"] for p in lab.rec.proposals()[2:7]} == {"no signal"}


def test_an_exam_in_a_seeded_ideas_second_family_closes_the_idea(lab):
    """R43, as in the loop: the idea's first plugin (its family) is broken, its second is another family's root,
    promoted, and that family's exam passed."""
    refreshed(lab)
    for p in LCE:
        write(lab, p, "5m", new(p, "5m", **LCE[p]))
    first = new("donchian_atr", "4h")
    first["diagnostics"]["gates"]["oos_trades"] = 10                         # too few: broken
    write(lab, "donchian_atr", "4h", first)
    second = {**new("donchian_atr_wide", "4h", holdout="passed"), "decision": "promote"}
    write(lab, "donchian_atr_wide", "4h", {**second, "lineage_id": "family:donchian_atr_wide"})
    ideas = do_seed(lab)
    d = ideas["Donchian breakout"]
    assert [p["pile"] for p in lab.rec.proposals(d["id"])] == ["broken", "promoted"]
    assert (d["family"], d["status"]) == ("family:donchian_atr", "passed")
    assert piles.idea_group(d, lab.rec.proposals(d["id"])) == "passed"
    assert ideas["Donchian breakout, 1h"]["status"] == "dead"      # its plugin is in no examined family


def test_without_verdict_files_a_trial_has_no_verdict_and_its_idea_no_family(lab):
    ideas = do_seed(lab)
    assert {(p["verdict"], p["pile"]) for p in lab.rec.proposals()} == {(None, "broken")}
    assert {i["family"] for i in ideas.values()} == {None}
    assert {i["status"] for t, i in ideas.items() if t != E} == {"open"}


def test_e_takes_neither_refinement_nor_port_and_lce_no_refinement(lab):
    as_today(lab)
    ideas = do_seed(lab)
    pc = settings.load(lab.cfg)
    e = ideas[E]["id"]
    with pytest.raises(Rejected, match="excluded"):
        check_idea_rules(Proposal("refinement", "4h", "h", "x", refines=Refines(e, "c")), lab.rec, pc)
    with pytest.raises(Rejected, match="excluded"):
        check_idea_rules(Proposal("port", "1h", "h", "x", ports=Ports(e, "w")), lab.rec, pc)
    lce = ideas[JUDGED[6]]["id"]
    with pytest.raises(Rejected, match="timeframe"):       # 5m is not one of the proposer's timeframes
        check_idea_rules(Proposal("refinement", "5m", "h", "x", refines=Refines(lce, "c")), lab.rec, pc)


def test_check_seed_refuses_with_the_seven_commands_while_verdicts_are_schema_1(lab):
    as_today(lab)
    do_seed(lab)
    with pytest.raises(StartRefused) as e:
        check_seed(lab.rec, lab.cfg)
    assert [ln for ln in str(e.value).splitlines() if ln.startswith("lab ")] == SEVEN


def test_check_seed_reads_the_files_so_it_passes_once_they_are_refreshed(lab):
    as_today(lab)
    do_seed(lab)
    refreshed(lab)
    check_seed(lab.rec, lab.cfg)
    assert lab.rec.proposals()[0]["pile"] == "unknown"    # the record catches up at the loop's snapshot


@pytest.mark.parametrize("broken", [
    None,                                                                      # missing
    {**new("lce_atr", "5m"), "diagnostics": {"gates": {"net_sharpe": -1.0}}},  # diagnostics, no before_costs
    {**new("lce_atr", "5m"), "diagnostics": {"before_costs": None}},
    {**new("lce_atr", "5m"), "diagnostics": {"before_costs": {}}},
    old("lce_atr", "5m"),
    "{not json",
    "[1, 2]",
])
def test_check_seed_names_lce_when_its_verdict_has_no_before_costs(lab, broken):
    refreshed(lab)
    for p in LCE:
        write(lab, p, "5m", new(p, "5m", **LCE[p]))
    do_seed(lab)
    write(lab, "lce_atr", "5m", broken)
    with pytest.raises(StartRefused) as e:
        check_seed(lab.rec, lab.cfg)
    assert [ln for ln in str(e.value).splitlines() if ln.startswith("lab ")] == [
        "lab verdict-refresh lce_atr --tf 5m"]


def test_seeding_twice_adds_nothing(lab):
    as_today(lab)
    do_seed(lab)
    before = (lab.rec.ideas(), lab.rec.proposals())
    refreshed(lab)                                          # even with other verdicts on disk now
    do_seed(lab)
    assert (lab.rec.ideas(), lab.rec.proposals()) == before


def test_a_record_with_any_idea_is_not_seeded(lab):
    lab.rec.add_idea("Mine", {}, "proposer", "1h")
    do_seed(lab)
    assert [i["title"] for i in lab.rec.ideas()] == ["Mine"] and lab.rec.proposals() == []


def test_a_missing_plugin_refuses_and_seeds_nothing_so_the_next_start_seeds(lab):
    as_today(lab)
    text = (lab.strategies / "flow_shock_fade.py").read_bytes()
    (lab.strategies / "flow_shock_fade.py").unlink()
    with pytest.raises(StartRefused, match="flow_shock_fade"):
        do_seed(lab)
    assert lab.rec.ideas() == [] and lab.rec.proposals() == []
    (lab.strategies / "flow_shock_fade.py").write_bytes(text)
    assert len(do_seed(lab)) == 11


def test_the_seeded_record_renders(lab):
    as_today(lab)
    ideas = do_seed(lab)
    text = render_record(lab.rec, 9, settings.load(lab.cfg).timeframes)
    assert f"### #{ideas['Donchian breakout']['id']} Donchian breakout" in text
    assert f"#{ideas[E]['id']} {E}: excluded, untested ({E_REASON})" in text


# -- the shipped seed
def test_the_seed_reads_plugins_where_the_judge_does():
    assert STRATEGIES == perp_lab.strategy.STRATEGY_DIR


def test_the_shipped_cards_follow_the_contract_and_titles_are_distinct():
    entries = load_seed()
    for e in entries:
        reply = {"kind": "new", "timeframe": e["timeframe"], "hypothesis": "h", "source": "x", "idea": e["card"]}
        assert contract.parse(reply, [e["timeframe"]]).idea.title == e["card"]["title"]
    titles = [e["card"]["title"].casefold() for e in entries]
    assert len(set(titles)) == len(titles) == 11


def test_the_shipped_plugins_exist_and_each_refinement_names_its_ideas_root_as_parent():
    for e in load_seed():
        plugins = e.get("plugins") or []
        for p in plugins:
            meta = parse_meta((REPO / "strategies" / f"{p}.py").read_bytes(), p)
            assert e["timeframe"] in meta.timeframes
            assert meta.parent == (plugins[0] if p != plugins[0] else None)

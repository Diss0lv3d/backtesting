"""Idea rules, plugin assembly and source checks: each rule has a pass and a refusal."""

from __future__ import annotations

import ast
from dataclasses import replace
from types import SimpleNamespace

import pytest

from perp_lab.plugins import parse_meta
from perp_lab.proposer.assemble import (Rejected, assemble, check_idea_rules, check_model_source,
                                        check_overlap_rules, check_source, plugin_name)
from perp_lab.proposer.contract import IdeaCard, Ports, Proposal, Refines
from perp_lab.proposer.record import Record

PC = SimpleNamespace(timeframes=("15m", "1h", "4h"), proposals_per_idea=4, max_grid_combos=16, overlap_max=0.5)

SRC = '''"""old doc"""
from perp_lab.indicators import atr
from perp_lab.strategy import Strategy


class S(Strategy):
    params = {"n": 5}
    grid = {"n": [3, 5]}
    timeframes = ["1h"]
'''

CARD = IdeaCard("Funding fade", "m", "w", "s", 10.0, "f", ("close",))
SBC = {"decision": "kill", "reasons": [], "holdout_status": "n/a", "diagnostics": {
    "gates": {"net_sharpe": -1, "random_p95": 0, "oos_trades": 100, "min_oos_trades": 64},
    "before_costs": {"sharpe": 0.5, "random_p95": 0.1}}}
NOSIG = {**SBC, "diagnostics": {**SBC["diagnostics"], "before_costs": {"sharpe": -1, "random_p95": 0.1}}}


def prop(kind="new", tf="1h", source=SRC, **kw):
    if kind == "new":
        kw.setdefault("idea", CARD)
    return Proposal(kind=kind, timeframe=tf, hypothesis="Hyp line", source=source, **kw)


def refine(i, tf="1h"):
    return prop("refinement", tf, refines=Refines(i, "c"))


def port(i, tf="4h"):
    return prop("port", tf, ports=Ports(i, "w"))


@pytest.fixture
def rec(tmp_path):
    return Record(tmp_path / "state")


def judged(rec, idea, plugin, verdict=None, tf="1h", **f):
    rec.add_proposal(idea_id=idea, plugin=plugin, kind="new", timeframe=tf, state="judged", judge_exit=0,
                     verdict=verdict, **f)


# -- idea rules
def test_new_idea_title_free_passes_and_taken_any_case_refused(rec):
    assert check_idea_rules(prop(), rec, PC) is None
    rec.add_idea("FUNDING fade", {}, "seed", "1h")
    with pytest.raises(Rejected, match="already exists"):
        check_idea_rules(prop(), rec, PC)


def test_timeframe_must_be_configured(rec):
    with pytest.raises(Rejected, match="timeframe"):
        check_idea_rules(prop(tf="5m"), rec, PC)


def test_refinement_passes_on_open_idea_with_room(rec):
    i = rec.add_idea("A", {}, "seed", "1h")
    assert check_idea_rules(refine(i), rec, PC)["id"] == i


@pytest.mark.parametrize("status", ["dead", "exhausted", "excluded", "passed"])
def test_refinement_refused_unless_open(rec, status):
    i = rec.add_idea("A", {}, "seed", "1h", status=status)
    with pytest.raises(Rejected):
        check_idea_rules(refine(i), rec, PC)


def test_refinement_refused_when_judged_count_reached_and_broken_ones_dont_count(rec):
    i = rec.add_idea("A", {}, "seed", "1h")
    for k in range(3):
        judged(rec, i, f"p{k}")
    rec.add_proposal(idea_id=i, plugin="nj", kind="refinement", timeframe="1h", state="dropped")
    assert check_idea_rules(refine(i), rec, PC)
    judged(rec, i, "p3")
    with pytest.raises(Rejected, match="judged plugins"):
        check_idea_rules(refine(i), rec, PC)


def test_refinement_refused_on_other_timeframe_and_unknown_idea(rec):
    i = rec.add_idea("A", {}, "seed", "1h")
    with pytest.raises(Rejected, match="keeps its idea's timeframe"):
        check_idea_rules(refine(i, "4h"), rec, PC)
    with pytest.raises(Rejected, match="no idea"):
        check_idea_rules(refine(99), rec, PC)


@pytest.mark.parametrize("status", ["open", "exhausted"])
def test_port_passes_from_signal_before_costs(rec, status):
    i = rec.add_idea("A", {}, "seed", "1h", status=status)
    judged(rec, i, "p0", SBC)
    assert check_idea_rules(port(i), rec, PC)["id"] == i


def test_port_refused_for_no_signal_unjudged_promoted_dead_excluded(rec):
    a = rec.add_idea("A", {}, "seed", "1h")
    judged(rec, a, "p0", NOSIG)
    with pytest.raises(Rejected, match="best pile"):
        check_idea_rules(port(a), rec, PC)
    b = rec.add_idea("B", {}, "seed", "1h")
    with pytest.raises(Rejected, match="best pile"):
        check_idea_rules(port(b), rec, PC)
    c = rec.add_idea("C", {}, "seed", "1h")
    judged(rec, c, "p1", {**SBC, "decision": "promote"})
    with pytest.raises(Rejected, match="promoted"):
        check_idea_rules(port(c), rec, PC)
    for n, st in (("D", "dead"), ("E", "excluded")):
        d = rec.add_idea(n, {}, "seed", "1h", status=st)
        judged(rec, d, "q" + n, SBC)
        with pytest.raises(Rejected):
            check_idea_rules(port(d), rec, PC)


def test_port_refused_on_same_timeframe_and_when_port_exists(rec):
    i = rec.add_idea("A", {}, "seed", "1h")
    judged(rec, i, "p0", SBC)
    with pytest.raises(Rejected, match="other than"):
        check_idea_rules(port(i, "1h"), rec, PC)
    rec.add_idea("A 4h", {}, "proposer", "4h", port_of=i)
    with pytest.raises(Rejected, match="already has a port"):
        check_idea_rules(port(i, "4h"), rec, PC)
    assert check_idea_rules(port(i, "15m"), rec, PC)


# -- names
def test_plugin_name():
    n = plugin_name(7, "Funding Fade: the ÜBER long-title that goes on and on forever")
    assert n == plugin_name(7, "Funding Fade: the ÜBER long-title that goes on and on forever")
    assert len(n) <= 40 and n.isidentifier() and n.startswith("px0007_") and n == n.lower()
    assert plugin_name(1, "!!!").isidentifier()


# -- PARENT in the model's source
def test_parent_assignment_refused_comment_and_string_fine():
    for bad in ('PARENT = "x"\n', 'PARENT: str = "x"\n', 'a = PARENT = "x"\n', 'PARENT += "x"\n',
                'if 1:\n    PARENT = "x"\n', 'class K:\n    PARENT = 1\n', '(PARENT := 1)\n',
                'PARENT, b = 1, 2\n', '*PARENT, = [1]\n', 'for PARENT in []:\n    pass\n',
                'with open("f") as PARENT:\n    pass\n', 'del PARENT\n'):
        with pytest.raises(Rejected, match="PARENT"):
            check_model_source(SRC + bad)
    check_model_source(SRC + '# PARENT = "x"\nS.note = "PARENT"\n"""PARENT = 1"""\n')


def test_reading_parent_is_not_assigning_it():
    check_model_source(SRC + 'd = {}\nd[PARENT] = 1\nPARENT.x = 1\nx = PARENT\n')


def test_model_source_is_read_as_assemble_reads_it_bom_and_crlf():
    for src in ("﻿" + SRC, "﻿" + SRC.replace("\n", "\r\n")):
        check_model_source(src)
        with pytest.raises(Rejected, match="PARENT"):
            check_model_source(src + 'PARENT = "x"\r\n')


def test_unparseable_source_is_rejected():
    with pytest.raises(Rejected, match="parse"):
        check_model_source("def (:")


# -- assemble
def test_assemble_normalises_line_endings_and_bom():
    src = "\ufeff" + SRC.replace("\n", "\r\n")
    out = assemble(prop(source=src), "px0001_a", None)
    assert not out.startswith(b"\xef\xbb\xbf") and b"\r" not in out
    ast.parse(out)


def test_assemble_docstring_first_line_is_hypothesis_replaced_or_inserted():
    for src in (SRC, SRC.split("\n", 1)[1]):
        out = assemble(prop(source=src), "px0001_a", None).decode()
        assert ast.get_docstring(ast.parse(out)).split("\n")[0] == "Hyp line"
        assert "old doc" not in out
        assert parse_meta(out.encode(), "px0001_a").hypothesis.split("\n")[0] == "Hyp line"


@pytest.mark.parametrize("doc", ['"""old"""; X = 1', '"""über"""  ;  X = 1; Y = 2',
                                 '"""old\nstill old"""; X = 1'])
def test_assemble_keeps_code_sharing_the_old_docstrings_line(doc):
    src = doc + "\n" + SRC.split("\n", 1)[1]
    for out in (assemble(prop(source=src), "n", None), assemble(replace(refine(1), source=src), "n", "champ")):
        tree = ast.parse(out)
        assert ast.get_docstring(tree) == "Hyp line" and b"old" not in out and "über".encode() not in out
        assert "X" in {t.id for n in tree.body if isinstance(n, ast.Assign) for t in n.targets}
    assert parse_meta(out, "n").parent == "champ"


def test_assemble_parent_follows_a_future_import_on_the_old_docstrings_line():
    src = '"""old"""; from __future__ import annotations\n' + SRC.split("\n", 1)[1]
    out = assemble(replace(refine(1), source=src), "n", "champ")
    compile(out, "n", "exec")
    assert b"\nfrom __future__ import annotations\nPARENT = " in out and parse_meta(out, "n").parent == "champ"


def test_assemble_hypothesis_with_quotes():
    h = 'says """x""" and ends with a quote"'
    out = assemble(replace(refine(1), hypothesis=h), "px0001_a", None).decode()
    assert ast.get_docstring(ast.parse(out), clean=False).startswith(h)


def test_assemble_parent_only_for_refinement_with_champion():
    out = assemble(refine(1), "n", "px0002_champ").decode()
    assert parse_meta(out.encode(), "n").parent == "px0002_champ"
    assert out.index("PARENT") > out.index('"""', 5)
    assert parse_meta(assemble(refine(1), "n", None), "n").parent is None
    assert parse_meta(assemble(port(1), "n", "px0002_champ"), "n").parent is None
    assert parse_meta(assemble(prop(), "n", "px0002_champ"), "n").parent is None


def test_assemble_keeps_future_import_legal():
    src = '"""d"""\nfrom __future__ import annotations\n' + SRC.split("\n", 1)[1]
    for s in (src, src.split("\n", 1)[1]):                    # with and without a docstring of its own
        out = assemble(replace(refine(1), source=s), "n", "px0002_champ")
        compile(out, "n", "exec")
        assert parse_meta(out, "n").parent == "px0002_champ"


# -- check_source
def chk(src, tf="1h", name="px0001_a"):
    check_source(src.encode(), name, tf, PC)


def test_check_source_passes_a_good_plugin():
    chk(SRC)
    chk(SRC.replace("import atr", "import atr\nimport numpy as np\nfrom pandas import Series"))


@pytest.mark.parametrize("src, match", [
    (SRC + "\nclass T(Strategy):\n    timeframes = ['1h']\n", "exactly one"),
    (SRC.replace('{"n": [3, 5]}', '{"n": list(range(3))}'), "literal"),
    (SRC.replace("import atr", "import atr\nimport os"), "import of 'os'"),
    (SRC.replace("import atr", "import atr\nfrom os import path"), "import of 'os'"),
    (SRC.replace("import atr", "import atr\nfrom . import x"), "import of"),
    (SRC.replace("[3, 5]", "[1, 2, 3, 4, 5, 6, 7, 8, 9]").replace("}\n    timeframes",
     ', "m": [1, 2]}\n    timeframes', 1), "18 combinations"),
    (SRC.replace('["1h"]', '["1h", "4h"]'), "exactly"),
    (SRC.replace('["1h"]', '["4h"]'), "exactly"),
    (SRC + "# " + "x" * 70000 + "\n", "bytes"),
])
def test_check_source_refusals(src, match):
    with pytest.raises(Rejected, match=match):
        chk(src)


@pytest.mark.parametrize("value", ["5", "[]", '"abc"', "(3, 5)", "{3, 5}", "None"])
def test_check_source_refuses_a_grid_value_that_is_not_a_non_empty_list(value):
    with pytest.raises(Rejected, match=r"grid\['n'\] must be a non-empty list"):
        chk(SRC.replace('{"n": [3, 5]}', '{"n": ' + value + "}"))


def test_check_source_grid_limit_is_inclusive():
    chk(SRC.replace('{"n": [3, 5]}', '{"n": [1, 2, 3, 4], "m": [1, 2, 3, 4]}'))
    with pytest.raises(Rejected, match="17 combinations"):
        chk(SRC.replace('{"n": [3, 5]}', '{"n": list(range(17))}') .replace("list(range(17))", str(list(range(17)))))


# -- overlap
def report(*entries, ok=True, skipped=None):
    """Entries are (strategy, overlap) at 1h, or (strategy, overlap, timeframe)."""
    return {"ok": ok, "overlap": [{"strategy": s, "timeframe": tf[0] if tf else "1h", "overlap": o, "mirror": 0.0,
                                   "judged": True} for s, o, *tf in entries], "skipped": skipped or {}}


@pytest.fixture
def world(rec):
    o = rec.add_idea("Open one", {}, "seed", "1h")
    c = rec.add_idea("Closed one", {}, "seed", "1h", status="dead")
    judged(rec, o, "open_plug")
    judged(rec, c, "closed_plug")
    return rec, o, c


def test_below_threshold_passes(world):
    rec, o, c = world
    check_overlap_rules(report(("closed_plug", 0.49), ("open_plug", 0.1)), prop(), None, rec, PC)


def test_closed_near_copy_refused_for_every_kind(world):
    rec, o, c = world
    r = report(("closed_plug", 0.5))
    for p, t in ((prop(), None), (refine(o), rec.idea(o))):
        with pytest.raises(Rejected, match=f"near-copy of closed idea #{c} \\(Closed one\\)"):
            check_overlap_rules(r, p, t, rec, PC)


def test_open_near_copy_as_new_is_redirected_but_its_own_refinement_and_port_may(world):
    rec, o, c = world
    r = report(("open_plug", 0.9))
    with pytest.raises(Rejected, match=f"propose it as a refinement \\(or a port\\) of idea #{o} \\(Open one\\)"):
        check_overlap_rules(r, prop(), None, rec, PC)
    check_overlap_rules(r, refine(o), rec.idea(o), rec, PC)
    check_overlap_rules(r, port(o), rec.idea(o), rec, PC)


def test_exemption_is_only_for_the_own_idea(world):
    rec, o, c = world
    r = report(("open_plug", 0.9), ("closed_plug", 0.9))
    with pytest.raises(Rejected, match="closed idea"):
        check_overlap_rules(r, refine(o), rec.idea(o), rec, PC)
    p2 = rec.add_idea("Other open", {}, "seed", "1h")
    judged(rec, p2, "other_plug")
    for p in (refine(p2), port(p2)):
        with pytest.raises(Rejected, match="closed idea"):
            check_overlap_rules(report(("closed_plug", 0.9)), p, rec.idea(p2), rec, PC)


def test_a_refinement_or_port_near_copying_another_open_idea_is_redirected_there(world):
    rec, o, c = world
    other = rec.add_idea("Other open", {}, "seed", "1h")
    judged(rec, other, "other_plug", SBC)
    redirect = f"propose it as a refinement \\(or a port\\) of idea #{o} \\(Open one\\)"
    for p in (refine(other), port(other)):
        with pytest.raises(Rejected, match=redirect):
            check_overlap_rules(report(("open_plug", 0.9)), p, rec.idea(other), rec, PC)
        check_overlap_rules(report(("other_plug", 0.9)), p, rec.idea(other), rec, PC)     # its own idea


def test_a_port_is_exempt_from_the_exhausted_idea_it_ports_and_no_other(rec):
    # exhausted counts as closed, so this is where the exemption decides the outcome
    x = rec.add_idea("Exhausted one", {}, "seed", "1h", status="exhausted")
    y = rec.add_idea("Exhausted two", {}, "seed", "1h", status="exhausted")
    judged(rec, x, "x_plug", SBC)
    judged(rec, y, "y_plug", SBC)
    own = report(("x_plug", 0.5))
    check_overlap_rules(own, port(x), rec.idea(x), rec, PC)
    with pytest.raises(Rejected, match=f"near-copy of closed idea #{x} \\(Exhausted one\\)"):
        check_overlap_rules(own, prop(), None, rec, PC)
    with pytest.raises(Rejected, match=f"near-copy of closed idea #{y} \\(Exhausted two\\)"):
        check_overlap_rules(report(("y_plug", 0.5)), port(x), rec.idea(x), rec, PC)


def test_a_plugin_under_two_ideas_at_two_timeframes_is_attributed_by_its_run(rec):
    # the planned seed: donchian_atr is the open 4h idea's plugin, and at 1h its dead port's
    d4 = rec.add_idea("Donchian 4h", {}, "seed", "4h")
    d1 = rec.add_idea("Donchian 1h", {}, "seed", "1h", port_of=d4, status="dead")
    judged(rec, d4, "donchian_atr", SBC, tf="4h")
    judged(rec, d1, "donchian_atr", tf="1h")
    at4, at1 = report(("donchian_atr", 0.9, "4h")), report(("donchian_atr", 0.9, "1h"))
    check_overlap_rules(at4, refine(d4, "4h"), rec.idea(d4), rec, PC)
    check_overlap_rules(at1, refine(d4, "4h"), rec.idea(d4), rec, PC)       # its port: one port group (R46)
    with pytest.raises(Rejected, match=f"refinement \\(or a port\\) of idea #{d4} \\(Donchian 4h\\)"):
        check_overlap_rules(at4, prop(), None, rec, PC)
    with pytest.raises(Rejected, match=f"near-copy of closed idea #{d1} \\(Donchian 1h\\)"):
        check_overlap_rules(at1, prop(), None, rec, PC)
    check_overlap_rules(report(("donchian_atr", 0.9, "15m")), prop(), None, rec, PC)   # a run it doesn't know


@pytest.fixture
def group(rec):
    """A source idea at 1h with two ports (4h, 15m), and two ideas outside that port group, one open, one dead."""
    def make(source_status):
        x = rec.add_idea("Source", {}, "seed", "1h", status=source_status)
        p4 = rec.add_idea("Source, 4h", {}, "proposer", "4h", port_of=x)
        p15 = rec.add_idea("Source, 15m", {}, "proposer", "15m", port_of=x)
        u = rec.add_idea("Unrelated open", {}, "seed", "1h")
        c = rec.add_idea("Unrelated dead", {}, "seed", "1h", status="dead")
        for i, plug, tf in zip((x, p4, p15, u, c), PLUGS, ("1h", "4h", "15m", "1h", "1h")):
            judged(rec, i, plug, SBC, tf=tf)
        return x, p4, p15, u, c
    return make


PLUGS = ("x_plug", "p4_plug", "p15_plug", "u_plug", "c_plug")


@pytest.mark.parametrize("source_status", ["open", "exhausted"])
def test_a_port_group_shares_the_preflight_exemption(rec, group, source_status):
    """R46: a port keeps its source's mechanism, so without a group exemption a refinement of the port is refused
    as a near-copy of the source (or redirected to it), and the source's refinements are redirected to the port:
    neither redirect can be met."""
    x, p4, p15, u, c = group(source_status)
    near = {plug: report((plug, 0.9, tf)) for plug, tf in zip(PLUGS, ("1h", "4h", "15m", "1h", "1h"))}
    check_overlap_rules(near["x_plug"], refine(p4, "4h"), rec.idea(p4), rec, PC)      # a port's against its source
    check_overlap_rules(near["p15_plug"], refine(p4, "4h"), rec.idea(p4), rec, PC)    # ... and its sibling port
    check_overlap_rules(near["p4_plug"], refine(x, "1h"), rec.idea(x), rec, PC)       # the source's against a port
    check_overlap_rules(near["p4_plug"], port(x, "15m"), rec.idea(x), rec, PC)        # a new port against another
    for p, t in ((refine(p4, "4h"), p4), (refine(x, "1h"), x), (port(x, "15m"), x)):
        with pytest.raises(Rejected, match=f"refinement \\(or a port\\) of idea #{u} \\(Unrelated open\\)"):
            check_overlap_rules(near["u_plug"], p, rec.idea(t), rec, PC)
        with pytest.raises(Rejected, match=f"near-copy of closed idea #{c} \\(Unrelated dead\\)"):
            check_overlap_rules(near["c_plug"], p, rec.idea(t), rec, PC)
    with pytest.raises(Rejected):                                                     # a new idea: no group
        check_overlap_rules(near["p4_plug"], prop(), None, rec, PC)


def test_unknown_plugin_ignored(world):
    rec, o, c = world
    check_overlap_rules(report(("human_plug", 0.99)), prop(), None, rec, PC)


def test_unmeasured_or_failed_report_is_never_a_pass_and_not_the_models_fault(world):
    rec, o, c = world
    with pytest.raises(ValueError, match="did not measure"):
        check_overlap_rules(report(skipped={"overlap": "boom"}, ok=True), prop(), None, rec, PC)
    with pytest.raises(ValueError, match="passing preflight"):
        check_overlap_rules(report(ok=False), prop(), None, rec, PC)
    assert not issubclass(Rejected, ValueError)

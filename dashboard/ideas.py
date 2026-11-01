"""The Ideas page: the proposer's record for the human (spec, "Where the user reads the record").

Reads record.db, the session digest and lock, and lab.db. It writes only record.db, and only on the human's
action: an idea card of their own, an exclusion, or the "try next" flag, each checked in code (human.py, Record),
as `lab idea` does. It never creates the state dir. The grouping is `piles.idea_group`, the rule `lab ideas` prints
by.
"""

from __future__ import annotations

import fcntl
import re
import typing
from dataclasses import fields
from datetime import date, datetime, timezone
from pathlib import Path

import streamlit as st

from perp_lab import dsr
from perp_lab.proposer import context, contract, human, piles, settings
from perp_lab.proposer.record import EXCLUDABLE, Record
from perp_lab.store import db

_TITLES = {"passed": "Passed", "promoted": "Promoted", "close call": "Close call",
           "signal before costs": "Signal before costs", "open": "Other open ideas"}
_PAUSE = re.compile(r"paused: .*; until (\d{4}-\d\d-\d\d \d\d:\d\d) UTC\s*$")


def state_dir(pc) -> Path:
    return context.ROOT / pc.state_dir        # an absolute state_dir wins


def session_running(state: Path) -> bool:
    """A running session holds session.lock; probe it without taking it (and without creating the file)."""
    try:
        f = open(state / "session.lock")
    except OSError:
        return False
    with f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False                           # closing the file releases the probe's lock


def sleeping_until(state: Path, sid: int | None, now: datetime | None = None) -> datetime | None:
    """The time of the digest's last line, when that line is a pause still in the future."""
    path = state / "sessions" / f"{sid}.md"
    if sid is None or not path.exists():
        return None
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    m = _PAUSE.search(lines[-1]) if lines else None
    if m is None:
        return None
    until = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    return until if until > (now or datetime.now(timezone.utc)) else None


def session_state(record: Record, state: Path) -> str:
    sid = record.last_session_id()
    if session_running(state):
        until = sleeping_until(state, sid)
        return f"sleeping until {until:%Y-%m-%d %H:%M} UTC" if until else "running"
    if sid is None:
        return "no session yet"
    return f"stopped: {record.session(sid).get('stop_reason') or 'unknown (never ended)'}"


def _fmt(x, pct=False, bp=False) -> str:
    if not isinstance(x, (int, float)) or isinstance(x, bool):
        return "n/a"
    return f"{x:.1%}" if pct else f"{x:+,.1f} bp" if bp else f"{x:,.3f}"


def review_checks(verdict: dict | None) -> dict[str, str]:
    """Criterion 6 (the research note): from the verdict's diagnostics; a field that is absent reads n/a."""
    diag = (verdict or {}).get("diagnostics") or {}
    sides, breadth = diag.get("sides") or {}, diag.get("breadth") or {}
    return {"long share": _fmt(sides.get("long_share"), pct=True),
            "long net per trade": _fmt(sides.get("long_net_bp"), bp=True),
            "short net per trade": _fmt(sides.get("short_net_bp"), bp=True),
            "top coin share of net": _fmt(breadth.get("top_coin_share"), pct=True),
            "top fold share of net": _fmt(breadth.get("top_fold_share"), pct=True)}


def _go_to_detail(plugin: str, tf: str) -> None:
    st.session_state["page"] = "Strategy detail"
    st.session_state["detail_pick"] = f"{plugin} @ {tf}"


def _broken(p: dict) -> bool:
    return p.get("state") == "broken" or (p.get("judge_exit") is not None and piles.pile(p.get("verdict")) == "broken")


def _proposal(cfg, p: dict, wf: set[tuple[str, str]]) -> None:
    verdict = p.get("verdict")
    pile = piles.pile(verdict) if p.get("judge_exit") is not None else p.get("state", "?")
    dup = f" · duplicate of {p['duplicate_of']}" if p.get("duplicate_of") is not None else ""
    st.markdown(f"**#{p['seq']:04d} {p.get('plugin') or 'no plugin'} @ {p.get('timeframe') or '?'}** · "
                f"{p.get('kind') or '?'} · {pile}{dup}")
    if p.get("source"):
        st.code(p["source"], language="python")
    if verdict:
        st.json({k: v for k, v in verdict.items() if k != "diagnostics"}, expanded=False)
        if verdict.get("diagnostics"):
            st.caption("diagnostics")
            st.json(verdict["diagnostics"], expanded=False)
    plugin, tf = p.get("plugin"), p.get("timeframe")
    if (plugin, tf) in wf:
        st.button("Walk-forward run detail", key=f"detail_{p['seq']}", on_click=_go_to_detail, args=(plugin, tf))
    if pile == "promoted":
        _review_packet(cfg, p)


def _review_packet(cfg, p: dict) -> None:
    st.markdown("**Review packet**")
    for name, value in review_checks(p.get("verdict")).items():
        st.markdown(f"- {name}: {value}")
    j = db.get_judgement(cfg, p["hash"], p["timeframe"]) if p.get("hash") and p.get("timeframe") else None
    if j is None:
        st.warning("No judgement found in lab.db for this plugin version and timeframe: no exam command to show.")
    else:
        st.code(f"lab exam --id {j['id']}", language="bash")


# -- the human's changes: each a callback, so the page that follows already shows the record they leave
def _card_input(name: str, tp) -> None:
    """One field of the contract's IdeaCard, so a field the contract gains shows up here (ADR 0007)."""
    label, key = name.replace("_", " ").capitalize(), f"new_{name}"
    doc = contract.FIELD_DOCS.get(f"IdeaCard.{name}")
    if name == "columns":
        st.multiselect(label, context.PLUGIN_COLUMNS, key=key, help=doc)
    elif tp is float:
        st.number_input(label, min_value=0.0, step=1.0, value=None, key=key, help=doc)
    elif name == "title":
        st.text_input(label, key=key, help=doc)
    elif tp is str:
        st.text_area(label, key=key, help=doc)
    else:
        raise TypeError(f"no form input for IdeaCard.{name} ({tp!r})")


def _add(record: Record, pc) -> None:
    entry = {f.name: st.session_state[f"new_{f.name}"] for f in fields(contract.IdeaCard)}
    try:
        i = human.add(record, pc, {**entry, "timeframe": st.session_state["new_timeframe"]})
    except human.Refused as e:
        st.session_state["new_errors"] = e.errors
        return
    st.session_state["new_added"] = f"Added idea #{i}"
    for name, tp in typing.get_type_hints(contract.IdeaCard).items():      # a blank form for the next one
        st.session_state[f"new_{name}"] = [] if name == "columns" else None if tp is float else ""


def _add_form(record: Record | None, pc) -> None:
    with st.expander("Add an idea"):
        if record is None or not record.seeded():
            st.info(human.NOT_SEEDED)
            return
        st.warning(human.WARNING)
        hints = typing.get_type_hints(contract.IdeaCard)
        with st.form("add_idea"):
            for f in fields(contract.IdeaCard):
                _card_input(f.name, hints[f.name])
            st.selectbox("Timeframe", pc.timeframes, key="new_timeframe")
            st.form_submit_button("Add idea", on_click=_add, args=(record, pc))
        for e in st.session_state.pop("new_errors", []):
            st.error(e)
        if added := st.session_state.pop("new_added", None):
            st.success(added)


def _act(i: int, done: str, change, *args) -> None:
    try:
        change(*args)
    except ValueError as e:
        st.session_state[f"idea_error_{i}"] = str(e)
        return
    st.session_state["ideas_toast"] = done


def _exclude(record: Record, i: int) -> None:
    _act(i, f"Excluded idea #{i}", record.exclude_idea, i, st.session_state.get(f"exclude_reason_{i}", ""))


def _controls(record: Record, pc, idea: dict) -> None:
    i, status = idea["id"], idea["status"]
    if idea.get("next_up") is not None:
        c = st.columns([5, 1])
        c[0].markdown("**next up**: the builder's next round is asked to work on this idea")
        c[1].button("Clear", key=f"clear_next_{i}", on_click=_act, args=(i, "Next up cleared", record.clear_next, i))
    elif status == "open":
        if why := human.why_not_next(record, pc, idea):
            st.caption(f"Try next: {why}")
        else:
            st.button("Try next", key=f"try_next_{i}", on_click=_act,
                      args=(i, f"Idea #{i} is next up", human.flag_next, record, pc, i))
    if status in EXCLUDABLE and st.checkbox("Exclude this idea", key=f"exclude_open_{i}"):
        st.warning(human.WARNING)
        st.text_input("Why: the builder reads this reason", key=f"exclude_reason_{i}")
        st.button("Exclude", key=f"exclude_{i}", on_click=_exclude, args=(record, i))
    if err := st.session_state.pop(f"idea_error_{i}", None):
        st.error(err)


def _idea(cfg, record: Record, pc, idea: dict, props: list[dict], wf: set[tuple[str, str]]) -> None:
    st.markdown(f"#### {idea['title']}")
    st.caption(f"idea {idea['id']} · {idea['origin']} · {idea.get('timeframe') or '?'} · {idea['status']}"
               + (f" · excluded: {idea['excluded_reason']}" if idea.get("excluded_reason") else ""))
    _controls(record, pc, idea)
    for k, v in (idea.get("card") or {}).items():
        st.markdown(f"**{k}:** {v}")
    for p in props:
        if not _broken(p):
            _proposal(cfg, p, wf)


def _wf_runs(cfg) -> set[tuple[str, str]]:
    runs = db.read_table(cfg, db.runs)
    runs = runs[runs.validation_mode == "walkforward"]
    return set(zip(runs.strategy, runs.timeframe))


def page_ideas(cfg) -> None:
    try:
        pc = settings.load(cfg)
    except settings.StartRefused as e:
        st.info(f"The proposer is not configured: {e}")
        return
    state = state_dir(pc)
    if not (state / "record.db").exists():
        st.info(f"No proposer record yet ({state / 'record.db'} does not exist). `lab propose` starts one.")
        _add_form(None, pc)
        return
    record = Record(state)
    if toast := st.session_state.pop("ideas_toast", None):
        st.toast(toast)
    n, _ = db.trial_stats(cfg)
    tin, tout = record.usage_on(datetime.now(timezone.utc).date())
    c = st.columns(4)
    c[0].metric("Proposer trials", f"{record.trials_used()} / {pc.trials_total}")
    c[1].metric("N store-wide", n)
    c[1].caption(f"bar at N: Sharpe {dsr.sharpe_bar(max(n, 1)):.2f}")
    c[2].metric("Session", session_state(record, state))
    c[3].metric("Tokens today", f"{tin:,} in / {tout:,} out")
    _add_form(record, pc)

    ideas = record.ideas()
    by_idea = {i["id"]: record.proposals(i["id"]) for i in ideas}
    groups: dict[str, list[dict]] = {g: [] for g in piles.GROUPS}
    for i in ideas:
        groups[piles.idea_group(i, by_idea[i["id"]])].append(i)
    wf = _wf_runs(cfg)
    if not ideas:
        st.info("The record has no ideas yet.")
    for g in piles.GROUPS:
        if not groups[g]:
            continue
        if g == "closed":
            with st.expander(f"Closed ({len(groups[g])}): dead, exhausted, failed, duplicate, excluded"):
                for i in groups[g]:
                    _idea(cfg, record, pc, i, by_idea[i["id"]], wf)
        else:
            st.subheader(f"{_TITLES[g]} ({len(groups[g])})")
            for i in groups[g]:
                _idea(cfg, record, pc, i, by_idea[i["id"]], wf)
    titles = {i["id"]: i["title"] for i in ideas}
    broken = [p for ps in by_idea.values() for p in ps if _broken(p)]
    if broken:
        with st.expander(f"Broken attempts ({len(broken)})"):
            for p in sorted(broken, key=lambda p: p["seq"]):
                st.markdown(f"**#{p['seq']:04d} {p.get('plugin') or 'no plugin'} @ {p.get('timeframe') or '?'}** · "
                            f"{titles.get(p['idea_id'], '?')} · {p.get('broken_reason') or p.get('state')}")

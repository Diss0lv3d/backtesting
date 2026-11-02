"""The Ideas page: the proposer's record for the human (spec, "Where the user reads the record").

Reads record.db, the session digest and lock, and lab.db. It writes only record.db: on the human's action (an idea
card of their own, an exclusion, the "try next" flag, or a stop item added or removed, each checked in code in
human.py and Record, as `lab idea` and `lab stop` do), and once more on its first load of a record older than the
tables and columns added since, when opening the record adds them (Record: a missing table such as stops, and the
column migration's next_up and web_reviewed on ideas and referee on research). It never creates the state dir.
The grouping is `piles.idea_group`, the rule `lab ideas` prints by.

A research round's referee could read academic papers, some maybe published after the model's training cutoff
(referee-and-human-stops spec, "Anti-goals"): an idea it reviewed carries a "web-reviewed" tag, and a promoted one's
review packet says its exam is weaker evidence. A page it read could also have put anything in its text, and that
text in a model's: every string the code didn't write is shown escaped and defanged (`_text`), never read as
markdown or linked, and the only links the page makes are to the referee's allowed hosts (`_link`).
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
from perp_lab.proposer import context, contract, human, model, piles, settings
from perp_lab.proposer.record import EXCLUDABLE, Record
from perp_lab.proposer.research import WEB_REVIEWED_CAVEAT
from perp_lab.store import db

_TITLES = {"passed": "Passed", "promoted": "Promoted", "close call": "Close call",
           "signal before costs": "Signal before costs", "open": "Other open ideas"}
# The digest's own line shape (digest.pause): top level, so a model-written summary indented under a research line
# can't match.
_PAUSE = re.compile(r"^- \d\d:\d\d · paused: .*; until (\d{4}-\d\d-\d\d \d\d:\d\d) UTC\s*$")
_DECIDED = {"accept": "accepted", "revise": "revised", "reject": "rejected"}
# Every ASCII punctuation character. CommonMark lets each be backslash-escaped and shows it as itself, so text
# escaped so reads as written: no link, image, HTML, emphasis, code, heading, list or table, and none of Streamlit's
# own markup ($ LaTeX, :colour[...], :emoji:).
_MD_PUNCT = re.compile(r"([!-/:-@\[-`{-~])")
# What GFM links in text as shown, after the escapes are read (Streamlit's frontend does; a real browser showed it,
# 2026-11-02): a URL, a www. host, an email address. Each is broken visibly, so no autolink can form: `://` becomes
# `[:]//` (every scheme), `www.` `www[.]` (any case), `@` `[@]`. Hosts keep their dots: the text stays readable.
_AUTOLINK = re.compile(r"://|(?i:www)\.|@")
_DEFANGED = {"://": "[:]//", "@": "[@]"}
_MD_DEST = re.compile(r"([()<>\\])")               # what would end or break a link's destination


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
    m = _PAUSE.match(lines[-1]) if lines else None
    if m is None:
        return None
    try:
        until = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    except ValueError:                                  # a date no calendar has
        return None
    return until if until > (now or datetime.now(timezone.utc)) else None


def session_state(record: Record, state: Path, running: bool) -> str:
    sid = record.last_session_id()
    if running:
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


def _esc(s: str) -> str:
    return _MD_PUNCT.sub(r"\\\1", s)


def _defang(s: str) -> str:
    return _AUTOLINK.sub(lambda m: _DEFANGED.get(m.group(), m.group()[:-1] + "[.]"), s)


def _text(s: str, lines: bool = False) -> str:
    """A string the code didn't write (a model's, a card's, a judge's reason, a URL off the allowlist), for
    st.markdown, st.caption or st.error: shown as written, never read as markup, with every URL, www. host and email
    defanged so none becomes a link. Its whitespace collapses to one line; with `lines`, its line breaks stay, each
    line stripped (so none is an indented code block or ends in a hard break)."""
    if lines:
        return "\n".join(_esc(_defang(ln.strip())) for ln in s.splitlines())
    return _esc(_defang(" ".join(s.split())))


def _head(p: dict) -> str:
    """A proposal's plugin and timeframe, as its lines start."""
    plugin, tf = _text(str(p.get("plugin") or "no plugin")), _text(str(p.get("timeframe") or "?"))
    return f"**#{p['seq']:04d} {plugin} @ {tf}**"


def _proposal(cfg, p: dict, wf: set[tuple[str, str]], web_reviewed: bool = False) -> None:
    verdict = p.get("verdict")
    pile = piles.pile(verdict) if p.get("judge_exit") is not None else p.get("state", "?")
    dup = f" · duplicate of {p['duplicate_of']}" if p.get("duplicate_of") is not None else ""
    st.markdown(f"{_head(p)} · {_text(str(p.get('kind') or '?'))} · {_text(str(pile))}{dup}")
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
        _review_packet(cfg, p, web_reviewed)


def _review_packet(cfg, p: dict, web_reviewed: bool = False) -> None:
    st.markdown("**Review packet**")
    if web_reviewed:
        st.warning(WEB_REVIEWED_CAVEAT)
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


def _stop_done(done: str) -> None:
    st.session_state["ideas_toast"] = done


def _stop_add(record: Record) -> None:
    try:
        i = record.add_stop(st.session_state["new_stop"])
    except ValueError as e:
        st.session_state["stop_error"] = str(e)
        return
    _stop_done(f"Stop #{i} added")
    st.session_state["new_stop"] = ""                     # a blank box for the next one


def _stop_remove(record: Record, i: int) -> None:
    try:
        record.remove_stop(i)
    except ValueError as e:
        st.session_state["stop_error"] = str(e)
        return
    _stop_done(f"Stop #{i} removed")


def _stop_list(record: Record) -> None:
    """The human's stop list (spec: referee-and-human-stops, "The human's stop list"). Unlike an idea, an item needs no
    seed: it adds nothing the seed check could miss."""
    with st.expander("Your stop list"):
        st.warning(human.WARNING)
        try:
            _stop_body(record)
        except Exception:                                # noqa: BLE001 - a bad row must not take the page down
            st.caption("Stop list unreadable")
        if err := st.session_state.pop("stop_error", None):
            st.caption(err)


def _stop_body(record: Record) -> None:
    stops = record.stops()
    if not stops:
        st.caption("Nothing ruled out yet.")
    for s in stops:
        c = st.columns([8, 1])
        c[0].markdown(f"#{s['id']} {_text(s['text'])}")      # the human's, maybe pasted from a page they read
        c[1].button("Remove", key=f"remove_stop_{s['id']}", on_click=_stop_remove, args=(record, s["id"]))
    st.text_input("Rule out a direction (one line)", key="new_stop")
    st.button("Add", key="add_stop", on_click=_stop_add, args=(record,))


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
            st.caption(f"Try next: {_text(why)}")          # it names the idea by its title
        else:
            st.button("Try next", key=f"try_next_{i}", on_click=_act,
                      args=(i, f"Idea #{i} is next up", human.flag_next, record, pc, i))
    if status in EXCLUDABLE and st.checkbox("Exclude this idea", key=f"exclude_open_{i}"):
        st.warning(human.WARNING)
        st.text_input("Why: the builder reads this reason", key=f"exclude_reason_{i}")
        st.button("Exclude", key=f"exclude_{i}", on_click=_exclude, args=(record, i))
    if err := st.session_state.pop(f"idea_error_{i}", None):
        st.error(_text(err))


def _idea(cfg, record: Record, pc, idea: dict, props: list[dict], wf: set[tuple[str, str]]) -> None:
    st.markdown(f"#### {_text(idea['title'])}")
    web = bool(idea.get("web_reviewed"))
    st.caption(f"idea {idea['id']} · {idea['origin']} · {idea.get('timeframe') or '?'} · {idea['status']}"
               + (" · web-reviewed" if web else "")
               + (f" · excluded: {_text(idea['excluded_reason'])}" if idea.get("excluded_reason") else ""))
    _controls(record, pc, idea)
    for k, v in (idea.get("card") or {}).items():
        st.markdown(f"**{_text(str(k))}:** {_text(str(v), lines=True)}")
    for p in props:
        if not _broken(p):
            _proposal(cfg, p, wf, web)


def _lines(x) -> list[str]:
    """A list of one-line strings from a stored reply field; anything else (an invalid round keeps the raw reply)
    reads as empty."""
    return [str(v) for v in x] if isinstance(x, list) else []


def _research_body(record: Record, pc, row: dict, running: bool) -> None:
    ok = row.get("outcome") in ("ok", "capped")      # only a valid note's list reaches the builder
    outcome = row["outcome"]
    # The loop writes a round's row first, as interrupted, and its outcome at the round's end: the running
    # session's own round is under way (a call, or a usage-limit wait, can take hours).
    if outcome == "error: interrupted" and running and row.get("session_id") == record.last_session_id():
        outcome = "under way"
    st.markdown(f"**{row['at']:%Y-%m-%d %H:%M} UTC** · {outcome} · at {row['trials_used']} trials")
    note = row.get("reply") if ok else (record.last_ok_research() or {}).get("reply")
    note = note if isinstance(note, dict) else {}
    if ok and isinstance(note.get("summary"), str) and note["summary"]:
        st.markdown(f"**Summary:** {_text(note['summary'], lines=True)}")
    if pc.research_every == 0:                       # the loop's rule: research off, no research stop list in force
        st.markdown("**Research is off: the research step's stop list is not in force** (`proposer.research_every` is 0)")
    elif stop := _lines(note.get("stop")):
        st.markdown("**Stop list** (the builder is told to stop trying these)" if ok else
                    "**Stop list in force** (from the latest valid round)")
        for line in stop:
            st.markdown(f"- {_text(line)}")
    added = row.get("added") if ok else None
    ids = [i for i in added if isinstance(i, int) and not isinstance(i, bool)] if isinstance(added, list) else []
    titles = {i["id"]: i["title"] for i in record.ideas()} if ids else {}
    if isinstance(row.get("referee"), dict):
        _referee(row["referee"], ids, titles, pc)
    if ids:
        # what each builds on, for the human only: in the round's reply, by the title the record keeps it under
        cards = note.get("ideas") if isinstance(note.get("ideas"), list) else []
        builds = {c["title"].strip().casefold(): " ".join(c["builds_on"].split()) for c in cards
                  if isinstance(c, dict) and isinstance(c.get("title"), str) and isinstance(c.get("builds_on"), str)}
        st.markdown("**Ideas added**")
        for i in ids:
            title = titles.get(i, "?")
            on = builds.get(title.casefold())
            st.markdown(f"- #{i} {_text(title)}" + (f" · builds on: {_text(on)}" if on else ""))


def _one(x) -> str:
    """A field that should be a one-line string, escaped; anything else reads ?."""
    return _text(x) if isinstance(x, str) else "?"


def _link(url: str, pc) -> str:
    """A URL the referee named or fetched: a link only when its host is on the referee's allowlist (the fence's rule,
    model.on_allowlist), defanged text otherwise, so the page links no host the referee was not allowed to read. A
    link's own text needs no defanging: GFM makes no link inside a link."""
    if not model.on_allowlist(url, pc.referee_domains):
        return _text(url)
    dest = _MD_DEST.sub(r"\\\1", url)
    return f"[{_esc(url)}]({dest})"


def _referee(ref: dict, ids: list[int], titles: dict[int, str], pc) -> None:
    """The referee's review of the round's cards (referee-and-human-stops spec, "Ideas page"): each card's verdict
    with its reason and sources, and the idea an accepted or revised card became; or its error, when no card was
    added (it fails closed). Then the fetches its calls issued and those the CLI denied. The verdicts pair in order
    with the round's added ids (loop._referee_row); when their counts differ, no verdict names an idea. Like the
    reply, nothing in the row is trusted to have a shape."""
    web = "with academic web access" if ref.get("web") is True else "without web access"
    if ref.get("error") is not None:
        st.markdown(f"**Referee failed** · {web}: {_one(ref['error'])}. No card from this round was added.")
    elif isinstance(ref.get("verdicts"), list):
        verdicts = [v for v in ref["verdicts"] if isinstance(v, dict)]
        n = {d: sum(v.get("decision") == d for v in verdicts) for d in _DECIDED}
        st.markdown(f"**Referee** · {len(verdicts)} {'card' if len(verdicts) == 1 else 'cards'}: "
                    f"{n['accept']} accepted, {n['revise']} revised, {n['reject']} rejected · {web}")
        kept = sum(v.get("decision") in ("accept", "revise") for v in verdicts)
        pair = iter(ids) if kept == len(ids) else None
        for v in verdicts:
            d = v.get("decision")
            line = f"- {_one(v.get('title'))}: **{_DECIDED.get(d, '?') if isinstance(d, str) else '?'}**"
            if pair is not None and d in ("accept", "revise"):
                i = next(pair)
                line += f" as #{i}" if d == "accept" else f" into #{i} {_text(titles.get(i, '?'))}"
            if isinstance(v.get("reason"), str):
                line += f" · {_one(v['reason'])}"
            if sources := _lines(v.get("sources")):
                line += " · sources: " + ", ".join(_link(u, pc) for u in sources)
            st.markdown(line)
    lists = (("fetches", _lines(ref.get("fetches"))), ("denied by the CLI", _lines(ref.get("denied"))))
    parts = [f"{what} ({len(urls)}): " + ", ".join(_link(u, pc) for u in urls) for what, urls in lists if urls]
    if parts:
        st.caption(" · ".join(parts))


def _research_notes(record: Record, pc, running: bool) -> None:
    """The latest research round (spec: research-round-design, Where the human sees it); absent before the first.
    A refused round keeps the model's raw reply, so nothing in it is trusted to have a shape."""
    row = record.last_research()
    if row is None:
        return
    with st.expander("Research notes"):
        try:
            _research_body(record, pc, row, running)
        except Exception:                                # noqa: BLE001 - a bad note must not take the page down
            st.caption("Research notes unreadable")


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
    running = session_running(state)               # one probe: the header and the research notes agree
    c[2].metric("Session", session_state(record, state, running))
    c[3].metric("Tokens today", f"{tin:,} in / {tout:,} out")
    _research_notes(record, pc, running)
    _stop_list(record)
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
                st.markdown(f"{_head(p)} · {_text(str(titles.get(p['idea_id'], '?')))} · "
                            f"{_text(str(p.get('broken_reason') or p.get('state')))}")

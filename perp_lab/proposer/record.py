"""The proposer's record: its own SQLite file, never lab.db.

The record is the model's only memory and what the human reads on the Ideas page, so it keeps everything:
sessions, ideas, proposals, model calls and judge calls, plus one transcript file per proposal. Errors propagate on
purpose: the loop fails closed rather than run on a record it could not write. Times here are for the human
(Ideas page, digest) and are never rendered into a prompt.
"""

from __future__ import annotations

import threading
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import sqlalchemy as sa

md = sa.MetaData()
BUSY_TIMEOUT_MS = 30_000
EXCLUDABLE = ("open", "exhausted", "dead")    # never passed or failed: an exam's outcome stays on the record


def _pragmas(dbapi_conn, _record) -> None:
    """On every connection: the Ideas page reads the record while a session writes it. In WAL mode readers and the
    writer don't block each other, and a brief lock is waited out instead of raising "database is locked", which
    would end the session (M-12)."""
    cur = dbapi_conn.cursor()
    cur.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    cur.execute("PRAGMA journal_mode = WAL")
    cur.close()


class UTCDateTime(sa.TypeDecorator):
    """SQLite drops tzinfo; store naive UTC and hand back aware UTC so a "UTC day" is unambiguous."""

    impl = sa.DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("record timestamps must be timezone-aware")
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        return None if value is None else value.replace(tzinfo=timezone.utc)


sessions = sa.Table(
    "sessions", md,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("started_at", UTCDateTime, nullable=False),
    sa.Column("ended_at", UTCDateTime),
    sa.Column("model", sa.String(128)),
    sa.Column("cutoff", sa.String(16)),
    sa.Column("cli_version", sa.String(64)),
    sa.Column("commit", sa.String(64)),
    sa.Column("branch", sa.String(128)),
    sa.Column("caps", sa.JSON),
    sa.Column("trials_used", sa.Integer),
    sa.Column("tokens_in", sa.Integer),
    sa.Column("tokens_out", sa.Integer),
    sa.Column("stop_reason", sa.Text),
)

ideas = sa.Table(
    "ideas", md,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("title", sa.String(256), nullable=False),
    sa.Column("card", sa.JSON),
    sa.Column("origin", sa.String(16), nullable=False),     # seed | proposer | human
    sa.Column("timeframe", sa.String(8)),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("port_of", sa.Integer, sa.ForeignKey("ideas.id")),
    sa.Column("family", sa.String(128)),
    sa.Column("root_plugin", sa.String(128)),
    sa.Column("excluded_reason", sa.Text),
    sa.Column("created_at", UTCDateTime, nullable=False),
    # When the human asked for the idea to be tried next; one idea at most. A time, so never in a prompt.
    sa.Column("next_up", UTCDateTime),
    sa.Index("ux_ideas_title_nocase", sa.func.lower(sa.column("title")), unique=True),
)

proposals = sa.Table(
    "proposals", md,
    sa.Column("seq", sa.Integer, primary_key=True, autoincrement=False),
    sa.Column("session_id", sa.Integer, sa.ForeignKey("sessions.id")),
    sa.Column("idea_id", sa.Integer, sa.ForeignKey("ideas.id"), index=True),
    sa.Column("origin", sa.String(16), nullable=False, default="proposer"),   # seed | proposer
    sa.Column("kind", sa.String(16)),
    sa.Column("plugin", sa.String(128)),
    sa.Column("timeframe", sa.String(8)),
    sa.Column("hash", sa.String(64)),
    sa.Column("source", sa.Text),
    sa.Column("preflight", sa.JSON),
    sa.Column("judge_exit", sa.Integer),
    sa.Column("judged_at", UTCDateTime),
    sa.Column("verdict", sa.JSON),
    sa.Column("pile", sa.String(32)),
    sa.Column("duplicate_of", sa.Integer),
    sa.Column("state", sa.String(16), nullable=False),      # dropped|staged|copied|judged|broken
    sa.Column("broken_reason", sa.Text),
    sa.Column("tokens_in", sa.Integer),
    sa.Column("tokens_out", sa.Integer),
    sa.Column("created_at", UTCDateTime, nullable=False),
    # Ideas own runs, not plugin names: a seed plugin runs at two timeframes under two ideas (an idea and its
    # port), so a run (plugin, timeframe) is unique. A missing timeframe counts as one value, never as many.
    sa.Index("ux_proposals_plugin_tf", sa.column("plugin"), sa.func.coalesce(sa.column("timeframe"), ""),
             unique=True),
    # A seeded human trial never counts toward trials_total, so a misspelt origin must not pass for one.
    sa.CheckConstraint("origin IN ('seed', 'proposer')", name="ck_proposals_origin"),
)

calls = sa.Table(
    "calls", md,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("session_id", sa.Integer, sa.ForeignKey("sessions.id")),
    sa.Column("seq", sa.Integer),
    sa.Column("at", UTCDateTime, nullable=False, index=True),
    sa.Column("tokens_in", sa.Integer),
    sa.Column("tokens_out", sa.Integer),
    sa.Column("cost_usd", sa.Float),
    sa.Column("outcome", sa.String(64)),
)

# One row per `lab judge` call: a judge exit 1 is retried on the same bytes, and the day's quota counts calls,
# not proposals (R45; spec, Pace and budget). The seeded human trials have none.
judge_calls = sa.Table(
    "judge_calls", md,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("seq", sa.Integer, sa.ForeignKey("proposals.seq"), nullable=False),
    sa.Column("at", UTCDateTime, nullable=False, index=True),
    sa.Column("exit", sa.Integer),
)


# Columns added after a table first shipped: create_all never adds a column to an existing table, and the user's
# record.db predates these.
_ADDED_COLUMNS: list[tuple[str, str, str]] = [
    ("ideas", "next_up", "DATETIME"),
]


def _has_column(eng: sa.Engine, table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(eng).get_columns(table)}


def _add_columns(eng: sa.Engine) -> None:
    for table, column, ddl in _ADDED_COLUMNS:
        if not _has_column(eng, table, column):
            try:
                with eng.begin() as c:
                    c.execute(sa.text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
            except sa.exc.OperationalError:
                if not _has_column(eng, table, column):   # another process added it meanwhile: fine
                    raise


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _day(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(0), tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


def _lookahead_kill(verdict: dict | None) -> bool:
    reasons = (verdict or {}).get("reasons") or []
    return any(str(r).startswith("lookahead:") for r in reasons)


class Record:
    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "transcripts").mkdir(exist_ok=True)
        self.engine = sa.create_engine(f"sqlite:///{self.state_dir / 'record.db'}", future=True)
        sa.event.listen(self.engine, "connect", _pragmas)
        md.create_all(self.engine)
        _add_columns(self.engine)
        # High-water mark of reserved sequence numbers: above every recorded proposal and every transcript
        # already on disk, so a number handed out once is never handed out again, even after a crash.
        self._lock = threading.Lock()
        with self.engine.connect() as c:
            top = c.execute(sa.select(sa.func.max(proposals.c.seq))).scalar() or 0
        for f in (self.state_dir / "transcripts").glob("*.jsonl"):
            if f.stem.isdigit():
                top = max(top, int(f.stem))
        self._seq = top

    # -- helpers
    def _insert(self, table: sa.Table, values: dict) -> int:
        with self.engine.begin() as c:
            return c.execute(sa.insert(table).values(**values)).inserted_primary_key[0]

    def _update(self, table: sa.Table, key, pk, values: dict) -> None:
        with self.engine.begin() as c:
            n = c.execute(sa.update(table).where(key == pk).values(**values)).rowcount
        if n != 1:
            raise KeyError(f"no {table.name} row {pk}")

    def _get(self, table: sa.Table, key, pk) -> dict:
        with self.engine.connect() as c:
            row = c.execute(sa.select(table).where(key == pk)).mappings().first()
        if row is None:
            raise KeyError(f"no {table.name} row {pk}")
        return dict(row)

    # -- sessions
    def start_session(self, **f) -> int:
        f.setdefault("started_at", _now())
        return self._insert(sessions, f)

    def end_session(self, sid: int, stop_reason: str, **f) -> None:
        f.setdefault("ended_at", _now())
        self._update(sessions, sessions.c.id, sid, {**f, "stop_reason": stop_reason})

    def last_session_id(self) -> int | None:
        with self.engine.connect() as c:
            return c.execute(sa.select(sa.func.max(sessions.c.id))).scalar()

    def session(self, sid: int) -> dict:
        return self._get(sessions, sessions.c.id, sid)

    # -- ideas
    def add_idea(self, title: str, card: dict, origin: str, timeframe: str, *,
                 port_of: int | None = None, status: str = "open") -> int:
        return self.add_ideas([{"title": title, "card": card, "origin": origin, "timeframe": timeframe,
                                "port_of": port_of, "status": status}])[0]

    def add_ideas(self, rows: list[dict]) -> list[int]:
        """Add the ideas in one transaction, all or none: a title any idea has, whatever the case, refuses them
        all. Each row has title, card, origin and timeframe, and may have port_of and status (default open)."""
        now, out = _now(), []
        with self.engine.begin() as c:
            taken = {t.casefold() for (t,) in c.execute(sa.select(ideas.c.title))}
            for row in rows:
                title = row["title"]
                if title.casefold() in taken:
                    raise ValueError(f"duplicate idea title: {title!r}")
                taken.add(title.casefold())
                try:
                    out.append(c.execute(sa.insert(ideas).values(
                        **{"port_of": None, "status": "open", **row}, created_at=now)).inserted_primary_key[0])
                except sa.exc.IntegrityError as e:
                    raise ValueError(f"duplicate idea title: {title!r}") from e
        return out

    def ideas(self) -> list[dict]:
        with self.engine.connect() as c:
            return [dict(r) for r in c.execute(sa.select(ideas).order_by(ideas.c.id)).mappings()]

    def idea(self, id: int) -> dict:
        return self._get(ideas, ideas.c.id, id)

    def seeded(self) -> bool:
        with self.engine.connect() as c:
            return c.execute(sa.select(ideas.c.id).where(ideas.c.origin == "seed").limit(1)).first() is not None

    def set_idea(self, id: int, **f) -> None:
        if "title" in f:
            raise ValueError("an idea's title is fixed")
        if f.get("status", "open") != "open":
            f["next_up"] = None             # only an open idea can be tried next
        self._update(ideas, ideas.c.id, id, f)

    def exclude_idea(self, id: int, reason: str) -> None:
        """Close an open, exhausted or dead idea for good, by the human. The model reads the reason (prompt._why)."""
        reason = (reason or "").strip()
        if not reason:
            raise ValueError("an exclusion needs a reason")
        if len(reason.splitlines()) > 1:             # the idea's closed entry in the prompt is one line
            raise ValueError("an exclusion reason must be one line")
        with self.engine.begin() as c:              # the write first, as in flag_next
            if c.execute(sa.update(ideas).where(ideas.c.id == id, ideas.c.status.in_(EXCLUDABLE))
                         .values(status="excluded", excluded_reason=reason, next_up=None)).rowcount:
                return
        raise ValueError(self._why_not(id, "only an open, exhausted or dead idea can be excluded"))

    # -- the human's "try next"
    def flag_next(self, id: int) -> None:
        """Ask for an open idea to be tried next, in place of any other: one idea is flagged at a time."""
        # The write first, conditional on the status: a session may write the record meanwhile, and SQLite opens
        # no transaction on a read, so a status read first could be stale by the write.
        with self.engine.begin() as c:
            if c.execute(sa.update(ideas).where(ideas.c.id == id, ideas.c.status == "open")
                         .values(next_up=_now())).rowcount:
                c.execute(sa.update(ideas).where(ideas.c.id != id, ideas.c.next_up.is_not(None)).values(next_up=None))
                return
        raise ValueError(self._why_not(id, "only an open idea can be tried next"))

    def _why_not(self, id: int, rule: str) -> str:
        try:
            status = self.idea(id)["status"]
        except KeyError:
            return f"there is no idea #{id}"
        return f"idea #{id} is {status}; {rule}"

    def clear_next(self, id: int) -> None:
        """Clear the flag from this idea, if it holds it; never from another."""
        with self.engine.begin() as c:
            c.execute(sa.update(ideas).where(ideas.c.id == id, ideas.c.next_up.is_not(None)).values(next_up=None))

    def clear_all_next(self) -> None:
        """Clear the flag, whichever idea holds it: the human's own "clear"."""
        with self.engine.begin() as c:
            c.execute(sa.update(ideas).where(ideas.c.next_up.is_not(None)).values(next_up=None))

    def next_up(self) -> dict | None:
        """The flagged idea, whatever its status now."""
        with self.engine.connect() as c:
            row = c.execute(sa.select(ideas).where(ideas.c.next_up.is_not(None))).mappings().first()
        return None if row is None else dict(row)

    # -- proposals
    def next_seq(self) -> int:
        """Reserve the next number: two calls never return the same one, used or not."""
        with self._lock:
            self._seq += 1
            return self._seq

    def add_proposal(self, **f) -> int:
        if f.get("seq") is None:
            f["seq"] = self.next_seq()
        else:
            with self._lock:
                self._seq = max(self._seq, f["seq"])
        f.setdefault("created_at", _now())
        return self._insert(proposals, f)

    def update_proposal(self, seq: int, **f) -> None:
        self._update(proposals, proposals.c.seq, seq, f)

    def proposals(self, idea_id: int | None = None) -> list[dict]:
        q = sa.select(proposals).order_by(proposals.c.seq)
        if idea_id is not None:
            q = q.where(proposals.c.idea_id == idea_id)
        with self.engine.connect() as c:
            return [dict(r) for r in c.execute(q).mappings()]

    # -- the seed
    def add_seed(self, idea_rows: list[dict], proposal_rows: list[dict]) -> bool:
        """Add the seed's rows in one transaction, and only to a record with no ideas. A seed that fails part way
        leaves nothing behind, so the next start seeds again instead of running on half a record. Rows carry their
        own idea ids and seq numbers. False, with nothing added, when the record already has an idea."""
        now = _now()
        with self._lock, self.engine.begin() as c:
            if c.execute(sa.select(sa.func.count()).select_from(ideas)).scalar():
                return False
            for row in idea_rows:
                c.execute(sa.insert(ideas).values(created_at=now, **row))
            for row in proposal_rows:
                c.execute(sa.insert(proposals).values(created_at=now, **row))
            self._seq = max([self._seq, *(row["seq"] for row in proposal_rows)])
        return True

    # -- calls
    def add_call(self, **f) -> None:
        f.setdefault("at", _now())
        self._insert(calls, f)

    # -- judge calls
    def add_judge_call(self, **f) -> None:
        f.setdefault("at", _now())
        self._insert(judge_calls, f)

    # -- counts
    def usage_on(self, day: date) -> tuple[int, int]:
        lo, hi = _day(day)
        q = sa.select(sa.func.coalesce(sa.func.sum(calls.c.tokens_in), 0),
                      sa.func.coalesce(sa.func.sum(calls.c.tokens_out), 0)).where(calls.c.at >= lo, calls.c.at < hi)
        with self.engine.connect() as c:
            a, b = c.execute(q).one()
        return int(a), int(b)

    def judge_calls_on(self, day: date) -> int:
        """The `lab judge` calls made on a UTC day, retries included, whatever their exit code."""
        lo, hi = _day(day)
        q = sa.select(sa.func.count()).select_from(judge_calls).where(judge_calls.c.at >= lo,
                                                                      judge_calls.c.at < hi)
        with self.engine.connect() as c:
            return int(c.execute(q).scalar())

    def trials_used(self) -> int:
        """The proposer's own judge calls, except a refusal (exit 2) and a lookahead kill; an interrupted judge
        counts. Seeded human trials never count: trials_total is "for the proposer's own trials" (spec, Pace)."""
        q = sa.select(proposals.c.judge_exit, proposals.c.verdict).where(
            proposals.c.judge_exit.is_not(None), proposals.c.origin == "proposer")
        with self.engine.connect() as c:
            rows = c.execute(q).all()
        return sum(1 for code, verdict in rows if code != 2 and not (code == 0 and _lookahead_kill(verdict)))

    # -- paths
    def transcript_path(self, seq: int) -> Path:
        return self.state_dir / "transcripts" / f"{seq:04d}.jsonl"

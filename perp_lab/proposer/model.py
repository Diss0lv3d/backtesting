"""One model call: `claude -p` on the user's login, its stream read line by line and checked in code.

The flags ask the CLI for no tools, no MCP server and no CLAUDE.md, skills, plugins or hooks, but flags are
not trusted (ADR 0006): the stream's `init` message, which comes before the model's first turn, must show the
pinned model, `StructuredOutput` as the only tool, and no MCP server, skill or slash command, and a hook event
anywhere in the stream is refused. Either kills the call before any result is used and raises
ModelConfigError, which stops the session with `model_config`. The CLI runs in a fresh empty directory
outside the repo (no CLAUDE.md, memory or git state) with only HOME and PATH, and every stdout line goes to
the transcript as is.

The stream's shape is the real CLI's (2.1.316; tests/fixtures/claude_stream_ok.jsonl, ruling R26): system
messages such as `ui_invalidate` may come before init, and a rate limit is read from
`rate_limit_event.rate_limit_info` (`status`, `resetsAt` in epoch seconds), never from `overageStatus`. Only a
status other than `allowed` and `allowed_warning` is a limit (R29): the warning is a near-limit notice on a call
that is still served. An error result's `api_error_status` decides whether a retry can help (R42): a 4xx won't
change on a retry, so 429 is the usage limit and any other 4xx a ModelConfigError; the rest is retried.

A call given web domains (the referee's) is the one place web access enters the loop, so it is fenced twice.
The CLI's allowlist is the first layer: `--tools WebFetch` (never WebSearch, whose snippets come from any site),
one allow rule, `WebFetch(domain:...)`, per domain, `--setting-sources ""` so no user, project or local settings
(allow rules, a default mode) apply, and CLAUDE_CODE_EVAL_CONFINED=1 in the environment, which turns off the hosts
the CLI otherwise fetches with no rule (about 90 documentation sites; probe of 2026-11-02, R-RF3); with
`--permission-prompts none` it denies the rest. That variable is undocumented, so the layer can't be relied on:
the code fence is the guarantee. Init must list exactly `StructuredOutput` and `WebFetch` with `permissionMode`
"default", every WebFetch the model issues is recorded, and one off the allowlist is harmless only if the CLI
denied it: its tool_result, which the model reads next, must be the CLI's denial, and the result's
`permission_denials` must list it. Anything else is a FenceBreach: the call is killed before its result is used,
and it is not retried; the caller fails closed for that call and goes on (R-RF5). An init that fails the check is
still a ModelConfigError, and so is a WebFetch in a tool-less call: the CLI did not hold `--tools ""`.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import selectors
import shutil
import signal
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from perp_lab.proposer import proc

TOOLS = ["StructuredOutput"]  # the --json-schema mechanism; --tools "" removes every built-in
CONFINED = {"CLAUDE_CODE_EVAL_CONFINED": "1"}  # a web call's env: no preapproved hosts (undocumented; R-RF3)
WEB_TOOLS = ["StructuredOutput", "WebFetch"]  # a call with web domains: in any order, nothing else
DENIAL = "Permission for this tool use was denied."  # how the CLI's tool_result for a fetch it refused begins
# An allowed domain: two labels or more, nothing else. re.A, as in NETLOC: with re.I alone, [a-z] also matches
# four non-ASCII letters (U+0130, U+0131, U+017F, U+212A).
DOMAIN = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", re.I | re.A)
# A URL every parser reads alike: printable ASCII without space or backslash (a WHATWG parser, the CLI's, reads
# `\` as `/`, so `https://evil.com\@arxiv.org` is evil.com to it and arxiv.org to urlsplit), and a netloc that is a
# bare letters-digits-hyphens host with an optional port: no userinfo, percent escapes, IP literals or Unicode.
URL_CHARS = re.compile(r"[!-\[\]-~]+")
NETLOC = re.compile(r"([a-z0-9-]+(?:\.[a-z0-9-]+)*)\.?(?::[0-9]*)?", re.I | re.A)
EMPTY_IN_INIT = ("mcp_servers", "skills", "slash_commands")  # checked where present; plugins/agents aren't (R26)
SERVED = ("allowed", "allowed_warning")  # rate_limit_info statuses that are not a usage limit (R29)
BACKOFF_S = (30, 60, 120, 240)  # between the 5 tries
VERSION_TIMEOUT_S = 60
PROBE_SYSTEM = "Answer as JSON."
PROBE_USER = "Reply with ok true."
PROBE_SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
CHUNK = 65536
STDERR_TAIL = 300


@dataclass(frozen=True)
class Reply:
    proposal: dict
    usage: dict
    cost_usd: float | None
    fetches: list[str] = field(default_factory=list)  # every WebFetch URL the model issued, in order
    denied: list[str] = field(default_factory=list)  # those the CLI denied (the result's permission_denials)


class ModelConfigError(Exception):
    """The CLI is not set up as the spec requires, or the API refuses the call as made (a 4xx other than 429): no
    retry can help, and the session stops with `model_config`."""


class FenceBreach(Exception):
    """A web call's stream showed a fetch off the allowlist that the CLI did not deny, or a shape the fetch audit
    can't vouch for: the call was killed before its result was used. Not retried (R-RF5)."""


class UsageLimit(Exception):
    """The plan's usage limit is reached; the loop sleeps until `resets_at`, or backs off hourly if None."""

    def __init__(self, resets_at: datetime | None):
        super().__init__(f"usage limit reached, resets at {resets_at.isoformat() if resets_at else 'unknown'}")
        self.resets_at = resets_at


class ModelCallError(Exception):
    """The call failed in a way another try may fix: retryable."""


class _TimedOut(Exception):
    pass


def argv(pc, system: str, schema: dict, *, web_domains: tuple[str, ...] = ()) -> list[str]:
    """Without web domains, no tools at all. With them, WebFetch and one allow rule per domain, each its own argv
    element (the form the CLI accepts, probe of 2026-11-01); the next flag ends the list."""
    tools = [""]
    if web_domains:
        bad = [d for d in web_domains if not (isinstance(d, str) and DOMAIN.fullmatch(d))]
        if bad:  # a rule is the CLI's syntax: a domain that isn't a plain hostname could widen it
            raise ModelConfigError(f"not a hostname for the web allowlist: {bad[0]!r}")
        tools = ["WebFetch", "--allowedTools", *(f"WebFetch(domain:{d})" for d in web_domains),
                 "--setting-sources", ""]
    return [pc.claude_bin, "-p", "--model", pc.model, "--effort", pc.effort, "--tools", *tools,
            "--strict-mcp-config", "--safe-mode", "--disable-slash-commands", "--no-session-persistence",
            "--permission-prompts", "none", "--system-prompt", system, "--json-schema", json.dumps(schema),
            "--output-format", "stream-json", "--verbose", "--include-hook-events"]


def env(web: bool = False) -> dict[str, str]:
    return {"HOME": os.environ["HOME"], "PATH": os.environ["PATH"], **(CONFINED if web else {})}


@contextlib.contextmanager
def _workdir():
    d = tempfile.mkdtemp(prefix="perp-lab-proposer-")
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _start(pc, args: list[str], cwd: str, web: bool = False, **kw):
    # Its own session, so a kill reaches every process it started and none is left holding the pipe open.
    return proc.run(args, claude_bin=pc.claude_bin, cwd=cwd, env=env(web), start_new_session=True, **kw)


def _end(p) -> None:
    """Unless the CLI has been reaped, kill its process group (with any child it started); then reap it."""
    if p.returncode is None:  # not reaped yet, so its pid, the group's id, can't have been reused
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    p.wait()
    for f in (p.stdin, p.stdout, p.stderr):
        if f is not None:
            f.close()


def _lines(p, data: bytes, deadline: float):
    """Feed stdin and yield stdout's lines until EOF; raise _TimedOut at the deadline.

    One select loop rather than readline and a blocking write: a CLI that goes silent mid-stream, or stops
    reading a large stdin, must not hold the call past its deadline.
    """
    out, inp = p.stdout.fileno(), p.stdin.fileno()
    pending = memoryview(data)
    sel = selectors.DefaultSelector()
    sel.register(out, selectors.EVENT_READ)
    if pending:
        os.set_blocking(inp, False)
        sel.register(inp, selectors.EVENT_WRITE)
    else:
        p.stdin.close()
    buf = b""
    try:
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise _TimedOut
            for key, _ in sel.select(left):
                if key.fd == out:
                    chunk = os.read(out, CHUNK)
                    if not chunk:
                        if buf:
                            yield buf
                        return
                    *done, buf = (buf + chunk).split(b"\n")
                    yield from done
                    continue
                try:
                    pending = pending[os.write(inp, pending[:CHUNK]):]
                except BlockingIOError:
                    continue
                except BrokenPipeError:  # the CLI stopped reading; its exit or stream says why
                    pending = pending[:0]
                if not pending:
                    sel.unregister(inp)
                    p.stdin.close()
    finally:
        sel.close()


def _parse(raw: bytes) -> dict:
    try:
        msg = json.loads(raw)
    except ValueError:
        raise ModelCallError(f"a stream line is not JSON: {raw[:80]!r}") from None
    if not isinstance(msg, dict):
        raise ModelCallError(f"a stream line is not a JSON object: {raw[:80]!r}")
    return msg


def _check_init(pc, init: dict, web: bool) -> None:
    bad = []
    if init.get("model") != pc.model:
        bad.append(f"model {init.get('model')!r}, not {pc.model!r}")
    tools, got = WEB_TOOLS if web else TOOLS, init.get("tools")
    if not (isinstance(got, list) and len(got) == len(tools) and all(t in got for t in tools)):
        bad.append(f"tools {got!r}, not {tools!r}")
    if web and init.get("permissionMode") != "default":  # any other mode could allow a fetch with no rule
        bad.append(f"permissionMode {init.get('permissionMode')!r}, not 'default'")
    bad += [f"{k} {init[k]!r}" for k in EMPTY_IN_INIT if init.get(k)]
    if bad:
        raise ModelConfigError("the CLI's init failed the check: " + "; ".join(bad))


def _host(url) -> str | None:
    """An http(s) URL's host, lowercased, without port or trailing dot; None for a URL not read alike by every
    parser."""
    if not isinstance(url, str) or not URL_CHARS.fullmatch(url):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    m = NETLOC.fullmatch(parts.netloc)
    return m.group(1).lower() if parts.scheme in ("http", "https") and m else None


def on_allowlist(url, domains) -> bool:
    """Whether `url`'s host is an allowed domain or a subdomain of one, compared without case, port or a trailing
    dot; `www.` on either side is the same site. A URL `_host` can't read is off the list."""
    host = _host(url)
    if host is None:
        return False
    for d in domains:
        d = d.lower() if isinstance(d, str) else ""
        if d and (host == d or host.endswith("." + d) or host.removeprefix("www.") == d.removeprefix("www.")):
            return True
    return False


def _items(msg: dict, kind: str) -> list[dict]:
    body = msg.get("message")
    content = body.get("content") if isinstance(body, dict) else None
    if not isinstance(content, list):
        return []
    return [c for c in content if isinstance(c, dict) and c.get("type") == kind]


def _is_denial(content) -> bool:
    """The CLI's denial is plain text; one text block holding it is the same. Anything more is not a denial."""
    if isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict):
        content = content[0].get("text") if content[0].get("type") == "text" else None
    return isinstance(content, str) and content.startswith(DENIAL)


def _id(v) -> str | None:
    return v if isinstance(v, str) and v else None


class _Fence:
    """One call's fetch audit: what the model fetched, and proof that each fetch off the allowlist was denied."""

    def __init__(self, domains: tuple[str, ...]):
        self.domains = domains
        self.fetches: list[tuple[str | None, str]] = []  # (tool_use id, URL)
        self.off: dict[str | None, str] = {}  # tool_use id -> the host, for each fetch off the allowlist
        self.denials: set[str] = set()  # the tool_use ids of the WebFetches the results list as denied

    def see(self, kind: str, msg: dict) -> None:
        if kind == "assistant":
            for item in _items(msg, "tool_use"):
                if item.get("name") == "WebFetch":
                    if not self.domains:
                        raise ModelConfigError('a WebFetch in a tool-less call: --tools "" did not hold')
                    self._fetch(item)
        elif kind == "user":
            for item in _items(msg, "tool_result"):
                tid = _id(item.get("tool_use_id"))
                if tid in self.off and not _is_denial(item.get("content")):
                    raise FenceBreach(f"off-list fetch: {self.off[tid]}: its tool_result is not the denial")
        elif kind == "result":
            denials = msg.get("permission_denials")
            for d in denials if isinstance(denials, list) else ():
                if isinstance(d, dict) and d.get("tool_name") == "WebFetch" and _id(d.get("tool_use_id")):
                    self.denials.add(d["tool_use_id"])
            for tid, host in self.off.items():
                if tid not in self.denials:
                    raise FenceBreach(f"off-list fetch: {host}: not in the result's permission_denials")

    def _fetch(self, item: dict) -> None:
        tid, inp = _id(item.get("id")), item.get("input")
        url = inp.get("url") if isinstance(inp, dict) else None
        if not isinstance(url, str):
            url = json.dumps(url)
        self.fetches.append((tid, url))
        if not on_allowlist(url, self.domains):
            self.off.setdefault(tid, _host(url) or repr(url)[:STDERR_TAIL])

    def urls(self) -> list[str]:
        return [u for _, u in self.fetches]

    def denied(self) -> list[str]:
        return [u for t, u in self.fetches if t in self.denials]


def _resets_at(v) -> datetime | None:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        try:
            return datetime.fromtimestamp(v, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    return None


def _read(pc, lines, log, fence: _Fence) -> tuple[bool, dict | None]:
    """Log and check each line as it arrives; return whether init was seen, and the result message if any."""
    init, result = False, None
    for raw in lines:
        log.write(raw + b"\n")
        log.flush()
        msg = _parse(raw)
        kind, sub = str(msg.get("type", "")), str(msg.get("subtype", ""))
        if "hook" in kind or "hook" in sub:
            raise ModelConfigError(f"a hook event in the stream ({kind}/{sub}): --safe-mode did not hold")
        if not init:
            if kind == "system" and sub == "init":
                _check_init(pc, msg, bool(fence.domains))
                init = True
            elif kind != "system":
                raise ModelCallError(f"a {kind!r} message before init")
        elif kind == "rate_limit_event":
            info = msg.get("rate_limit_info")
            info = info if isinstance(info, dict) else {}
            if info.get("status") not in SERVED:
                raise UsageLimit(_resets_at(info.get("resetsAt")))
        else:
            fence.see(kind, msg)
            if kind == "result":
                result = msg
    return init, result


def _api_status(result: dict) -> int | None:
    v = result.get("api_error_status")
    if isinstance(v, str) and v.isdigit():
        v = int(v)
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _refused(result: dict) -> None:
    """An error result the API refused: no retry can change a 4xx (R42). Checked before the exit code, which an
    API error may make non-zero."""
    status = _api_status(result)
    if not result.get("is_error") or status is None or not 400 <= status < 500:
        return
    if status == 429:
        raise UsageLimit(None)
    text = result.get("result")
    raise ModelConfigError(f"the API refused the call ({status})" + (f": {text[:STDERR_TAIL]}"
                                                                     if isinstance(text, str) and text else ""))


def _tail(err) -> str:
    size = err.seek(0, os.SEEK_END)
    err.seek(max(0, size - STDERR_TAIL))
    text = err.read().decode("utf-8", "replace").strip()
    return f"; stderr: {text}" if text else ""


def call(pc, system: str, user: str, schema: dict, transcript: Path, web_domains: tuple[str, ...] = ()) -> Reply:
    args = argv(pc, system, schema, web_domains=web_domains)
    fence = _Fence(tuple(web_domains))
    transcript.parent.mkdir(parents=True, exist_ok=True)
    with _workdir() as cwd, tempfile.TemporaryFile() as err, open(transcript, "ab") as log:
        p = _start(pc, args, cwd, bool(web_domains), stdin=proc.PIPE, stdout=proc.PIPE, stderr=err)
        try:
            deadline = time.monotonic() + pc.call_timeout_s
            try:
                with contextlib.closing(_lines(p, user.encode(), deadline)) as lines:
                    init, result = _read(pc, lines, log, fence)
                p.wait(timeout=max(0.0, deadline - time.monotonic()))
            except (_TimedOut, proc.TimeoutExpired):
                raise ModelCallError(f"no exit within {pc.call_timeout_s} s: timed out, killed") from None
        finally:
            _end(p)
        if result is not None:
            _refused(result)
        if p.returncode != 0:
            raise ModelCallError(f"claude exited {p.returncode}" + _tail(err))
        if not init:
            raise ModelCallError("the stream ended before init" + _tail(err))
        if result is None:
            raise ModelCallError("the stream ended without a result" + _tail(err))
    if result.get("is_error"):
        raise ModelCallError(f"the result is an error: {result.get('subtype')!r}, "
                             f"api_error_status {result.get('api_error_status')!r}")
    proposal = result.get("structured_output")
    if not isinstance(proposal, dict):
        raise ModelCallError(f"the result has no structured_output object: {type(proposal).__name__}")
    usage = result.get("usage")
    if not isinstance(usage, dict):  # the daily token caps count it: a call they can't count fails closed
        raise ModelCallError(f"the result has no usage object: {type(usage).__name__}")
    cost = result.get("total_cost_usd")
    return Reply(proposal=proposal, usage=usage,
                 cost_usd=float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None,
                 fetches=fence.urls(), denied=fence.denied())


def call_with_retries(pc, system: str, user: str, schema: dict, transcript: Path, *, sleep=time.sleep,
                      web_domains: tuple[str, ...] = ()) -> Reply:
    """Up to 5 tries on ModelCallError; UsageLimit, ModelConfigError and FenceBreach pass straight through."""
    for delay in BACKOFF_S:
        try:
            return call(pc, system, user, schema, transcript, web_domains)
        except ModelCallError:
            sleep(delay)
    return call(pc, system, user, schema, transcript, web_domains)


def cli_version(pc) -> str:
    """`claude --version`, recorded at each session's start: the CLI updates itself."""
    with _workdir() as cwd:
        p = _start(pc, [pc.claude_bin, "--version"], cwd, stdin=proc.PIPE, stdout=proc.PIPE, stderr=proc.PIPE)
        try:
            out, err = p.communicate(timeout=VERSION_TIMEOUT_S)
        except proc.TimeoutExpired:
            raise ModelCallError(f"claude --version: no exit within {VERSION_TIMEOUT_S} s") from None
        finally:
            _end(p)
    if p.returncode != 0:
        raise ModelCallError(f"claude --version exited {p.returncode}: {err.decode('utf-8', 'replace')[-200:]}")
    return out.decode("utf-8", "replace").strip()


def probe(pc, transcript: Path) -> None:
    """Each session's first call: a one-line reply that must pass the same init check as every call."""
    call(pc, PROBE_SYSTEM, PROBE_USER, PROBE_SCHEMA, transcript)

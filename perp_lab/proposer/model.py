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
that is still served.
"""

from __future__ import annotations

import contextlib
import json
import os
import selectors
import shutil
import signal
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from perp_lab.proposer import proc

TOOLS = ["StructuredOutput"]  # the --json-schema mechanism; --tools "" removes every built-in
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


class ModelConfigError(Exception):
    """The CLI is not set up as the spec requires; the session stops with `model_config`."""


class UsageLimit(Exception):
    """The plan's usage limit is reached; the loop sleeps until `resets_at`, or backs off hourly if None."""

    def __init__(self, resets_at: datetime | None):
        super().__init__(f"usage limit reached, resets at {resets_at.isoformat() if resets_at else 'unknown'}")
        self.resets_at = resets_at


class ModelCallError(Exception):
    """The call failed in a way another try may fix: retryable."""


class _TimedOut(Exception):
    pass


def argv(pc, system: str, schema: dict) -> list[str]:
    return [pc.claude_bin, "-p", "--model", pc.model, "--effort", pc.effort, "--tools", "", "--strict-mcp-config",
            "--safe-mode", "--disable-slash-commands", "--no-session-persistence", "--permission-prompts", "none",
            "--system-prompt", system, "--json-schema", json.dumps(schema), "--output-format", "stream-json",
            "--verbose", "--include-hook-events"]


def env() -> dict[str, str]:
    return {"HOME": os.environ["HOME"], "PATH": os.environ["PATH"]}


@contextlib.contextmanager
def _workdir():
    d = tempfile.mkdtemp(prefix="perp-lab-proposer-")
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _start(pc, args: list[str], cwd: str, **kw):
    # Its own session, so a kill reaches every process it started and none is left holding the pipe open.
    return proc.run(args, claude_bin=pc.claude_bin, cwd=cwd, env=env(), start_new_session=True, **kw)


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


def _check_init(pc, init: dict) -> None:
    bad = []
    if init.get("model") != pc.model:
        bad.append(f"model {init.get('model')!r}, not {pc.model!r}")
    if init.get("tools") != TOOLS:
        bad.append(f"tools {init.get('tools')!r}, not {TOOLS!r}")
    bad += [f"{k} {init[k]!r}" for k in EMPTY_IN_INIT if init.get(k)]
    if bad:
        raise ModelConfigError("the CLI's init failed the check: " + "; ".join(bad))


def _resets_at(v) -> datetime | None:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        try:
            return datetime.fromtimestamp(v, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    return None


def _read(pc, lines, log) -> tuple[bool, dict | None]:
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
                _check_init(pc, msg)
                init = True
            elif kind != "system":
                raise ModelCallError(f"a {kind!r} message before init")
        elif kind == "rate_limit_event":
            info = msg.get("rate_limit_info")
            info = info if isinstance(info, dict) else {}
            if info.get("status") not in SERVED:
                raise UsageLimit(_resets_at(info.get("resetsAt")))
        elif kind == "result":
            result = msg
    return init, result


def _tail(err) -> str:
    size = err.seek(0, os.SEEK_END)
    err.seek(max(0, size - STDERR_TAIL))
    text = err.read().decode("utf-8", "replace").strip()
    return f"; stderr: {text}" if text else ""


def call(pc, system: str, user: str, schema: dict, transcript: Path) -> Reply:
    transcript.parent.mkdir(parents=True, exist_ok=True)
    with _workdir() as cwd, tempfile.TemporaryFile() as err, open(transcript, "ab") as log:
        p = _start(pc, argv(pc, system, schema), cwd, stdin=proc.PIPE, stdout=proc.PIPE, stderr=err)
        try:
            deadline = time.monotonic() + pc.call_timeout_s
            try:
                with contextlib.closing(_lines(p, user.encode(), deadline)) as lines:
                    init, result = _read(pc, lines, log)
                p.wait(timeout=max(0.0, deadline - time.monotonic()))
            except (_TimedOut, proc.TimeoutExpired):
                raise ModelCallError(f"no exit within {pc.call_timeout_s} s: timed out, killed") from None
        finally:
            _end(p)
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
                 cost_usd=float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None)


def call_with_retries(pc, system: str, user: str, schema: dict, transcript: Path, *, sleep=time.sleep) -> Reply:
    """Up to 5 tries on ModelCallError; UsageLimit and ModelConfigError pass straight through."""
    for delay in BACKOFF_S:
        try:
            return call(pc, system, user, schema, transcript)
        except ModelCallError:
            sleep(delay)
    return call(pc, system, user, schema, transcript)


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

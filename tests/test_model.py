from __future__ import annotations

import dataclasses
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from perp_lab.config import load_config
from perp_lab.proposer import model
from perp_lab.proposer.settings import load
from tests.conftest import REPO

# The real stream, CLI 2.1.316 (the build's one real model call): ui_invalidate, init, assistant, user,
# rate_limit_event, result. Init's cwd, original_cwd, socket path and plugin paths are scrubbed; nothing else.
FIXTURE = REPO / "tests/fixtures/claude_stream_ok.jsonl"
OK = FIXTURE.read_text().splitlines()
UI, INIT, ASSISTANT, USER, RATE, RESULT = range(6)
SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
TOKEN = re.compile(r"sk-ant-|Bearer ")
QUICK_S = 10  # every scenario that should be cut short pauses 30 s first


def _msg(i: int) -> dict:
    return json.loads(OK[i])


def _line(i: int, drop: tuple[str, ...] = (), **over) -> str:
    m = {k: v for k, v in _msg(i).items() if k not in drop}
    return json.dumps({**m, **over})


def _rate(**info) -> str:
    m = _msg(RATE)
    return json.dumps({**m, "rate_limit_info": {**m["rate_limit_info"], **info}})


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)  # succeeds on a live process or an unreaped zombie
    except ProcessLookupError:
        return True
    return False


def _dead_soon(pid: int, within_s: float = 5) -> bool:
    """An orphan is reaped by init, not by us: a zombie counts as dead."""
    end = time.monotonic() + within_s
    while time.monotonic() < end:
        try:
            if Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z":
                return True
        except FileNotFoundError:
            return True
        time.sleep(0.05)
    return False


class Fake:
    def __init__(self, bindir: Path):
        self.dir = bindir

    def scenario(self, lines, exit: int = 0, version: str | None = None, child: int | None = None) -> None:
        s = {"lines": list(lines), "exit": exit}
        if version is not None:
            s["version"] = version
        if child is not None:
            s["child"] = child
        (self.dir / "scenario.json").write_text(json.dumps(s))

    @property
    def seen(self) -> dict:
        return json.loads((self.dir / "seen.json").read_text())


@pytest.fixture
def fake(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    body = (REPO / "tests/fake_claude.py").read_text().split("\n", 1)[1]
    exe = bindir / "claude"
    exe.write_text(f"#!{sys.executable}\n{body}")  # the venv's python: the CLI's PATH may not reach one
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PERP_LAB_TEST_OTHER", "must not reach the CLI")
    return Fake(bindir)


@pytest.fixture
def pc():
    return dataclasses.replace(load(load_config(REPO / "config/lab.yaml")), claude_bin="claude", call_timeout_s=30)


@pytest.fixture
def transcript(tmp_path):
    return tmp_path / "transcripts" / "t.jsonl"


def test_the_argv_is_the_specs_command_line(pc):
    assert model.argv(pc, "the static context", SCHEMA) == [
        "claude", "-p", "--model", "claude-opus-5-5", "--effort", "high", "--tools", "", "--strict-mcp-config",
        "--safe-mode", "--disable-slash-commands", "--no-session-persistence", "--permission-prompts", "none",
        "--system-prompt", "the static context", "--json-schema", json.dumps(SCHEMA), "--output-format",
        "stream-json", "--verbose", "--include-hook-events"]


def test_the_cli_runs_in_an_empty_temp_dir_outside_the_repo_with_only_home_and_path(fake, pc, transcript):
    fake.scenario(OK)
    model.call(pc, "the static context", "the record", SCHEMA, transcript)
    seen = fake.seen
    assert seen["argv"][1:] == model.argv(pc, "the static context", SCHEMA)[1:]
    assert seen["stdin"] == "the record"
    cwd = Path(seen["cwd"])
    assert seen["cwd_files"] == []
    assert cwd.name.startswith("perp-lab-proposer-") and not cwd.is_relative_to(REPO.resolve())
    assert not cwd.exists()
    assert seen["env"] == {"HOME": os.environ["HOME"], "PATH": os.environ["PATH"]}


def test_the_ok_fixture_parses_to_its_structured_output_and_usage(fake, pc, transcript):
    fake.scenario(OK)
    reply = model.call(pc, "s", "u", SCHEMA, transcript)
    assert reply == model.Reply(proposal={"ok": True}, usage=_msg(RESULT)["usage"], cost_usd=0.020392)


def test_the_transcript_holds_every_line_and_no_token(fake, pc, transcript):
    fake.scenario(OK)
    model.call(pc, "s", "u", SCHEMA, transcript)
    assert transcript.read_bytes() == FIXTURE.read_bytes()
    model.call(pc, "s", "u", SCHEMA, transcript)
    assert transcript.read_bytes() == FIXTURE.read_bytes() * 2
    assert not TOKEN.search(FIXTURE.read_text())
    assert not TOKEN.search(transcript.read_text())
    assert "/home/" not in FIXTURE.read_text()  # scrubbed of the capturing machine's paths


def test_a_large_stdin_is_fed_whole(fake, pc, transcript):
    fake.scenario(OK)
    user = "record line\n" * 200_000  # 2.4 MB, far past a pipe's buffer
    model.call(pc, "s", user, SCHEMA, transcript)
    assert fake.seen["stdin"] == user


@pytest.mark.parametrize("init", [
    _line(INIT, model="claude-sonnet-5-5"),
    _line(INIT, model="opus"),
    _line(INIT, drop=("model",)),
    _line(INIT, tools=["StructuredOutput", "Bash"]),
    _line(INIT, tools=[]),
    _line(INIT, drop=("tools",)),
    _line(INIT, mcp_servers=[{"name": "x", "status": "connected"}]),
    _line(INIT, skills=["x"]),
    _line(INIT, slash_commands=["/x"]),
], ids=["other-model", "alias", "no-model", "extra-tool", "no-tool", "no-tools-key", "mcp", "skill", "slash"])
def test_a_failed_init_check_kills_the_call_before_any_result(fake, pc, transcript, init):
    fake.scenario([OK[UI], init, 30, *OK[ASSISTANT:]])
    start = time.monotonic()
    with pytest.raises(model.ModelConfigError):
        model.call(pc, "s", "u", SCHEMA, transcript)
    assert time.monotonic() - start < QUICK_S
    assert _gone(fake.seen["pid"])
    assert transcript.read_text().splitlines() == [OK[UI], init]
    assert not Path(fake.seen["cwd"]).exists()


def test_init_lists_that_are_absent_pass(fake, pc, transcript):
    fake.scenario([OK[UI], _line(INIT, drop=("mcp_servers", "skills", "slash_commands")), *OK[ASSISTANT:]])
    assert model.call(pc, "s", "u", SCHEMA, transcript).proposal == {"ok": True}


@pytest.mark.parametrize("hook", [
    json.dumps({"type": "system", "subtype": "hook_started", "hook_name": "SessionStart"}),
    json.dumps({"type": "system", "subtype": "hook_response", "hook_name": "Stop"}),
    json.dumps({"type": "hook_event"}),
], ids=["hook_started", "hook_response", "hook-type"])
def test_a_later_hook_event_kills_the_call(fake, pc, transcript, hook):
    fake.scenario([OK[UI], OK[INIT], hook, 30, *OK[ASSISTANT:]])
    start = time.monotonic()
    with pytest.raises(model.ModelConfigError):
        model.call(pc, "s", "u", SCHEMA, transcript)
    assert time.monotonic() - start < QUICK_S
    assert _gone(fake.seen["pid"])


def test_a_hook_event_before_init_kills_the_call(fake, pc, transcript):
    fake.scenario([OK[UI], json.dumps({"type": "system", "subtype": "hook_started"}), 30, *OK[INIT:]])
    with pytest.raises(model.ModelConfigError):
        model.call(pc, "s", "u", SCHEMA, transcript)
    assert _gone(fake.seen["pid"])


def test_a_rejected_rate_limit_raises_usage_limit_with_its_reset(fake, pc, transcript):
    fake.scenario([*OK[:RATE], _rate(status="rejected", resetsAt=1793539200), 30, OK[RESULT]])
    start = time.monotonic()
    with pytest.raises(model.UsageLimit) as e:
        model.call(pc, "s", "u", SCHEMA, transcript)
    assert e.value.resets_at == datetime(2026, 11, 1, 13, 20, tzinfo=timezone.utc)
    assert time.monotonic() - start < QUICK_S
    assert _gone(fake.seen["pid"])


def test_a_rejected_rate_limit_without_a_reset_gives_none(fake, pc, transcript):
    m = _msg(RATE)
    info = {k: v for k, v in m["rate_limit_info"].items() if k != "resetsAt"}
    fake.scenario([*OK[:RATE], json.dumps({**m, "rate_limit_info": {**info, "status": "rejected"}}), OK[RESULT]])
    with pytest.raises(model.UsageLimit) as e:
        model.call(pc, "s", "u", SCHEMA, transcript)
    assert e.value.resets_at is None


def test_the_overage_status_is_not_the_limit(fake, pc, transcript):
    assert _msg(RATE)["rate_limit_info"]["overageStatus"] == "rejected"  # the real fixture, status "allowed"
    fake.scenario([*OK[:RATE], _rate(overageStatus="rejected"), OK[RESULT]])
    assert model.call(pc, "s", "u", SCHEMA, transcript).proposal == {"ok": True}


def test_an_allowed_warning_still_serves_the_call(fake, pc, transcript):
    fake.scenario([*OK[:RATE], _rate(status="allowed_warning", resetsAt=1793539200), OK[RESULT]])
    assert model.call(pc, "s", "u", SCHEMA, transcript).proposal == {"ok": True}


@pytest.mark.parametrize("lines,exit", [
    (["not json", *OK], 0),
    ([OK[UI], OK[INIT], "not json", 30, *OK[ASSISTANT:]], 0),
    ([OK[UI], OK[INIT], "[1, 2]", *OK[ASSISTANT:]], 0),
    ([], 0),
    ([OK[UI]], 0),
    ([OK[ASSISTANT], *OK], 0),
    (OK[:RESULT], 0),
    (OK, 1),
    ([*OK[:RESULT], _line(RESULT, is_error=True, subtype="error_during_execution")], 0),
    ([*OK[:RESULT], _line(RESULT, drop=("structured_output",))], 0),
    ([*OK[:RESULT], _line(RESULT, structured_output="ok")], 0),
    ([*OK[:RESULT], _line(RESULT, drop=("usage",))], 0),  # the daily token caps fail closed
], ids=["non-json-first", "non-json-later", "not-an-object", "empty", "eof-before-init", "assistant-before-init",
        "eof-without-result", "non-zero-exit", "is-error", "no-structured-output", "structured-output-not-object",
        "no-usage"])
def test_a_broken_stream_raises_model_call_error(fake, pc, transcript, lines, exit):
    fake.scenario(lines, exit=exit)
    start = time.monotonic()
    with pytest.raises(model.ModelCallError):
        model.call(pc, "s", "u", SCHEMA, transcript)
    assert time.monotonic() - start < QUICK_S
    assert _gone(fake.seen["pid"])


def test_a_cli_silent_mid_stream_times_out(fake, pc, transcript):
    fake.scenario([OK[UI], OK[INIT], 30, *OK[ASSISTANT:]])
    start = time.monotonic()
    with pytest.raises(model.ModelCallError, match="timed out"):
        model.call(dataclasses.replace(pc, call_timeout_s=1), "s", "u", SCHEMA, transcript)
    assert time.monotonic() - start < QUICK_S
    assert _gone(fake.seen["pid"])
    assert not Path(fake.seen["cwd"]).exists()
    assert transcript.read_text().splitlines() == OK[:ASSISTANT]


def test_the_kill_takes_the_processes_the_cli_started(fake, pc, transcript):
    fake.scenario([OK[UI], OK[INIT], 30, *OK[ASSISTANT:]], child=30)
    with pytest.raises(model.ModelCallError, match="timed out"):
        model.call(dataclasses.replace(pc, call_timeout_s=1), "s", "u", SCHEMA, transcript)
    assert _gone(fake.seen["pid"])
    assert _dead_soon(int((fake.dir / "child.pid").read_text()))


def test_five_failures_raise_after_four_backoffs(fake, pc, transcript):
    fake.scenario(["not json"])
    sleeps = []
    with pytest.raises(model.ModelCallError):
        model.call_with_retries(pc, "s", "u", SCHEMA, transcript, sleep=sleeps.append)
    assert sleeps == [30, 60, 120, 240]
    assert transcript.read_text().splitlines() == ["not json"] * 5


def test_a_retry_that_succeeds_returns_its_reply(pc, transcript, monkeypatch):
    reply = model.Reply(proposal={"ok": True}, usage={}, cost_usd=None)
    outcomes = [model.ModelCallError("x"), model.ModelCallError("y"), reply]

    def call(*a, **kw):
        out = outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out

    monkeypatch.setattr(model, "call", call)
    sleeps = []
    assert model.call_with_retries(pc, "s", "u", SCHEMA, transcript, sleep=sleeps.append) is reply
    assert sleeps == [30, 60]


@pytest.mark.parametrize("lines,error", [
    ([*OK[:RATE], _rate(status="rejected"), OK[RESULT]], model.UsageLimit),
    ([OK[UI], _line(INIT, model="claude-sonnet-5-5"), *OK[ASSISTANT:]], model.ModelConfigError),
], ids=["usage-limit", "model-config"])
def test_usage_limit_and_model_config_pass_straight_through(fake, pc, transcript, lines, error):
    fake.scenario(lines)
    sleeps = []
    with pytest.raises(error):
        model.call_with_retries(pc, "s", "u", SCHEMA, transcript, sleep=sleeps.append)
    assert sleeps == []


def test_cli_version(fake, pc):
    fake.scenario([], version="2.1.316 (Claude Code)")
    assert model.cli_version(pc) == "2.1.316 (Claude Code)"


def test_cli_version_failure_is_a_model_call_error(fake, pc):
    fake.scenario([], exit=1)
    with pytest.raises(model.ModelCallError):
        model.cli_version(pc)


def test_the_probe_asks_for_ok_and_passes_the_init_check(fake, pc, transcript):
    fake.scenario(OK)
    assert model.probe(pc, transcript) is None
    seen = fake.seen
    assert seen["stdin"] == "Reply with ok true."
    assert json.loads(seen["argv"][seen["argv"].index("--json-schema") + 1]) == SCHEMA


def test_the_probe_fails_on_a_bad_init(fake, pc, transcript):
    fake.scenario([OK[UI], _line(INIT, tools=["StructuredOutput", "Bash"]), *OK[ASSISTANT:]])
    with pytest.raises(model.ModelConfigError):
        model.probe(pc, transcript)


# The API's own refusal, from the real call of 2026-11-01 (R42): the schema had a top-level allOf.
API_400 = ("API Error: 400 tools.1.custom.input_schema: input_schema does not support oneOf, allOf, or anyOf at the "
           "top level")


def _api_error(status, text=API_400) -> str:
    return _line(RESULT, drop=("structured_output",), is_error=True, api_error_status=status,
                 terminal_reason="api_error", result=text)


@pytest.mark.parametrize("exit", [0, 1])
@pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 422])
def test_an_api_4xx_is_a_config_error_with_no_retry(fake, pc, transcript, status, exit):
    fake.scenario([*OK[:RESULT], _api_error(status)], exit=exit)
    sleeps = []
    with pytest.raises(model.ModelConfigError) as e:
        model.call_with_retries(pc, "s", "u", SCHEMA, transcript, sleep=sleeps.append)
    assert sleeps == [] and str(status) in str(e.value) and API_400 in str(e.value)


@pytest.mark.parametrize("exit", [0, 1])
def test_an_api_429_is_a_usage_limit_with_no_reset_time(fake, pc, transcript, exit):
    fake.scenario([*OK[:RESULT], _api_error(429, "API Error: 429 rate_limit_error")], exit=exit)
    sleeps = []
    with pytest.raises(model.UsageLimit) as e:
        model.call_with_retries(pc, "s", "u", SCHEMA, transcript, sleep=sleeps.append)
    assert sleeps == [] and e.value.resets_at is None


@pytest.mark.parametrize("status", [500, 529, None])
def test_an_api_5xx_or_unknown_error_is_still_retried(fake, pc, transcript, status):
    fake.scenario([*OK[:RESULT], _api_error(status, "API Error: overloaded")])
    sleeps = []
    with pytest.raises(model.ModelCallError):
        model.call_with_retries(pc, "s", "u", SCHEMA, transcript, sleep=sleeps.append)
    assert sleeps == [30, 60, 120, 240]


# A web-fenced call (the referee), in the shapes of CLI 2.1.316 (the controller's probes of 2026-11-01): WebFetch
# is the one built-in tool, each domain its own --allowedTools rule; a fetch is an assistant tool_use, its result a
# user tool_result; a fetch the CLI refused gets the denial below as its tool_result and is listed in the result's
# permission_denials.
DOMAINS = ("arxiv.org", "export.arxiv.org", "api.semanticscholar.org", "www.nber.org")
WEB_TOOLS = ["StructuredOutput", "WebFetch"]
INIT_WEB = _line(INIT, tools=WEB_TOOLS)
ARXIV = "https://export.arxiv.org/abs/1707.06221"
GECKO = "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart?days=120"
DENIED = ("Permission for this tool use was denied. It requires approval, and this session has no approval "
          "surface — nobody can answer a permission prompt here — so it was denied automatically.")
# the other denial path's text (probe of 2026-11-02, a preapproved host under CLAUDE_CODE_EVAL_CONFINED): the texts
# differ, so only is_error and permission_denials decide (R-RF4)
DENIED_RULE = ("Permission to use WebFetch has been denied. IMPORTANT: You *may* attempt to accomplish this action "
               "using other tools that might naturally be used to accomplish this goal.")
NO_CONTENT = object()


def _fetch(tid: str, url, **wire) -> str:
    """A WebFetch tool_use. Real assistant messages carry `wire_tool_inputs`, the inputs as the API sent them; it
    matches here unless the test passes its own."""
    item = {"type": "tool_use", "id": tid, "name": "WebFetch", "input": {"url": url, "prompt": "Its findings?"}}
    return _said(item, **wire)


def _fetched(tid: str, content=f"Paper {ARXIV}: no round-number effect survives costs.", **flags) -> str:
    m = {k: v for k, v in _msg(USER).items() if k not in ("tool_use_result", "tool_result_meta")}
    item = {"tool_use_id": tid, "type": "tool_result", **flags}
    if content is not NO_CONTENT:
        item["content"] = content
    return json.dumps({**m, "message": {**m["message"], "content": [item]}})


def _denied(tid: str, text: str = DENIED) -> list[str]:
    """The CLI's refusal as the stream shows it: a permission_denied notice, then an error tool_result."""
    notice = {"type": "system", "subtype": "permission_denied", "tool_name": "WebFetch", "tool_use_id": tid,
              "decision_reason_type": "asyncAgent", "message": text}
    return [json.dumps(notice), _fetched(tid, text, is_error=True)]


def _said(*blocks, **wire) -> str:
    """An assistant message with these content blocks, and their `wire_tool_inputs` unless given."""
    m = _msg(ASSISTANT)
    inputs = {b["id"]: b.get("input") for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use"
              and isinstance(b.get("id"), str)}
    return json.dumps({**m, "message": {**m["message"], "content": list(blocks)},
                       **(wire or {"wire_tool_inputs": inputs})})


def _told(content) -> str:
    """A user message with this content."""
    m = {k: v for k, v in _msg(USER).items() if k not in ("tool_use_result", "tool_result_meta")}
    return json.dumps({**m, "message": {**m["message"], "content": content}})


def _result(*denied: tuple[str, str], tool: str = "WebFetch") -> str:
    return _line(RESULT, permission_denials=[{"tool_name": tool, "tool_use_id": t, "tool_input": {"url": u}}
                                             for t, u in denied])


def _web(*middle: str) -> list[str]:
    """A web call's stream up to its result: init with WebFetch, the given lines, then the fixture's reply."""
    return [OK[UI], INIT_WEB, *middle, *OK[ASSISTANT:RESULT]]


def test_a_web_argv_has_webfetch_and_one_allow_rule_per_domain_before_the_next_flag(pc):
    assert model.argv(pc, "the static context", SCHEMA, web_domains=DOMAINS) == [
        "claude", "-p", "--model", "claude-opus-5-5", "--effort", "high", "--tools", "WebFetch", "--allowedTools",
        "WebFetch(domain:arxiv.org)", "WebFetch(domain:export.arxiv.org)",
        "WebFetch(domain:api.semanticscholar.org)", "WebFetch(domain:www.nber.org)", "--setting-sources", "",
        "--strict-mcp-config", "--safe-mode", "--disable-slash-commands", "--no-session-persistence",
        "--permission-prompts", "none", "--system-prompt", "the static context", "--json-schema",
        json.dumps(SCHEMA), "--output-format", "stream-json", "--verbose", "--include-hook-events"]


def test_the_allow_rules_are_lowercased(pc):
    a = model.argv(pc, "s", SCHEMA, web_domains=("ArXiv.ORG", "x-1.API.Crossref.org"))
    assert a[a.index("--allowedTools") + 1:a.index("--setting-sources")] == [
        "WebFetch(domain:arxiv.org)", "WebFetch(domain:x-1.api.crossref.org)"]


def test_no_web_domains_is_todays_tool_less_argv(pc):
    a = model.argv(pc, "s", SCHEMA, web_domains=())
    assert a == model.argv(pc, "s", SCHEMA)
    assert a[a.index("--tools") + 1] == "" and "--allowedTools" not in a and not any("WebFetch" in x for x in a)
    assert "--setting-sources" not in a


@pytest.mark.parametrize("domain", ["", "org", "arxiv.org) Bash(", "arxiv.org,evil.com", "arxiv.org evil.com",
                                    "*.arxiv.org", "https://arxiv.org", "arxiv.org/abs", "аrxiv.org",
                                    "arx\u0131v.org", "\u212aaggle.com", None, "10.0.0.1", "arxiv.123", "arxiv.o-g",
                                    "-arxiv.org", "arxiv-.org", "arxiv.-.org", "--.org", "arxiv..org", "arxiv.org."])
def test_a_web_domain_that_is_not_a_hostname_is_refused_before_any_process(fake, pc, transcript, domain):
    fake.scenario(OK)
    with pytest.raises(model.ModelConfigError, match="not a hostname"):
        model.argv(pc, "s", SCHEMA, web_domains=("arxiv.org", domain))
    with pytest.raises(model.ModelConfigError, match="not a hostname"):
        model.call(pc, "s", "u", SCHEMA, transcript, web_domains=("arxiv.org", domain))
    assert not (fake.dir / "seen.json").exists()


def test_a_tool_less_reply_has_no_fetches(fake, pc, transcript):
    fake.scenario(OK)
    reply = model.call(pc, "s", "u", SCHEMA, transcript)
    assert reply.fetches == [] and reply.denied == []


def test_a_web_call_runs_the_fenced_argv_and_records_an_allowed_fetch(fake, pc, transcript):
    fake.scenario([*_web(_fetch("toolu_a", ARXIV), _fetched("toolu_a")), _result()])
    reply = model.call(pc, "the static context", "the cards", SCHEMA, transcript, web_domains=DOMAINS)
    assert fake.seen["argv"][1:] == model.argv(pc, "the static context", SCHEMA, web_domains=DOMAINS)[1:]
    # R-RF3: no user, project or local settings (--setting-sources ""), and the CLI's preapproved hosts off
    assert fake.seen["env"] == {"HOME": os.environ["HOME"], "PATH": os.environ["PATH"],
                                "CLAUDE_CODE_EVAL_CONFINED": "1"}
    assert reply.proposal == {"ok": True} and reply.usage == _msg(RESULT)["usage"]
    assert reply.fetches == [ARXIV] and reply.denied == []
    assert transcript.read_text().splitlines() == [*_web(_fetch("toolu_a", ARXIV), _fetched("toolu_a")), _result()]


def test_a_web_init_in_either_order_passes(fake, pc, transcript):
    fake.scenario([OK[UI], _line(INIT, tools=["WebFetch", "StructuredOutput"]), *OK[ASSISTANT:]])
    assert model.call(pc, "s", "u", SCHEMA, transcript, web_domains=DOMAINS).proposal == {"ok": True}


@pytest.mark.parametrize("tools", [
    ["StructuredOutput"], ["WebFetch"], [*WEB_TOOLS, "WebSearch"], [*WEB_TOOLS, "Bash"], [*WEB_TOOLS, "WebFetch"],
    ["WebFetch", "WebFetch"], [], "StructuredOutput,WebFetch", None,
], ids=["no-webfetch", "no-structured-output", "websearch", "bash", "duplicate", "webfetch-twice", "none",
        "a-string", "no-tools-key"])
def test_a_web_init_must_list_exactly_structured_output_and_webfetch(fake, pc, transcript, tools):
    init = _line(INIT, drop=("tools",)) if tools is None else _line(INIT, tools=tools)
    fake.scenario([OK[UI], init, 30, *OK[ASSISTANT:]])
    start = time.monotonic()
    with pytest.raises(model.ModelConfigError, match="tools"):
        model.call(pc, "s", "u", SCHEMA, transcript, web_domains=DOMAINS)
    assert time.monotonic() - start < QUICK_S
    assert _gone(fake.seen["pid"])


@pytest.mark.parametrize("mode", ["acceptEdits", "bypassPermissions", "plan", "dontAsk", "", None, "missing"])
def test_a_web_init_whose_permission_mode_is_not_default_kills_the_call(fake, pc, transcript, mode):
    init = _line(INIT, drop=("permissionMode",), tools=WEB_TOOLS) if mode == "missing" else \
        _line(INIT, tools=WEB_TOOLS, permissionMode=mode)
    fake.scenario([OK[UI], init, 30, *OK[ASSISTANT:]])
    start = time.monotonic()
    with pytest.raises(model.ModelConfigError, match="permissionMode"):
        model.call(pc, "s", "u", SCHEMA, transcript, web_domains=DOMAINS)
    assert time.monotonic() - start < QUICK_S
    assert _gone(fake.seen["pid"])


def test_a_tool_less_call_does_not_check_the_permission_mode(fake, pc, transcript):
    fake.scenario([OK[UI], _line(INIT, permissionMode="acceptEdits"), *OK[ASSISTANT:]])
    assert model.call(pc, "s", "u", SCHEMA, transcript).proposal == {"ok": True}


def test_a_tool_less_call_refuses_an_init_with_webfetch(fake, pc, transcript):
    fake.scenario([OK[UI], INIT_WEB, 30, *OK[ASSISTANT:]])
    start = time.monotonic()
    with pytest.raises(model.ModelConfigError, match="tools"):
        model.call(pc, "s", "u", SCHEMA, transcript)
    assert time.monotonic() - start < QUICK_S
    assert _gone(fake.seen["pid"])


@pytest.mark.parametrize("text", [DENIED, DENIED_RULE, [{"type": "text", "text": DENIED}], ""],
                         ids=["no-approval-surface", "permission-rule", "one-text-block", "no-text"])
def test_a_denied_off_list_fetch_is_recorded_and_harmless(fake, pc, transcript, text):
    """R-RF4: denied means an error tool_result and a permission_denials entry, whatever the text says."""
    fake.scenario([*_web(_fetch("toolu_a", ARXIV), _fetched("toolu_a"), _fetch("toolu_b", GECKO),
                         *_denied("toolu_b", text)), _result(("toolu_b", GECKO))])
    reply = model.call(pc, "s", "u", SCHEMA, transcript, web_domains=DOMAINS)
    assert reply.proposal == {"ok": True}
    assert reply.fetches == [ARXIV, GECKO] and reply.denied == [GECKO]


def test_an_on_list_fetch_that_errs_is_harmless(fake, pc, transcript):
    fake.scenario([*_web(_fetch("toolu_a", ARXIV), _fetched("toolu_a", "HTTP 503", is_error=True)), _result()])
    reply = model.call(pc, "s", "u", SCHEMA, transcript, web_domains=DOMAINS)
    assert reply.fetches == [ARXIV] and reply.denied == []


def test_an_off_list_fetch_listed_as_denied_whose_tool_result_never_shows_is_a_breach(fake, pc, transcript):
    fake.scenario([*_web(_fetch("toolu_b", GECKO)), _result(("toolu_b", GECKO)), 30])
    start = time.monotonic()
    with pytest.raises(model.FenceBreach, match="off-list fetch: api.coingecko.com: no error tool_result"):
        model.call(pc, "s", "u", SCHEMA, transcript, web_domains=DOMAINS)
    assert time.monotonic() - start < QUICK_S
    assert _gone(fake.seen["pid"])


def test_an_off_list_fetch_with_no_tool_result_at_the_end_of_the_stream_is_a_breach(fake, pc, transcript):
    fake.scenario([OK[UI], INIT_WEB, _fetch("toolu_b", GECKO)])  # no result: the stream just ends
    with pytest.raises(model.FenceBreach, match="off-list fetch: api.coingecko.com: no error tool_result"):
        model.call(pc, "s", "u", SCHEMA, transcript, web_domains=DOMAINS)


def test_a_stream_that_ends_after_a_denial_is_an_ordinary_call_error(fake, pc, transcript):
    fake.scenario([OK[UI], INIT_WEB, _fetch("toolu_b", GECKO), *_denied("toolu_b")])
    with pytest.raises(model.ModelCallError, match="without a result"):
        model.call(pc, "s", "u", SCHEMA, transcript, web_domains=DOMAINS)


@pytest.mark.parametrize("content,flags", [
    ("Bitcoin closed at 67,000 on 2026-08-01.", {}),
    ("", {}),
    (NO_CONTENT, {}),
    (DENIED, {}),
    (DENIED_RULE, {}),
    ([{"type": "text", "text": DENIED}], {}),
    ([{"type": "image", "source": {"type": "base64", "data": "AAAA"}}], {}),
    (DENIED, {"is_error": False}),
    (DENIED, {"is_error": "true"}),
    (DENIED, {"is_error": 1}),
    (DENIED, {"is_error": None}),
], ids=["page", "empty", "no-content", "denial-text-alone", "rule-text-alone", "denial-block-alone", "image-block",
        "is-error-false", "is-error-a-string", "is-error-one", "is-error-null"])
def test_an_off_list_fetch_that_ran_kills_the_call_at_once(fake, pc, transcript, content, flags):
    head = [OK[UI], INIT_WEB, _fetch("toolu_b", GECKO), _fetched("toolu_b", content, **flags)]
    # listed as denied in the result too: the tool_result is what the model reads, so it is checked as it comes
    fake.scenario([*head, 30, *OK[ASSISTANT:RESULT], _result(("toolu_b", GECKO))])
    start = time.monotonic()
    with pytest.raises(model.FenceBreach, match="off-list fetch: api.coingecko.com"):
        model.call(pc, "s", "u", SCHEMA, transcript, web_domains=DOMAINS)
    assert time.monotonic() - start < QUICK_S
    assert _gone(fake.seen["pid"])
    assert transcript.read_text().splitlines() == head
    assert not Path(fake.seen["cwd"]).exists()


@pytest.mark.parametrize("result", [
    _result(), _result(("toolu_a", ARXIV)), _result(("toolu_b", GECKO), tool="Bash"),
    _line(RESULT, drop=("permission_denials",)), _line(RESULT, permission_denials={"toolu_b": GECKO}),
    _line(RESULT, permission_denials=["toolu_b"]),
], ids=["none-denied", "another-denied", "other-tool", "no-key", "an-object", "bare-ids"])
def test_an_off_list_fetch_the_result_does_not_list_as_denied_kills_the_call(fake, pc, transcript, result):
    fake.scenario([*_web(_fetch("toolu_a", ARXIV), _fetched("toolu_a"), _fetch("toolu_b", GECKO),
                         *_denied("toolu_b")), result, 30])
    start = time.monotonic()
    with pytest.raises(model.FenceBreach, match="off-list fetch: api.coingecko.com"):
        model.call(pc, "s", "u", SCHEMA, transcript, web_domains=DOMAINS)
    assert time.monotonic() - start < QUICK_S
    assert _gone(fake.seen["pid"])


def test_an_off_list_fetch_with_no_tool_result_and_no_denial_kills_the_call(fake, pc, transcript):
    fake.scenario([*_web(_fetch("toolu_b", GECKO)), _result(), 30])
    with pytest.raises(model.FenceBreach, match="off-list fetch: api.coingecko.com"):
        model.call(pc, "s", "u", SCHEMA, transcript, web_domains=DOMAINS)
    assert _gone(fake.seen["pid"])


@pytest.mark.parametrize("url", [None, 42, "arxiv.org/abs/1", "https://evil.com\\@arxiv.org/", "http://[",
                                 "ftp://arxiv.org/"],
                         ids=["no-url", "a-number", "no-scheme", "backslash", "bad-ipv6", "ftp"])
def test_an_unreadable_fetch_url_that_was_not_denied_kills_the_call(fake, pc, transcript, url):
    fake.scenario([*_web(_fetch("toolu_b", url), _fetched("toolu_b", "a page")), _result()])
    with pytest.raises(model.FenceBreach, match="off-list fetch"):
        model.call(pc, "s", "u", SCHEMA, transcript, web_domains=DOMAINS)


SRV_FETCH = {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_fetch", "input": {"url": GECKO}}


@pytest.mark.parametrize("lines", [
    [_fetched("toolu_x", "Bitcoin closed at 67,000.")],
    [_fetched("toolu_x", DENIED, is_error=True)],
    [_fetched("toolu_b", DENIED, is_error=True), _fetch("toolu_b", GECKO)],
    [_told("Bitcoin closed at 67,000.")],
    [_told([{"type": "text", "text": "Bitcoin closed at 67,000."}])],
    [_told(None)],
    [_told(["a string block"])],
    [_said(SRV_FETCH)],
    [_said({"type": "web_fetch_tool_result", "tool_use_id": "srvtoolu_1", "content": {"url": GECKO}})],
    [_said({"type": "image", "source": {"type": "base64", "data": "AAAA"}})],
    [_said("a string block")],
    [_said({"type": "tool_use", "id": "toolu_c", "name": "WebSearch", "input": {"query": "bitcoin price"}})],
    [_said({"type": "tool_use", "id": "toolu_c", "name": "Bash", "input": {"command": "curl coingecko.com"}})],
    [_said({"type": "tool_use", "name": "WebFetch", "input": {"url": ARXIV}})],
    [_fetch("toolu_a", ARXIV), _fetched("toolu_a"), _fetch("toolu_a", GECKO)],
    [_fetch("toolu_a", ARXIV), _fetched("toolu_a"), _fetched("toolu_a", "Bitcoin closed at 67,000.")],
    [json.dumps({**_msg(ASSISTANT), "message": {**_msg(ASSISTANT)["message"], "content": "Bitcoin at 67,000."}})],
    [json.dumps({"type": "assistant"})],
], ids=["result-for-an-unrecorded-id", "error-for-an-unrecorded-id", "result-before-its-tool-use",
        "user-string-content", "user-text-block", "user-no-content", "user-string-block", "server-tool-use",
        "web-fetch-tool-result", "assistant-image", "assistant-string-block", "websearch", "bash", "tool-use-no-id",
        "tool-use-id-reused", "a-second-result", "assistant-string-content", "assistant-no-message"])
def test_a_web_stream_shape_the_fence_cannot_vouch_for_is_a_breach(fake, pc, transcript, lines):
    """R-RF4: fail closed on any shape that could carry fetched content past the audit."""
    fake.scenario([OK[UI], INIT_WEB, *lines, 30, *OK[ASSISTANT:]])
    start = time.monotonic()
    with pytest.raises(model.FenceBreach):
        model.call(pc, "s", "u", SCHEMA, transcript, web_domains=DOMAINS)
    assert time.monotonic() - start < QUICK_S
    assert _gone(fake.seen["pid"])


def _wire(tid: str, url) -> dict:
    return {"wire_tool_inputs": {tid: {"url": url, "prompt": "Its findings?"}}}


def test_a_fetch_whose_wire_input_agrees_passes(fake, pc, transcript):
    assert _msg(ASSISTANT)["wire_tool_inputs"]  # the real reply's StructuredOutput input: not compared
    fake.scenario([*_web(_fetch("toolu_a", ARXIV, **_wire("toolu_a", ARXIV)), _fetched("toolu_a")), _result()])
    assert model.call(pc, "s", "u", SCHEMA, transcript, web_domains=DOMAINS).fetches == [ARXIV]


@pytest.mark.parametrize("wire", [
    _wire("toolu_a", GECKO), _wire("toolu_a", ARXIV + "?x"), _wire("toolu_a", None), _wire("toolu_other", ARXIV),
    {"wire_tool_inputs": {"toolu_a": {"prompt": "Its findings?"}}}, {"wire_tool_inputs": {"toolu_a": ARXIV}},
    {"wire_tool_inputs": [ARXIV]}, {"wire_tool_inputs": None},
], ids=["other-host", "other-url", "null-url", "no-entry", "no-url", "entry-not-object", "a-list", "null"])
def test_a_fetch_whose_wire_input_differs_is_a_breach(fake, pc, transcript, wire):
    """Review minor 4: when the CLI shows the input it got from the API, the fetch must be that one."""
    fake.scenario([OK[UI], INIT_WEB, _fetch("toolu_a", ARXIV, **wire), 30, *OK[ASSISTANT:]])
    start = time.monotonic()
    with pytest.raises(model.FenceBreach, match="wire"):
        model.call(pc, "s", "u", SCHEMA, transcript, web_domains=DOMAINS)
    assert time.monotonic() - start < QUICK_S
    assert _gone(fake.seen["pid"])


@pytest.mark.parametrize("line,says", [
    (_told("a note"), "a user message"), (_said("a block"), "an assistant message"),
], ids=["user", "assistant"])
def test_a_breach_names_the_message_it_could_not_read(line, says):
    with pytest.raises(model.FenceBreach, match=f"^{says} whose content is not a list of blocks$"):
        msg = json.loads(line)
        model._Fence(DOMAINS).see(msg["type"], msg)


def test_text_and_thinking_pass_in_a_web_call(fake, pc, transcript):
    blocks = [{"type": "thinking", "thinking": "Check arXiv first.", "signature": "c2ln"},
              {"type": "redacted_thinking", "data": "cmVkYWN0ZWQ="}, {"type": "text", "text": "Fetching the paper."}]
    fake.scenario([*_web(_said(*blocks), _fetch("toolu_a", ARXIV), _fetched("toolu_a")), _result()])
    assert model.call(pc, "s", "u", SCHEMA, transcript, web_domains=DOMAINS).fetches == [ARXIV]


def test_the_shape_checks_are_for_web_calls_only(fake, pc, transcript):
    fake.scenario([OK[UI], OK[INIT], _told("a note"), _said(SRV_FETCH), _fetched("toolu_x"), *OK[ASSISTANT:]])
    assert model.call(pc, "s", "u", SCHEMA, transcript).proposal == {"ok": True}


def test_a_webfetch_in_a_tool_less_call_is_a_config_error_at_once(fake, pc, transcript):
    # cannot happen past the init check, which lists no WebFetch: the CLI did not hold --tools "", so the session
    # stops (model_config) rather than one call failing closed
    head = [OK[UI], OK[INIT], _fetch("toolu_a", ARXIV)]
    fake.scenario([*head, _fetched("toolu_a"), 30, *OK[ASSISTANT:]])
    start = time.monotonic()
    with pytest.raises(model.ModelConfigError, match="WebFetch"):
        model.call(pc, "s", "u", SCHEMA, transcript)
    assert time.monotonic() - start < QUICK_S
    assert _gone(fake.seen["pid"])
    assert transcript.read_text().splitlines() == head


def test_a_fence_breach_is_its_own_error():
    """R-RF5: not ModelConfigError (the referee round fails closed, the session goes on) and not retried."""
    assert not issubclass(model.FenceBreach, (model.ModelConfigError, model.ModelCallError, model.UsageLimit))


def test_web_domains_are_keyword_only(fake, pc, transcript):
    fake.scenario(OK)
    with pytest.raises(TypeError):
        model.call(pc, "s", "u", SCHEMA, transcript, DOMAINS)
    with pytest.raises(TypeError):
        model.argv(pc, "s", SCHEMA, DOMAINS)
    assert not (fake.dir / "seen.json").exists()


def test_no_web_domains_as_none_is_a_tool_less_call(fake, pc, transcript):
    assert model.argv(pc, "s", SCHEMA, web_domains=None) == model.argv(pc, "s", SCHEMA)
    fake.scenario(OK)
    sleeps = []
    reply = model.call_with_retries(pc, "s", "u", SCHEMA, transcript, sleep=sleeps.append, web_domains=None)
    assert reply.proposal == {"ok": True} and reply.fetches == []
    assert fake.seen["argv"][1:] == model.argv(pc, "s", SCHEMA)[1:]
    assert fake.seen["env"] == {"HOME": os.environ["HOME"], "PATH": os.environ["PATH"]}


def test_call_with_retries_passes_the_web_domains_through(fake, pc, transcript):
    fake.scenario([*_web(_fetch("toolu_a", ARXIV), _fetched("toolu_a")), _result()])
    sleeps = []
    reply = model.call_with_retries(pc, "s", "u", SCHEMA, transcript, sleep=sleeps.append, web_domains=DOMAINS)
    assert reply.fetches == [ARXIV] and sleeps == []
    assert fake.seen["argv"][1:] == model.argv(pc, "s", SCHEMA, web_domains=DOMAINS)[1:]


def test_an_off_list_fetch_is_not_retried(fake, pc, transcript):
    fake.scenario([*_web(_fetch("toolu_b", GECKO), _fetched("toolu_b", "a price chart")), _result()])
    sleeps = []
    with pytest.raises(model.FenceBreach, match="off-list fetch"):
        model.call_with_retries(pc, "s", "u", SCHEMA, transcript, sleep=sleeps.append, web_domains=DOMAINS)
    assert sleeps == []


@pytest.mark.parametrize("url", [
    "https://arxiv.org/abs/1707.06221", "http://arxiv.org/abs/1", "https://export.arxiv.org/api/query?id_list=1",
    "https://www.arxiv.org/abs/1", "https://ARXIV.ORG/abs/1", "HTTPS://Export.ArXiv.Org/abs/1",
    "https://arxiv.org:443/abs/1", "https://arxiv.org:/abs/1", "https://arxiv.org./abs/1", "https://arxiv.org",
    "https://api.semanticscholar.org/graph/v1/paper/search?query=round+numbers", "https://www.nber.org/papers/w1",
    "https://nber.org/papers/w1", "https://arxiv.org/abs/1#@evil.com",
])
def test_an_allowed_host_is_on_the_allowlist(url):
    assert model.on_allowlist(url, DOMAINS)


@pytest.mark.parametrize("url", [
    GECKO, "https://www.coindesk.com/", "https://evilarxiv.org/", "https://arxiv.org.evil.com/",
    "https://xarxiv.org/", "https://semanticscholar.org/", "https://data.nber.org/", "https://nber.org.evil.com/",
    "ftp://arxiv.org/abs/1", "file:///etc/passwd", "arxiv.org/abs/1", "//arxiv.org/abs/1", "https:arxiv.org/abs/1",
    "https://arxiv.org@evil.com/", "https://evil.com@arxiv.org/", "https://arxiv.org:443@evil.com/",
    "https://evil.com\\@arxiv.org/", "https://evil.com\\.arxiv.org/", "https://arxiv.org evil.com/",
    "https://arxiv.org\t/abs/1", " https://arxiv.org/abs/1", "https://ar%78iv.org/", "https://arxiv%2eorg/",
    "https://[::1]/", "http://[", "https://аrxiv.org/", "https://arxiv。org/", "https://", "https://:443/", "",
    "https://evil.com/?u=https://arxiv.org/", "https://evil.com#https://arxiv.org/", None, 42,
    ["https://arxiv.org/"],
])
def test_any_other_url_is_off_the_allowlist(url):
    assert not model.on_allowlist(url, DOMAINS)


def test_nothing_is_on_an_empty_allowlist():
    assert not model.on_allowlist(ARXIV, ())


@pytest.mark.parametrize("after", [
    [_fetch("toolu_b", GECKO), _fetched("toolu_b", "Bitcoin closed at 67,000.")],
    [_fetch("toolu_b", GECKO)],
    [_fetch("toolu_c", ARXIV), _fetched("toolu_c")],
    [OK[RATE]],
    [OK[UI]],
    [_result()],
    ["not json"],
], ids=["off-list-fetch-that-ran", "off-list-fetch", "allowed-fetch", "rate-limit-event", "system", "a-second-result",
        "not-json"])
def test_any_line_after_a_web_calls_result_is_a_breach(fake, pc, transcript, after):
    """Task 4's review: nothing reaches a web call's result after it, so a fetch there would go unaudited."""
    head = [*_web(_fetch("toolu_a", ARXIV), _fetched("toolu_a")), _result()]
    fake.scenario([*head, *after, 30])
    start = time.monotonic()
    with pytest.raises(model.FenceBreach, match="^a stream line after the result$"):
        model.call(pc, "s", "u", SCHEMA, transcript, web_domains=DOMAINS)
    assert time.monotonic() - start < QUICK_S
    assert _gone(fake.seen["pid"])
    assert transcript.read_text().splitlines() == [*head, after[0]]


def test_a_tool_less_call_still_reads_past_its_result(fake, pc, transcript):
    fake.scenario([*OK, OK[RATE]])
    assert model.call(pc, "s", "u", SCHEMA, transcript).proposal == {"ok": True}


# R-RF6: the real CLI resumes a cut-off response with a user line of its own (seen in the controller's real referee
# call), which the model reads; exactly this shape is allowed.
RESUME = ('{"type": "user", "isSynthetic": true, "message": {"role": "user", "content": [{"type": "text", "text": '
          '"Your response above was cut off mid-stream. Resume directly from where it stops — no apology, no '
          'recap. If none of it survived, answer the request from the start."}]}}')


def _synthetic(*blocks, flag=True) -> str:
    return json.dumps({"type": "user", "isSynthetic": flag, "message": {"role": "user", "content": list(blocks)}})


def test_the_clis_synthetic_resume_message_passes_the_fence(fake, pc, transcript):
    fake.scenario([*_web(_fetch("toolu_a", ARXIV), _fetched("toolu_a"), _said({"type": "text", "text": "Half a"}),
                         RESUME, _synthetic({"type": "text", "text": "a"}, {"type": "text", "text": "b"})),
                   _result()])
    reply = model.call(pc, "s", "u", SCHEMA, transcript, web_domains=DOMAINS)
    assert reply.proposal == {"ok": True} and reply.fetches == [ARXIV]


TEXT = {"type": "text", "text": "Resume."}


@pytest.mark.parametrize("line", [
    _synthetic(TEXT, {"tool_use_id": "toolu_a", "type": "tool_result", "content": "Bitcoin closed at 67,000."}),
    _synthetic({"tool_use_id": "toolu_x", "type": "tool_result", "content": "Bitcoin closed at 67,000."}),
    _synthetic({"type": "image", "source": {"type": "base64", "data": "AAAA"}}),
    _synthetic(TEXT, "a string block"),
    _synthetic(TEXT, flag="true"),
    _synthetic(TEXT, flag=1),
    _told([TEXT]),
    json.dumps({"type": "user", "isSynthetic": True, "message": {"role": "user", "content": "Resume."}}),
], ids=["text-and-tool-result", "tool-result", "image", "string-block", "flag-a-string", "flag-one",
        "not-synthetic", "string-content"])
def test_any_other_user_line_with_text_is_still_a_breach(fake, pc, transcript, line):
    """R-RF6: only a synthetic line of text blocks alone is the CLI's own; anything else in a user line could carry
    what the audit never sees."""
    fake.scenario([OK[UI], INIT_WEB, _fetch("toolu_a", ARXIV), line, 30, *OK[ASSISTANT:]])
    start = time.monotonic()
    with pytest.raises(model.FenceBreach):
        model.call(pc, "s", "u", SCHEMA, transcript, web_domains=DOMAINS)
    assert time.monotonic() - start < QUICK_S
    assert _gone(fake.seen["pid"])

"""The session digest: the human's markdown record of one session, written as it happens."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from perp_lab.proposer.digest import Digest

UTC = timezone.utc
T = datetime(2026, 11, 2, 10, 5, tzinfo=UTC)


def _digest(tmp_path):
    return Digest(tmp_path / "sessions" / "7.md", now=lambda: T)


def test_the_header_names_the_session_model_cli_and_harness(tmp_path):
    d = _digest(tmp_path)
    d.header("claude-opus-5-5", "2026-06-30", "2.1.316 (Claude Code)", "abc123", "main")
    text = d.path.read_text()
    assert text.startswith("# Proposer session 7\n")
    for s in ("claude-opus-5-5", "2026-06-30", "2.1.316 (Claude Code)", "abc123", "main", "2026-11-02 10:05 UTC"):
        assert s in text
    assert "Warning" not in text


def test_the_header_warns_when_the_branch_is_not_main(tmp_path):
    d = _digest(tmp_path)
    d.header("claude-opus-5-5", "2026-06-30", "2.1.316", "abc123", "feat/x")
    warning = [ln for ln in d.path.read_text().splitlines() if "Warning" in ln]
    assert len(warning) == 1 and "feat/x" in warning[0] and "main" in warning[0]


def test_each_line_is_on_disk_as_soon_as_it_is_written(tmp_path):
    d = _digest(tmp_path)
    d.header("m", "c", "v", "abc", "main")
    d.line(12, "px0012_funding_skew", "1h", "new", "Funding skew", "close call")
    assert "px0012_funding_skew@1h" in d.path.read_text()       # no close() needed: written and flushed
    d.line(13, None, None, None, None, "dropped after 3 fix-ups")
    lines = d.path.read_text().splitlines()
    first = next(ln for ln in lines if "px0012" in ln)
    for s in ("10:05", "0012", "new", "Funding skew", "close call"):
        assert s in first
    assert any("0013" in ln and "dropped after 3 fix-ups" in ln for ln in lines)
    assert lines.index(first) < next(k for k, ln in enumerate(lines) if "0013" in ln)


def test_a_line_is_one_line_whatever_the_model_put_in_the_title(tmp_path):
    d = _digest(tmp_path)
    d.line(1, "px0001_x", "1h", "new", "Two\nlines\r\n# and a heading", "no signal")
    assert len(d.path.read_text().splitlines()) == 1


def test_a_pause_says_why_and_until_when(tmp_path):
    d = _digest(tmp_path)
    d.pause("the day's 12 judge calls are used", datetime(2026, 11, 3, 0, 0, tzinfo=UTC))
    text = d.path.read_text()
    assert "paused" in text and "the day's 12 judge calls are used" in text and "2026-11-03 00:00 UTC" in text


def test_finish_writes_the_counts_the_new_progress_ideas_the_stop_reason_and_tokens(tmp_path):
    d = _digest(tmp_path)
    d.header("m", "c", "v", "abc", "main")
    d.line(1, "px0001_a", "1h", "new", "A", "no signal")
    d.finish({"no signal": 3, "close call": 1, "dropped": 2}, ["#14 Funding skew: close call"], "trials",
             1_234_567, 89_012)
    text = d.path.read_text()
    end = text[text.index("px0001_a"):]
    for s in ("close call 1", "no signal 3", "dropped 2", "#14 Funding skew: close call", "trials", "1,234,567",
              "89,012"):
        assert s in end
    # piles in the spec's order, progress first
    assert end.index("close call 1") < end.index("no signal 3") < end.index("dropped 2")


def test_finish_with_nothing_new_says_so(tmp_path):
    d = _digest(tmp_path)
    d.finish({}, [], "signal", 0, 0)
    text = d.path.read_text()
    assert "signal" in text and "none" in text


def test_a_research_round_is_one_line_with_its_summary_indented_under_it(tmp_path):
    d = _digest(tmp_path)
    d.research([17, 18, 19], 4, "ok", "The fades died on costs.\n\n## End\nCarry is next.  ")
    assert d.path.read_text().splitlines() == [
        "- 10:05 · research: +3 ideas (#17, #18, #19); stop list 4 items",
        "  The fades died on costs.", "  ## End", "  Carry is next."]


def test_a_failed_research_call_says_why_on_its_line(tmp_path):
    d = _digest(tmp_path)
    d.research([], 0, "error: model_call", None, "claude exited 1;\nstderr: timed out")
    assert d.path.read_text().splitlines() == ["- 10:05 · research: error model_call: claude exited 1; stderr: timed out"]


@pytest.mark.parametrize("args, line", [
    (([5], 1, "ok", "s"), "research: +1 idea (#5); stop list 1 item"),
    (([], 0, "ok", "s"), "research: +0 ideas; stop list 0 items"),
    (([], 2, "capped", "s", None, 3), "research: 3 ideas not added (cap); stop list 2 items"),
    (([], 0, "capped", "s", None, 1), "research: 1 idea not added (cap); stop list 0 items"),
    (([], 0, "invalid", None), "research: invalid after fix-ups"),
    (([], 0, "error: model_call", None), "research: error model_call"),
    (([], 0, "error: signal", None), "research: error signal"),
])
def test_each_research_outcome_has_its_line(tmp_path, args, line):
    d = _digest(tmp_path)
    d.research(*args)
    lines = d.path.read_text().splitlines()
    assert lines[0] == f"- 10:05 · {line}" and len(lines) == (2 if args[3] else 1)


# -- the referee (referee-and-human-stops spec, Digest)
def _verdict(title, decision, reason="r"):
    return {"title": title, "decision": decision, "reason": reason, "sources": []}


def test_a_refereed_round_counts_its_verdicts_and_gives_each_rejections_reason_under_it(tmp_path):
    d = _digest(tmp_path)
    referee = {"web": True, "fetches": [], "denied": [], "verdicts": [
        _verdict("Basis squeeze", "accept"), _verdict("Carry unwind", "revise"),
        _verdict("Round-number\ncascade", "reject", "Urquhart (2017): no round-number\neffect survives costs"),
        _verdict("Volume premium", "reject", "Garfinkel et al. (2025) find none")]}
    d.research([17, 18], 1, "ok", "The fades died on costs.", referee=referee)
    assert d.path.read_text().splitlines() == [
        "- 10:05 · research: +2 ideas (#17, #18); stop list 1 item; 4 cards: 1 accepted, 1 revised, 2 rejected",
        "  rejected: Round-number cascade: Urquhart (2017): no round-number effect survives costs",
        "  rejected: Volume premium: Garfinkel et al. (2025) find none",
        "  The fades died on costs."]


def test_one_card_is_one_card(tmp_path):
    d = _digest(tmp_path)
    d.research([17], 0, "ok", None, referee={"web": False, "verdicts": [_verdict("Basis squeeze", "accept")]})
    assert d.path.read_text().splitlines() == [
        "- 10:05 · research: +1 idea (#17); stop list 0 items; 1 card: 1 accepted, 0 revised, 0 rejected"]


def test_a_referee_that_failed_says_why_and_the_round_keeps_its_stop_list_and_summary(tmp_path):
    d = _digest(tmp_path)
    referee = {"web": True, "error": "model_call: claude exited 1;\nstderr: x"}
    d.research([], 2, "ok", "Carry is next.", referee=referee)
    assert d.path.read_text().splitlines() == [
        "- 10:05 · research: +0 ideas; stop list 2 items; referee error: model_call: claude exited 1; stderr: x",
        "  Carry is next."]

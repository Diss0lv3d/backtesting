"""The session digest: the human's markdown record of one session, written as it happens."""

from __future__ import annotations

from datetime import datetime, timezone

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

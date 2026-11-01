# Research Referee and Human Stop List Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The human can rule out directions for the whole loop (a stop list in every proposal and research prompt), and every research round's cards are reviewed by a skeptical referee call before any enters the record.

**Architecture:** A `stops` table with CLI and page controls feeding a fixed prompt section; a `RefereeReport` contract beside `ResearchNote`; a referee step inside `_research` between the note's checks and `add_ideas`, using the shared caller and fix-up mechanism; verdicts recorded on the research row and shown in the digest and on the Ideas page.

**Tech Stack:** Python 3.12, SQLAlchemy (SQLite), Streamlit, typer, pytest. No new dependency.

**Spec:** `docs/superpowers/specs/2026-11-01-referee-and-human-stops-design.md` (binding), building on `docs/superpowers/specs/2026-11-01-research-round-design.md` and the proposer spec.

## Global Constraints

- The referee call is the shared caller (`_call`): tool-less `claude -p`, the guarded model, the init check, usage-limit waits, seq None token accounting. No web, no file access, no new subprocess.
- The referee schema has no `oneOf`/`allOf`/`anyOf` at its top level; "revised iff revise" is checked in code.
- Prompts carry nothing time-bearing and no CLI error text; the human's stop items are fixed text the human wrote (one line, ≤ 200 chars).
- Fail closed: a referee failure adds no cards; it never loses the round's stop list and never stops the session (except `ModelConfigError`).
- `perp_lab/proposer/` boundary rules hold (tests/test_proposer_boundaries.py).
- record.db exists on the user's machine: new tables via `create_all`; the new `research.referee` column via the existing column migration in `Record.__init__`.
- Config: `research_referee: true` (default).
- Style: match the surrounding code; TDD; one concern per commit; stage by path; trailers per the dispatch.

## Review Focus

1. A referee reply that disagrees with the note (wrong count, wrong order, unknown title, revised missing or present on accept) is refused as a fix-up, never half-applied.
2. Fail closed: after a referee error or invalid-after-fix-ups, no card is added, the stop list and summary are kept, the session goes on.
3. The human's stop list is in every proposal and research prompt (before the research list), absent when empty, and survives `research_every: 0`.
4. A revised card goes through `human.check`; a revision that fails it is a fix-up, not a silent add.
5. The e2e sentinel check covers the referee call.

---

### Task 1: The human's stop list — record, CLI, prompts

**Files:** Modify `perp_lab/proposer/record.py`, `perp_lab/cli.py`, `perp_lab/proposer/prompt.py`, `perp_lab/proposer/loop.py`. Test: `tests/test_record.py`, `tests/test_proposer_cli.py`, `tests/test_prompt.py`, `tests/test_loop.py`.

**Interfaces:**
- Table `stops` (id, text, created_at, removed_at). `Record.add_stop(text) -> int` (validates: non-empty after strip, one line, ≤ 200 chars; raises ValueError with a human message), `Record.remove_stop(id)` (ValueError on unknown or already removed), `Record.stops() -> list[dict]` (in force, by id).
- CLI group `lab stop`: `add TEXT` (prints the real-data warning, then "added stop #N"), `remove ID`, `list`. Refusals exit 2; a missing record refuses without creating it (as `lab idea` does).
- `prompt.human_stop_section(items: list[str]) -> str`: `"Directions the human has ruled out:\n- …\n"` or `""`.
- Proposal prompt: record, human stop section, research stop section (with the human-first sentence), instruction — empty parts skipped (today's exact prompt when both are empty). Research prompt: record, human stop section, research stop section, research instruction. The research instruction gains one sentence: the human's directions are not the research step's to revise, and its own list must not restate them.
- Unaffected by `research_every: 0`.

- [ ] Tests first (record rules; CLI add/remove/list/refusals/missing record; section text; placement in both prompts; absent when empty; survives research off; no times), fail, implement, pass, full suite.
- [ ] Commits: `feat(proposer): the human's stop list in the record and the CLI`, `feat(proposer): the human's stop list in every proposal and research prompt`.

### Task 2: The human's stop list on the Ideas page

**Files:** Modify `dashboard/ideas.py`, `README.md`. Test: `tests/test_dashboard.py`.

**Interfaces:** a "Your stop list" expander near the top of the Ideas page: the items in force each with a Remove button, an add box (text input + Add button) with the real-data warning; errors from `Record.add_stop` shown; missing or unseeded record → the expander is absent (no state dir created). README: the human stop list (page and CLI), where it reaches, and the warning.

- [ ] Tests first (AppTest: add, remove, refusal message, absent without a record), fail, implement, pass, full suite.
- [ ] Commits: `feat(dashboard): the human's stop list on the Ideas page`, `docs(proposer): the human's stop list`.

### Task 3: The referee contract and the research row's referee column

**Files:** Modify `perp_lab/proposer/contract.py`, `perp_lab/proposer/research.py`, `perp_lab/proposer/record.py`, `perp_lab/proposer/settings.py`, `config/lab.yaml`. Test: `tests/test_contract.py`, `tests/test_research.py`, `tests/test_record.py`, `tests/test_proposer_settings.py`.

**Interfaces:**
- `contract.Verdict` (title: str one line; decision: one of accept/revise/reject; reason: one line ≤ 300; revised: `ResearchIdea | None`), `contract.RefereeReport` (verdicts: tuple of Verdict, at most `research_max_ideas`). `referee_schema(timeframes, max_ideas)`, `describe_referee(...)`, `parse_referee(obj, timeframes, max_ideas)` from the shared machinery (nullable nested object support if the machinery lacks it — extend minimally, test it; no top-level combinator).
- `research.check_report(report, note, record, pc) -> list[str]`: exactly one verdict per note card, titles matching in order; `revised` present iff `revise`; each revised card through `human.check` (title: the original or a new unused one; distinct within the result).
- `research.referee_result(report, note) -> list[ResearchIdea]`: the cards to add (accepted as written, revised as revised).
- `ProposerConfig.research_referee: bool` (StartRefused if not a bool); config default `true`.
- `research.referee` JSON column, nullable, via the column migration (test on an old record.db with a research table lacking it).

- [ ] Tests first (schema shape and no combinator; parse/describe agree; limits; check_report cases incl. mismatched count/order/title, revised iff revise, a revised card failing human.check; referee_result; settings; migration), fail, implement, pass, full suite.
- [ ] Commits: `feat(proposer): the referee report contract and its checks`, `feat(proposer): the research row keeps the referee's verdicts`.

### Task 4: The referee in the research round

**Files:** Modify `perp_lab/proposer/prompt.py`, `perp_lab/proposer/loop.py`, `perp_lab/proposer/digest.py`. Test: `tests/test_prompt.py`, `tests/test_loop.py`, `tests/test_digest.py`.

**Interfaces:**
- `prompt.referee_instruction(pc) -> str` (fixed text per the spec's "The call" + `describe_referee`), `prompt.note_cards(note) -> str` (the note's cards as the model wrote them, builds_on included, as JSON or a fixed layout — no times).
- In `_research`, after a valid note with ideas and not capped, when `pc.research_referee`: one referee call through `_call` with the referee schema (same transcript), fix-ups on parse/`check_report` errors, then `add_ideas(referee_result(...))`. Fail closed per the spec (no cards; stop list and summary kept; outcome `ok`; referee outcome on the row and the digest). `research_referee: false` → today's behaviour.
- `Digest.research(...)` reports the verdict counts and each rejection's reason; a referee failure says why.

- [ ] Tests first (fake Deps: accept/revise/reject mixes and what's added; fix-up on mismatch and on a failing revised card; fail closed on call error and invalid-after-fix-ups; skipped when no ideas, capped, or off; the referee prompt exactly static context + record + stop sections + cards + instruction; tokens counted; digest lines; no times or CLI text in any prompt), fail, implement, pass, full suite.
- [ ] Commits: `feat(proposer): the referee instruction`, `feat(proposer): a referee reviews each research round's cards`.

### Task 5: Verdicts on the Ideas page, and end to end

**Files:** Modify `dashboard/ideas.py`, `README.md`, `tests/test_proposer_e2e.py`, `docs/superpowers/specs/2026-11-01-research-round-design.md` (a pointer to the referee spec). Test: `tests/test_dashboard.py`, `tests/test_proposer_e2e.py`.

**Interfaces:** the research notes list each card with its verdict and reason (revised → the added idea), and a referee failure; malformed `referee` values guarded like the reply. README: the referee (what it checks, fail-closed, `research_referee`). e2e: the research round's fake reply has ideas, the fake referee replies (e.g. one accept, one reject), the record holds only the accepted card, and the sentinel/time check covers the referee call.

- [ ] Tests first, fail, implement, pass; e2e; full suite.
- [ ] Commits: `feat(dashboard): the referee's verdicts in the research notes`, `test(proposer): the end-to-end research round is refereed`, `docs(proposer): the research referee`.

# The research referee and the human's stop list

Date: 2026-11-01. Status: approved by the user in chat. The user, after the first real research round re-proposed two
directions the human's web research had already ruled out: "add the stop list input, keep web research here, i think
when the research arrives, someone like you should fact check it or try to correct or improve it". Choices not quoted
from the user are the author's, marked ★.

Builds on `docs/superpowers/specs/2026-11-01-research-round-design.md` (the research round, including its "Amended
during the build" section) and the proposer spec it builds on. Both stay binding where this document doesn't change
them.

## Intent

Two gaps the first real research round showed:
- **The human's knowledge doesn't reach the loop.** Web research is done by hand, outside the loop (no web access in
  the loop keeps the holdout clean). When it rules a direction out, nothing tells the builder or the research step.
- **Nobody checks the research step's work.** Its cards go straight into the record. Its first real round proposed a
  round-number stop cascade and a high-volume return premium — both directions with published evidence against them
  that the model knows (Urquhart 2017; Garfinkel et al. 2025, both before its training cutoff) but didn't apply.

## Goals

1. **The human's stop list.** The human adds and removes one-line directions to rule out, from the Ideas page and the
   CLI. They are in force in every proposal prompt and every research prompt, beside the research step's own list,
   and only the human changes them.
2. **The referee.** After a research round's note passes its checks and before any of its cards enters the record, a
   second call reviews each card adversarially and returns accept, revise (with a corrected card) or reject (with a
   reason). Only accepted and revised cards are added. The verdicts are shown to the human.
3. Web fact-checking stays a human-side activity (in Claude Code sessions); its findings reach the loop through the
   human's stop list and Exclude.

## Non-goals

- Web access for the referee or the research step (the research-round spec's anti-goal stands).
- A referee for proposals (a critic before each trial) — a separate follow-up.
- The referee closing, excluding or editing ideas already in the record, or editing the stop list.

## Anti-goals

- The referee becomes a second author that rewrites everything: a revision must keep the card's idea (it corrects
  claims, sharpens the rule, fixes the exit or the timeframe); a different idea is a reject ★.
- The human's stop items carry real-data statistics to the model: the same warning as human cards, and the same
  one-line rule.
- A referee failure loses a research round's stop list or stops a session.

## How it works

### The human's stop list ★

- **Record:** a new table `stops`: id, text (one line, at most 200 characters), created_at, removed_at (null while in
  force). The column migration pattern isn't needed (a new table).
- **CLI:** `lab stop add "TEXT"`, `lab stop remove ID`, `lab stop list`. Refusals exit 2 (empty, multi-line, too long,
  unknown id, already removed). `add` prints the same real-data warning as `lab idea add`.
- **Ideas page:** a "Your stop list" expander near the top: the items in force, each with a Remove button, and an
  add box with the warning. Writes go only to record.db.
- **Prompts:** a fixed section, "Directions the human has ruled out:" followed by the items, placed before the
  research step's stop section in every proposal prompt and every research prompt (absent when empty). The research
  instruction says these are the human's and are not the research step's to revise; its own stop list must not
  restate them ★. The human-first sentence ("When the human asks for an idea below, that request comes before this
  list.") stays where it is.
- **Not affected by** `research_every: 0` (it's the human's, not research's) ★.

### The referee ★

- **When:** inside a research round, after the note passed `parse_research` and `check_note` and before
  `add_ideas`. Skipped when the note has no ideas, and in a capped round (no cards are added anyway).
  Config `research_referee: true` (default on); `false` adds the cards unreviewed as today.
- **The call:** the same caller as a proposal and a research round (tool-less `claude -p`, the guarded model, the
  init check, usage-limit waits, token accounting with seq None), with the static context as system prompt. The user
  message is the rendered record, both stop sections, the research note's ideas (as the cards the model wrote,
  including `builds_on`), and a fixed referee instruction:
  - you are a skeptical referee; for each card decide accept, revise or reject;
  - check it against the record: a near-copy of a judged plugin's mechanism, a sign flip of a closed idea, a filter on
    a closed idea's entry, a restatement of an open idea, an edge that would come from the exit;
  - check it against the literature you know: name published evidence against it if any; check that the citations
    in the card exist and say what the card claims;
  - check it against costs: can the move it expects pay the round-trip cost at its timeframe;
  - check it against the data: the columns it needs exist and are readable;
  - revise only to correct a claim, sharpen the rule, neutralise the exit or fix the timeframe — keeping the idea; a
    different idea is a reject;
  - no numbers from the record in a card.
- **The contract** (`contract.py`, same machinery, no top-level combinator): `RefereeReport`: `verdicts`, a list with
  exactly one `Verdict` per card in the note's order: `title` (the card's title, to match), `decision` (one of
  `accept`, `revise`, `reject`), `reason` (one line, at most 300 characters), `revised` (a `ResearchIdea` or null —
  required exactly when the decision is `revise`; checked in code, since the schema can't express it without a
  combinator).
- **Checks in code:** one verdict per card, titles matching the note's in order; `revised` present iff revise; each
  revised card passes `human.check` (as research cards do) and keeps the original title or a new unused one ★.
  A failing reply goes back as a fix-up (the same mechanism, within `fixups_per_proposal`).
- **What is added:** accepted cards as written; revised cards as revised; rejected cards not at all. All or none in
  one `add_ideas` call, as today.
- **Failures:** a referee call that fails (`ModelCallError` after retries) or a reply still invalid after its fix-ups:
  the round adds **no** cards (fail closed — unreviewed cards don't slip through) ★, keeps its stop list and summary,
  and its outcome is `ok` with the referee outcome recorded; the digest says why. `ModelConfigError` stops the
  session; `UsageLimit` waits, as everywhere.
- **Record:** the research row gains a `referee` JSON column (the verdicts, or the error) — a new column on an existing
  table, so it goes through the column migration on open. Transcript: the same `research-<id>.jsonl`.
- **Digest:** the research line reports the verdicts (`3 cards: 1 accepted, 1 revised, 1 rejected`), and each
  rejection's reason under it.
- **Ideas page:** the research notes list each card with its verdict and reason (and "revised" ideas link to what was
  added); a referee failure shows as such.

### Accounting

The referee call counts toward the day's token caps like any call, never toward trials. One extra call (plus fix-ups)
per research round with ideas, so about one call per 5 trials.

## Testing

- Human stops: record, CLI, page (add, remove, refusals, warning); the section in proposal and research prompts,
  before the research list, absent when empty; unaffected by research off; no times in prompts.
- Referee: contract (no top-level combinator; one verdict per card; revised iff revise; limits); accept / revise /
  reject outcomes and what's added; a revised card failing human.check → fix-up; fail-closed on call error and on
  invalid-after-fix-ups (no cards, stop list kept); skipped when no ideas, when capped, and when
  `research_referee: false`; the referee prompt built only from the static context, the record, the stop sections,
  the note's cards and the instruction (no times, no CLI error text); digest and page; e2e: the research round's
  referee call is covered by the sentinel check.
- One real call by the controller: the referee schema accepted by the API and a real referee reply parsed, on a copy
  of the real record with the cards from the first real research round.

## Residuals

- The referee is the same model as the research step: it shares its knowledge and some of its blind spots. Its value
  is the adversarial job, not new information. Web fact-checks by the human remain the deeper check.

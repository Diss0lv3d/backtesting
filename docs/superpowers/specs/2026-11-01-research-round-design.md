# The research round: research inside the proposer loop

Date: 2026-11-01. Status: approved by the user in chat ("build the loop"); the user skipped the design questions,
so every choice below not quoted from the user is the author's call, marked ★.

Builds on the proposer spec, `docs/superpowers/specs/2026-10-19-proposer-design.md` (binding wherever this
document doesn't change it), and on the human-ideas amendment at its end.

## Intent

The user, 2026-11-01: "this idea, research, testing all of this should keep happening in a loop", "we can do
research after every 5 trials". Today the proposer loops on propose → test → record, and the *research* — reading
what the record has taught, choosing new directions, writing idea cards, naming directions to stop — happens by hand
outside it (the cards in `docs/research/2026-11-01-ideas/` were made that way). After three trials the record had no
untried idea left except the human's, so an unattended week would have the builder invent ideas in the same families
that already failed. The research round puts that step inside the loop.

## Goals

- Every `research_every` (5) proposer trials, the session runs one research round before its next proposal.
- A research round reads the record and the lessons, and returns:
  - up to `research_max_ideas` (3) new idea cards, each checked exactly like a human card;
  - a short list of directions to stop;
  - a summary for the human.
- The cards enter the record as open ideas (origin `research`), and the proposer can work on them like any other open
  idea. The stop list is shown to the proposer in every later round, replacing the previous one.
- The human sees each research round in the digest and on the Ideas page.

## Non-goals

- A critic step before each trial (a model reviewing a proposal before it spends a trial). It's a good follow-up;
  not in this build ★.
- Research that can exclude, close or edit ideas. Only the human excludes ★: an agent that can close ideas could
  close the good ones, and a closed idea never reopens.
- Changing the trial budget, the gates or the judge.

## Anti-goals: failures even if it works

- **The research step sees market history from the holdout period.** ★ No live web access. Web search results and
  pages include market news and prices from after `holdout_start` (2026-07-01). An agent that read them could steer
  ideas toward what happened in the holdout, and the exams — the lab's only clean test — would be contaminated without
  anyone seeing it. So the research call is the same tool-less `claude -p` call as the proposer's, on the same pinned
  model whose training cutoff (2026-06-30) is guarded before `holdout_start`. It reasons from the record and its own
  knowledge of the literature. (Web research with a recorded caveat can be designed later, behind a switch.)
- **Real-data statistics reach the model outside verdicts.** The research round sees exactly what the proposer sees
  (the static context and the rendered record), nothing else: no `data/`, `runs/`, `lab.db`, judge logs, research
  notes written by humans, or file access.
- **The research step floods the record.** Untried research ideas are capped (below), so the record doesn't fill with
  ideas faster than trials can test them.
- **Research failure ends a week-long session.** A failed research round is logged and skipped; the session goes on.

## How it works

### When a research round runs ★

- Before a proposal round, when the proposer trials counted since the last research round reach `research_every`
  (5). Trials are counted as `Record.trials_used()` counts them (a refusal and a lookahead kill don't count).
- Also when the record has **no open idea a proposal could work on** (every idea closed, or open but unanswerable by
  `assemble.answerable`) — the builder would otherwise only invent from scratch — but at most once per trial count:
  if a research round already ran at the current `trials_used()`, it doesn't run again until a trial is spent.
- Never more than one research round in a row, and never while the day's judge quota or token caps are spent (the
  same waits as a proposal round apply first).
- The first research round of a fresh record runs after its first 5 proposer trials (or earlier by the "nothing to
  work on" rule).

### The call

- The same caller as a proposal (`model.call_with_retries`, the same argv, environment, init check, usage-limit and
  error handling), with the same system prompt (the static context) and a different schema and instruction.
- The user message is the rendered record (`prompt.render_record`) plus a fixed research instruction ★:
  - what a research round is for: read what the record has taught (the piles, the diagnostics of every judged
    proposal, what closed each idea), and propose new ideas that build on it rather than repeat it;
  - what counts as redundant (a near-copy of any judged plugin's mechanism, a sign flip of a closed idea, a filter
    added to a closed idea's entry, an idea whose edge would come from its exit rather than its entry);
  - that a stop with no target earns on trends whatever the entry, so new ideas should say how their exits stay
    neutral;
  - that cards must not cite numbers from the record — mechanism, literature and rule only;
  - return at most `research_max_ideas` cards, the stop list (at most 5 one-line items) and a summary.
- One call per round, plus up to `fixups_per_proposal` fix-ups when the reply fails validation (the same fix-up
  mechanism as proposals: the errors go back with the round's prompt and the rejected reply).

### The research contract ★

A new typed contract in `perp_lab/proposer/contract.py`, generated into the schema, the description and the parser
the same way `Proposal` is (no top-level `oneOf`/`allOf`/`anyOf` — the API refuses them, Ruling R42):

- `ResearchNote`:
  - `ideas`: a list of 0..`research_max_ideas` `ResearchIdea`;
  - `stop`: a list of 0..5 strings, each one line, at most 200 characters;
  - `summary`: a string, at most 2,000 characters (for the human; the model never sees it again).
- `ResearchIdea`: the `IdeaCard` fields, plus `timeframe` (one of `proposer.timeframes`) and `builds_on` (one line,
  at most 300 characters: which lesson or result in the record it builds on — for the human).

Each idea is checked with the human-card rules (`human.check`: the card checks, a new title, a proposer timeframe,
readable columns, one-line title). An invalid idea fails the whole reply (fix-up), so the record never takes half a
note.

### What it writes

- Each idea → `Record.add_ideas` with origin `research` (all or none, one transaction). Ideas get no flag.
- The cap ★: if the record already holds `research_open_cap` (6) or more open research-origin ideas with no judged
  proposal, the round adds none of its ideas (they're kept in the research row, marked "not added: cap") and still
  updates the stop list.
- A new `research` table in record.db: id, session_id, at, trials_used at the round, the reply (JSON), the ids of
  the ideas added, tokens in/out, outcome (`ok` / `invalid` / `error: <code>`). The column migration on open (from the
  human-ideas change) creates nothing here — `create_all` creates a new table on an existing db.
- The stop list of the latest `ok` round is shown to the proposer in every later round, as a fixed heading plus the
  list: "Directions the research step recommends avoiding:" ★. It replaces, not accumulates.
- The digest gets one line per research round (`research: +3 ideas (#17, #18, #19); stop list 4 items` or the error).

### Accounting

- Research calls count toward the day's token caps (they go through the calls table like any call) and are
  recorded with no proposal seq.
- They don't count as trials, judge calls or proposals; `trials_used()` is unchanged.

### Failures

- `ModelConfigError` (the init check) stops the session `model_config`, as for a proposal.
- `UsageLimit`: the same wait as a proposal, then the same call again.
- `ModelCallError` after retries, or an invalid reply after the fix-ups: the round is recorded with its outcome,
  the digest says so, and the session goes on to its proposal round. The next research round is due after
  `research_every` more trials.

### Where the human sees it

- **Digest:** the research line, and the summary under it.
- **Ideas page:** research-origin ideas carry a "research" tag; a "Research notes" expander shows the latest round's
  summary, its stop list and when it ran (times are fine for the human).
- **`lab ideas`:** research ideas marked `(research)`.

## Config

```yaml
proposer:
  research_every: 5          # proposer trials between research rounds; 0 turns research off
  research_max_ideas: 3      # new idea cards per research round
  research_open_cap: 6       # untried research ideas the record may hold before a round adds no more
```

## Testing

- The contract: schema has no top-level combinator; parse/describe/schema agree; limits enforced.
- Scheduling with fake Deps and a fake clock: a research round after every 5 trials; the "nothing to work on" rule
  at most once per trial count; research off at `research_every: 0`.
- Writes: ideas added all-or-none with origin research; the cap; the stop list replacing the previous one and
  appearing in the next proposal prompt; the digest line.
- Failures: an invalid reply after fix-ups, a call error and a usage limit don't stop the session; an init-check
  failure does.
- Isolation: the research prompt is built from the static context, the rendered record and the fixed instruction
  only — the existing sentinel end-to-end test extended with one research round (no sentinel, no planted time in any
  recorded call).
- One real call (by the controller, not a test): the research schema accepted by the real API and the reply parsed.

## Residuals

- The model's training covers market history up to June 2026, so research ideas, like proposals, are hypotheses
  until their exams (the proposer spec's Model section).
- The research instruction asks the model not to cite numbers from the record in its cards; the record it sees holds
  only verdict figures, which the proposer already sees, so a card that quotes them leaks nothing new.

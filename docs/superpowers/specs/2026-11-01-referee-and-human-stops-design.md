# The research referee and the human's stop list

Date: 2026-11-01. Status: approved by the user in chat. The user, after the first real research round re-proposed two
directions the human's web research had already ruled out: "add the stop list input, keep web research here, i think
when the research arrives, someone like you should fact check it or try to correct or improve it", then "connect point
2 and 3 together" — the fact-checker (2) should do the web checking (3): a referee with web access, inside the loop.
Choices not quoted from the user are the author's, marked ★.

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
3. **The referee checks the web** ("connect point 2 and 3"): it may read academic sources — and only those — to check
   each card's claims. Open-ended web research (finding new ideas) stays a human-side activity; its findings reach
   the loop through the human's stop list and Exclude.

## Non-goals

- Web access for the research step (the research-round spec's anti-goal stands for the author of ideas). Web search,
  news, prices or any non-academic site for the referee.
- A referee for proposals (a critic before each trial) — a separate follow-up.
- The referee closing, excluding or editing ideas already in the record, or editing the stop list.

## Anti-goals

- **The referee reads market history from the holdout period.** The referee is the one place web access enters the
  loop, so it is fenced in code, not by a prompt (ADR 0006) ★:
  - **No search tool.** Only `WebFetch`, whose domains can be limited; `WebSearch` returns snippets from any site.
  - **An allowlist of academic sources** (`proposer.referee_domains`, default: `arxiv.org`, `export.arxiv.org`,
    `api.semanticscholar.org`, `api.crossref.org`, `ideas.repec.org`, `papers.ssrn.com`, `www.nber.org`), passed to
    the CLI as `--allowedTools "WebFetch(domain:<d>)"` rules; with `--permission-prompts none` every other fetch is
    denied by the CLI (probe, 2026-11-01, CLI 2.1.316: an arXiv fetch ran, a CoinGecko fetch was denied and reported
    in the result's `permission_denials`).
  - **Checked again in code:** the stream's init must list exactly `StructuredOutput` and `WebFetch`; every `WebFetch`
    the model issues is recorded with its URL; a fetch whose host is not on the allowlist that was *not* denied (a
    tool result that isn't the CLI's denial) kills the call and stops the session `model_config`.
  - **It can only judge cards that exist.** Accept, revise (keeping the idea) or reject — it never adds an idea.
  - **Every card a web-informed referee reviewed is marked** (`ideas.web_reviewed`), and the Ideas page and the exam
    review packet say so: academic papers after the cutoff may discuss the holdout period, so its exam is weaker
    evidence. Residual, accepted by the user.

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
- **The call:** the shared caller (the guarded model, usage-limit waits, token accounting with seq None, the
  transcript), with the static context as system prompt, but with web access as fenced above: `--tools "WebFetch"`,
  the allowlist rules, the init check expecting `StructuredOutput` and `WebFetch`, and the fetch audit. Config
  `research_referee_web: true` (default); `false` makes the referee tool-less like every other call (no fetches, no
  `web_reviewed` mark). The user
  message is the rendered record, both stop sections, the research note's ideas (as the cards the model wrote,
  including `builds_on`), and a fixed referee instruction:
  - you are a skeptical referee; for each card decide accept, revise or reject;
  - check it against the record: a near-copy of a judged plugin's mechanism, a sign flip of a closed idea, a filter on
    a closed idea's entry, a restatement of an open idea, an edge that would come from the exit;
  - check it against the literature: use the allowed academic sources to find published evidence for or against it
    and to check that the card's citations exist and say what the card claims; name what you read (URLs) in the
    verdict; never look for prices, charts or market news (the allowlist blocks them anyway);
  - check it against costs: can the move it expects pay the round-trip cost at its timeframe;
  - check it against the data: the columns it needs exist and are readable;
  - revise only to correct a claim, sharpen the rule, neutralise the exit or fix the timeframe — keeping the idea; a
    different idea is a reject;
  - no numbers from the record in a card.
- **The contract** (`contract.py`, same machinery, no top-level combinator): `RefereeReport`: `verdicts`, a list with
  exactly one `Verdict` per card in the note's order: `title` (the card's title, to match), `decision` (one of
  `accept`, `revise`, `reject`), `reason` (one line, at most 300 characters), `sources` (0..5 URLs it relied on, each
  on the allowlist — checked in code), `revised` (a `ResearchIdea` or null — required exactly when the decision is
  `revise`; checked in code, since the schema can't express it without a combinator).
- **Checks in code:** one verdict per card, titles matching the note's in order; `revised` present iff revise; each
  revised card passes `human.check` (as research cards do) and keeps the original title or a new unused one ★.
  A failing reply goes back as a fix-up (the same mechanism, within `fixups_per_proposal`).
- **What is added:** accepted cards as written; revised cards as revised; rejected cards not at all. All or none in
  one `add_ideas` call, as today.
- **Failures:** a referee call that fails (`ModelCallError` after retries) or a reply still invalid after its fix-ups:
  the round adds **no** cards (fail closed — unreviewed cards don't slip through) ★, keeps its stop list and summary,
  and its outcome is `ok` with the referee outcome recorded; the digest says why. `ModelConfigError` stops the
  session; `UsageLimit` waits, as everywhere.
- **Record:** the research row gains a `referee` JSON column (the verdicts with their sources, the fetches the model
  issued and those denied, or the error); ideas gain `web_reviewed` (boolean) — new columns on existing tables, so
  both go through the column migration on open. Transcript: the same `research-<id>.jsonl`.
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
- Web fence: the argv carries `--tools WebFetch` and one `--allowedTools WebFetch(domain:…)` rule per allowed domain;
  the init check (exactly StructuredOutput + WebFetch); a fetch off the allowlist that wasn't denied → kill +
  `model_config`; a denied one is recorded and harmless; `sources` off the allowlist refused as a fix-up.
- Referee: contract (no top-level combinator; one verdict per card; revised iff revise; limits); accept / revise /
  reject outcomes and what's added; a revised card failing human.check → fix-up; fail-closed on call error and on
  invalid-after-fix-ups (no cards, stop list kept); skipped when no ideas, when capped, and when
  `research_referee: false`; the referee prompt built only from the static context, the record, the stop sections,
  the note's cards and the instruction (no times, no CLI error text); digest and page; e2e: the research round's
  referee call is covered by the sentinel check.
- One real call by the controller: the referee schema accepted by the API, the allowlist enforced, and a real referee
  reply parsed, on a copy of the real record with the cards from the first real research round (the round-number and
  volume-premium cards, which the human's web research had rejected — a good test of the referee).

## Residuals

- The referee is the same model as the research step: it shares its knowledge and some of its blind spots. Its web
  access is academic only, so its value is checking claims against the literature, not market data.
- Academic papers published after `holdout_start` may discuss the holdout period. The referee can read them, so a card
  it accepted or revised could carry that knowledge, and a card it rejected could have been rejected for it. The
  `web_reviewed` mark keeps this visible at exam time; the user accepted this residual by asking for a web-checking
  referee.
- Cross-host redirects (R-RF2). The fence audits the URL the model asked for, not where the CLI landed: WebFetch
  documents that a redirect to a different host is reported back to the model, which must fetch the new URL itself,
  through the fence again, and the code relies on that. No allowlisted test URL redirects off-host (checked during
  the build: arXiv http→https on the same host, SSRN 403, NBER and Crossref 200). If WebFetch ever follows one,
  content from an off-list host could arrive under an allowed URL; the round's transcript would show it.
- A rejection steers later research (R-RF8). The titles of rejected cards reach every later research prompt, so a
  rejection the referee based on a post-holdout paper steers what the research step proposes next, by title only:
  no reason or source reaches it.
- The instruction to cite only pre-holdout sources is not a fence. The real run of 2026-11-02 fetched the metadata
  of an October 2026 paper from Crossref although told to cite only sources from before 2026-07-01 (it didn't cite
  it). The date-bounded searches (M3, below) cut what a search returns, but a fetch by DOI or URL has no date bound,
  and no code checks a source's date: the `web_reviewed` mark stays the safeguard.
- A breached call counts no tokens (R-RF9): the CLI's usage on a killed stream isn't trusted, so the day's caps
  undercount one call per breach.
- The audit bets on today's stream shapes (R-RF4): a shape a later CLI changes fails closed, so every review is
  skipped until the fence learns it (each round's digest line says so, and the digest's end counts them). The
  start-of-session web probe (below) catches a change in the flags, the init or the permission mode, not in how a
  fetch looks.
- A stop during the referee (M6, accepted) ends its round with the stop's outcome, so the round's new stop list is
  not in force until the next session's research round runs.

## Amended during the build (2026-11-01/02)

The controller's rulings (R-RF1…R-RF11), the final fix wave (M1…M10) and the conventions the build settled, with
what the real calls showed. Where they differ from the sections above, these win.

- **R-RF1: `referee_domains` is checked with the call's own rule.** Config validates each domain with `model.DOMAIN`
  (ASCII, two labels or more, none starting or ending with a hyphen, the last alphabetic, so no IP address; rules
  lowercased), so it can never accept a domain the call would refuse. An empty list is refused only when
  `research_referee_web` is on. If wrong: none.
- **R-RF2: cross-host redirects** are left to WebFetch's documented behaviour (Residuals). If wrong: content from an
  off-list host could arrive under an allowed URL; the real run's transcripts would show it.
- **R-RF3: the CLI does not deny every other fetch.** The text above says `--permission-prompts none` denies every
  fetch the allow rules don't name. It doesn't: CLI 2.1.316 fetches about 90 preapproved documentation hosts with
  no rule (a probe of 2026-11-02 fetched docs.python.org), and user settings (allow rules, a default mode) still
  apply. So a web call also runs with `--setting-sources ""` (no user, project or local settings) and
  `CLAUDE_CODE_EVAL_CONFINED=1` in its environment, which turns the preapproved hosts off (probe-verified:
  docs.python.org denied, arXiv allowed), and the init's `permissionMode` must be `default`, checked in code. The
  variable is undocumented, so the code fence stays the guarantee. If wrong (a later CLI drops the variable): a
  preapproved host's fetch is caught by the fence (R-RF5), never used.
- **R-RF4: what counts as denied.** The text above calls an off-list fetch harmless when its tool result is the
  CLI's denial. Denial texts differ by path (two seen), so the text is never read: an off-list fetch is denied only
  if its tool_result has `is_error` true (checked as each tool_result arrives) and the result lists its
  `tool_use_id` in `permission_denials`. Fail closed on anything else: a tool_result for a tool_use id the stream
  never showed, an off-list fetch whose denial never arrives (checked at the result and at the stream's end), an
  assistant block other than text, thinking, redacted_thinking or tool_use, a fetch whose URL differs from the one
  the API sent (when the stream shows it), and any line after a web call's result. A WebFetch in a tool-less call
  is a `ModelConfigError`: the CLI did not hold `--tools ""`. If wrong: a harmless shape is a breach, and that
  round's review is skipped (R-RF5).
- **R-RF5: a fence breach skips the review, not the session.** The text above (and the Testing section) say a fetch
  off the allowlist that wasn't denied kills the call and stops the session `model_config`. Instead it kills the
  call and raises `FenceBreach`, a distinct error that is never retried, and the referee fails closed for that
  round: no card added, the row's referee error `fence: <what>`, the digest's research line saying so, and the
  session goes on. A stopped week-long session costs more than a skipped review, and nothing from the killed call
  reaches the record or a later prompt (the transcript is the human's only). An init that fails its check (the
  tools, `permissionMode`) still stops the session `model_config`. If wrong: a repeating breach skips every review;
  the digest shows it each round, and its end counts them.
- **R-RF6: the CLI's own message is no breach.** The first real web call (Task 5's code) was killed on line 60: a
  user line the CLI writes itself to resume a reply cut off mid-stream (`"isSynthetic": true`, a text block
  "Your response above was cut off mid-stream. Resume directly …"). A user line with `isSynthetic: true` whose
  blocks are all text is allowed; a tool_result in it, or any other block, stays a breach. If wrong: CLI-written
  text reaches the referee (no fetched content: the fence still audits every fetch).
- **R-RF7: the only date in a user prompt is the referee's.** The text above and the plan keep every prompt free of
  anything time-bearing. The referee's web instruction now names the lab's `holdout_start` (from config:
  2026-07-01) and asks it to cite only sources published before it and to disregard later ones, because the first
  real call surfaced an October 2026 paper. Proposal and research prompts stay undated, the tool-less referee's
  instruction has no date, and no prompt carries a time of anything the lab did; the static context already states
  the research span's end, the same date. It can't be checked in code: the `web_reviewed` mark stays the safeguard.
  If wrong: none (an extra instruction).
- **M3: the referee's searches end before the holdout.** The second real run still fetched that paper's metadata, so
  the web instruction also gives each search API's bound, the day before `holdout_start`: Crossref
  `filter=until-pub-date:<day>`, Semantic Scholar `publicationDateOrYear=:<day>`, and arXiv
  `submittedDate:[199101010000 TO <yyyymmdd>2359]` joined to its query by AND (arXiv's API refuses an open
  `[* TO …]` range, so the range starts before its first submission). It is the instruction's second date, from
  the same config value. If wrong: none (an instruction; a fetch by DOI or URL is not bounded, Residuals).
- **M2: the referee is told it has a tool.** Its system prompt, the static context, says "You have no tools."; the
  web instruction adds "In this review, unlike a proposal, you have one tool: WebFetch, limited to the hosts below."
- **R-RF8: rejected cards don't come back.** With the referee on, every research prompt lists the titles of the
  cards the referee rejected in earlier rounds (the latest 30, each once, oldest first) after the stop sections,
  under the fixed heading "Cards the referee rejected (do not propose them again):", and the research instruction
  gains one sentence asking for none of them again, under their titles or new ones (both only when there are any).
  Titles only, which the research step wrote: never the referee's reasons or sources, which are web-informed. None
  while the referee is off: a human turns it off when it judges badly. If wrong: a few lines of extra prompt.
- **R-RF9: a breached call counts 0 tokens**, like any failed call (Residuals). If wrong: the day's caps undercount
  one call per breach.
- **R-RF10 (process):** the scoped re-reviews of two fix rounds were folded into the final whole-branch review. If
  wrong: the final review misses a fix-round regression; the end-to-end test and the full suite still gate.
- **R-RF11: text the code didn't write is shown escaped and defanged.** On the Ideas page every string a model or a
  page it read could have written (each verdict's title and reason, the referee's error, the research summary and
  stop items, `builds_on`, sources and fetches off the allowlist) has every ASCII punctuation character escaped, so
  it is never read as a link, an image, HTML or other markup, and is then defanged, because Streamlit's frontend
  autolinks URLs in text whatever the escapes (a real-browser check found 15 autolinks to an injected host):
  `://` → `[:]//`, `www.` → `www[.]`, `@` → `[@]`. The only links the page makes are to sources on the allowlist.
  Re-checked in a real browser at 563488c: no off-list link, no image. The human's own stop items go through the
  same helper (M4): pasted from a page they read, a link or an image shows as text. If wrong: displayed URLs read
  slightly odd.
- **The web path is probed at the start.** When a session would make the web call (`research_every` above 0,
  `research_referee` and `research_referee_web`), its start checks make a second probe, through the web call's own
  argv, environment, init check and fence with `referee_domains` as the allowlist, asked to fetch nothing. Any
  failure of it (a fence breach, a config or call error) refuses the start, naming the cause and that
  `research_referee_web: false` runs the referee without the web; a usage limit refuses it as the first probe's
  does. So a CLI update that breaks the web flags, the init's tools or `permissionMode` shows while the human is
  there, not as a review failed closed every round, hours in. `lab propose --trials 0` makes both probes.
- **M1: a call's tokens are every model's.** WebFetch summarises each fetched page with a second model, whose tokens
  appear only in the result's `modelUsage`. A call counts every model there once (input, cache creation and cache
  reads; output), never `usage` on top, and never less than `usage` says. The real referee call counted ≈394k input
  (≈349k of them cache reads) and ≈8.6k output for the referee, and ≈163k input and ≈14k output for the
  summariser: ≈557k and ≈22k against the day's caps. The Accounting section's "one extra call" is a round's
  heaviest by far.
- **M5: the digest's end counts the referee.** The finish block gains one line: the rounds the referee reviewed this
  session, their cards accepted, revised and rejected, and the reviews that failed by kind (`model_call`, `fence`,
  `invalid`, then any other), so a week of rejections or breaches shows in one place.
- **The mark travels.** A port copies its target's card, so it carries the target's `web_reviewed`, and the exam
  caveat with it. `lab ideas` marks a web-reviewed idea `(web-reviewed)` after its title (and `(research)`), and
  `lab exam`'s awaiting-review line for a promotion whose plugin version is a web-reviewed idea's proposal carries
  the same mark, with the review packet's caveat under it (M8).
- **What the referee sees.** Its research stop section is the list in force when the round began (the one the
  round was asked to revise), not the round's new one. Its prompt shares the research round's prefix: the record,
  the human's stop list, that list, then the round's cards and the referee instruction. Its verdicts pair with the
  row's `added` in order: the accepted and revised cards, in the verdicts' order.
- **A stop during the referee** (`model_config`, a signal, the deadline in a usage-limit wait) writes its reason as
  the row's referee error before it goes up: the note was valid, and the referee's call is the one that stopped.
- **The real calls** (Testing's "one real call by the controller"). The referee schema, with the nullable nested
  `revised` as `{"type": ["object", "null"]}` (no combinator at any depth), was accepted by the API in a tool-less
  call at f935aae, and the reply parsed. The first real web call (Task 5's code, a copy of the real record, the
  first real research round's two cards) fetched only allowlisted hosts and was killed by the synthetic message
  (R-RF6). The second (at 563488c, effort high) completed: 13 fetches (Crossref, Semantic Scholar, the arXiv API,
  SSRN, RePEc), all allowlisted, none denied; the report passed its checks; both cards were revised with
  literature-grounded reasons and allowlisted sources; $0.62 at API prices. It fetched the October 2026 paper's
  metadata (Residuals; M3).

# Task builder instructions (v6)

You turn web searches harvested from a user's agent transcripts into tasks that several search providers will replay. Each provider gets the same task and searches however it naturally would; a judge then compares what the providers found. The tasks must be fair to every provider and faithful to what the user's agent actually needed.

## Input

One record per task: the original date, the origin (`user`, `subagent` or `workflow`), the context before the searches (delegation text for subagents; recent user messages and the agent's text for main sessions), and the searches the agent ran.

## Output

For each record, write either a task or a drop:

- `task`: what the agent needed to find out, written as a request to a research assistant. Usually one to three sentences; a delegation with several questions may need a short list.
- `drop`: a short reason, when no fair public task can be written (see Drop).
- `basis`: `context` or `query` (see the last-resort rule).

## Rules

1. **State the need, not the searches.** Work from the context: what the agent was trying to learn and why. The search queries may only supply missing terms and details (a product name, a version, an error string), never new sub-questions, premises or source restrictions (a query that says "official docs" doesn't make "official docs only" a constraint). Two reasons: they were written for one provider, and later queries in a run often carry findings from earlier results (a name or fact the agent just learned).
2. **Scope to what these searches served.** When the agent's own text just before the searches names a narrower need than its brief ("I'll check one detail myself on X", while other agents handle the rest), the task is that narrower need. Otherwise, keep the context's questions, all of them: when the context (often a delegation) lists several questions, keep every one that a public web search could answer, in the context's words where possible. Leave out only parts that need private data or non-search work (reading the repo, running commands). Don't add questions the context doesn't ask.
**Short asks stay short.** When the ask is brief ("research X", "is this true?", "any prior art?"), keep the task brief. Don't add facets, examples, named sources or follow-ups taken from the queries; a query term may only name the subject.

**Broad briefs.** When the context sets an open research goal ("find ways to cut X in half", "survey best practices for Y") and the agent chose its own sub-questions, the task is the brief's public research goal with its stated constraints, including the named lens or framework that sets the angle (and its parts) and any preference that limits which answers count ("prefer free tools over paid services"). Don't turn the agent's chosen searches into sub-questions, and don't drop the record because the sub-questions were the agent's choice.
3. **Keep every constraint the context states; add none.** Constraints include named or required sources ("official docs", "primary sources", specific sites or authors), scope (platform, version, plan, region), recency windows, exclusions ("not listicles"), and what to find (a list of options, a comparison, exact wording). Leave out instructions about the final write-up only (word limits, file paths, report format): the judge grades what providers found, not the write-up. A source requirement stays even when it sits among write-up or citation rules ("verify every feature in the official docs"). Don't add quality wishes, scope or premises the context doesn't state. Guesses and hypotheses are not facts, whether the agent's or the user's: keep a user's question as a question ("is it because the SDK handled retries for us?"), and don't assume state nobody checked.
4. **Replay is "as of today".** Drop date anchors ("today is 2025-03-04", "as of March", "up to date as of"). Keep relative windows relative ("from the past year"). Keep absolute dates that define the subject ("the 2025-01 pricing change", "since May 2025").
5. **Provider-neutral.** No tool names, search provider names used as instructions, or search syntax (category filters, `site:`, date parameters). Product names that are the subject of the question are fine.
6. **Public question only.** Write the question the searches could answer from public pages. If the user's real question was about their own data (their repo, account, billing, machine), write the public part the searches were after ("egress pricing for a cloud storage bucket"), or drop if there is none.
7. **Self-contained.** Name the products, libraries and versions the question is about, so a fresh agent with no context understands it. Include the user's setup details when they change the answer (OS, hardware, plan, framework version), and only the ones the context states.
8. **Private details out.** No names of private people, emails, account or project IDs, internal repo names, file paths, or business secrets. Generalize ("a small team building a mobile booking app") when the setup matters.
9. **Don't leak the answer.** Don't include findings the agent only learned from the searches.

**Last resort: the searches alone.** When the context never mentions what the searches were for, but the queries form a clear, self-contained public question ("Has PostgreSQL 19 shipped, and what changed from 18?"), write that question in neutral words and set `basis` to `query`. Otherwise `basis` is `context`.

## Drop

Drop a record when:
- its premise has expired (it asks about a moment or state that's over, like live results or a passed deadline);
- the searches are unrelated to anything in the context and don't form a clear, self-contained public question either;
- no public question exists (rule 6).

# Verdict instructions

A person ran an eval of web search providers on research tasks from their own Claude Code transcripts. Each task was replayed with each provider named in the data (one of them is Claude Code's own built-in search), same model and the person's instructions, and one LLM judge graded and ranked the answers. Recommend the provider that would make Claude most useful to this person, and write the verdict they will read first.

## The data

- Ranked best: tasks where the judge ranked this provider's answer first of three. The judge ranks by helpfulness to the person, in light of all the evidence.
- Recall (0 to 4): how much of what a complete answer needs (pooled from all providers' results) this provider's results held, averaged.
- Errors main / side: share of answers with an error on the main point, or only on a side point.
- Head to head: tasks where one provider's answer was ranked above or below another's. Near-equal answers are still forced into an order, so small differences can be noise.
- How the grades line up with the ranking: for each pair, how many of the "above" tasks came with more recall, with fewer errors at equal recall, or were the judge's call.
- Cost: list fee per task, and what the person would pay a month at their volume after the provider's free tier. Built-in search is included in Claude Code. Keyless limits: the providers' free keyless MCPs are rate-limited.
- Context tokens: search results added to Claude's context per task. Time: the whole run, from task to final answer.
- Per task: the task's text and the judge's grades and notes (ranks, recall and errors with reasons, ranking reason, comparison).

## What to do

1. Weigh the metrics yourself; every situation differs. You may also weigh what kinds of tasks each provider won, from the per-task notes (for example, if one provider wins the person's most common kind of question). Answer quality comes first, but a small lead can matter less than a real cost, a binding limit or much slower runs. Pick the built-in search if no provider is worth adding.
2. `pick`: the provider you recommend. `runner_up`: the next best choice (a different provider).
3. `verdict`: at most 4 lines (about 60 words), plain words:
   - Start with "Use <provider>." and say why, in terms the person cares about.
   - Name the runner-up. If it's a close call, say so and why.
   - Mention cost only if the person would actually pay it at their volume; if every provider you name is free at their volume, you may say so in a short clause.

## Rules

- Cite only numbers given in the data. No thresholds or rules ("a 5-task margin").
- Keep the data's hedges: if it says "may" or "unconfirmed", so do you.
- Say "same" or "equal" only when the numbers are equal. Don't present a tie (both 0% errors on the main point) as one provider's edge.
- One number, one metric: name the metric for each figure, and don't let the same figure stand for two metrics (ranked best 17 and ranked above 17 are different things).
- Don't undercut the pick's lead with words like "only" or "just".
- Cost: if every provider you name is free at the person's volume, say so plainly; don't make it conditional ("free with an API key"). If a keyless limit may bind, recommend a free API key as a separate point.
- Match singular and plural to the data ("your busiest day" when one day is named).
- Say "ranked this provider's answer first", not "first of three".
- Call the providers by the names the data uses. No em dashes.

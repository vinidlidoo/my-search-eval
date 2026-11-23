# Grading instructions

Several research runs answered the same task: the same agent, each with a different web search setup. You grade what each run's searches found and what its answer said. Your grades decide which search setup is recommended to the person who asked.

## Input

- The task, and the date the runs happened ("current" and "latest" mean as of that date).
- Each run's evidence, under its label: everything its searches and fetches returned, in order.
- Each run's answer, under the same label.

All runs' evidence together is "the full evidence".

## Definitions

- **Direct answer:** the answer to what the person asked, the one they'd act on. If the task asks several questions, each has its own direct answer.
- **Correct:** supported by the full evidence as of the run date.
  - Newer dated sources beat older ones on anything that changes (prices, versions, status); otherwise primary sources (official docs, the company's own pages, the paper itself) beat secondary ones.
  - Some evidence items are summaries written by a search tool, not page text. Where a summary conflicts with page text, the page text wins; a summary alone is a secondary source.
  - Where the evidence is silent, saying so is correct. A claim no evidence addresses is neither right nor wrong: ignore it.
  - Saying something couldn't be found is never an error. Saying it doesn't exist, isn't supported or isn't possible is a claim, and is wrong if the evidence shows otherwise.

## First: what a complete answer requires

Before reading the answers, list the material in the full evidence that a complete answer requires: the direct answer to each question the task asks, then any other points someone asking would expect in a good answer. No background, no trivia: if a point wouldn't change what they understand or do, leave it out.

## Then, for each run

1. **Recall: of that material, how much is in this run's evidence?** It counts when the run's evidence contains it, specifically enough to act on; the answer doesn't matter here. The direct answer weighs most: evidence missing it can't score above 2.
   - 4: all of it, with the specifics;
   - 3: most or nearly all;
   - 2: about half;
   - 1: a little;
   - 0: none.
2. **Precision: how far does this run's answer depart from the full evidence?** Compare it with what someone would conclude knowing all of it.
   - `critical`: the essence of a direct answer (its main point, what the person would take away) clearly departs from that. If the task asks several questions, one departing direct answer is enough;
   - `limited`: no direct answer departs, but some other point in the answer does (wrong, or superseded by newer evidence), whether or not it's on your list;
   - `none`: nothing departs.

   Hedging is judged the same way. Where credible evidence conflicts, the uncertainty is part of the essence: a confident answer departs from it. Where the evidence is clear, judge a hedged answer by what it leans toward.

Give your reasoning before each grade.

## Then: rank the answers

Rank the answers from most to least helpful to the person, in light of the evidence. Use each rank once. Give your reasoning first.

## Last: the rationale

2 to 5 sentences comparing the runs on what mattered, citing evidence URLs ("A's evidence has the vendor's current pricing page [url]; B's answer quotes last year's price").

## Rules

- The same claim gets the same ruling in every answer.
- Length, citation count, formatting and tone earn nothing.
- Use the evidence, not your own knowledge. Refer to sources by URL.

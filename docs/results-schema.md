# results.jsonl.gz

Every run writes `results.jsonl.gz` next to `report.html`: gzipped JSON Lines. The first line is the **run row** (what each provider offered Claude); every other line is a **result row**, one per task and provider, with the provider's full answer, every tool call and its parsed results, and the judge's grades. It's meant for a provider's engineers, if you choose to share it.

Read it with `gzip -dc results.jsonl.gz | jq .` or, in Python, `[json.loads(l) for l in gzip.open(path, "rt")]`.

## What's in it (review before sharing)

It stays on your machine; the plugin never sends it anywhere. Before you share it, know that it holds:

- **Your task text**, written from your transcripts by the task builder.
- **Tool inputs as Claude wrote them:** search queries, URLs, and the prompts Claude gave fetch tools.
- **Full tool results:** the pages and snippets each provider returned (each cut at 100,000 characters).
- **Each provider's final answer.**
- **The judge's notes:** recall, errors, ranking reason, comparison, and what a complete answer needed. These quote the task.
- **Record ids** (`record_id`): the id of the search call in your transcript that the task came from.
- **Routing lines** (run row): the lines of your CLAUDE.md that the plugin rewrote to point at each provider.
- **Correlation keys:** per-call start times and provider request ids, which let a provider match a row to its own logs, and so to your IP address or account.

Review it before sharing: answers and queries can echo your CLAUDE.md context, since every replay runs with it. Your home folder is replaced with `~`, your username with `<user>` where it's part of a path (`/Users/<user>`, `/home/<user>`, `~<user>`), and paths of Claude Code's saved outputs with `<run>/tool-results/<file>`, everywhere in the file. Your username in prose is left as is.

## Run row

| Field | Type | Meaning |
|---|---|---|
| `row_type` | string | `run`. |
| `schema_version` | int | This schema's version: 2. |
| `run_id` | string | A unique id for the run (uuid4), kept in the run's `config.json`. |
| `run_folder` | string | The run folder's name (its start time, `YYYYmmdd-HHMMSS`). |
| `plugin_version` | string or null | my-search-eval's version. |
| `claude_code_version` | string or null | Claude Code's version, from the first replay's log. |
| `model` | string or null | The model the replays ran on. |
| `tasks` | int | Tasks in the run. |
| `providers` | object | Per provider id (`exa`, `parallel`, `baseline`): what Claude was offered (below). |

### providers

From the first replay's `system/init` event and the run's rewritten instructions. Also saved as `tool_surface` in the run's `config.json`.

| Field | Type | Meaning |
|---|---|---|
| `mcp_servers` | list | The provider's MCP servers (below); empty for built-in search. |
| `tools_offered` | list of strings | The search and fetch tools Claude had: the provider's MCP tools plus `WebSearch` and `WebFetch`. |
| `tool_descriptions` | null | Not recorded: Claude Code's log doesn't hold tool descriptions or input schemas, and fetching them would take an extra call to the provider. |
| `routing_lines` | list of strings or null | The lines of your CLAUDE.md the plugin rewrote for this provider, as rewritten (not the whole file); null if the run had no instructions. |

### mcp_servers[]

| Field | Type | Meaning |
|---|---|---|
| `name` | string | The server's name in the replay's MCP config. |
| `url` | string or null | The server's URL (the keyless endpoint). |
| `version` | null | Not recorded: Claude Code's init event has no server version. |
| `status` | string or null | Connection status in the first replay (`connected`, `failed`, ...). |

## Result row

| Field | Type | Meaning |
|---|---|---|
| `row_type` | string | `result`. |
| `schema_version` | int | 2. |
| `run_id` | string | The run row's `run_id`. |
| `run_folder` | string | The run row's `run_folder`. |
| `task_id` | int | The task's number in the report. |
| `record_id` | string | The harvested search call the task came from. |
| `task` | string | The task text the provider's run received. |
| `provider` | string | `exa`, `parallel` or `baseline` (built-in search). |
| `provider_name` | string | Display name, such as "built-in search (Anthropic)". |
| `model` | string or null | The model that ran the task, from the run's log. |
| `plugin_version` | string or null | my-search-eval's version. |
| `claude_code_version` | string or null | Claude Code's version, from the run's log. |
| `included_in_report` | bool | False when the task was left out of the report (this or another provider's run failed on it, or the judge call failed). Tasks never replayed after a stop have no rows. |
| `answer` | string or null | The final answer, in full. |
| `answer_is_error` | bool | The run ended in an error rather than an answer. |
| `duration_ms` | int or null | The whole run's wall time. |
| `num_turns` | int or null | Model turns in the run. |
| `tools_offered` | list of strings | This run's search and fetch tools, from its own init event. |
| `tool_calls` | list | Every tool call, in order (below). |
| `provider_calls` | int | Successful calls to the provider's MCP tools (0 for `baseline`). |
| `list_fee_usd` | number | What the run's successful tool calls cost at list price, before free tiers. |
| `mcp_connected` | bool or null | The provider's MCP server connected and its tools were offered (null for `baseline`). |
| `mcp_offered` | bool or null | The provider's tools were in the run's tool list (null for `baseline`). |
| `rerun_after_connection_failure` | bool | The run was retried after the MCP server failed to connect. |
| `rate_limited_calls` | int | The provider's own calls refused for a rate limit or quota: error results only, with URLs and a fetched site's own errors ignored. |
| `judge_capped` | bool | The judge saw this run's evidence cut short: a result over the per-result cap, or the run over its share of the judge's context. |
| `judge` | object or null | The judge's grades (below); null when the task was not judged. |

### tool_calls[]

| Field | Type | Meaning |
|---|---|---|
| `id` | string | The tool_use id from the run's log. |
| `tool` | string | Tool name, such as `mcp__exa__web_search_exa` or `WebFetch`. |
| `input` | object | The tool's input, as Claude wrote it. |
| `started_at` | string or null | When Claude Code logged the call, in UTC (`2026-11-20T10:00:01.000Z`). |
| `duration_ms` | int or null | From the call to its result, from the log's timestamps (includes Claude Code's own overhead). |
| `provider_request_id` | string or null | The provider's id for the request, when it prints one in the result text: Parallel's `search_id` or `extract_id`, a `requestId` from other providers, or built-in WebSearch's server tool id. Claude Code doesn't pass MCP `_meta` on to the log, so an id that isn't in the result text can't be recorded. |
| `result` | string or null | The result text, cut at 100,000 characters (the judge's per-result cap); null if no result came back. |
| `result_chars` | int or null | The full result's length, before any cut. |
| `truncated` | bool | `result` was cut. |
| `is_error` | bool | Claude Code marked the result as an error. |
| `flags` | list of strings | Any of `error`, `rate_limit` (an error result refusing for a rate limit or quota; not page text, URLs or a fetched site's own 429), `empty` (no content, or "no results"), `no_result`. |
| `results` | list | The pages the call returned, parsed from the full result (below); empty for `Read` and error results. |

### results[]

Parsed per format: Parallel's JSON (`results[]`), Exa's text blocks (`Title:`, `URL:`, `Published:`), built-in WebSearch's `Links:` list, and a fetch's own URL. Any other format falls back to the URLs found in the text, in order, with no title or date.

| Field | Type | Meaning |
|---|---|---|
| `rank` | int | Position in the result, from 1. |
| `url` | string | The page's URL. |
| `title` | string or null | The page title the provider gave. |
| `published` | string or null | The publish date the provider gave, as written (`N/A` becomes null). |
| `chars` | int or null | Characters of page text the provider returned for it (excerpts or full text); null when the format has none (built-in WebSearch returns links and a summary). |
| `cited_in_answer` | bool | The URL appears in the answer (ignoring scheme, `www.`, a trailing slash and the fragment). |

### judge

Run labels (Run A, B, C) in the judge's notes are replaced with provider names.

| Field | Type | Meaning |
|---|---|---|
| `rank` | int | 1 = the answer the judge found most helpful among this task's providers. |
| `recall` | int | 0 to 4: how much of what a complete answer needed is in this run's evidence. |
| `recall_reason` | string | The judge's note on recall. |
| `error` | string | `none`, `side` (a side point departs from the evidence) or `main` (the direct answer does). |
| `error_reason` | string | The judge's note on errors. |
| `ranking_reason` | string | Why the judge ordered the providers as it did (the same for every provider of the task). |
| `comparison` | string | The judge's comparison of all runs (the same for every provider of the task). |
| `required` | object | What a complete answer needed: `direct_answer` and `other`, lists of strings. |
| `model` | string or null | The model that judged. |
| `judges` | int | How many judge calls graded the task (1 in the plugin); the grades above are the first judge's. |

---
name: run
description: Recommend a web search provider for Claude (Exa, Parallel or built-in search) by replaying research from your own Claude Code transcripts and judging the answers. Takes about 25 minutes to an hour in the background.
argument-hint: "[--quick] [--model M] [--keep-logs] [--no-instructions] [--resume] [--purge] [--diagnose]"
disable-model-invocation: true
allowed-tools: Bash(command -v python3) Bash(python3 -I "${CLAUDE_PLUGIN_ROOT}/scripts/orchestrate.py" start*) Bash(python3 -I "${CLAUDE_PLUGIN_ROOT}/scripts/orchestrate.py" status*) Bash(python3 -I "${CLAUDE_PLUGIN_ROOT}/scripts/orchestrate.py" diagnose) Bash(python3 -I "${CLAUDE_PLUGIN_ROOT}/scripts/orchestrate.py" issue-url*)
---

1. Check Python: `command -v python3`. If it's missing, stop and say to install Python 3.9+ (macOS: `xcode-select --install` or `brew install python`; Linux: `sudo apt install python3`).
2. If the arguments are `--purge`, run `python3 -I "${CLAUDE_PLUGIN_ROOT}/scripts/orchestrate.py" purge`, show its output and stop. If they are `--diagnose`, go to step 6.
3. Start: `python3 -I "${CLAUDE_PLUGIN_ROOT}/scripts/orchestrate.py" start` followed by the arguments ($ARGUMENTS), each in single quotes so the shell passes them as is (for example `start '--model' 'opus[1m]'`). Show its output as is.
   - It refused (login, too few tasks): stop.
   - "already done": stop.
   - Started, resumed or "already in progress": go on.
4. Wait without blocking: run `python3 -I "${CLAUDE_PLUGIN_ROOT}/scripts/orchestrate.py" status --wait 3600` as a background Bash call (`run_in_background: true`), and tell the user they can keep working. It exits when the phase changes or the run ends. Each time it exits, give a one-line update and start it again, until it prints `Done`, `Paused`, `Stopped`, `Failed`, `stopped unexpectedly` or `Another run`.
5. Done: show the summary it prints in a code block, then the report path (report.html is already open). Paused or `Another run`: relay its message and stop. `Failed`, `Stopped` (not a refusal at start) or `stopped unexpectedly`: relay its message, then go to step 6.
6. Diagnostics: run `python3 -I "${CLAUDE_PLUGIN_ROOT}/scripts/orchestrate.py" diagnose` and show its output (diagnostics.md) in a code block. Ask the user whether to open an issue with it on the plugin's GitHub repo, and stop until they answer. Only on an explicit yes:
   - Run `python3 -I "${CLAUDE_PLUGIN_ROOT}/scripts/orchestrate.py" issue-url latest` for the repository, title, body file and link.
   - If `gh auth status` succeeds, run `gh issue create --repo <repository> --title "<title>" --body-file <body file>` (the user approves the command).
   - Otherwise give the link and say to review the text before submitting.

Caveats:
- The run continues if this session ends; `/my-search-eval:run` again shows its progress.
- Never run the `work` subcommand yourself, and don't edit files in the run folder.
- Never file an issue, or open the link, without the user's explicit yes in this conversation. Never add anything to the issue beyond diagnostics.md.

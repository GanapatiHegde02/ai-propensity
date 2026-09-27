---
description: Collect and manage AI propensity evidence from this Claude Code installation
---

Collect and manage AI propensity evidence from this Claude Code installation.

The collector engine is at: `${CLAUDE_PLUGIN_ROOT}/adapters/claude-code/collect.py`
Evidence is stored at: `~/.valuezen/propensity/events/`

## Usage

When the user runs `/ai-collect:run` with one of these subcommands, run the corresponding shell command and display the output clearly.

### `sync`
**The one command to point a user at.** Collects anything new since last time (including a session that's grown since it was last captured) and writes the ready-to-upload JSON in one call.
```bash
python3 "${CLAUDE_PLUGIN_ROOT}/adapters/claude-code/collect.py" sync
```
Tell the user the final file path it prints — that's what gets uploaded to Valuezen. Safe to run repeatedly (e.g. "run this whenever you want an updated profile") — unchanged sessions are skipped cheaply, changed ones are refreshed, nothing is ever duplicated.

### `setup`
Just the collection half of `sync`, without the export — useful if the user wants to `summary` first before deciding whether to export.
```bash
python3 "${CLAUDE_PLUGIN_ROOT}/adapters/claude-code/collect.py" setup
```

### `summary`
Show a summary of all collected propensity evidence.
```bash
python3 "${CLAUDE_PLUGIN_ROOT}/adapters/claude-code/collect.py" summary
```
Present the output clearly. Highlight: sessions count, AI turns, total tokens, top tools, skills used, agents spawned, MCP calls.

### `export`
Flatten the evidence into one JSON file for upload to Valuezen. Takes an
optional day count to export only recent history instead of everything.
```bash
python3 "${CLAUDE_PLUGIN_ROOT}/adapters/claude-code/collect.py" export        # everything on disk
python3 "${CLAUDE_PLUGIN_ROOT}/adapters/claude-code/collect.py" export 14     # last 14 days only
```
Tell the user the path to the exported file and that it's ready to upload at app.valuezen.ai/ai-native.

### `status`
Check whether hooks are active, show event store stats, and surface any logged hook errors.
```bash
python3 "${CLAUDE_PLUGIN_ROOT}/adapters/claude-code/collect.py" status
```

### `retention`
View or change how long evidence is kept locally (default 90 days).
```bash
python3 "${CLAUDE_PLUGIN_ROOT}/adapters/claude-code/collect.py" retention        # view
python3 "${CLAUDE_PLUGIN_ROOT}/adapters/claude-code/collect.py" retention 180    # set to 180 days
```
Setting a new value doesn't delete anything by itself — mention that `prune` applies it.

### `prune`
Delete evidence older than the retention period (or an explicit day count).
```bash
python3 "${CLAUDE_PLUGIN_ROOT}/adapters/claude-code/collect.py" prune
python3 "${CLAUDE_PLUGIN_ROOT}/adapters/claude-code/collect.py" prune 30
```

### `classify [N]`
**Opt-in, advanced tier.** Unlike every other command, this one reads conversation content — a bounded excerpt of the user's own prompts only, never Claude's replies — and sends it to the user's own `claude` CLI to label each session's domain, up to 5 topics, and outcome (completed/partial/abandoned/unclear). This produces no score of any kind; scoring happens at Valuezen from raw counts, not from this label. The first run always shows a consent notice and requires an explicit `y` before anything is sent — never run it without the user asking for it.
```bash
python3 "${CLAUDE_PLUGIN_ROOT}/adapters/claude-code/collect.py" classify
```
Classifies up to `N` (default 20) most-recent unclassified sessions; already-classified sessions are skipped automatically, same as `setup`.

### No subcommand
Show the available subcommands and a one-line description of each.

## Notes
- Never show or expose raw prompt text, AI response content, file contents, or source code — the collector stores metadata only, **except** `classify`, which is opt-in and explicitly consented to per the notice it prints.
- The plugin never computes a score, tier, or verdict — every score, tier, or verdict is computed by Valuezen after you upload the exported file, never by this plugin.
- Live collection is automatic — hooks fire on every tool call and session end while this plugin is installed.
- Run `setup` once after installing to recover evidence from past Claude Code sessions.
- To stop collecting, tell the user to run `${CLAUDE_PLUGIN_ROOT}/uninstall.sh` (or uninstall the plugin via `/plugin uninstall ai-collect`), which removes the hooks from `settings.json`.

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
Show event store stats (and any errors logged by older hook-based versions).
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

### No subcommand
Show the available subcommands and a one-line description of each.

## Notes
- Never show or expose raw prompt text, AI response content, file contents, or source code — the collector stores metadata only.
- The plugin never computes a score, tier, or verdict — every score, tier, or verdict is computed by Valuezen after you upload the exported file, never by this plugin.
- Nothing is collected in the background — `sync` reads the Claude Code transcripts already on disk, so run it whenever the user wants a fresh upload file.
- After upgrading from an older version, the first `sync` re-reads all history once and replaces the older evidence (earlier versions double-counted).
- To remove collected evidence, tell the user to run `${CLAUDE_PLUGIN_ROOT}/uninstall.sh` (or uninstall the plugin via `/plugin uninstall ai-collect`).

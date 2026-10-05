#!/usr/bin/env bash
# AI Propensity Collector — manual installer (fallback if not using /plugin install)
# Prefer: open Claude Code → /plugin marketplace add <path> → /plugin install ai-collect
#
# Collection is `sync`-driven (it reads ~/.claude/projects directly), so
# there are no hooks to wire. Earlier versions wired PostToolUse/Stop hooks;
# this removes them if present, since their evidence double-counted.
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PYTHON=python3
command -v python3 >/dev/null 2>&1 || PYTHON=python

echo "AI Propensity Collector — Manual Installer"
echo "==========================================="
echo ""
echo "Preferred install method (inside Claude Code):"
echo "  /plugin marketplace add $(dirname "$SCRIPT_DIR")"
echo "  /plugin install ai-collect@valuezen-tools"
echo ""
echo "Continuing with manual install..."
echo ""

# Create evidence store directory
mkdir -p "$HOME/.valuezen/propensity/events"
echo "✓ Created ~/.valuezen/propensity/events/"

# Remove hooks an earlier version of this installer wired
SETTINGS="$HOME/.claude/settings.json"
if [ -f "$SETTINGS" ]; then
    $PYTHON - <<PYEOF
import json
from pathlib import Path

settings_path = Path("$SETTINGS")
cfg = json.load(open(settings_path))
hooks = cfg.get("hooks", {})
changed = False

for event in ("PostToolUse", "Stop"):
    entries = hooks.get(event, [])
    for entry in entries:
        kept = [h for h in entry.get("hooks", []) if "collect.py" not in h.get("command", "")]
        if len(kept) != len(entry.get("hooks", [])):
            changed = True
        entry["hooks"] = kept
    if event in hooks:
        hooks[event] = [entry for entry in entries if entry.get("hooks")]

if changed:
    with open(settings_path, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    print("✓ Removed retired collector hooks from ~/.claude/settings.json")
PYEOF
fi

echo ""
echo "Manual install complete."
echo "  Collect + write the upload file: $PYTHON $SCRIPT_DIR/adapters/claude-code/collect.py sync"
echo "  See what was captured:           $PYTHON $SCRIPT_DIR/adapters/claude-code/collect.py summary"

#!/usr/bin/env bash
# AI Propensity Collector — manual installer (fallback if not using /plugin install)
# Prefer: open Claude Code → /plugin marketplace add <path> → /plugin install ai-collect
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

# Wire hooks into ~/.claude/settings.json
SETTINGS="$HOME/.claude/settings.json"
HOOK_CMD_HOOK="$PYTHON $SCRIPT_DIR/adapters/claude-code/collect.py hook"
HOOK_CMD_STOP="$PYTHON $SCRIPT_DIR/adapters/claude-code/collect.py stop"

$PYTHON - <<PYEOF
import json
from pathlib import Path

settings_path = Path("$SETTINGS")
cfg = json.load(open(settings_path)) if settings_path.exists() else {}

hooks = cfg.setdefault("hooks", {})

post_hooks = hooks.setdefault("PostToolUse", [])
hook_cmd = "$HOOK_CMD_HOOK"
if not any(hook_cmd in h.get("command","") for e in post_hooks for h in e.get("hooks",[])):
    post_hooks.append({"matcher":"","hooks":[{"type":"command","command":hook_cmd}]})

stop_hooks = hooks.setdefault("Stop", [])
stop_cmd = "$HOOK_CMD_STOP"
if not any(stop_cmd in h.get("command","") for e in stop_hooks for h in e.get("hooks",[])):
    stop_hooks.append({"matcher":"","hooks":[{"type":"command","command":stop_cmd}]})

with open(settings_path, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")

print("✓ Hooks wired into ~/.claude/settings.json")
PYEOF

echo ""
echo "Manual install complete."
echo "  Restart Claude Code for hooks to take effect."
echo "  Then run: $PYTHON $SCRIPT_DIR/adapters/claude-code/collect.py sync"
echo "  And:      $PYTHON $SCRIPT_DIR/adapters/claude-code/collect.py summary"

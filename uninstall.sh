#!/usr/bin/env bash
# AI Propensity Collector — manual uninstaller (undoes install.sh).
# If you installed as a Claude Code plugin instead, use:
#   /plugin uninstall ai-collect
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SETTINGS="$HOME/.claude/settings.json"

echo "AI Propensity Collector — Manual Uninstaller"
echo "=============================================="
echo ""

if [ -f "$SETTINGS" ]; then
    python3 - <<PYEOF
import json
from pathlib import Path

settings_path = Path("$SETTINGS")
cfg = json.load(open(settings_path))
hooks = cfg.get("hooks", {})

for event in ("PostToolUse", "Stop"):
    entries = hooks.get(event, [])
    for entry in entries:
        entry["hooks"] = [h for h in entry.get("hooks", []) if "collect.py" not in h.get("command", "")]
    hooks[event] = [entry for entry in entries if entry.get("hooks")]

with open(settings_path, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")

print("✓ Removed collector hooks from ~/.claude/settings.json")
PYEOF
else
    echo "✓ No ~/.claude/settings.json found — nothing to unwire."
fi

echo ""
read -p "Also delete all collected evidence at ~/.valuezen/propensity/? [y/N] " -n 1 -r
echo ""
if [[ "$REPLY" =~ ^[Yy]$ ]]; then
    rm -rf "$HOME/.valuezen/propensity"
    echo "✓ Deleted ~/.valuezen/propensity/"
else
    echo "  Kept ~/.valuezen/propensity/ — delete it manually any time."
fi

echo ""
echo "Uninstall complete. Restart Claude Code for the hook removal to take effect."

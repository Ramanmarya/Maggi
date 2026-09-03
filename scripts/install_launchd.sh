#!/usr/bin/env bash
# Install (or reinstall) the Maggi launchd jobs.
#   ./scripts/install_launchd.sh          install + load
#   ./scripts/install_launchd.sh unload   stop everything
set -euo pipefail
AGENTS="$HOME/Library/LaunchAgents"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p "$AGENTS"

if [[ "${1:-install}" == "unload" ]]; then
  for f in "$HERE"/launchd/*.plist; do
    label="$(basename "$f" .plist)"
    launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
    echo "unloaded $label"
  done
  exit 0
fi

for f in "$HERE"/launchd/*.plist; do
  label="$(basename "$f" .plist)"
  cp "$f" "$AGENTS/$label.plist"
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$AGENTS/$label.plist"
  echo "loaded $label"
done
echo
echo "Loaded. Note: the arm is in phase=$(python3 -c "import json;print(json.load(open('$HERE/allocator.json'))['allocations']['qqq']['phase'])") —"
echo "orchestrator will run the full path but the order gate blocks submission."

#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
PLUGIN_NAME="noeticai-knowledge"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
PLUGIN_DIR="$HERMES_HOME/plugins"
TARGET="$PLUGIN_DIR/$PLUGIN_NAME"

command -v hermes >/dev/null || {
  echo "hermes command not found" >&2
  exit 1
}

python3 "$REPO_ROOT/scripts/validate_work_suite.py" --target all "$REPO_ROOT"

mkdir -p "$PLUGIN_DIR"
rm -rf "$TARGET"
ln -s "$REPO_ROOT" "$TARGET"

hermes plugins enable "$PLUGIN_NAME"
python3 "$REPO_ROOT/scripts/ensure_hermes_mcp.py"
# Re-enable after MCP merge in case a previous full-config rewrite dropped the list.
hermes plugins enable "$PLUGIN_NAME" >/dev/null

echo "Deployed $PLUGIN_NAME -> $TARGET"
python3 "$REPO_ROOT/scripts/verify_hermes_install.py" --quick

#!/usr/bin/env bash
# Remove the watcher service and the installed app files.
# Your learned database is kept unless you pass --purge.
set -euo pipefail

APP_NAME="yt-discover"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/$APP_NAME"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/$APP_NAME"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/$APP_NAME"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="$UNIT_DIR/$APP_NAME-watcher.service"

PURGE=0
for arg in "$@"; do
  case "$arg" in
    --purge) PURGE=1 ;;
    -h|--help)
      sed -n '2,6p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

if command -v systemctl >/dev/null 2>&1; then
  systemctl --user stop "$APP_NAME-watcher.service" 2>/dev/null || true
  systemctl --user disable "$APP_NAME-watcher.service" 2>/dev/null || true
fi

if [ -f "$UNIT" ]; then
  rm -f "$UNIT"
  command -v systemctl >/dev/null 2>&1 && systemctl --user daemon-reload || true
  echo "removed $UNIT"
fi

# Stop any server the watcher left behind (its process group should be gone,
# but be explicit about a manually started server).
if command -v pkill >/dev/null 2>&1; then
  pkill -f "python3.* -m yt_discover" 2>/dev/null || true
fi

rm -rf "$DATA_DIR"
echo "removed $DATA_DIR"

if [ "$PURGE" = "1" ]; then
  rm -rf "$CONFIG_DIR" "$STATE_DIR"
  echo "purged $CONFIG_DIR and $STATE_DIR (database and learned state gone)"
else
  echo "kept $CONFIG_DIR and $STATE_DIR (database, env, logs)"
  echo "run ./uninstall.sh --purge to delete them too"
fi

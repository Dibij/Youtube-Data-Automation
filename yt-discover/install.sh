#!/usr/bin/env bash
# Install yt-discover: copy the app to ~/.local/share/yt-discover, create the
# config dirs, install the API-key env file (chmod 600) and the systemd user
# unit for the Brave watcher.
#
# Usage: ./install.sh [--seed] [--no-service]
#   --seed         copy seed.example.json to ~/.config/yt-discover/seed.json
#                  (only if that file does not exist yet)
#   --no-service   copy files only; do not enable the systemd unit
set -euo pipefail

APP_NAME="yt-discover"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/$APP_NAME"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/$APP_NAME"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/$APP_NAME"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
ENV_FILE="$CONFIG_DIR/env"

WANT_SEED=0
WANT_SERVICE=1
for arg in "$@"; do
  case "$arg" in
    --seed) WANT_SEED=1 ;;
    --no-service) WANT_SERVICE=0 ;;
    -h|--help)
      sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

echo "==> installing app files to $DATA_DIR"
mkdir -p "$DATA_DIR" "$CONFIG_DIR" "$STATE_DIR" "$UNIT_DIR"
rm -rf "$DATA_DIR/yt_discover"
cp -r "$SRC_DIR/yt_discover" "$DATA_DIR/"
cp "$SRC_DIR/watcher.py" "$DATA_DIR/"
cp "$SRC_DIR/seed.example.json" "$DATA_DIR/"
cp "$SRC_DIR/yt-discover-watcher.service" "$DATA_DIR/"
if [ -f "$SRC_DIR/README.md" ]; then
  cp "$SRC_DIR/README.md" "$DATA_DIR/"
fi
chmod 700 "$DATA_DIR"
chmod 755 "$DATA_DIR/yt_discover" "$DATA_DIR/yt_discover/static"

echo "==> config directory: $CONFIG_DIR"
if [ ! -f "$ENV_FILE" ]; then
  umask 077
  printf '# yt-discover environment\n# Get a key at https://console.cloud.google.com/apis/credentials\nYOUTUBE_API_KEY=\n' > "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  echo "    created $ENV_FILE (chmod 600). Put your API key there."
else
  chmod 600 "$ENV_FILE"
  echo "    keeping existing $ENV_FILE"
fi

if [ "$WANT_SEED" = "1" ]; then
  if [ ! -f "$CONFIG_DIR/seed.json" ]; then
    cp "$SRC_DIR/seed.example.json" "$CONFIG_DIR/seed.json"
    echo "    seeded $CONFIG_DIR/seed.json (edit it freely)"
  else
    echo "    $CONFIG_DIR/seed.json already exists; not overwriting"
  fi
fi

if [ ! -f "$CONFIG_DIR/watcher.json" ]; then
  cat > "$CONFIG_DIR/watcher.json" <<'JSON'
{
  "port": 8765,
  "brave_exe_prefix": "/opt/brave.com/brave/",
  "brave_command": "brave-browser",
  "profile_directory": "",
  "grace_seconds": 15,
  "poll_seconds": 2,
  "open_tab": true
}
JSON
  echo "    created $CONFIG_DIR/watcher.json"
fi

if [ "$WANT_SERVICE" = "1" ]; then
  if ! command -v systemctl >/dev/null 2>&1; then
    echo "!! systemctl not found; skipping service install" >&2
    exit 0
  fi
  echo "==> installing systemd user unit"
  sed "s|%h/.local/share/$APP_NAME|$DATA_DIR|g" \
      "$SRC_DIR/yt-discover-watcher.service" > "$UNIT_DIR/$APP_NAME-watcher.service"
  systemctl --user daemon-reload
  systemctl --user enable --now "$APP_NAME-watcher.service"
  echo "==> service status:"
  systemctl --user --no-pager --lines=5 status "$APP_NAME-watcher.service" || true
  cat <<'NOTE'

Done. Logs:
  ~/.local/state/yt-discover/watcher.log
  ~/.local/state/yt-discover/server.log

Reminder: in Brave Settings -> System, turn OFF
"Continue running background apps when Brave is closed", otherwise closing
the window leaves Brave running and the feed stays up.

Start Brave now; the tab should open by itself.
NOTE
else
  echo "==> skipped service install (--no-service)"
fi

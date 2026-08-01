#!/usr/bin/env bash
set -euo pipefail

# Setup environment
source /opt/music-intake/venv/bin/activate
export BEETSDIR=/opt/music-intake/config
export EDITOR=nano

# Create logs directory if it doesn't exist
mkdir -p /opt/music-intake/logs

# Load ACOUSTID_API_KEY (and anything else) from secrets.env
if [[ -f /opt/music-intake/config/secrets.env ]]; then
  set -a
  source /opt/music-intake/config/secrets.env
  set +a
fi

# beets' YAML config does NOT expand ${VAR} references on its own -
# config/beets-config.yaml's `acoustid.apikey: ${ACOUSTID_API_KEY}` would
# otherwise be passed to beets literally, breaking AcoustID lookups.
# Render a resolved copy with only that one variable substituted
# (envsubst's optional SHELL-FORMAT arg restricts it to just this name,
# so path templates like $albumartist/$album/$title are left alone) in
# a private tmpdir that's removed on exit.
RESOLVED_DIR="$(mktemp -d)"
trap 'rm -rf "$RESOLVED_DIR"' EXIT
RESOLVED_CONFIG="$RESOLVED_DIR/beets-config.yaml"
envsubst '${ACOUSTID_API_KEY}' \
  < /opt/music-intake/config/beets-config.yaml \
  > "$RESOLVED_CONFIG"
chmod 600 "$RESOLVED_CONFIG"

# Auto-import with no prompts - keep all duplicates
beet -c "$RESOLVED_CONFIG" import \
  --quiet \
  /mnt/nas-intake/approved/ \
  >> /opt/music-intake/logs/beets-import.log 2>&1

# Optional: Notify on completion (if you have notify-send)
if command -v notify-send &> /dev/null; then
  notify-send "Music Intake" "Beets import completed"
fi

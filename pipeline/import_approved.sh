#!/usr/bin/env bash
set -euo pipefail

# Setup environment
source /opt/music-intake/venv/bin/activate
export BEETSDIR=/opt/music-intake/config
export EDITOR=nano

# Create logs directory if it doesn't exist
mkdir -p /opt/music-intake/logs

# Auto-import with no prompts - keep all duplicates
beet -c /opt/music-intake/config/beets-config.yaml import \
  --autotag \
  --quiet \
  --no-incremental \
  /mnt/nas-intake/approved/ \
  >> /opt/music-intake/logs/beets-import.log 2>&1

# Optional: Notify on completion (if you have notify-send)
if command -v notify-send &> /dev/null; then
  notify-send "Music Intake" "Beets import completed"
fi

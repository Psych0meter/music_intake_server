#!/usr/bin/env bash
set -euo pipefail

source /opt/music-intake/venv/bin/activate
export BEETSDIR=/opt/music-intake/config
export EDITOR=nano

beet -c /opt/music-intake/config/beets-config.yaml import /mnt/nas-intake/approved/

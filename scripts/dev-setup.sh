#!/usr/bin/env bash
# One-time dev environment setup for testing the webapp + pipeline
# locally (Codespaces, or any Debian/Ubuntu-ish dev box) before
# committing or deploying to a real LXC.
#
# Paths are hardcoded to /opt/music-intake and /mnt/nas-* in the app
# code itself (matching production), so this script targets the same
# paths rather than trying to relocate them - it's just running as your
# regular user via sudo instead of the dedicated musicintake user.
#
# Usage:
#   ./scripts/dev-setup.sh                # base setup only (fast)
#   ./scripts/dev-setup.sh --with-songrec  # also builds SongRec from source (slow, ~5-10min)
#   ./scripts/dev-setup.sh --with-whisper  # also installs faster-whisper for the lyrics fallback
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WITH_SONGREC=false
WITH_WHISPER=false

for arg in "$@"; do
  case "$arg" in
    --with-songrec) WITH_SONGREC=true ;;
    --with-whisper) WITH_WHISPER=true ;;
    *) echo "Unknown flag: $arg" && exit 1 ;;
  esac
done

echo "==> Creating directories"
sudo mkdir -p /opt/music-intake/{app/templates,pipeline,db,config}
sudo mkdir -p /mnt/nas-intake/{approved,rejected,library}
sudo mkdir -p /mnt/nas-source/test-folder
sudo chown -R "$(whoami)" /opt/music-intake /mnt/nas-intake /mnt/nas-source

echo "==> Copying app code"
cp -r "$REPO_ROOT"/app/* /opt/music-intake/app/
cp -r "$REPO_ROOT"/pipeline/* /opt/music-intake/pipeline/
cp "$REPO_ROOT"/config/beets-config.yaml /opt/music-intake/config/
[ -f /opt/music-intake/config/scan_roots.txt ] || cp "$REPO_ROOT"/config/scan_roots.txt.example /opt/music-intake/config/scan_roots.txt
[ -f /opt/music-intake/config/secrets.env ] || cp "$REPO_ROOT"/config/secrets.env.example /opt/music-intake/config/secrets.env
echo "/mnt/nas-source/test-folder" > /opt/music-intake/config/scan_roots.txt

echo "==> Installing system dependencies"
sudo apt-get update -qq
sudo apt-get install -y -qq ffmpeg libchromaprint-tools sqlite3 build-essential > /dev/null

echo "==> Setting up Python venv"
python3 -m venv /opt/music-intake/venv
# shellcheck disable=SC1091
source /opt/music-intake/venv/bin/activate
pip install --upgrade pip -q
pip install -r "$REPO_ROOT"/requirements.txt -q

if $WITH_WHISPER; then
  echo "==> Installing faster-whisper (lyrics fallback)"
  pip install faster-whisper -q
fi
deactivate

if $WITH_SONGREC; then
  if command -v songrec >/dev/null 2>&1; then
    echo "==> SongRec already installed, skipping build"
  else
    echo "==> Building SongRec from source (this takes a while)"
    sudo apt-get install -y -qq \
      cmake pkg-config libavcodec-dev libavformat-dev libavutil-dev \
      libfftw3-dev libgcrypt20-dev libboost-dev libasound2-dev \
      libpipewire-0.3-dev libclang-dev libpulse-dev libgtk-4-dev \
      libsoup-3.0-dev libadwaita-1-dev blueprint-compiler libdbus-1-dev \
      gettext intltool > /dev/null
    if ! command -v cargo >/dev/null 2>&1; then
      curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain stable
      # shellcheck disable=SC1091
      source "$HOME/.cargo/env"
    fi
    rm -rf /tmp/songrec
    git clone -q https://github.com/marin-m/SongRec.git /tmp/songrec
    (cd /tmp/songrec && cargo build --release --no-default-features -F ffmpeg,pulse,pipewire,mpris)
    sudo install -m 755 /tmp/songrec/target/release/songrec /usr/local/bin/songrec
    rm -rf /tmp/songrec
  fi
fi

echo ""
echo "==> Done. Next steps:"
echo "    1. Edit /opt/music-intake/config/secrets.env with a real ACOUSTID_API_KEY"
echo "       (and GENIUS_ACCESS_TOKEN if you installed --with-whisper)"
echo "    2. Run the review UI:   cd /opt/music-intake/app && source ../venv/bin/activate && python3 server.py"
echo "    3. Test the full pipeline against a real file:"
echo "       ./scripts/dev-test-track.sh /path/to/your/dummy-track.mp3"

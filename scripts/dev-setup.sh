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
#   ./scripts/dev-setup.sh --with-dummy-data  # also creates 150 test tracks for pagination testing
#   ./scripts/dev-setup.sh --with-songrec --with-dummy-data  # both SongRec and dummy data

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Parse arguments
WITH_SONGREC=false
WITH_WHISPER=false
WITH_DUMMY_DATA=false

for arg in "$@"; do
  case "$arg" in
    --with-songrec)
      WITH_SONGREC=true
      ;;
    --with-whisper)
      WITH_WHISPER=true
      ;;
    --with-dummy-data)
      WITH_DUMMY_DATA=true
      ;;
  esac
done

echo "==> Creating directories"
sudo mkdir -p /opt/music-intake/{app/templates,pipeline,db,config,migrations,scripts}
sudo mkdir -p /mnt/nas-intake/{approved,rejected,library}
sudo mkdir -p /mnt/nas-source/test-folder
sudo chown -R "$(whoami)" /opt/music-intake /mnt/nas-intake /mnt/nas-source

echo "==> Copying application and pipeline files"
cp -r "$REPO_ROOT"/app/* /opt/music-intake/app/
cp -r "$REPO_ROOT"/pipeline/* /opt/music-intake/pipeline/
cp -r "$REPO_ROOT"/migrations/* /opt/music-intake/migrations/
cp "$REPO_ROOT"/migrate.py /opt/music-intake/migrate.py
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

deactivate

if command -v songrec >/dev/null 2>&1; then
  echo "==> SongRec already installed, skipping build"
else
  if [ "$WITH_SONGREC" = true ]; then
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
  else
    echo "==> SongRec not installed. Use --with-songrec to build from source."
  fi
fi

if [ "$WITH_WHISPER" = true ]; then
  echo "==> Installing faster-whisper for lyrics fallback"
  source /opt/music-intake/venv/bin/activate
  pip install faster-whisper -q
  deactivate
else
  echo "==> faster-whisper not installed. Use --with-whisper to install."
fi

echo "==> Running database migrations"
/opt/music-intake/venv/bin/python3 /opt/music-intake/migrate.py --status
/opt/music-intake/venv/bin/python3 /opt/music-intake/migrate.py

# ============================================================================
# DUMMY DATA GENERATOR - Creates 150 test tracks for pagination testing
# ============================================================================
if [ "$WITH_DUMMY_DATA" = true ]; then
  echo ""
  echo "==> Creating dummy test data (150 tracks for pagination testing)"

  # Create the Python script for generating dummy data
  cat > /tmp/create_dummy_data.py << 'PYTHON_EOF'
import sqlite3
import random
import hashlib
from pathlib import Path
from datetime import datetime, timedelta

DB_PATH = Path("/opt/music-intake/db/queue.sqlite3")

# Artists, albums, and titles for variety
artists = [
    "The Beatles", "Pink Floyd", "Led Zeppelin", "Queen", "Nirvana",
    "Radiohead", "Coldplay", "Adele", "Ed Sheeran", "Taylor Swift",
    "Drake", "Kendrick Lamar", "Eminem", "Rihanna", "Beyonce",
    "Metallica", "Red Hot Chili Peppers", "U2", "Oasis", "Blink-182",
    "Green Day", "Foo Fighters", "Maroon 5", "Bruno Mars", "Justin Timberlake"
]

albums = [
    "Abbey Road", "The Dark Side of the Moon", "Led Zeppelin IV",
    "A Night at the Opera", "Nevermind", "OK Computer", "Parachutes",
    "21", "Divide", "1989", "Views", "DAMN.", "The Marshall Mathers LP",
    "Master of Puppets", "Blood Sugar Sex Magik", "The Joshua Tree",
    "Definitely Maybe", "Enema of the State", "American Idiot"
]

titles = [
    "Hey Jude", "Bohemian Rhapsody", "Stairway to Heaven", "Sweet Child O' Mine",
    "Smells Like Teen Spirit", "Creep", "Yellow", "Someone Like You", "Shape of You",
    "Blank Space", "God's Plan", "HUMBLE.", "Lose Yourself", "Umbrella",
    "Crazy in Love", "Enter Sandman", "Californication", "With or Without You",
    "Wonderwall", "All the Small Things", "Basket Case", "24K Magic", "Can't Stop the Feeling"
]

def generate_filehash(seed):
    """Generate a deterministic filehash based on seed"""
    return hashlib.md5(seed.encode()).hexdigest()

def generate_duration():
    """Generate a random duration between 2 and 8 minutes"""
    return random.randint(120, 480)

def generate_filesize(duration):
    """Generate a filesize based on duration (approx 2MB per minute)"""
    mb_per_min = random.uniform(1.8, 2.5)
    return int(duration * mb_per_min * 1024 * 1024)

def generate_confidence():
    """Generate a confidence score"""
    return random.choice([0, 45, 55, 65, 75, 85, 95, 100])

def generate_mtime():
    """Generate a modification time within the last 30 days"""
    return (datetime.now() - timedelta(days=random.randint(0, 30))).timestamp()

# Connect to database
conn = sqlite3.connect(DB_PATH)
conn.execute("PRAGMA journal_mode=WAL;")

# Clear existing pending data (but keep approved/rejected for history testing)
conn.execute("DELETE FROM queue WHERE status = 'pending'")

# Insert 150 test tracks
for i in range(1, 151):
    artist = random.choice(artists)
    album = random.choice(albums)
    title = random.choice(titles)

    duration = generate_duration()
    filesize = generate_filesize(duration)
    confidence = generate_confidence()
    mtime = generate_mtime()
    filehash = generate_filehash(f"{artist}-{title}-{i}")

    filepath = f"/mnt/nas-source/test-folder/{artist.replace(' ', '_')}/{album.replace(' ', '_')}/{title.replace(' ', '_')}_{i}.mp3"

    if random.random() < 0.15:
        confidence = 0

    if random.random() < 0.20:
        prev_idx = random.randint(1, i-1) if i > 1 else 1
        filehash = generate_filehash(f"{artists[prev_idx % len(artists)]}-{titles[prev_idx % len(titles)]}-{prev_idx}")

    conn.execute("""
        INSERT INTO queue
        (filepath, artist, title, album, confidence, filesize, duration, filehash,
         sr_artist, sr_title, sr_album, ac_artist, ac_title, ac_score,
         gn_artist, gn_title, agreement, error, status, mtime, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'pending', ?, CURRENT_TIMESTAMP)
    """, (
        filepath,
        artist,
        title,
        album,
        confidence,
        filesize,
        duration,
        filehash,
        artist if random.random() > 0.3 else None,
        title if random.random() > 0.3 else None,
        album if random.random() > 0.3 else None,
        artist if random.random() > 0.4 else None,
        title if random.random() > 0.4 else None,
        random.uniform(0.7, 0.95) if random.random() > 0.4 else None,
        artist if random.random() > 0.5 else None,
        title if random.random() > 0.5 else None,
        None,  # agreement
        mtime
    ))

conn.commit()

# Also create some approved and rejected tracks for history page testing
for i in range(1, 21):
    artist = random.choice(artists)
    album = random.choice(albums)
    title = random.choice(titles)
    status = random.choice(['approved', 'rejected'])

    duration = generate_duration()
    filesize = generate_filesize(duration)
    confidence = generate_confidence()
    mtime = generate_mtime()
    filehash = generate_filehash(f"{artist}-{title}-history-{i}")
    filepath = f"/mnt/nas-source/test-folder/{artist.replace(' ', '_')}/{album.replace(' ', '_')}/{title.replace(' ', '_')}_history_{i}.mp3"

    created_at = (datetime.now() - timedelta(days=random.randint(1, 60))).strftime('%Y-%m-%d %H:%M:%S')

    conn.execute("""
        INSERT INTO queue
        (filepath, artist, title, album, confidence, filesize, duration, filehash,
         sr_artist, sr_title, sr_album, ac_artist, ac_title, ac_score,
         gn_artist, gn_title, agreement, error, status, mtime, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
    """, (
        filepath,
        artist,
        title,
        album,
        confidence,
        filesize,
        duration,
        filehash,
        artist,
        title,
        album,
        artist,
        title,
        random.uniform(0.8, 0.98),
        artist,
        title,
        None,  # agreement - FIXED: Added missing value for the 20th ? placeholder
        status,
        mtime,
        created_at
    ))

conn.commit()
conn.close()

print(f"Created 150 pending tracks + 20 history tracks")
PYTHON_EOF

  # Run the Python script
  /opt/music-intake/venv/bin/python3 /tmp/create_dummy_data.py
  rm /tmp/create_dummy_data.py

  echo "==> Dummy data created successfully!"
  echo "    - 150 pending tracks (3 pages at 50 per page)"
  echo "    - 20 approved/rejected tracks for history page"
  echo "    - Various confidence levels and metadata sources"
  echo "    - Some duplicates for testing duplicate detection"
fi

echo ""
echo "==> Done. Next steps:"
echo "    1. Edit /opt/music-intake/config/secrets.env with a real ACOUSTID_API_KEY"
echo "       (and GENIUS_ACCESS_TOKEN if you installed --with-whisper)"
echo "    2. Run the review UI:   cd /opt/music-intake/app && source ../venv/bin/activate && python3 server.py"
echo "    3. Test the full pipeline against a real file:"
echo "       ./scripts/dev-test-track.sh /path/to/your/dummy-track.mp3"
if [ "$WITH_DUMMY_DATA" = true ]; then
  echo "    4. Visit http://localhost:5000 to see 150 test tracks with pagination"
fi

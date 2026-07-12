#!/usr/bin/env bash
# Runs one audio file through every stage of the identification pipeline
# individually (SongRec, AcoustID, and the Whisper+Genius lyrics fallback
# if enabled), then through the real process_file() majority-vote logic
# end-to-end against a throwaway in-memory database - so you see both
# what each source found AND what the pipeline decided to trust.
#
# Usage: ./scripts/dev-test-track.sh /path/to/track.mp3
set -euo pipefail

if [ $# -ne 1 ]; then
  echo "Usage: $0 /path/to/track.mp3"
  exit 1
fi

TRACK="$(realpath "$1")"
if [ ! -f "$TRACK" ]; then
  echo "File not found: $TRACK"
  exit 1
fi

if [ ! -d /opt/music-intake/venv ]; then
  echo "No venv found at /opt/music-intake/venv - run ./scripts/dev-setup.sh first"
  exit 1
fi

# shellcheck disable=SC1091
source /opt/music-intake/venv/bin/activate
if [ -f /opt/music-intake/config/secrets.env ]; then
  set -a
  # shellcheck disable=SC1091
  source /opt/music-intake/config/secrets.env
  set +a
fi

echo "=================================================="
echo " Testing: $TRACK"
echo "=================================================="

echo ""
echo "-- Preflight checks --"
if command -v songrec >/dev/null 2>&1; then
  echo "[OK]   songrec binary found"
else
  echo "[SKIP] songrec not installed (run dev-setup.sh --with-songrec) - SongRec results will be empty"
fi
if [ -n "${ACOUSTID_API_KEY:-}" ]; then
  echo "[OK]   ACOUSTID_API_KEY is set"
else
  echo "[WARN] ACOUSTID_API_KEY is not set in secrets.env - AcoustID results will be empty"
fi
if [ -n "${GENIUS_ACCESS_TOKEN:-}" ]; then
  echo "[OK]   GENIUS_ACCESS_TOKEN is set"
  python3 -c "import faster_whisper" 2>/dev/null \
    && echo "[OK]   faster-whisper is installed" \
    || echo "[WARN] faster-whisper not installed (run dev-setup.sh --with-whisper) - lyrics fallback will error"
else
  echo "[SKIP] GENIUS_ACCESS_TOKEN not set - lyrics fallback disabled (this is fine, it's optional)"
fi

PYTHONPATH=/opt/music-intake/pipeline python3 <<EOF
import sys
import time
from pathlib import Path

sys.path.insert(0, "/opt/music-intake/pipeline")
import recognize as r

track = Path("$TRACK")

def timed(label, fn):
    print(f"\n-- {label} --")
    start = time.perf_counter()
    result = fn()
    elapsed = time.perf_counter() - start
    print(f"  result:  {result}")
    print(f"  time:    {elapsed:.1f}s")
    return result

sr_artist, sr_title, sr_album = timed(
    "SongRec (Shazam)", lambda: r.songrec_identify(track)
)

ac_artist, ac_title, ac_score = timed(
    "AcoustID / MusicBrainz", lambda: r.acoustid_lookup(track)
)

duration = r.probe_duration(track)
print(f"\n-- Track duration --\n  {duration}s")

if r.GENIUS_ACCESS_TOKEN:
    try:
        timed("Lyrics fallback (Whisper transcription + Genius search)",
              lambda: r.lyrics_identify(track, duration))
    except Exception as e:
        print(f"\n-- Lyrics fallback --\n  FAILED: {e}")
else:
    print("\n-- Lyrics fallback --\n  skipped (GENIUS_ACCESS_TOKEN not set)")

print("\n=================================================="
      "\n Full pipeline (process_file - majority vote + tie-break logic)"
      "\n==================================================")

conn = r.sqlite3.connect(":memory:")
conn.row_factory = r.sqlite3.Row
conn.executescript(r.SCHEMA)
r.migrate_schema(conn)

start = time.perf_counter()
r.process_file(conn, track)
elapsed = time.perf_counter() - start

row = dict(conn.execute("SELECT * FROM queue").fetchone())
for key, value in row.items():
    print(f"  {key:14s}: {value}")
print(f"\n  total pipeline time: {elapsed:.1f}s")
EOF

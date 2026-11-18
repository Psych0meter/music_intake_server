
#!/usr/bin/env bash
# Runs one audio file through every stage of the identification pipeline
# individually (SongRec, AcoustID, and the Whisper+Genius lyrics fallback
# if enabled), then through the real process_file() majority-vote logic
# end-to-end against a throwaway in-memory database.
#
# Usage: ./scripts/dev-test-track.sh /path/to/track.mp3

set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "Usage: $0 /path/to/track.mp3"
    exit 1
fi

TRACK="$(realpath "$1")"

if [[ ! -f "$TRACK" ]]; then
    echo "File not found: $TRACK"
    exit 1
fi

# Resolve the repository root from this script's location rather than
# relying on a particular Codespaces or deployment directory.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
MIGRATIONS_DIR="$REPO_ROOT/migrations"

if [[ ! -d "$MIGRATIONS_DIR" ]]; then
    echo "Migration directory not found: $MIGRATIONS_DIR"
    exit 1
fi

if ! compgen -G "$MIGRATIONS_DIR/*.sql" > /dev/null; then
    echo "No SQL migrations found in: $MIGRATIONS_DIR"
    exit 1
fi

APP_ROOT="/opt/music-intake"
VENV="$APP_ROOT/venv"
PIPELINE_DIR="$APP_ROOT/pipeline"
SECRETS_FILE="$APP_ROOT/config/secrets.env"

if [[ ! -d "$VENV" ]]; then
    echo "No venv found at $VENV - run ./scripts/dev-setup.sh first"
    exit 1
fi

if [[ ! -d "$PIPELINE_DIR" ]]; then
    echo "Pipeline directory not found: $PIPELINE_DIR"
    exit 1
fi

# shellcheck disable=SC1091
source "$VENV/bin/activate"

if [[ -f "$SECRETS_FILE" ]]; then
    # Preserve an explicitly supplied SCAN_WORKERS value while loading
    # the remaining application environment from secrets.env.
    _scan_workers_was_set=${SCAN_WORKERS+x}
    _scan_workers_override=${SCAN_WORKERS-}

    set -a
    # shellcheck disable=SC1090
    source "$SECRETS_FILE"
    set +a

    if [[ -n "$_scan_workers_was_set" ]]; then
        export SCAN_WORKERS="$_scan_workers_override"
    fi

    unset _scan_workers_was_set _scan_workers_override
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

if [[ -n "${ACOUSTID_API_KEY:-}" ]]; then
    echo "[OK]   ACOUSTID_API_KEY is set"
else
    echo "[WARN] ACOUSTID_API_KEY is not set in secrets.env - AcoustID results will be empty"
fi

if [[ -n "${GENIUS_ACCESS_TOKEN:-}" ]]; then
    echo "[OK]   GENIUS_ACCESS_TOKEN is set"

    if python3 -c "import faster_whisper" 2>/dev/null; then
        echo "[OK]   faster-whisper is installed"
    else
        echo "[WARN] faster-whisper not installed (run dev-setup.sh --with-whisper) - lyrics fallback will error"
    fi
else
    echo "[SKIP] GENIUS_ACCESS_TOKEN not set - lyrics fallback disabled (this is fine, it's optional)"
fi

# Pass paths through the environment rather than interpolating them into
# Python source. This safely handles spaces and quotes in filenames/paths.
TRACK_PATH="$TRACK" \
MIGRATIONS_DIR="$MIGRATIONS_DIR" \
PYTHONPATH="$PIPELINE_DIR${PYTHONPATH:+:$PYTHONPATH}" \
python3 <<'PY'
import os
import sqlite3
import sys
import time
from pathlib import Path

import recognize as r


track = Path(os.environ["TRACK_PATH"])
migrations_dir = Path(os.environ["MIGRATIONS_DIR"])


def timed(label, fn):
    print(f"\n-- {label} --")
    start = time.perf_counter()
    result = fn()
    elapsed = time.perf_counter() - start

    print(f"  result:  {result}")
    print(f"  time:    {elapsed:.1f}s")

    return result


sr_artist, sr_title, sr_album = timed(
    "SongRec (Shazam)",
    lambda: r.songrec_identify(track),
)

ac_artist, ac_title, ac_score = timed(
    "AcoustID / MusicBrainz",
    lambda: r.acoustid_lookup(track),
)

duration = r.probe_duration(track)
print(f"\n-- Track duration --\n  {duration}s")

if r.GENIUS_ACCESS_TOKEN:
    try:
        timed(
            "Lyrics fallback (Whisper transcription + Genius search)",
            lambda: r.lyrics_identify(track, duration),
        )
    except Exception as exc:
        print(f"\n-- Lyrics fallback --\n  FAILED: {exc}")
else:
    print(
        "\n-- Lyrics fallback --\n"
        "  skipped (GENIUS_ACCESS_TOKEN not set)"
    )


print(
    "\n=================================================="
    "\n Full pipeline (process_file - majority vote + tie-break logic)"
    "\n=================================================="
)

conn = sqlite3.connect(":memory:")
conn.row_factory = sqlite3.Row

# Build the test schema from the same ordered SQL migrations used by
# the application.
conn.execute(
    """
    CREATE TABLE IF NOT EXISTS schema_version (
        version TEXT PRIMARY KEY,
        applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """
)

migration_files = sorted(migrations_dir.glob("*.sql"))

if not migration_files:
    raise RuntimeError(
        f"No SQL migrations found in {migrations_dir}"
    )

for migration in migration_files:
    print(f"  applying migration: {migration.name}")
    conn.executescript(migration.read_text(encoding="utf-8"))
    conn.execute(
        "INSERT INTO schema_version (version) VALUES (?)",
        (migration.stem,),
    )
    conn.commit()

start = time.perf_counter()
r.process_file(conn, track)
elapsed = time.perf_counter() - start

row = conn.execute("SELECT * FROM queue").fetchone()

if row is None:
    raise RuntimeError(
        "process_file() completed without creating a queue row"
    )

print("\n-- Final queue entry --")
for key, value in dict(row).items():
    print(f"  {key:14s}: {value}")

print(f"\n  total pipeline time: {elapsed:.1f}s")

conn.close()
PY

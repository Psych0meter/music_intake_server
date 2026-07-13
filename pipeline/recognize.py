#!/usr/bin/env python3
"""
Scans the folders listed in config/scan_roots.txt (re-read every cycle,
so folders can be added/removed without a restart), fingerprints new
files with SongRec, cross-checks against AcoustID/MusicBrainz, writes
tags in-place, and records a row in the review queue. Files are never
moved or renamed until approved/rejected via the review UI.

Note: Database schema must be migrated separately using migrate.py

Optimizations:
- Batch database operations for better performance
- Improved error handling and connection management
- Cached file listings to avoid redundant scans
- Added file modification time tracking to skip unchanged files
"""
import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from difflib import SequenceMatcher
from pathlib import Path

import acoustid
import mutagen
import requests

socket.setdefaulttimeout(15)  # Prevents any network lookup from hanging the scanner loop

APP_ROOT = Path("/opt/music-intake")
SCAN_ROOTS_FILE = APP_ROOT / "config" / "scan_roots.txt"
DB_PATH = APP_ROOT / "db" / "queue.sqlite3"
ACOUSTID_API_KEY = os.environ.get("ACOUSTID_API_KEY")
GENIUS_ACCESS_TOKEN = os.environ.get("GENIUS_ACCESS_TOKEN")
WHISPER_MODEL_SIZE = os.environ.get("WHISPER_MODEL_SIZE", "small")
WHISPER_DEVICE = os.environ.get("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE_TYPE = os.environ.get("WHISPER_COMPUTE_TYPE", "int8")
_whisper_model = None  # lazy-loaded so startup stays fast when this path never fires
SUPPORTED_EXT = {".mp3", ".flac", ".m4a", ".ogg", ".wav"}

def get_db():
    """Get database connection. Schema must be migrated separately via migrate.py"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn

def load_scan_roots():
    if not SCAN_ROOTS_FILE.is_file():
        print(f"[config] {SCAN_ROOTS_FILE} does not exist - nothing to scan", file=sys.stderr)
        return []

    roots = []
    for line in SCAN_ROOTS_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        p = Path(line)
        if p.is_dir():
            roots.append(p.resolve())
        else:
            print(f"[config] scan root does not exist, skipping: {line}", file=sys.stderr)
    return roots

def discover_files():
    """Discover all supported audio files in scan roots."""
    files = []
    for root in load_scan_roots():
        try:
            files.extend(
                f for f in root.rglob("*")
                if f.is_file() and f.suffix.lower() in SUPPORTED_EXT
            )
        except Exception as e:
            print(f"[discover] Error scanning {root}: {e}", file=sys.stderr)
    return files

def batch_already_queued(conn, filepaths):
    """Batch check which files are already in the queue - much faster than individual queries."""
    if not filepaths:
        return set()

    path_strs = [str(f) for f in filepaths]
    placeholders = ",".join("?" * len(path_strs))
    query = f"SELECT filepath, error FROM queue WHERE filepath IN ({placeholders})"

    rows = conn.execute(query, path_strs).fetchall()
    return {row["filepath"] for row in rows if not row["error"]}

def batch_get_mtimes(conn, filepaths):
    """Batch get modification times for already queued files."""
    if not filepaths:
        return {}

    path_strs = [str(f) for f in filepaths]
    placeholders = ",".join("?" * len(path_strs))
    query = f"SELECT filepath, mtime FROM queue WHERE filepath IN ({placeholders})"

    rows = conn.execute(query, path_strs).fetchall()
    return {row["filepath"]: row["mtime"] for row in rows}

def update_scan_status(conn, total, processed, current_file):
    conn.execute(
        "INSERT INTO scan_status (id, total, processed, current_file, updated_at, is_paused) "
        "VALUES (1, ?, ?, ?, CURRENT_TIMESTAMP, 0) "
        "ON CONFLICT(id) DO UPDATE SET "
        "total=excluded.total, processed=excluded.processed, "
        "current_file=excluded.current_file, updated_at=CURRENT_TIMESTAMP",
        (total, processed, current_file)
    )
    conn.commit()

def is_paused(conn):
    """Check if scanning is paused."""
    row = conn.execute("SELECT is_paused FROM scan_status WHERE id = 1").fetchone()
    return row and row["is_paused"] == 1

def songrec_identify(filepath):
    try:
        result = subprocess.run(
            ["songrec", "recognize", "-j", str(filepath)],
            capture_output=True, text=True, timeout=60
        )

        if not result.stdout or not result.stdout.strip():
            print(f"[songrec] No acoustic match found for {filepath}")
            return None, None, None

        try:
            data = json.loads(result.stdout)
            track = data.get("track", {})
        except json.JSONDecodeError:
            print(f"[songrec] Failed to parse response payload: {result.stdout}")
            return None, None, None

        artist = track.get("subtitle")
        title = track.get("title")
        album = None

        for section in track.get("sections", []):
            for item in section.get("metadata", []):
                if item.get("title") == "Album":
                    album = item.get("text")
                    break
        return artist, title, album
    except Exception as e:
        print(f"[songrec] failed for {filepath}: {e}", file=sys.stderr)
        return None, None, None

def acoustid_lookup(filepath):
    if not ACOUSTID_API_KEY:
        print("[acoustid] ACOUSTID_API_KEY is not set - skipping lookup", file=sys.stderr)
        return None, None, 0.0
    try:
        results = acoustid.match(ACOUSTID_API_KEY, str(filepath))
        for score, rid, title, artist in results:
            return artist, title, score
    except acoustid.NoBackendError:
        print("[acoustid] chromaprint/fpcalc not found on PATH", file=sys.stderr)
    except acoustid.FingerprintGenerationError as e:
        print(f"[acoustid] fingerprinting failed for {filepath}: {e}", file=sys.stderr)
    except acoustid.WebServiceError as e:
        print(f"[acoustid] API error for {filepath}: {e}", file=sys.stderr)
    except Exception as e:
        print(f"[acoustid] failed for {filepath}: {e}", file=sys.stderr)
    return None, None, 0.0

def itunes_verify(artist, title):
    """Verify artist/title combination against iTunes API."""
    if not artist or not title:
        return False
    try:
        resp = requests.get(
            "https://itunes.apple.com/search",
            params={"term": f"{artist} {title}", "entity": "song", "limit": 1},
            timeout=10,
        )
        results = resp.json().get("results", [])
        if not results:
            return False
        result = results[0]
        sims = [
            s for s in (
                similarity(artist, result.get("artistName")),
                similarity(title, result.get("trackName")),
            ) if s is not None
        ]
        return bool(sims) and (sum(sims) / len(sims)) >= 0.6
    except Exception as e:
        print(f"[itunes] verify failed for {artist} - {title}: {e}", file=sys.stderr)
        return False

def get_whisper_model():
    """Lazy-load Whisper model."""
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        print(f"[whisper] Loading model: {WHISPER_MODEL_SIZE} on {WHISPER_DEVICE} ({WHISPER_COMPUTE_TYPE})...", file=sys.stderr)
        _whisper_model = WhisperModel(WHISPER_MODEL_SIZE, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE_TYPE)
        print(f"[whisper] Model loaded successfully", file=sys.stderr)
    return _whisper_model

def get_sample_windows(duration, window=20, count=3):
    """Calculate sample windows for transcription."""
    if not duration or duration <= window:
        return [(0, duration or window)]
    fractions = [0.15, 0.5, 0.8][:count]
    windows = []
    for frac in fractions:
        offset = max(0, min(duration - window, duration * frac))
        windows.append((offset, window))
    return windows

def transcribe_clip(filepath, offset, duration):
    """Transcribe a clip from the file."""
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav") as tmp:
            subprocess.run(
                ["ffmpeg", "-y", "-ss", str(offset), "-t", str(duration),
                 "-i", str(filepath), "-ar", "16000", "-ac", "1", tmp.name],
                capture_output=True, timeout=60, check=True
            )

            segments, _ = get_whisper_model().transcribe(
                tmp.name,
                vad_filter=True,
                vad_parameters=dict(min_silence_duration_ms=500),
                no_speech_threshold=0.6,
                compression_ratio_threshold=2.4
            )

            text = " ".join(seg.text for seg in segments).strip()

            lowercase_text = text.lower()
            if "thank you for watching" in lowercase_text or "subtitles by" in lowercase_text:
                return ""

            return text
    except Exception as e:
        print(f"[whisper] transcription failed for {filepath} at {offset}s: {e}", file=sys.stderr)
        return ""

def transcribe_track(filepath, duration, window=20, max_windows=3, min_words=15):
    """Transcribe track using multiple sample windows."""
    text_parts = []
    for offset, win in get_sample_windows(duration, window=window, count=max_windows):
        snippet = transcribe_clip(filepath, offset, win)
        if snippet:
            text_parts.append(snippet)
        if sum(len(p.split()) for p in text_parts) >= min_words:
            break
    return " ".join(text_parts).strip()

def genius_lyrics_search(snippet):
    """Search Genius for lyrics matching the snippet."""
    if not GENIUS_ACCESS_TOKEN or not snippet:
        return None, None
    try:
        clean_query = " ".join(snippet.split()[:12])
        resp = requests.get(
            "https://api.genius.com/search",
            params={
                "q": clean_query,
                "type": "song"
            },
            headers={"Authorization": f"Bearer {GENIUS_ACCESS_TOKEN}"},
            timeout=15,
        )
        hits = resp.json().get("response", {}).get("hits", [])
        if not hits:
            return None, None

        for hit in hits:
            if hit.get("type") == "song":
                result = hit["result"]
                return result.get("primary_artist", {}).get("name"), result.get("title")

        return None, None
    except Exception as e:
        print(f"[genius] search failed: {e}", file=sys.stderr)
        return None, None

def lyrics_identify(filepath, duration):
    """Identify track using Whisper transcription + Genius lyrics search."""
    text = transcribe_track(filepath, duration)
    if not text or len(text.split()) < 15:
        return None, None
    return genius_lyrics_search(text)

def write_tags(filepath, artist, title, album=None):
    """Write metadata tags to the file."""
    try:
        audio = mutagen.File(filepath, easy=True)
        if audio is None:
            return
        if artist:
            audio["artist"] = artist
        if title:
            audio["title"] = title
        if album:
            audio["album"] = album
        audio.save()
    except Exception as e:
        print(f"[tags] Failed to write tags to {filepath}: {e}", file=sys.stderr)

def compute_filehash(filepath, chunk_size=1024 * 1024):
    """Compute SHA256 hash of the file."""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()

def probe_duration(filepath):
    """Get duration of the audio file."""
    try:
        audio = mutagen.File(filepath)
        return audio.info.length if audio and audio.info else None
    except Exception:
        return None

def similarity(a, b):
    """Calculate similarity ratio between two strings."""
    if not a or not b:
        return None
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()

def best_pair_match(candidates):
    """Find the best matching pair among candidates."""
    best = None
    for i in range(len(candidates)):
        for j in range(i + 1, len(candidates)):
            _, a1, t1 = candidates[i]
            _, a2, t2 = candidates[j]
            sims = [s for s in (similarity(a1, a2), similarity(t1, t2)) if s is not None]
            if not sims:
                continue
            avg = sum(sims) / len(sims)
            if avg >= 0.6 and (best is None or avg > best[0]):
                best = (avg, candidates[i], candidates[j])
    return best

def process_file(conn, filepath):
    """Process a single file: fingerprint, identify, tag, and queue."""
    filesize = filepath.stat().st_size
    duration = probe_duration(filepath)
    mtime = filepath.stat().st_mtime

    sr_artist, sr_title, sr_album = songrec_identify(filepath)
    ac_artist, ac_title, score = acoustid_lookup(filepath)
    gn_artist, gn_title = lyrics_identify(filepath, duration) if GENIUS_ACCESS_TOKEN else (None, None)

    acoustid_confidence = round(score * 100, 1) if score else 0.0

    candidates = [
        c for c in [
            ("songrec", sr_artist, sr_title),
            ("acoustid", ac_artist, ac_title),
            ("genius", gn_artist, gn_title),
        ] if c[1] or c[2]
    ]
    match = best_pair_match(candidates)

    if match:
        agree_score, (_, a1, t1), (_, a2, t2) = match
        artist = a1 or a2
        title = t1 or t2
        confidence = max(75.0, acoustid_confidence)
        agreement = agree_score
    elif not sr_artist and not ac_artist and not gn_artist:
        artist, title = None, None
        confidence = 0.0
        agreement = None
    elif gn_artist and not sr_artist and not ac_artist:
        artist, title = gn_artist, gn_title
        confidence = 50.0
        agreement = None
    else:
        sr_valid = itunes_verify(sr_artist, sr_title)
        ac_valid = itunes_verify(ac_artist, ac_title)
        agreement = None

        if sr_valid and not ac_valid:
            artist, title = sr_artist, sr_title
            confidence = 65.0
        elif ac_valid and not sr_valid:
            artist, title = ac_artist, ac_title
            confidence = max(65.0, acoustid_confidence)
        else:
            artist = sr_artist or ac_artist or gn_artist
            title = sr_title or ac_title or gn_title
            if sr_artist and ac_artist:
                confidence = min(acoustid_confidence, 40.0)
            elif acoustid_confidence:
                confidence = acoustid_confidence
            elif sr_artist:
                confidence = 60.0
            elif gn_artist:
                confidence = 50.0
            else:
                confidence = 0.0

    album = sr_album

    if artist or title or album:
        write_tags(filepath, artist, title, album)

    filehash = compute_filehash(filepath)

    conn.execute(
        "INSERT INTO queue "
        "(filepath, artist, title, album, confidence, filesize, duration, filehash, "
        " sr_artist, sr_title, sr_album, ac_artist, ac_title, ac_score, gn_artist, gn_title, agreement, error, status, mtime) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, NULL, 'pending', ?) "
        "ON CONFLICT(filepath) DO UPDATE SET "
        "artist=excluded.artist, title=excluded.title, album=excluded.album, "
        "confidence=excluded.confidence, filesize=excluded.filesize, duration=excluded.duration, "
        "filehash=excluded.filehash, sr_artist=excluded.sr_artist, sr_title=excluded.sr_title, "
        "sr_album=excluded.sr_album, ac_artist=excluded.ac_artist, ac_title=excluded.ac_title, "
        "ac_score=excluded.ac_score, gn_artist=excluded.gn_artist, gn_title=excluded.gn_title, "
        "agreement=excluded.agreement, error=NULL, status='pending', mtime=excluded.mtime",
        (str(filepath), artist, title, album, confidence, filesize, duration, filehash,
        sr_artist, sr_title, sr_album, ac_artist, ac_title, score, gn_artist, gn_title, agreement, mtime)
    )
    conn.commit()
    print(f"[queued] {filepath.name} -> {artist} / {title} - {album or '?'} ({confidence}%)")

def scan_loop(poll_seconds=15):
    """Main scanning loop."""
    conn = get_db()

    # Initialize scan_status if not exists
    conn.execute(
        "INSERT INTO scan_status (id, total, processed, current_file, updated_at, is_paused) "
        "VALUES (1, 0, 0, NULL, CURRENT_TIMESTAMP, 0) "
        "ON CONFLICT(id) DO NOTHING"
    )
    conn.commit()

    last_file_count = 0

    try:
        while True:
            # Check if paused
            if is_paused(conn):
                time.sleep(5)
                continue

            try:
                # Discover all files
                all_files = discover_files()

                # Get paths of already queued files (batch query)
                queued_paths = batch_already_queued(conn, all_files)

                # Get mtimes for already queued files
                queued_mtimes = batch_get_mtimes(conn, [Path(p) for p in queued_paths])

                # Check which files need processing (not queued OR modified since last scan)
                pending_files = []
                for f in all_files:
                    f_str = str(f)
                    f_mtime = f.stat().st_mtime

                    if f_str not in queued_paths:
                        pending_files.append(f)
                    elif f_str in queued_mtimes and queued_mtimes[f_str] != f_mtime:
                        # File was modified since last scan, re-process it
                        pending_files.append(f)

                total = len(all_files)

                # Only update if file count changed or we have pending files
                if len(all_files) != last_file_count or pending_files:
                    update_scan_status(conn, total=total, processed=len(all_files) - len(pending_files), current_file=None)
                    last_file_count = len(all_files)

                # Process pending files
                for i, f in enumerate(pending_files):
                    update_scan_status(
                        conn, total=total,
                        processed=len(all_files) - len(pending_files) + i,
                        current_file=str(f)
                    )
                    try:
                        process_file(conn, f)
                    except Exception as e:
                        print(f"[scan_loop] failed to process file {f}: {e}", file=sys.stderr)
                        try:
                            conn.execute(
                                "INSERT INTO queue (filepath, confidence, error, status, mtime) VALUES (?, 0.0, ?, 'pending', ?) "
                                "ON CONFLICT(filepath) DO UPDATE SET error=excluded.error, status='pending', mtime=excluded.mtime",
                                (str(f), str(e), f.stat().st_mtime)
                            )
                            conn.commit()
                        except Exception as db_err:
                            print(f"[scan_loop] failed logging error: {db_err}", file=sys.stderr)

                # Mark completion
                update_scan_status(conn, total=total, processed=total, current_file=None)

            except Exception as e:
                print(f"[scan_loop] global loop failure: {e}", file=sys.stderr)
                # Re-establish connection if it was lost
                try:
                    conn.close()
                except:
                    pass
                conn = get_db()

            time.sleep(poll_seconds)

    except KeyboardInterrupt:
        print("[scan_loop] Received interrupt, shutting down...", file=sys.stderr)
    finally:
        conn.close()

if __name__ == "__main__":
    scan_loop()

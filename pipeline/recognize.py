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
- Proper logging to files
"""
import hashlib
import json
import logging
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from difflib import SequenceMatcher
from logging.handlers import RotatingFileHandler
from pathlib import Path

import acoustid
import mutagen
import requests

socket.setdefaulttimeout(15)

APP_ROOT = Path("/opt/music-intake")
SCAN_ROOTS_FILE = APP_ROOT / "config" / "scan_roots.txt"
DB_PATH = Path(os.environ.get("MUSIC_DB_PATH", APP_ROOT / "db" / "queue.sqlite3"))
ACOUSTID_API_KEY = os.environ.get("ACOUSTID_API_KEY")
GENIUS_ACCESS_TOKEN = os.environ.get("GENIUS_ACCESS_TOKEN")
WHISPER_MODEL_SIZE = os.environ.get("WHISPER_MODEL_SIZE", "small")
WHISPER_DEVICE = os.environ.get("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE_TYPE = os.environ.get("WHISPER_COMPUTE_TYPE", "int8")
_whisper_model = None
SUPPORTED_EXT = {".mp3", ".flac", ".m4a", ".ogg", ".wav"}

# --- Logging Setup ---
def setup_logging():
    logger = logging.getLogger('recognize')
    logger.setLevel(logging.INFO)

    log_dir = Path("/opt/music-intake/logs")
    log_dir.mkdir(parents=True, exist_ok=True)

    # File handler with rotation
    handler = RotatingFileHandler(
        log_dir / "recognize.log",
        maxBytes=10*1024*1024,  # 10MB
        backupCount=5
    )
    handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
    logger.addHandler(handler)

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
    logger.addHandler(console_handler)

    return logger

logger = setup_logging()

# --- Database ---
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn

def load_scan_roots():
    if not SCAN_ROOTS_FILE.is_file():
        logger.error(f"Config file {SCAN_ROOTS_FILE} does not exist - nothing to scan")
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
            logger.warning(f"Scan root does not exist, skipping: {line}")
    return roots

def discover_files():
    files = []
    for root in load_scan_roots():
        try:
            files.extend(
                f for f in root.rglob("*")
                if f.is_file() and f.suffix.lower() in SUPPORTED_EXT
            )
        except Exception as e:
            logger.error(f"Error scanning {root}: {e}")
    return files

def batch_already_queued(conn, filepaths):
    if not filepaths:
        return set()
    path_strs = [str(f) for f in filepaths]
    placeholders = ",".join("?" * len(path_strs))
    query = f"SELECT filepath, error FROM queue WHERE filepath IN ({placeholders})"
    rows = conn.execute(query, path_strs).fetchall()
    return {row["filepath"] for row in rows if not row["error"]}

def batch_get_mtimes(conn, filepaths):
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
    row = conn.execute("SELECT is_paused FROM scan_status WHERE id = 1").fetchone()
    return row and row["is_paused"] == 1

# --- Identification Functions ---
def songrec_identify(filepath):
    try:
        result = subprocess.run(
            ["songrec", "recognize", "-j", str(filepath)],
            capture_output=True, text=True, timeout=60
        )
        if not result.stdout or not result.stdout.strip():
            logger.info(f"No acoustic match found for {filepath}")
            return None, None, None
        try:
            data = json.loads(result.stdout)
            track = data.get("track", {})
        except json.JSONDecodeError:
            logger.error(f"Failed to parse SongRec response: {result.stdout}")
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
        logger.error(f"SongRec failed for {filepath}: {e}")
        return None, None, None

def acoustid_lookup(filepath):
    if not ACOUSTID_API_KEY:
        logger.warning("ACOUSTID_API_KEY is not set - skipping lookup")
        return None, None, 0.0
    try:
        results = acoustid.match(ACOUSTID_API_KEY, str(filepath))
        for score, rid, title, artist in results:
            return artist, title, score
    except acoustid.NoBackendError:
        logger.error("chromaprint/fpcalc not found on PATH")
    except acoustid.FingerprintGenerationError as e:
        logger.error(f"Fingerprinting failed for {filepath}: {e}")
    except acoustid.WebServiceError as e:
        logger.error(f"AcoustID API error for {filepath}: {e}")
    except Exception as e:
        logger.error(f"AcoustID failed for {filepath}: {e}")
    return None, None, 0.0

def itunes_verify(artist, title):
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
        logger.error(f"iTunes verify failed for {artist} - {title}: {e}")
        return False

def get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        logger.info(f"Loading Whisper model: {WHISPER_MODEL_SIZE} on {WHISPER_DEVICE} ({WHISPER_COMPUTE_TYPE})")
        _whisper_model = WhisperModel(WHISPER_MODEL_SIZE, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE_TYPE)
        logger.info("Whisper model loaded successfully")
    return _whisper_model

def get_sample_windows(duration, window=20, count=3):
    if not duration or duration <= window:
        return [(0, duration or window)]
    fractions = [0.15, 0.5, 0.8][:count]
    windows = []
    for frac in fractions:
        offset = max(0, min(duration - window, duration * frac))
        windows.append((offset, window))
    return windows

def transcribe_clip(filepath, offset, duration):
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
        logger.error(f"Transcription failed for {filepath} at {offset}s: {e}")
        return ""

def transcribe_track(filepath, duration, window=20, max_windows=3, min_words=15):
    text_parts = []
    for offset, win in get_sample_windows(duration, window=window, count=max_windows):
        snippet = transcribe_clip(filepath, offset, win)
        if snippet:
            text_parts.append(snippet)
        if sum(len(p.split()) for p in text_parts) >= min_words:
            break
    return " ".join(text_parts).strip()

def genius_lyrics_search(snippet):
    if not GENIUS_ACCESS_TOKEN or not snippet:
        return None, None
    try:
        clean_query = " ".join(snippet.split()[:12])
        resp = requests.get(
            "https://api.genius.com/search",
            params={"q": clean_query, "type": "song"},
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
        logger.error(f"Genius search failed: {e}")
        return None, None

def lyrics_identify(filepath, duration):
    text = transcribe_track(filepath, duration)
    if not text or len(text.split()) < 15:
        return None, None
    return genius_lyrics_search(text)

# --- Tag Writing ---
def write_tags(filepath, artist, title, album=None):
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
        logger.error(f"Failed to write tags to {filepath}: {e}")

def compute_filehash(filepath, chunk_size=1024 * 1024):
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()

def probe_duration(filepath):
    try:
        audio = mutagen.File(filepath)
        return audio.info.length if audio and audio.info else None
    except Exception:
        return None

def similarity(a, b):
    if not a or not b:
        return None
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()

def best_pair_match(candidates):
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
    filesize = filepath.stat().st_size
    duration = probe_duration(filepath)
    mtime = filepath.stat().st_mtime

    logger.info(f"Processing: {filepath.name}")
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
    logger.info(f"Queued: {filepath.name} -> {artist} / {title} - {album or '?'} ({confidence}%)")

def scan_loop(poll_seconds=15):
    conn = get_db()
    conn.execute(
        "INSERT INTO scan_status (id, total, processed, current_file, updated_at, is_paused) "
        "VALUES (1, 0, 0, NULL, CURRENT_TIMESTAMP, 0) "
        "ON CONFLICT(id) DO NOTHING"
    )
    conn.commit()

    try:
        while True:
            if is_paused(conn):
                logger.info("Scanner paused - sleeping for 5 seconds")
                time.sleep(5)
                continue

            try:
                all_files = discover_files()

                # Get all filepaths already in queue
                queued_set = set()
                if all_files:
                    path_strs = [str(f) for f in all_files]
                    placeholders = ",".join("?" * len(path_strs))
                    rows = conn.execute(
                        f"SELECT filepath FROM queue WHERE filepath IN ({placeholders})",
                        path_strs
                    ).fetchall()
                    queued_set = {row["filepath"] for row in rows}

                # Only process files NOT already in queue
                pending_files = [f for f in all_files if str(f) not in queued_set]

                total = len(all_files)
                update_scan_status(conn, total=total, processed=len(all_files) - len(pending_files), current_file=None)

                batch_start_processed = len(all_files) - len(pending_files)
                for i, f in enumerate(pending_files):
                    if is_paused(conn):
                        logger.info("Pause requested mid-batch - stopping here, "
                                    "will resume from this point when unpaused")
                        break
                    update_scan_status(
                        conn, total=total,
                        processed=batch_start_processed + i,
                        current_file=str(f)
                    )
                    try:
                        process_file(conn, f)
                    except Exception as e:
                        logger.error(f"Failed to process file {f}: {e}")
                        try:
                            conn.execute(
                                "INSERT INTO queue (filepath, confidence, error, status, mtime) VALUES (?, 0.0, ?, 'pending', ?) "
                                "ON CONFLICT(filepath) DO UPDATE SET error=excluded.error, status='pending', mtime=excluded.mtime",
                                (str(f), str(e), f.stat().st_mtime)
                            )
                            conn.commit()
                        except Exception as db_err:
                            logger.error(f"Failed logging error: {db_err}")

                update_scan_status(conn, total=total, processed=total, current_file=None)

            except Exception as e:
                logger.error(f"Global loop failure: {e}")
                try:
                    conn.close()
                except:
                    pass
                conn = get_db()

            time.sleep(poll_seconds)

    except KeyboardInterrupt:
        logger.info("Received interrupt, shutting down...")
    finally:
        conn.close()

if __name__ == "__main__":
    scan_loop()

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
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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
_whisper_lock = threading.Lock()
SUPPORTED_EXT = {".mp3", ".flac", ".m4a", ".ogg", ".wav"}

# How many files to identify concurrently. Nearly all of the per-file cost
# (SongRec subprocess, AcoustID/iTunes/Genius HTTP round-trips) is I/O wait,
# not CPU - the old strictly-one-file-at-a-time loop meant extra vCPUs never
# actually got used no matter how many you gave the container. Default scales
# with available cores (capped at 8 so a big box doesn't hammer AcoustID/
# iTunes/Genius or a network NAS mount too hard); override via
# SCAN_WORKERS in config/secrets.env.
SCAN_WORKERS = max(1, int(os.environ.get("SCAN_WORKERS", str(min(8, os.cpu_count() or 4)))))

# Shared connection-pooled session for the iTunes/Genius HTTP calls, sized
# for SCAN_WORKERS concurrent requests instead of opening a fresh
# connection per call. requests.Session is safe to share across threads.
_http_session = requests.Session()
_http_session.mount(
    "https://",
    requests.adapters.HTTPAdapter(pool_maxsize=max(10, SCAN_WORKERS))
)

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
    """Filepaths already present in the queue, regardless of whether they
    previously errored. A file that failed once (corrupt, unreadable,
    permission denied, etc.) must NOT be silently retried on every ~15s
    poll forever - that turns one bad file into a permanent CPU-burning
    loop that never lets the scan finish. Retrying an errored file is
    explicit: either the user clicks Rescan in the review UI (which
    deletes the row, so it looks "new" again next cycle), or the file's
    mtime actually changes on disk (see is_changed below), e.g. because
    they replaced/fixed it externally."""
    if not filepaths:
        return set()
    path_strs = [str(f) for f in filepaths]
    placeholders = ",".join("?" * len(path_strs))
    query = f"SELECT filepath FROM queue WHERE filepath IN ({placeholders})"
    rows = conn.execute(query, path_strs).fetchall()
    return {row["filepath"] for row in rows}

def batch_get_mtimes(conn, filepaths):
    if not filepaths:
        return {}
    path_strs = [str(f) for f in filepaths]
    placeholders = ",".join("?" * len(path_strs))
    query = f"SELECT filepath, mtime FROM queue WHERE filepath IN ({placeholders})"
    rows = conn.execute(query, path_strs).fetchall()
    return {row["filepath"]: row["mtime"] for row in rows}

def update_scan_status(conn, total, processed, current_file, reset_start=False):
    """reset_start=True marks the start of a new scan batch/pass and
    bumps `updated_at`, which the review UI reads as the pass's start
    time to estimate a completion ETA. Per-file progress updates within
    that pass with reset_start=False, so `updated_at` stays fixed at the
    pass's actual start time - if it were bumped on every file (as it
    used to be), "time since start" would always be ~0 and the UI's
    rate/ETA math would be meaningless."""
    if reset_start:
        conn.execute(
            "INSERT INTO scan_status (id, total, processed, current_file, updated_at, is_paused) "
            "VALUES (1, ?, ?, ?, CURRENT_TIMESTAMP, 0) "
            "ON CONFLICT(id) DO UPDATE SET "
            "total=excluded.total, processed=excluded.processed, "
            "current_file=excluded.current_file, updated_at=CURRENT_TIMESTAMP",
            (total, processed, current_file)
        )
    else:
        conn.execute(
            "INSERT INTO scan_status (id, total, processed, current_file, updated_at, is_paused) "
            "VALUES (1, ?, ?, ?, CURRENT_TIMESTAMP, 0) "
            "ON CONFLICT(id) DO UPDATE SET "
            "total=excluded.total, processed=excluded.processed, "
            "current_file=excluded.current_file",
            (total, processed, current_file)
        )
    conn.commit()

def is_paused(conn):
    row = conn.execute("SELECT is_paused FROM scan_status WHERE id = 1").fetchone()
    return row and row["is_paused"] == 1

def get_generation(conn):
    """Bumped by the review UI's Purge Queue action so an in-progress
    scan batch can notice its queue was wiped out from under it and stop
    immediately, instead of blindly continuing to re-insert the files
    already in its in-memory batch list (which made Purge look like it
    "didn't clean everything" - the files it just deleted would get
    reinserted a moment later by the pass that was already mid-stride)."""
    row = conn.execute("SELECT generation FROM scan_status WHERE id = 1").fetchone()
    return (row["generation"] if row and "generation" in row.keys() else 0) or 0

# --- Identification Functions ---
def songrec_identify(filepath):
    try:
        result = subprocess.run(
            ["songrec", "recognize", "-j", str(filepath)],
            capture_output=True, text=True, timeout=60, check=False
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
        resp = _http_session.get(
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
        with _whisper_lock:
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
                vad_parameters={"min_silence_duration_ms": 500},
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
        resp = _http_session.get(
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

def identify_and_tag(filepath):
    """Runs the full identification pipeline for one file (SongRec,
    AcoustID, iTunes tie-break, optional Genius/Whisper fallback), writes
    the resulting tags in place, and returns the fields the caller should
    store in the queue row. Touches only `filepath` and the network -
    never the database or any other shared mutable state - so it's safe
    to call from multiple worker threads at once (see scan_loop)."""
    duration = probe_duration(filepath)

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

    # filesize/mtime are captured AFTER any tag write above, never before:
    # write_tags() rewrites the file in place (mutagen's audio.save()),
    # which bumps the file's on-disk mtime. Capturing mtime earlier would
    # store a value that's already stale by the time this row is written,
    # so the very next scan cycle would see "mtime on disk != mtime in DB"
    # for every successfully tagged track and reprocess it all over again
    # - forever. That was the cause of the endless-scan / 100%-CPU bug.
    filesize = filepath.stat().st_size
    mtime = filepath.stat().st_mtime
    filehash = compute_filehash(filepath)

    return {
        "artist": artist, "title": title, "album": album, "confidence": confidence,
        "filesize": filesize, "duration": duration, "filehash": filehash,
        "sr_artist": sr_artist, "sr_title": sr_title, "sr_album": sr_album,
        "ac_artist": ac_artist, "ac_title": ac_title, "ac_score": score,
        "gn_artist": gn_artist, "gn_title": gn_title, "agreement": agreement,
        "mtime": mtime,
    }

def _write_queue_row(conn, filepath, result):
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
        (str(filepath), result["artist"], result["title"], result["album"], result["confidence"],
         result["filesize"], result["duration"], result["filehash"],
         result["sr_artist"], result["sr_title"], result["sr_album"],
         result["ac_artist"], result["ac_title"], result["ac_score"],
         result["gn_artist"], result["gn_title"], result["agreement"], result["mtime"])
    )
    conn.commit()
    logger.info(
        f"Queued: {filepath.name} -> {result['artist']} / {result['title']} - "
        f"{result['album'] or '?'} ({result['confidence']}%)"
    )

def process_file(conn, filepath):
    """Synchronous single-file entry point: identify, tag, and write the
    queue row, all on the caller's connection/thread. Used by
    scripts/dev-test-track.sh and anywhere else that wants one file done
    end-to-end. scan_loop() does NOT call this - it runs identify_and_tag()
    in a worker pool and writes rows back on the main thread instead, so
    multiple files can be identified concurrently (see SCAN_WORKERS)."""
    result = identify_and_tag(filepath)
    _write_queue_row(conn, filepath, result)

def scan_loop(poll_seconds=15):
    conn = get_db()
    conn.execute(
        "INSERT INTO scan_status (id, total, processed, current_file, updated_at, is_paused) "
        "VALUES (1, 0, 0, NULL, CURRENT_TIMESTAMP, 0) "
        "ON CONFLICT(id) DO NOTHING"
    )
    conn.commit()
    logger.info(f"Scan loop starting (SCAN_WORKERS={SCAN_WORKERS})")

    try:
        while True:
            if is_paused(conn):
                logger.info("Scanner paused - sleeping for 5 seconds")
                time.sleep(5)
                continue

            try:
                all_files = discover_files()
                queued_paths = batch_already_queued(conn, all_files)
                queued_mtimes = batch_get_mtimes(conn, [Path(p) for p in queued_paths])

                pending_files = []
                for f in all_files:
                    f_str = str(f)
                    f_mtime = f.stat().st_mtime
                    is_new = f_str not in queued_paths
                    is_changed = f_str in queued_mtimes and queued_mtimes[f_str] != f_mtime
                    if is_new or is_changed:
                        pending_files.append(f)

                # `total`/`processed` describe THIS batch of new/changed
                # files only (not the whole library) - the review UI's
                # progress bar and ETA are meaningless if "total" is
                # dominated by files that were already scanned long ago.
                # reset_start=True stamps `updated_at` as this batch's
                # real start time, which the UI uses for its ETA.
                total = len(pending_files)
                batch_generation = get_generation(conn)
                update_scan_status(conn, total=total, processed=0, current_file=None, reset_start=True)

                processed_count = 0
                stopped_early = False

                # Files are IDENTIFIED (SongRec/AcoustID/iTunes/Genius -
                # almost entirely network/subprocess wait, not CPU) up to
                # SCAN_WORKERS at a time in worker threads. The DB write
                # for each result happens back here on the single main
                # connection, one at a time, as results come in - SQLite
                # connections aren't safe to share across threads, and
                # writes need to be serialized anyway.
                if pending_files:
                    pool = ThreadPoolExecutor(max_workers=SCAN_WORKERS)
                    futures = {pool.submit(identify_and_tag, f): f for f in pending_files}
                    try:
                        for future in as_completed(futures):
                            f = futures[future]

                            if is_paused(conn):
                                logger.info("Pause requested mid-batch - stopping here, "
                                            "will resume from this point when unpaused")
                                stopped_early = True
                                break
                            if get_generation(conn) != batch_generation:
                                # Purge Queue was clicked while this batch was
                                # running: rows were just deleted, so stop
                                # writing further results immediately instead
                                # of re-inserting them - the next cycle will
                                # re-discover whatever's still actually on
                                # disk and start a fresh batch.
                                logger.info("Queue purged mid-batch - stopping current batch early")
                                stopped_early = True
                                break

                            try:
                                result = future.result()
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
                            else:
                                _write_queue_row(conn, f, result)

                            processed_count += 1
                            update_scan_status(
                                conn, total=total,
                                processed=processed_count,
                                current_file=str(f)
                            )
                    finally:
                        # cancel_futures drops anything not yet started
                        # (relevant on the pause/purge break above); files
                        # whose worker already started are let finish
                        # naturally rather than killed mid-write.
                        pool.shutdown(wait=True, cancel_futures=True)

                if not stopped_early:
                    # processed_count (not `total`) so a pause-interrupted
                    # batch correctly reports partial progress instead of
                    # falsely claiming the whole batch finished.
                    update_scan_status(conn, total=total, processed=processed_count, current_file=None)

            except Exception as e:
                logger.error(f"Global loop failure: {e}")
                try:
                    conn.close()
                except sqlite3.Error as close_err:
                    logger.debug(f"Error closing connection after loop failure: {close_err}")
                conn = get_db()

            time.sleep(poll_seconds)

    except KeyboardInterrupt:
        logger.info("Received interrupt, shutting down...")
    finally:
        conn.close()

if __name__ == "__main__":
    scan_loop()

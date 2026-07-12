#!/usr/bin/env python3
"""
Scans the folders listed in config/scan_roots.txt (re-read every cycle,
so folders can be added/removed without a restart), fingerprints new
files with SongRec, cross-checks against AcoustID/MusicBrainz, writes
tags in-place, and records a row in the review queue. Files are never
moved or renamed until approved/rejected via the review UI.
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

SCHEMA = """
CREATE TABLE IF NOT EXISTS queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filepath TEXT UNIQUE NOT NULL,
    artist TEXT,
    album TEXT,
    title TEXT,
    confidence REAL,
    status TEXT DEFAULT 'pending', -- pending|approved|rejected
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS scan_status (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    total INTEGER DEFAULT 0,
    processed INTEGER DEFAULT 0,
    current_file TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


def migrate_schema(conn):
    cols = {row[1] for row in conn.execute("PRAGMA table_info(queue)")}
    if "filesize" not in cols:
        conn.execute("ALTER TABLE queue ADD COLUMN filesize INTEGER")
    if "duration" not in cols:
        conn.execute("ALTER TABLE queue ADD COLUMN duration REAL")
    if "filehash" not in cols:
        conn.execute("ALTER TABLE queue ADD COLUMN filehash TEXT")
    if "sr_artist" not in cols:
        conn.execute("ALTER TABLE queue ADD COLUMN sr_artist TEXT")
    if "sr_title" not in cols:
        conn.execute("ALTER TABLE queue ADD COLUMN sr_title TEXT")
    if "sr_album" not in cols:
        conn.execute("ALTER TABLE queue ADD COLUMN sr_album TEXT")
    if "ac_artist" not in cols:
        conn.execute("ALTER TABLE queue ADD COLUMN ac_artist TEXT")
    if "ac_title" not in cols:
        conn.execute("ALTER TABLE queue ADD COLUMN ac_title TEXT")
    if "ac_score" not in cols:
        conn.execute("ALTER TABLE queue ADD COLUMN ac_score REAL")
    if "agreement" not in cols:
        conn.execute("ALTER TABLE queue ADD COLUMN agreement REAL")
    if "error" not in cols:
        conn.execute("ALTER TABLE queue ADD COLUMN error TEXT")
    conn.commit()


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    migrate_schema(conn)
    return conn


def load_scan_roots():
    """Re-read scan_roots.txt on every call, so adding/removing a folder
    to scan takes effect on the next cycle without a restart."""
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
    files = []
    for root in load_scan_roots():
        files.extend(
            f for f in root.rglob("*")
            if f.is_file() and f.suffix.lower() in SUPPORTED_EXT
        )
    return files


def already_queued(conn, filepath):
    row = conn.execute(
        "SELECT id, error FROM queue WHERE filepath = ?", (str(filepath),)
    ).fetchone()
    if row is None:
        return False
    if row["error"]:
        return False
    return True


def update_scan_status(conn, total, processed, current_file):
    conn.execute(
        "INSERT INTO scan_status (id, total, processed, current_file, updated_at) "
        "VALUES (1, ?, ?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(id) DO UPDATE SET "
        "total=excluded.total, processed=excluded.processed, "
        "current_file=excluded.current_file, updated_at=CURRENT_TIMESTAMP",
        (total, processed, current_file)
    )
    conn.commit()


def songrec_identify(filepath):
    try:
        result = subprocess.run(
            ["songrec", "audio-file-to-recognized-song", str(filepath)],
            capture_output=True, text=True, timeout=60
        )
        data = json.loads(result.stdout)
        track = data.get("track", {})

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
    """Checks Apple's free iTunes Search API for a plausible match. This
    is NOT independent audio fingerprinting - it's a text search that
    confirms whether a given artist/title actually corresponds to a real,
    cataloged release. Free, no API key, no signup, no billing risk.
    Apple's informal guidance caps this around 20 requests/minute, which
    is not a concern since this only fires on the disagreement subset."""
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
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        _whisper_model = WhisperModel(WHISPER_MODEL_SIZE, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE_TYPE)
    return _whisper_model


def get_sample_windows(duration, window=20, count=3):
    """Picks up to `count` short windows spread proportionally across the
    track - a fixed 'first 60 seconds' guess misses punchlines or choruses
    that land later, and breaks entirely on short clips. Falls back to a
    single window covering whatever's available if duration is unknown
    or shorter than the window itself."""
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
            segments, _ = get_whisper_model().transcribe(tmp.name)
            return " ".join(seg.text for seg in segments).strip()
    except Exception as e:
        print(f"[whisper] transcription failed for {filepath} at {offset}s: {e}", file=sys.stderr)
        return ""


def transcribe_track(filepath, duration, window=20, max_windows=3, min_words=15):
    """Samples up to max_windows clips across the track, stopping early
    once enough words have been transcribed to search meaningfully -
    avoids paying for all three windows when the first one already
    landed on a clear chorus or spoken passage."""
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
        resp = requests.get(
            "https://api.genius.com/search",
            params={"q": snippet[:200]},
            headers={"Authorization": f"Bearer {GENIUS_ACCESS_TOKEN}"},
            timeout=15,
        )
        hits = resp.json().get("response", {}).get("hits", [])
        if not hits:
            return None, None
        result = hits[0]["result"]
        return result.get("primary_artist", {}).get("name"), result.get("title")
    except Exception as e:
        print(f"[genius] search failed: {e}", file=sys.stderr)
        return None, None


def lyrics_identify(filepath, duration):
    text = transcribe_track(filepath, duration)
    if not text or len(text.split()) < 6:
        return None, None
    return genius_lyrics_search(text)


def write_tags(filepath, artist, title, album=None):
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
    """candidates: list of (source_name, artist, title). Finds the pair of
    independent sources that agree most strongly with each other - a lone
    outlier among three sources gets ignored rather than dragging the
    result down, unlike simple two-source averaging."""
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

    sr_artist, sr_title, sr_album = songrec_identify(filepath)
    ac_artist, ac_title, score = acoustid_lookup(filepath)

    acoustid_confidence = round(score * 100, 1) if score else 0.0

    two_source_candidates = [
        c for c in [
            ("songrec", sr_artist, sr_title),
            ("acoustid", ac_artist, ac_title),
        ] if c[1] or c[2]
    ]
    two_match = best_pair_match(two_source_candidates)

    if two_match:
        # SongRec and AcoustID already agree - trust it.
        agree_score, (_, a1, t1), (_, a2, t2) = two_match
        artist = a1 or a2
        title = t1 or t2
        confidence = max(75.0, acoustid_confidence)
        agreement = agree_score
    elif not sr_artist and not ac_artist:
        # Both primary sources found genuinely nothing - last resort:
        # transcribe the track locally (free, no API cost) and search
        # the transcript against Genius. Only fires on true dead ends,
        # not disagreements, since transcription is the most expensive
        # step in the whole pipeline.
        lyric_artist, lyric_title = lyrics_identify(filepath, duration)
        agreement = None
        if lyric_artist or lyric_title:
            artist, title = lyric_artist, lyric_title
            confidence = 50.0  # single unverified source, still flagged for review
        else:
            artist, title = None, None
            confidence = 0.0
    else:
        # They disagree (both returned something, but different). Check
        # whether either candidate corresponds to a real cataloged
        # release via Apple's free iTunes Search API before giving up.
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
            artist = sr_artist or ac_artist
            title = sr_title or ac_title
            if sr_artist and ac_artist:
                confidence = min(acoustid_confidence, 40.0)
            elif acoustid_confidence:
                confidence = acoustid_confidence
            elif sr_artist:
                confidence = 60.0
            else:
                confidence = 0.0

    album = sr_album

    if artist or title or album:
        write_tags(filepath, artist, title, album)

    filehash = compute_filehash(filepath)

    conn.execute(
        "INSERT INTO queue "
        "(filepath, artist, title, album, confidence, filesize, duration, filehash, "
        " sr_artist, sr_title, sr_album, ac_artist, ac_title, ac_score, agreement, error, status) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, NULL, 'pending') "
        "ON CONFLICT(filepath) DO UPDATE SET "
        "artist=excluded.artist, title=excluded.title, album=excluded.album, "
        "confidence=excluded.confidence, filesize=excluded.filesize, duration=excluded.duration, "
        "filehash=excluded.filehash, sr_artist=excluded.sr_artist, sr_title=excluded.sr_title, "
        "sr_album=excluded.sr_album, ac_artist=excluded.ac_artist, ac_title=excluded.ac_title, "
        "ac_score=excluded.ac_score, agreement=excluded.agreement, error=NULL, status='pending'",
        (str(filepath), artist, title, album, confidence, filesize, duration, filehash,
         sr_artist, sr_title, sr_album, ac_artist, ac_title, score, agreement)
    )
    conn.commit()
    print(f"[queued] {filepath.name} -> {artist} / {title} - {album or '?'} ({confidence}%)")


def scan_loop(poll_seconds=15):
    conn = get_db()
    while True:
        try:
            all_files = discover_files()
            already_done = [f for f in all_files if already_queued(conn, f)]
            pending_files = [f for f in all_files if f not in already_done]
            total = len(all_files)

            update_scan_status(conn, total=total, processed=len(already_done), current_file=None)

            for i, f in enumerate(pending_files):
                update_scan_status(
                    conn, total=total,
                    processed=len(already_done) + i,
                    current_file=str(f)
                )
                try:
                    process_file(conn, f)
                except Exception as e:
                    print(f"[scan_loop] failed to process file {f}: {e}", file=sys.stderr)
                    try:
                        conn.execute(
                            "INSERT INTO queue (filepath, confidence, error, status) VALUES (?, 0.0, ?, 'pending') "
                            "ON CONFLICT(filepath) DO UPDATE SET error=excluded.error, status='pending'",
                            (str(f), str(e))
                        )
                        conn.commit()
                    except Exception as db_err:
                        print(f"[scan_loop] failed logging error: {db_err}", file=sys.stderr)

            update_scan_status(conn, total=total, processed=total, current_file=None)
        except Exception as e:
            print(f"[scan_loop] global loop failure: {e}", file=sys.stderr)
        time.sleep(poll_seconds)


if __name__ == "__main__":
    scan_loop()

#!/usr/bin/env python3
import hashlib
import re
import shutil
import sqlite3
from pathlib import Path

import mutagen
from flask import Flask, abort, jsonify, render_template, request, send_file

APP_ROOT = Path("/opt/music-intake")
SCAN_ROOTS_FILE = APP_ROOT / "config" / "scan_roots.txt"
DB_PATH = APP_ROOT / "db" / "queue.sqlite3"

NAS_INTAKE = Path("/mnt/nas-intake")
APPROVED = NAS_INTAKE / "approved"
REJECTED = NAS_INTAKE / "rejected"

app = Flask(__name__)


def load_scan_roots():
    """Mirrors recognize.py's loader - used here only to validate that
    audio-streaming requests stay within known, expected directories."""
    if not SCAN_ROOTS_FILE.is_file():
        return []
    roots = []
    for line in SCAN_ROOTS_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        p = Path(line)
        if p.is_dir():
            roots.append(p.resolve())
    return roots


def allowed_roots():
    return load_scan_roots() + [APPROVED.resolve(), REJECTED.resolve()]


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
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS scan_status ("
        "id INTEGER PRIMARY KEY CHECK (id = 1), "
        "total INTEGER DEFAULT 0, processed INTEGER DEFAULT 0, "
        "current_file TEXT, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);"
    )
    migrate_schema(conn)
    return conn


def sanitize_filename(name):
    if not name:
        return ""
    sanitized = re.sub(r'[\/\\\x00]', '_', str(name).strip())
    return sanitized or "_"


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


def ensure_metadata(conn, row):
    if row["error"]:
        return row

    filepath = Path(row["filepath"])
    if not filepath.is_file():
        return row

    updates = {}
    if row["filesize"] is None:
        updates["filesize"] = filepath.stat().st_size
    if row["duration"] is None:
        updates["duration"] = probe_duration(filepath)
    if row["filehash"] is None:
        updates["filehash"] = compute_filehash(filepath)

    if updates:
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(
            f"UPDATE queue SET {set_clause} WHERE id = ?",
            (*updates.values(), row["id"])
        )
        conn.commit()
        row = conn.execute("SELECT * FROM queue WHERE id = ?", (row["id"],)).fetchone()

    return row


def human_size(num_bytes):
    if num_bytes is None:
        return "?"
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024


def human_duration(seconds):
    if seconds is None:
        return "?"
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    return f"{m}:{s:02d}"


def relative_source(filepath_str):
    """Shows which configured scan root a file came from, e.g.
    'NOK/_a_trier/song.mp3' instead of the full /mnt/nas-source/... path."""
    p = Path(filepath_str)
    for root in load_scan_roots():
        try:
            return str(p.relative_to(root.parent))
        except ValueError:
            continue
    return filepath_str


@app.route("/")
def index():
    conn = get_db()
    raw_rows = conn.execute(
        "SELECT * FROM queue WHERE status = 'pending' ORDER BY confidence ASC"
    ).fetchall()

    rows = [ensure_metadata(conn, r) for r in raw_rows]

    enriched = []
    for r in rows:
        d = dict(r)
        d["size_human"] = human_size(r["filesize"])
        d["duration_human"] = human_duration(r["duration"])
        d["ac_score_human"] = f"{r['ac_score'] * 100:.0f}%" if r["ac_score"] is not None else "-"
        d["error"] = r["error"]
        d["relative_path"] = relative_source(r["filepath"])

        if r["agreement"] is None:
            d["agreement_human"] = "-"
        else:
            d["agreement_human"] = f"{r['agreement'] * 100:.0f}%"
        enriched.append(d)

    # Group exact-duplicate files (same content hash) under one primary row
    grouped_rows = []
    hash_groups = {}
    for r in enriched:
        h = r["filehash"]
        r["duplicates"] = []
        if h:
            if h not in hash_groups:
                hash_groups[h] = r
                grouped_rows.append(r)
            else:
                hash_groups[h]["duplicates"].append(r)
        else:
            grouped_rows.append(r)

    return render_template(
        "index.html",
        rows=grouped_rows,
        total_count=len(enriched),
        scan_roots=[str(p) for p in load_scan_roots()],
    )


@app.route("/api/scan-status")
def scan_status():
    conn = get_db()
    row = conn.execute("SELECT * FROM scan_status WHERE id = 1").fetchone()
    if not row or not row["total"]:
        return jsonify({"total": 0, "processed": 0, "percent": 100, "current_file": None, "scanning": False})

    total = row["total"]
    processed = row["processed"] or 0
    percent = int((processed / total) * 100) if total else 100
    return jsonify({
        "total": total,
        "processed": processed,
        "percent": percent,
        "current_file": row["current_file"],
        "scanning": processed < total
    })


@app.route("/api/scan-roots")
def scan_roots_status():
    """Read-only visibility into what's currently configured - editing
    happens by hand in config/scan_roots.txt, not through the UI."""
    return jsonify({"roots": [str(p) for p in load_scan_roots()]})


@app.route("/api/audio/<int:item_id>")
def audio(item_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM queue WHERE id = ?", (item_id,)).fetchone()
    if not row or row["error"]:
        abort(404)

    src = Path(row["filepath"]).resolve()
    roots = allowed_roots()
    if not any(root in src.parents or root == src for root in roots):
        abort(403)
    if not src.is_file():
        abort(404)

    return send_file(src, conditional=True)


@app.route("/api/approve/<int:item_id>", methods=["POST"])
def approve(item_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM queue WHERE id = ?", (item_id,)).fetchone()
    if not row or row["error"]:
        return jsonify({"error": "Cannot approve unreadable files"}), 400

    artist = sanitize_filename(row["artist"]) or "Unknown Artist"
    album = sanitize_filename(row["album"]) or "Unknown Album"
    title = sanitize_filename(row["title"]) or "Unknown Title"

    src = Path(row["filepath"])
    extension = src.suffix

    dest_dir = APPROVED / artist / album
    dest_file = dest_dir / f"{title}{extension}"

    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest_file))
    except Exception as e:
        return jsonify({"error": f"File system move failed: {str(e)}"}), 500

    conn.execute("UPDATE queue SET status = 'approved' WHERE id = ?", (item_id,))
    conn.commit()
    return jsonify({"status": "approved"})


@app.route("/api/reject/<int:item_id>", methods=["POST"])
def reject(item_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM queue WHERE id = ?", (item_id,)).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404

    src = Path(row["filepath"])
    dest = REJECTED / src.name
    try:
        shutil.move(str(src), str(dest))
    except Exception as e:
        return jsonify({"error": f"Failed to move file to rejected: {str(e)}"}), 500

    conn.execute("UPDATE queue SET status = 'rejected' WHERE id = ?", (item_id,))
    conn.commit()
    return jsonify({"status": "rejected"})


@app.route("/api/delete/<int:item_id>", methods=["POST"])
def delete_item(item_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM queue WHERE id = ?", (item_id,)).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404

    src = Path(row["filepath"])
    try:
        if src.exists():
            src.unlink()
    except Exception as e:
        return jsonify({"error": f"Failed to permanently delete file: {str(e)}"}), 500

    conn.execute("DELETE FROM queue WHERE id = ?", (item_id,))
    conn.commit()
    return jsonify({"status": "deleted"})


@app.route("/api/purge", methods=["POST"])
def purge_queue():
    conn = get_db()
    try:
        conn.execute("DELETE FROM queue WHERE status = 'pending'")
        conn.execute("UPDATE scan_status SET total = 0, processed = 0, current_file = NULL WHERE id = 1")
        conn.commit()
        return jsonify({"status": "purged"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/edit/<int:item_id>", methods=["POST"])
def edit(item_id):
    data = request.get_json()
    conn = get_db()
    conn.execute(
        "UPDATE queue SET artist = ?, title = ?, album = ? WHERE id = ?",
        (data.get("artist"), data.get("title"), data.get("album"), item_id)
    )
    conn.commit()
    return jsonify({"status": "updated"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

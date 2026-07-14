#!/usr/bin/env python3
import logging
import math
import os
import re
import shutil
import sqlite3
from logging.handlers import RotatingFileHandler
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_file

# --- Setup ---
APP_ROOT = Path("/opt/music-intake")
SCAN_ROOTS_FILE = APP_ROOT / "config" / "scan_roots.txt"
DB_PATH = Path(os.environ.get("MUSIC_DB_PATH", APP_ROOT / "db" / "queue.sqlite3"))
NAS_INTAKE = Path("/mnt/nas-intake")
APPROVED = NAS_INTAKE / "approved"
REJECTED = NAS_INTAKE / "rejected"

app = Flask(__name__)

# --- Logging ---
def setup_logging(name):
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    log_dir = Path("/opt/music-intake/logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_dir / f"{name}.log",
        maxBytes=10*1024*1024,
        backupCount=5
    )
    handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
    logger.addHandler(handler)
    return logger

logger = setup_logging('web')

# --- Helpers ---
def load_scan_roots():
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

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn

def sanitize_filename(name):
    if not name:
        return ""
    sanitized = re.sub(r'[\/\\\x00]', '_', str(name).strip())
    if sanitized in (".", "..") or sanitized.startswith(".."):
        return "_"
    return sanitized

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
    p = Path(filepath_str)
    for root in load_scan_roots():
        try:
            return str(p.relative_to(root))
        except ValueError:
            continue
    return filepath_str

# --- Approve/Reject/Rescan Helpers ---
def _approve_one(conn, item_id):
    row = conn.execute("SELECT * FROM queue WHERE id = ?", (item_id,)).fetchone()
    if not row:
        return False, "not found"
    if row["error"]:
        return False, "Cannot approve unreadable files"
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
        return False, f"File system move failed: {str(e)}"
    conn.execute("UPDATE queue SET status = 'approved' WHERE id = ?", (item_id,))
    conn.commit()
    return True, None

def _reject_one(conn, item_id):
    row = conn.execute("SELECT * FROM queue WHERE id = ?", (item_id,)).fetchone()
    if not row:
        return False, "not found"
    src = Path(row["filepath"])
    dest = REJECTED / src.name
    try:
        shutil.move(str(src), str(dest))
    except Exception as e:
        return False, f"Failed to move file to rejected: {str(e)}"
    conn.execute("UPDATE queue SET status = 'rejected' WHERE id = ?", (item_id,))
    conn.commit()
    return True, None

def _rescan_one(conn, item_id):
    row = conn.execute("SELECT * FROM queue WHERE id = ?", (item_id,)).fetchone()
    if not row:
        return False, "not found"
    conn.execute("DELETE FROM queue WHERE id = ?", (item_id,))
    conn.commit()
    return True, None

def _delete_one(conn, item_id):
    """Delete a single item from disk and database."""
    row = conn.execute("SELECT * FROM queue WHERE id = ?", (item_id,)).fetchone()
    if not row:
        return False, "not found"
    try:
        src = Path(row["filepath"])
        if src.exists():
            src.unlink()
    except Exception as e:
        return False, f"Failed to delete file: {str(e)}"
    conn.execute("DELETE FROM queue WHERE id = ?", (item_id,))
    conn.commit()
    return True, None

def _run_batch(conn, ids, fn):
    results = {}
    for item_id in ids:
        try:
            ok, err = fn(conn, int(item_id))
        except (TypeError, ValueError):
            ok, err = False, "invalid id"
        results[item_id] = {"ok": ok, "error": err}
    return results

# --- Routes ---

@app.route("/")
def index():
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 50, type=int)
    search_query = request.args.get("q", "").strip()
    show_unrecognized = request.args.get("unrecognized", "0") == "1"
    show_duplicates = request.args.get("duplicates", "0") == "1"

    sort_by = request.args.get("sort", "confidence")
    order = request.args.get("order", "asc")

    if page < 1:
        page = 1

    conn = get_db()
    query_conditions = ["status = 'pending'"]
    query_params = []

    if not show_unrecognized:
        query_conditions.append("confidence > 0")

    if show_duplicates:
        query_conditions.append("filehash IN (SELECT filehash FROM queue WHERE status = 'pending' AND filehash IS NOT NULL AND filehash != '' GROUP BY filehash HAVING COUNT(*) > 1)")

    if search_query:
        query_conditions.append(
            "(filepath LIKE ? OR artist LIKE ? OR title LIKE ? OR album LIKE ?)"
        )
        like_param = f"%{search_query}%"
        query_params.extend([like_param, like_param, like_param, like_param])

    where_clause = " WHERE " + " AND ".join(query_conditions)

    sort_map = {
        "source": "filepath",
        "songrec": "sr_artist",
        "acoustid": "ac_artist",
        "genius": "gn_artist",
        "title": "title",
        "artist": "artist",
        "album": "album",
        "size": "filesize",
        "length": "duration",
        "confidence": "confidence"
    }
    db_column = sort_map.get(sort_by, "confidence")
    db_order = "ASC" if order.lower() == "asc" else "DESC"

    total_count = conn.execute(
        f"SELECT COUNT(*) FROM queue {where_clause}", query_params
    ).fetchone()[0]

    total_pages = max(1, math.ceil(total_count / per_page))
    if page > total_pages:
        page = total_pages
    offset = (page - 1) * per_page

    final_query = f"""
        SELECT * FROM queue
        {where_clause}
        ORDER BY {db_column} {db_order}
        LIMIT ? OFFSET ?
    """
    raw_rows = conn.execute(final_query, query_params + [per_page, offset]).fetchall()

    # Get set of global duplicate filehashes in pending queue to tag row-duplicate classes correctly
    dup_hashes = set()
    dup_rows = conn.execute(
        "SELECT filehash FROM queue WHERE status = 'pending' AND filehash IS NOT NULL AND filehash != '' GROUP BY filehash HAVING COUNT(*) > 1"
    ).fetchall()
    for dr in dup_rows:
        if dr["filehash"]:
            dup_hashes.add(dr["filehash"])

    # Convert all rows to plain dicts and add human-readable fields
    enriched = []
    for r in raw_rows:
        d = dict(r)
        d["size_human"] = human_size(r["filesize"])
        d["duration_human"] = human_duration(r["duration"])
        d["ac_score_human"] = f"{r['ac_score'] * 100:.0f}%" if r["ac_score"] is not None else "-"
        d["error"] = r["error"]
        d["relative_path"] = relative_source(r["filepath"])
        d["agreement_human"] = f"{r['agreement'] * 100:.0f}%" if r["agreement"] is not None else "-"
        d["is_duplicate"] = (r["filehash"] in dup_hashes) if r["filehash"] else False
        enriched.append(d)

    # Group by filehash - convert duplicates to simplified dicts for JSON serialization
    grouped_rows = []
    hash_groups = {}
    for r in enriched:
        h = r.get("filehash")
        r["duplicates"] = []
        if h:
            if h not in hash_groups:
                hash_groups[h] = r
                grouped_rows.append(r)
            else:
                # Create simplified duplicate entry with only essential fields
                dup_dict = {
                    "id": r["id"],
                    "filepath": r["filepath"],
                    "filehash": r["filehash"],
                    "size_human": r.get("size_human", ""),
                    "duration_human": r.get("duration_human", ""),
                    "filesize": r.get("filesize"),
                    "duration": r.get("duration"),
                    "artist": r.get("artist", ""),
                    "title": r.get("title", ""),
                    "album": r.get("album", ""),
                    "confidence": r.get("confidence", 0)
                }
                hash_groups[h]["duplicates"].append(dup_dict)
        else:
            grouped_rows.append(r)

    conn.close()

    return render_template(
        "index.html",
        rows=grouped_rows,
        total_count=total_count,
        page=page,
        per_page=per_page,
        total_pages=total_pages,
        search_query=search_query,
        show_unrecognized=show_unrecognized,
        show_duplicates=show_duplicates,
        sort_by=sort_by,
        order=order,
    )

@app.route("/history")
def history():
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 50, type=int)
    status_filter = request.args.get("status", "all")
    search_query = request.args.get("q", "").strip()

    conn = get_db()
    query_conditions = []
    query_params = []

    if status_filter != "all":
        query_conditions.append("status = ?")
        query_params.append(status_filter)

    if search_query:
        query_conditions.append(
            "(filepath LIKE ? OR artist LIKE ? OR title LIKE ? OR album LIKE ?)"
        )
        like_param = f"%{search_query}%"
        query_params.extend([like_param, like_param, like_param, like_param])

    where_clause = " WHERE " + " AND ".join(query_conditions) if query_conditions else ""

    total_count = conn.execute(
        f"SELECT COUNT(*) FROM queue {where_clause}", query_params
    ).fetchone()[0]

    total_pages = max(1, math.ceil(total_count / per_page))
    if page > total_pages:
        page = total_pages
    offset = (page - 1) * per_page

    final_query = f"""
        SELECT * FROM queue
        {where_clause}
        ORDER BY created_at DESC
        LIMIT ? OFFSET ?
    """
    raw_rows = conn.execute(final_query, query_params + [per_page, offset]).fetchall()

    enriched = []
    for r in raw_rows:
        d = dict(r)
        d["size_human"] = human_size(r["filesize"])
        d["duration_human"] = human_duration(r["duration"])
        d["ac_score_human"] = f"{r['ac_score'] * 100:.0f}%" if r["ac_score"] is not None else "-"
        d["error"] = r["error"]
        d["relative_path"] = relative_source(r["filepath"])
        d["status_class"] = {
            'approved': 'bg-emerald-50 text-emerald-700 border-emerald-200',
            'rejected': 'bg-rose-50 text-rose-700 border-rose-200',
            'pending': 'bg-amber-50 text-amber-700 border-amber-200'
        }.get(r["status"], 'bg-gray-50 text-gray-700 border-gray-200')
        enriched.append(d)

    conn.close()

    return render_template(
        "history.html",
        rows=enriched,
        total_count=total_count,
        page=page,
        per_page=per_page,
        total_pages=total_pages,
        search_query=search_query,
        status_filter=status_filter,
    )

@app.route("/logs")
def view_logs():
    log_files = {
        'recognize': '/opt/music-intake/logs/recognize.log',
        'web': '/opt/music-intake/logs/web.log',
        'beets': '/opt/music-intake/logs/beets-import.log'
    }

    logs = {}
    for name, path in log_files.items():
        try:
            with open(path, 'r') as f:
                logs[name] = {
                    'content': f.read().splitlines()[-500:],
                    'path': path
                }
        except FileNotFoundError:
            logs[name] = {'content': [], 'path': path, 'error': 'File not found'}

    return render_template("logs.html", logs=logs)

@app.route("/api/scan-status")
def scan_status():
    conn = get_db()
    row = conn.execute("SELECT * FROM scan_status WHERE id = 1").fetchone()
    conn.close()
    if not row or not row["total"]:
        return jsonify({
            "total": 0, "processed": 0, "percent": 100,
            "current_file": None, "scanning": False, "is_paused": False,
            "start_time": None
        })
    total = row["total"]
    processed = row["processed"] or 0
    is_paused = bool(row["is_paused"]) if "is_paused" in row.keys() else False
    return jsonify({
        "total": total,
        "processed": processed,
        "percent": int((processed / total) * 100) if total else 100,
        "current_file": row["current_file"],
        "scanning": processed < total,
        "is_paused": is_paused,
        "start_time": row["updated_at"] if row else None
    })

@app.route("/api/scan/pause", methods=["POST"])
def pause_scan():
    conn = get_db()
    conn.execute("UPDATE scan_status SET is_paused = 1 WHERE id = 1")
    conn.commit()
    conn.close()
    return jsonify({"status": "paused"})

@app.route("/api/scan/resume", methods=["POST"])
def resume_scan():
    conn = get_db()
    conn.execute("UPDATE scan_status SET is_paused = 0 WHERE id = 1")
    conn.commit()
    conn.close()
    return jsonify({"status": "resumed"})

@app.route("/api/audio/<int:item_id>")
def audio(item_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM queue WHERE id = ?", (item_id,)).fetchone()
    conn.close()
    if not row or row["error"]:
        abort(404)
    src = Path(row["filepath"]).resolve()
    if not any(root in src.parents or root == src for root in allowed_roots()):
        abort(403)
    if not src.is_file():
        abort(404)
    return send_file(src, conditional=True)

@app.route("/api/approve/<int:item_id>", methods=["POST"])
def approve(item_id):
    conn = get_db()
    ok, err = _approve_one(conn, item_id)
    conn.close()
    return jsonify({"status": "approved"}) if ok else (jsonify({"error": err}), 400)

@app.route("/api/reject/<int:item_id>", methods=["POST"])
def reject(item_id):
    conn = get_db()
    ok, err = _reject_one(conn, item_id)
    conn.close()
    return jsonify({"status": "rejected"}) if ok else (jsonify({"error": err}), 404)

@app.route("/api/rescan/<int:item_id>", methods=["POST"])
def rescan(item_id):
    conn = get_db()
    ok, err = _rescan_one(conn, item_id)
    conn.close()
    return jsonify({"status": "rescan_queued"}) if ok else (jsonify({"error": err}), 404)

@app.route("/api/delete/<int:item_id>", methods=["POST"])
def delete_item(item_id):
    conn = get_db()
    ok, err = _delete_one(conn, item_id)
    conn.close()
    if not ok:
        return jsonify({"error": err}), 404 if err == "not found" else 500
    return jsonify({"status": "deleted"})

@app.route("/api/delete-batch", methods=["POST"])
def delete_batch():
    """Delete multiple items at once."""
    ids = (request.get_json() or {}).get("ids", [])
    if not ids:
        return jsonify({"error": "No IDs provided"}), 400
    conn = get_db()
    results = _run_batch(conn, ids, _delete_one)
    conn.close()
    failed = {k: v for k, v in results.items() if not v["ok"]}
    if failed:
        return jsonify({"results": results, "failed": failed}), 207
    return jsonify({"results": results})

@app.route("/api/duplicates/<int:item_id>")
def get_duplicates(item_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM queue WHERE id = ?", (item_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "not found"}), 404

    filehash = row["filehash"]
    rows = conn.execute(
        "SELECT * FROM queue WHERE filehash = ? ORDER BY id",
        (filehash,)
    ).fetchall()

    enriched = []
    for r in rows:
        d = dict(r)
        d["size_human"] = human_size(r["filesize"])
        d["duration_human"] = human_duration(r["duration"])
        d["ac_score_human"] = f"{r['ac_score'] * 100:.0f}%" if r["ac_score"] is not None else "-"
        d["relative_path"] = relative_source(r["filepath"])
        enriched.append(d)

    conn.close()
    return jsonify({"main": enriched[0], "duplicates": enriched[1:]})

@app.route("/api/fuzzy-duplicates/<int:item_id>")
def get_fuzzy_duplicates(item_id):
    """
    Get tracks with similar size and duration (fuzzy duplicate detection).
    Useful for finding duplicates that have different hashes but similar characteristics.
    """
    conn = get_db()
    row = conn.execute("SELECT * FROM queue WHERE id = ?", (item_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "not found"}), 404

    # Get the main track's size and duration
    size = row["filesize"]
    duration = row["duration"]

    if size is None or duration is None:
        conn.close()
        return jsonify({"error": "Cannot find fuzzy duplicates for tracks without size or duration"}), 400

    # Find tracks with similar size (+/- 1KB) and duration (+/- 0.5 seconds)
    # Exclude the main track itself
    fuzzy_dupes = conn.execute("""
        SELECT * FROM queue 
        WHERE id != ? 
        AND filesize IS NOT NULL 
        AND duration IS NOT NULL
        AND ABS(filesize - ?) <= 1024 
        AND ABS(duration - ?) <= 0.5
        ORDER BY ABS(filesize - ?), ABS(duration - ?)
        LIMIT 20
    """, (item_id, size, duration, size, duration)).fetchall()

    enriched = []
    for r in fuzzy_dupes:
        d = dict(r)
        d["size_human"] = human_size(r["filesize"])
        d["duration_human"] = human_duration(r["duration"])
        d["ac_score_human"] = f"{r['ac_score'] * 100:.0f}%" if r["ac_score"] is not None else "-"
        d["relative_path"] = relative_source(r["filepath"])
        d["size_diff"] = r["filesize"] - size
        d["duration_diff"] = r["duration"] - duration
        enriched.append(d)

    conn.close()
    return jsonify({"main": dict(row), "fuzzy_duplicates": enriched})

@app.route("/api/approve-batch", methods=["POST"])
def approve_batch():
    payload = request.get_json() or {}
    ids = payload.get("ids", [])
    edits = payload.get("edits", {})
    conn = get_db()
    try:
        for item_id, fields in edits.items():
            conn.execute(
                "UPDATE queue SET artist = ?, title = ?, album = ? WHERE id = ?",
                (fields.get("artist"), fields.get("title"), fields.get("album"), int(item_id))
            )
        conn.commit()
        results = _run_batch(conn, ids, _approve_one)
        return jsonify({"results": results})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route("/api/reject-batch", methods=["POST"])
def reject_batch():
    ids = (request.get_json() or {}).get("ids", [])
    conn = get_db()
    results = _run_batch(conn, ids, _reject_one)
    conn.close()
    return jsonify({"results": results})

@app.route("/api/rescan-batch", methods=["POST"])
def rescan_batch():
    ids = (request.get_json() or {}).get("ids", [])
    conn = get_db()
    results = _run_batch(conn, ids, _rescan_one)
    conn.close()
    return jsonify({"results": results})

@app.route("/api/purge", methods=["POST"])
def purge_queue():
    conn = get_db()
    try:
        conn.execute("DELETE FROM queue WHERE status = 'pending'")
        conn.execute("UPDATE scan_status SET total = 0, processed = 0, current_file = NULL, is_paused = 0 WHERE id = 1")
        conn.commit()
        return jsonify({"status": "purged"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route("/api/edit/<int:item_id>", methods=["POST"])
def edit(item_id):
    data = request.get_json() or {}
    conn = get_db()
    conn.execute(
        "UPDATE queue SET artist = ?, title = ?, album = ? WHERE id = ?",
        (data.get("artist"), data.get("title"), data.get("album"), item_id)
    )
    conn.commit()
    conn.close()
    return jsonify({"status": "updated"})

@app.route("/api/clear-logs", methods=["POST"])
def clear_all_logs():
    """Clear all log files."""
    log_dir = Path("/opt/music-intake/logs")
    log_files = ['recognize.log', 'web.log', 'beets-import.log']
    
    for log_file in log_files:
        log_path = log_dir / log_file
        try:
            if log_path.exists():
                log_path.write_text('')
        except Exception as e:
            logger.error(f"Failed to clear {log_file}: {e}")
    
    return jsonify({"status": "cleared"})

@app.route("/api/clear-log/<name>", methods=["POST"])
def clear_log(name):
    """Clear a specific log file."""
    log_dir = Path("/opt/music-intake/logs")
    log_path = log_dir / f"{name}.log"
    
    try:
        if log_path.exists():
            log_path.write_text('')
        return jsonify({"status": "cleared"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/download-log/<name>")
def download_log(name):
    """Download a log file."""
    log_dir = Path("/opt/music-intake/logs")
    log_path = log_dir / f"{name}.log"
    
    try:
        if log_path.exists():
            return send_file(log_path, as_attachment=True, download_name=f"{name}.log")
        return jsonify({"error": "Log file not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

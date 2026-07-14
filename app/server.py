#!/usr/bin/env python3
import math
import os
import re
import shutil
import sqlite3
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_file

APP_ROOT = Path("/opt/music-intake")
SCAN_ROOTS_FILE = APP_ROOT / "config" / "scan_roots.txt"
DB_PATH = Path(os.environ.get("MUSIC_DB_PATH", APP_ROOT / "db" / "queue.sqlite3"))

NAS_INTAKE = Path("/mnt/nas-intake")
APPROVED = NAS_INTAKE / "approved"
REJECTED = NAS_INTAKE / "rejected"

app = Flask(__name__)

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

def _run_batch(conn, ids, fn):
    results = {}
    for item_id in ids:
        try:
            ok, err = fn(conn, int(item_id))
        except (TypeError, ValueError):
            ok, err = False, "invalid id"
        results[item_id] = {"ok": ok, "error": err}
    return results

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

    # Server-side duplicates filtering
    if show_duplicates:
        query_conditions.append("filehash IN (SELECT filehash FROM queue WHERE status = 'pending' AND filehash IS NOT NULL GROUP BY filehash HAVING COUNT(*) > 1)")

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

    enriched = []
    for r in raw_rows:
        d = dict(r)
        d["size_human"] = human_size(r["filesize"])
        d["duration_human"] = human_duration(r["duration"])
        d["ac_score_human"] = f"{r['ac_score'] * 100:.0f}%" if r["ac_score"] is not None else "-"
        d["error"] = r["error"]
        d["relative_path"] = relative_source(r["filepath"])
        d["agreement_human"] = f"{r['agreement'] * 100:.0f}%" if r["agreement"] is not None else "-"
        enriched.append(d)

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
        scan_roots=[str(p) for p in load_scan_roots()],
    )

@app.route("/api/scan-status")
def scan_status():
    conn = get_db()
    row = conn.execute("SELECT * FROM scan_status WHERE id = 1").fetchone()
    conn.close()
    if not row or not row["total"]:
        return jsonify({
            "total": 0, "processed": 0, "percent": 100,
            "current_file": None, "scanning": False, "is_paused": False
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
        "is_paused": is_paused
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

@app.route("/api/delete/<int:item_id>", methods=["POST"])
def delete_item(item_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM queue WHERE id = ?", (item_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "not found"}), 404
    try:
        src = Path(row["filepath"])
        if src.exists():
            src.unlink()
    except Exception as e:
        conn.close()
        return jsonify({"error": str(e)}), 500
    conn.execute("DELETE FROM queue WHERE id = ?", (item_id,))
    conn.commit()
    conn.close()
    return jsonify({"status": "deleted"})

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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

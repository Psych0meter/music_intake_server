#!/usr/bin/env python3
"""
Applies pending database migrations from migrations/*.sql, in filename
order, tracked via a schema_version table so each file is applied
exactly once per database, ever. This deliberately runs as a standalone
step - NOT auto-triggered inside server.py/recognize.py on every
connection - so schema changes are an explicit, auditable action rather
than something that silently happens the first time any process
happens to connect.

Usage:
    python3 migrate.py              # apply any pending migrations
    python3 migrate.py --status     # show applied/pending, change nothing
    python3 migrate.py --dry-run    # show what WOULD be applied, without applying it

Run manually after deploying new code, or automatically via the
music-migrate.service systemd unit (see docs/CONFIGURATION.md) which
both music-recognize.service and music-review-ui.service depend on.
"""
import argparse
import os
import sqlite3
import sys
from pathlib import Path

APP_ROOT = Path("/opt/music-intake")

DB_PATH = Path(os.environ.get("MUSIC_DB_PATH", APP_ROOT / "db" / "queue.sqlite3"))
MIGRATIONS_DIR = APP_ROOT / "migrations"

def get_connection():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        "version TEXT PRIMARY KEY, "
        "applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
    )
    return conn


def discover_migrations():
    return sorted(MIGRATIONS_DIR.glob("*.sql"), key=lambda p: p.name)


def applied_versions(conn):
    return {row[0] for row in conn.execute("SELECT version FROM schema_version")}


def apply_migration(conn, path):
    conn.executescript(path.read_text())
    conn.execute("INSERT INTO schema_version (version) VALUES (?)", (path.stem,))
    conn.commit()


def main():
    parser = argparse.ArgumentParser(description="Apply Music Intake database migrations")
    parser.add_argument("--status", action="store_true", help="Show applied/pending migrations and exit")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be applied, without applying it")
    args = parser.parse_args()

    if not MIGRATIONS_DIR.exists():
        raise FileNotFoundError(
            f"Migration directory not found: {MIGRATIONS_DIR}"
        )

    conn = get_connection()
    applied = applied_versions(conn)
    migrations = discover_migrations()
    pending = [m for m in migrations if m.stem not in applied]

    if args.status:
        for m in migrations:
            status = "applied" if m.stem in applied else "PENDING"
            print(f"  [{status:7s}] {m.name}")
        print(f"\n{len(applied)} applied, {len(pending)} pending")
        return

    if not pending:
        print("Database is up to date - no pending migrations.")
        return

    for m in pending:
        if args.dry_run:
            print(f"[dry-run] would apply: {m.name}")
            continue
        print(f"Applying {m.name} ...", end=" ")
        try:
            apply_migration(conn, m)
            print("OK")
        except Exception as e:
            print("FAILED")
            print(f"  {e}", file=sys.stderr)
            sys.exit(1)

    if not args.dry_run:
        print(f"\nDatabase is now up to date ({len(applied) + len(pending)} migrations applied).")


if __name__ == "__main__":
    main()

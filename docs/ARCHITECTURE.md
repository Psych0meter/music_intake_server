# Architecture

```
scan_roots.txt (any NAS folders)
        │
        ▼
Recognition daemon (recognize.py, systemd: music-recognize.service)
  - SongRec + AcoustID on every file
  - iTunes verification on disagreement
  - Optional Whisper+Genius on total dead ends
  - Tags written in-place, file NOT moved
  - Row inserted into SQLite queue
        │
        ▼
Review UI (server.py, systemd: music-review-ui.service)
  http://<lxc-ip>:5000
        │
   Approve ──────────┐         Reject ──► /mnt/nas-intake/rejected/
        │             │
        ▼             ▼
  /mnt/nas-intake/approved/   (staged, NOT yet in final library)
        │
        ▼
beets import (import_approved.sh, systemd timer: music-import.timer, every 30min)
  - move + rename + tag + artwork + ReplayGain
        │
        ▼
/mnt/nas-intake/library/  (final organized library)
```

## Directory layout inside the LXC

```
/opt/music-intake/          # local disk only, never bind-mounted
├── venv/
├── app/
│   ├── server.py
│   └── templates/index.html
├── pipeline/
│   ├── recognize.py
│   └── import_approved.sh
├── db/
│   └── queue.sqlite3
└── config/
    ├── beets-config.yaml
    ├── secrets.env
    └── scan_roots.txt

/mnt/nas-source/            # bind mount, read side
/mnt/nas-intake/            # bind mount, managed output
    ├── approved/
    ├── rejected/
    └── library/
```

## Database

SQLite, single `queue` table (plus `scan_status` for progress reporting).
Both `recognize.py` and `server.py` run idempotent schema migrations on
every connection, so pulling a newer version of either script never
requires a manual `ALTER TABLE` — new columns are added automatically if
missing.

## Why files aren't moved until approval

Any automated audio identification can be wrong — even a "100%
confidence" fingerprint match can be wrong, since fingerprint databases
are crowd-sourced and short/generic/sample-heavy audio can collide with
the wrong recording. Separating identification from the actual file move
means a bad match costs you one row in a review queue, not a
mis-filed/mis-renamed file buried in your library.

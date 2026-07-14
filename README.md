# Music Intake Server

A human-approved audio identification and import pipeline: point it at
messy, unsorted music folders on your NAS, it fingerprints and tags
everything automatically, and **nothing gets renamed or moved into your
library until you approve it** in a web review UI.

Built for [Proxmox VE](https://www.proxmox.com/en/proxmox-virtual-environment/overview)
as an LXC container, installable the same way as
[community-scripts.org](https://community-scripts.github.io/ProxmoxVE/) apps.

## Install

On your Proxmox host:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/Psych0meter/music_intake_server/main/ct/music-intake.sh)"
```

This creates the LXC and installs everything. Three manual steps remain
after (can't be automated — they're specific to your storage layout):

1. Bind-mount your NAS source folder (read side):
   ```bash
   pct set <CTID> -mp0 /path/to/your/music,mp=/mnt/nas-source
   ```
2. Bind-mount a **separate, dedicated** managed output folder:
   ```bash
   pct set <CTID> -mp1 /path/to/managed,mp=/mnt/nas-intake
   ```
3. Inside the container, edit `/opt/music-intake/config/scan_roots.txt`
   (which folders to scan) and `/opt/music-intake/config/secrets.env`
   (your free AcoustID API key), then:
   ```bash
   pct exec <CTID> -- systemctl restart music-recognize.service
   ```

Full details: [docs/CONFIGURATION.md](docs/CONFIGURATION.md).

## Why

The common mistake with automated tagging is enabling move+rename
immediately — a single misidentified track can scatter hundreds of files
into the wrong place. This pipeline separates **identification** from
**library ingestion** entirely: files are fingerprinted and tagged
in-place, queued for review, and only moved after a human clicks Approve.

## How it identifies tracks

Three free, unlimited/low-cost sources are cross-checked against each
other rather than trusted individually:

1. **SongRec** (queries Shazam) and **AcoustID/MusicBrainz** run on every
   file. If they agree, that's trusted directly.
2. If they disagree, **iTunes Search** (free, no key) checks whether
   either candidate corresponds to a real cataloged release, breaking
   the tie.
3. If both come back with *nothing at all* (common for homemade or
   obscure content no fingerprint database has), an optional local
   **Whisper transcription + Genius lyrics search** fallback can identify
   spoken/sung content by its actual words instead of its audio
   fingerprint. Off by default — see [docs/CONFIGURATION.md](docs/CONFIGURATION.md).

Every file also gets exact-duplicate detection (SHA-256 content hash),
so re-scanning the same file twice under different names doesn't create
double entries.

## Review UI

- Inline audio preview, per-source identification comparison (SongRec /
  AcoustID / Genius side by side when the lyrics fallback fires),
  confidence scoring
- Live scan progress bar with **Pause/Resume** — pausing takes effect
  within one file's processing time, not at the end of the current batch
- Server-side pagination, search (by filename/artist/title/album), and
  filters for unrecognized tracks and exact-duplicates-only
- Sortable columns, column visibility toggle
- Multi-select for bulk approve/reject/rescan, plus per-row Rescan
  (re-runs identification from scratch without touching the file itself)
- Approve → staged for `beets import` (tags + renames using the metadata
  already approved, no re-matching against MusicBrainz). Reject → moved
  aside. Nothing else touches your library.
- Files are automatically re-processed if modified on disk after being
  queued (tracked via modification time), so fixing a file externally
  doesn't require a manual rescan

## Repository layout

```
ct/music-intake.sh              # Proxmox host: creates the LXC
install/music-intake-install.sh # Runs inside the LXC: installs everything
app/                             # Flask review UI
pipeline/                        # Recognition daemon + beets import script
config/                          # beets config + example scan_roots/secrets
migrations/                      # Versioned SQL schema migrations
migrate.py                       # Standalone migration runner (see below)
scripts/                         # Local dev/test tooling (Codespaces etc.)
docs/                            # Architecture and configuration reference
```

## Database migrations

Schema changes are plain, ordered `.sql` files in `migrations/`, applied
by `migrate.py` — a standalone script, not code embedded in `server.py`/
`recognize.py`. It runs automatically before either service starts (via
the `music-migrate.service` systemd unit the installer sets up), or you
can run it manually:

```bash
python3 migrate.py --status   # show applied/pending, change nothing
python3 migrate.py            # apply any pending migrations
```

Adding a new migration for a future feature is just adding the next
numbered `.sql` file — no application code changes needed.

## Requirements

- Proxmox VE 8+
- A NAS or storage share reachable from the Proxmox host for bind-mounting
- Free [AcoustID](https://acoustid.org/my-applications) API key (required)
- Optional: free [Genius](https://genius.com/api-clients) token (only for
  the lyrics-transcription fallback)

Default container sizing: 4 vCPU / 4GB RAM / 12GB disk — sized for the
Whisper fallback path; drop to 2 vCPU / 2GB RAM if you don't plan to use
it (see [docs/CONFIGURATION.md](docs/CONFIGURATION.md)).

## Testing locally before deploying

No Proxmox host needed. In a GitHub Codespace or any Debian/Ubuntu dev box:

```bash
./scripts/dev-setup.sh --with-songrec --with-whisper   # one-time, ~10min
# edit /opt/music-intake/config/secrets.env with your API keys

./scripts/dev-test-track.sh /path/to/a/test-track.mp3
```

This runs SongRec, AcoustID, and the lyrics fallback individually against
one file, then the full identification pipeline end-to-end, printing
exactly what each source found and what got decided. `MUSIC_DB_PATH` can
be set to point `server.py`/`recognize.py`/`migrate.py` at an alternate
database file for isolated test runs.

## License

MIT — see [LICENSE](LICENSE).
